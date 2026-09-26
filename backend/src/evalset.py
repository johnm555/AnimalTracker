"""Frozen, labelled offline evaluation set (P4-08, first half).

Every threshold in the local stack was fitted to whatever was in
``staging/archive/`` on the day it was tuned, and that archive is deleted after
72 hours. Without a frozen set, "the new threshold is better" can't be checked
again later. This module builds that set. It doesn't score anything; the
replay harness (P4-08b) reads the manifest written here.

Three rules decide what goes in, and each exists to keep the numbers honest:

**Labels come only from someone who looked at the frames.** Sources, strongest
first: an owner review (``observation_reviews`` or ``extra.owner_confirmation``),
a session audit of a local decision (``local_audits``), then a session verdict
(the latest observation per Ring event whose ``extra.detector`` is ``session``).
A local-only decision (DINOv2 accept, gate ``no_animal`` skip) is *not* a label:
scoring the local models against their own output measures nothing. Those
events stay out until someone audits them.

**Label vocabulary is what a reviewer can assert:** ``winston``, ``not_winston``
(an animal that is not Winston), ``no_animal``, ``uncertain``. "Person" vs.
"empty" is not recorded anywhere structured, and we don't infer it from free
text, so both are ``no_animal``. A session verdict uses the reviewer's own
``is_winston_confidence`` (``vision_confidence``), not the fused probability,
because verdicts recorded before P1-27 carry a stale temporal penalty in the
fused value.

**Splits are by sequence, and anything a threshold was tuned on is dev.**
Consecutive events on one device less than ``gap_seconds`` apart are one
moment seen several times. Putting half of them in test would leak. A sequence
is:

* ``excluded`` if any event is in the reference gallery (``cam_MANIFEST.json``),
  because a gallery image scores ~1.0 against itself;
* ``dev`` if any event was in the data some threshold was fitted to: the
  575-event archive behind P4-11/P4-12/P4-27 (captured at or before
  ``CALIBRATION_ARCHIVE_END``), or P4-18's held-out split
  (``build_reference_set.is_holdout``);
* ``test`` otherwise, meaning nothing has been tuned on it yet. The first
  time a threshold is fitted to a test event, that event has to move to dev.

Frames are frozen by hard link (a copy if linking fails) into
``<out>/frames/<staging_key>/``, with a sha256 per frame. A hard link survives
the archive's retention sweep and costs no extra disk space.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .observation import parse_timestamp

LABELS = ("winston", "not_winston", "no_animal", "uncertain")
#: Last observation in the 575-event archive the P4-11/P4-12/P4-27 thresholds
#: were measured on (obs 575, the rescue DB's watermark). Anything captured at
#: or before this instant has been tuned on.
CALIBRATION_ARCHIVE_END = "2026-09-22T04:01:55.095000+00:00"
#: Mirrors scripts/build_reference_set.py — must stay byte-identical.
HOLDOUT = 0.30
SESSION_WINSTON_MIN = 0.70
SESSION_NOT_WINSTON_MAX = 0.30
MANIFEST = "manifest.jsonl"
SUMMARY = "summary.json"


def is_refset_holdout(event_id: str) -> bool:
    """Same split as ``build_reference_set.is_holdout`` (P4-18 tuned on these)."""
    h = hashlib.sha256(f"refset:{event_id}".encode()).digest()
    return (h[0] / 255.0) < HOLDOUT


@dataclass
class LabelledEvent:
    device_id: str
    event_id: str
    camera_id: str
    timestamp: str
    label: str
    label_source: str                    # owner | audit | session
    evidence: str = ""
    observation_id: int | None = None
    species: str | None = None
    staging_key: str | None = None
    sequence_id: str = ""
    split: str = ""                      # test | dev | excluded
    split_reason: str = ""
    frames: list[dict[str, str]] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.device_id, self.event_id)


def session_label(animal_present: Any, vision_confidence: float | None) -> str:
    """Map a session verdict's own answer to the eval vocabulary."""
    if animal_present is not None and not bool(animal_present):
        return "no_animal"
    if vision_confidence is None:
        return "uncertain"
    if vision_confidence >= SESSION_WINSTON_MIN:
        return "winston"
    if vision_confidence <= SESSION_NOT_WINSTON_MAX and animal_present:
        return "not_winston"
    return "uncertain"


def _owner_label(review_label: str, animal_present: Any) -> str:
    if review_label == "not_winston":
        return "not_winston" if animal_present else "no_animal"
    return review_label


def collect_labels(conn: sqlite3.Connection) -> list[LabelledEvent]:
    """One label per Ring device/event, strongest source wins."""
    conn.row_factory = sqlite3.Row
    events: dict[tuple[str, str], LabelledEvent] = {}
    rank = {"session": 1, "audit": 2, "owner": 3}

    def offer(ev: LabelledEvent) -> None:
        cur = events.get(ev.key)
        if cur is None or rank[ev.label_source] > rank[cur.label_source]:
            if cur is not None and cur.species and not ev.species:
                ev.species = cur.species
            events[ev.key] = ev

    # Latest observation per Ring event (same rule as iter_calibration_observations).
    rows = conn.execute("""
        WITH ranked AS (
            SELECT o.*,
                   CAST(json_extract(o.extra, '$.ring_device_id') AS TEXT) AS dev,
                   CAST(json_extract(o.extra, '$.ring_event_id') AS TEXT) AS evt,
                   ROW_NUMBER() OVER (PARTITION BY
                        json_extract(o.extra, '$.ring_device_id'),
                        json_extract(o.extra, '$.ring_event_id') ORDER BY o.id DESC) AS rk
              FROM observations o
             WHERE json_extract(o.extra, '$.ring_event_id') IS NOT NULL
               AND json_extract(o.extra, '$.ring_device_id') IS NOT NULL)
        SELECT * FROM ranked WHERE rk = 1""").fetchall()
    reviews: dict[int, sqlite3.Row] = {}
    for r in conn.execute("SELECT * FROM observation_reviews ORDER BY id"):
        reviews[r["observation_id"]] = r          # latest review wins
    for r in rows:
        extra = json.loads(r["extra"] or "{}")
        base = dict(device_id=r["dev"], event_id=r["evt"], camera_id=r["camera_id"],
                    timestamp=r["timestamp"], observation_id=r["id"],
                    staging_key=extra.get("staging_key"))
        review = reviews.get(r["id"])
        owner = extra.get("owner_confirmation")
        if review is not None:
            offer(LabelledEvent(**base, label=_owner_label(review["label"], r["animal_present"]),
                                label_source="owner",
                                evidence=f"{review['reviewer']}: {review['notes']}"[:300]))
        elif owner:
            lab = owner.get("label") if isinstance(owner, dict) else None
            if lab in ("winston", "not_winston", "uncertain"):
                offer(LabelledEvent(**base, label=_owner_label(lab, r["animal_present"]),
                                    label_source="owner", evidence="owner_confirmation"))
        if extra.get("detector") == "session":
            offer(LabelledEvent(**base, label=session_label(r["animal_present"], r["vision_confidence"]),
                                label_source="session",
                                evidence=str(extra.get("reasoning", ""))[:300]))

    for a in conn.execute("SELECT * FROM local_audits ORDER BY id"):
        if a["reviewer_label"] not in LABELS:
            continue
        src = "owner" if a["reviewer"].lower().startswith("owner") else "audit"
        offer(LabelledEvent(device_id=str(a["device_id"]), event_id=str(a["event_id"]),
                            camera_id=a["camera_id"], timestamp=a["event_at"],
                            label=a["reviewer_label"], label_source=src,
                            evidence=f"{a['reviewer']}: {a['notes']}"[:300],
                            observation_id=a["observation_id"], staging_key=a["staging_key"]))

    # Species labels are reviewer-written (source session/owner/backfill only).
    by_obs = {ev.observation_id: ev for ev in events.values() if ev.observation_id}
    for s in conn.execute("SELECT * FROM animal_sightings ORDER BY id"):
        ev = by_obs.get(s["observation_id"])
        if ev is not None:
            ev.species = s["species"]
    return sorted(events.values(), key=lambda e: (e.timestamp, e.device_id, e.event_id))


def assign_sequences(events: list[LabelledEvent], gap_seconds: float = 300.0) -> None:
    """Chain events on the same device less than ``gap_seconds`` apart."""
    by_dev: dict[str, list[LabelledEvent]] = defaultdict(list)
    for ev in events:
        by_dev[ev.device_id].append(ev)
    for evs in by_dev.values():
        evs.sort(key=lambda e: parse_timestamp(e.timestamp))
        seq, prev = None, None
        for ev in evs:
            t = parse_timestamp(ev.timestamp)
            if prev is None or (t - prev).total_seconds() > gap_seconds:
                seq = f"{ev.device_id}:{ev.event_id}"
            ev.sequence_id = seq or ""
            prev = t


def assign_splits(events: list[LabelledEvent], gallery_event_ids: set[str],
                  calibration_end: str = CALIBRATION_ARCHIVE_END) -> None:
    """Whole sequences go to one side; see the module docstring for the rules."""
    cal_end = parse_timestamp(calibration_end)
    seqs: dict[str, list[LabelledEvent]] = defaultdict(list)
    for ev in events:
        seqs[ev.sequence_id].append(ev)
    for evs in seqs.values():
        if any(e.event_id in gallery_event_ids for e in evs):
            split, why = "excluded", "sequence contains a reference-gallery event"
        elif any(parse_timestamp(e.timestamp) <= cal_end for e in evs):
            split, why = "dev", "in the 575-event calibration archive (P4-11/P4-12/P4-27)"
        elif any(is_refset_holdout(e.event_id) for e in evs):
            split, why = "dev", "P4-18 held-out split (thresholds tuned on it)"
        else:
            split, why = "test", "never used for tuning"
        for e in evs:
            e.split, e.split_reason = split, why


def load_gallery_event_ids(manifest_path: Path) -> set[str]:
    if not manifest_path.is_file():
        return set()
    return {str(r["event_id"]) for r in json.loads(manifest_path.read_text()) if r.get("event_id")}


def index_archive(archive_root: Path) -> dict[tuple[str, str], Path]:
    """(device_id, event_id) → archived event dir, from each sidecar."""
    out: dict[tuple[str, str], Path] = {}
    if not archive_root.is_dir():
        return out
    for day in sorted(archive_root.iterdir()):
        if not day.is_dir():
            continue
        for p in sorted(day.iterdir()):
            side = p / "event.json"
            if not side.is_file():
                continue
            try:
                d = json.loads(side.read_text())
            except (OSError, ValueError):
                continue
            out[(str(d.get("device_id")), str(d.get("event_id")))] = p
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def freeze_frames(src_dir: Path, dest_dir: Path) -> list[dict[str, str]]:
    """Hard-link (or copy) frame-*.jpg into dest_dir; return name + sha256."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(src_dir.glob("frame-*.jpg")):
        target = dest_dir / f.name
        if not target.exists():
            try:
                os.link(f, target)
            except OSError:
                shutil.copy2(f, target)
        out.append({"name": f.name, "sha256": sha256_file(target)})
    return out


def load_manifest(out_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    p = out_dir / MANIFEST
    if not p.is_file():
        return {}
    rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return {(r["device_id"], r["event_id"]): r for r in rows}


def build(conn: sqlite3.Connection, archive_root: Path, out_dir: Path,
          gallery_event_ids: set[str], *, gap_seconds: float = 300.0,
          dry_run: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """Label, sequence, split, and (unless dry_run) freeze. Returns a summary.

    Additive: events already in the manifest keep their frozen frames even
    after the archive has deleted the originals, and their labels are
    refreshed from the DB so a later owner review wins.
    """
    events = collect_labels(conn)
    assign_sequences(events, gap_seconds)
    assign_splits(events, gallery_event_ids)
    existing = load_manifest(out_dir)
    archive = index_archive(archive_root)
    kept: list[LabelledEvent] = []
    missing = 0
    for ev in events:
        prior = existing.get(ev.key)
        frozen_dir = out_dir / "frames" / (ev.staging_key or f"{ev.device_id}__{ev.event_id}")
        if prior and prior.get("frames") and frozen_dir.is_dir():
            ev.frames = prior["frames"]
            if prior.get("staging_key"):
                ev.staging_key = prior["staging_key"]
        else:
            src = archive.get(ev.key)
            if src is None:
                missing += 1
                continue
            ev.staging_key = ev.staging_key or src.name
            frozen_dir = out_dir / "frames" / ev.staging_key
            ev.frames = ([{"name": f.name, "sha256": ""} for f in sorted(src.glob("frame-*.jpg"))]
                         if dry_run else freeze_frames(src, frozen_dir))
            if not ev.frames:
                missing += 1
                continue
        kept.append(ev)
    summary = summarise(kept)
    summary.update({"labelled_events": len(events), "frames_unavailable": missing,
                    "gap_seconds": gap_seconds, "calibration_archive_end": CALIBRATION_ARCHIVE_END,
                    "session_bands": {"winston_min": SESSION_WINSTON_MIN,
                                      "not_winston_max": SESSION_NOT_WINSTON_MAX},
                    "built_at": (now or datetime.now().astimezone()).isoformat(),
                    "dry_run": dry_run})
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir / (MANIFEST + ".tmp")
        tmp.write_text("".join(json.dumps(asdict(e), sort_keys=True) + "\n" for e in kept))
        tmp.replace(out_dir / MANIFEST)
        (out_dir / SUMMARY).write_text(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def summarise(events: Iterable[LabelledEvent | dict[str, Any]]) -> dict[str, Any]:
    by_split: dict[str, Counter] = defaultdict(Counter)
    sources: Counter = Counter()
    seqs: dict[str, set] = defaultdict(set)
    cams: Counter = Counter()
    n = 0
    for e in events:
        d = asdict(e) if isinstance(e, LabelledEvent) else e
        by_split[d["split"]][d["label"]] += 1
        sources[d["label_source"]] += 1
        seqs[d["split"]].add(d["sequence_id"])
        cams[d["camera_id"]] += 1
        n += 1
    return {"events": n,
            "by_split": {k: dict(v) for k, v in sorted(by_split.items())},
            "sequences": {k: len(v) for k, v in sorted(seqs.items())},
            "label_sources": dict(sources), "cameras": dict(cams.most_common())}


def verify(out_dir: Path) -> dict[str, Any]:
    """Re-hash every frozen frame against the manifest."""
    rows = load_manifest(out_dir)
    bad, missing, ok = [], [], 0
    for r in rows.values():
        d = out_dir / "frames" / (r.get("staging_key") or f"{r['device_id']}__{r['event_id']}")
        for f in r.get("frames", []):
            p = d / f["name"]
            if not p.is_file():
                missing.append(str(p))
            elif sha256_file(p) != f["sha256"]:
                bad.append(str(p))
            else:
                ok += 1
    return {"events": len(rows), "frames_ok": ok, "frames_missing": missing, "frames_mismatched": bad}
