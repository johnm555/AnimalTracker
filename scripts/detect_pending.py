#!/usr/bin/env python
"""Session-mode detection CLI: the bridge between staged frames and the tracker.

The Ring poller (running inside `scripts/run.sh api`) stages frames for every
motion event under backend/staging/pending/. A scheduled Claude session runs:

    scripts/run.sh detect list [--limit N] [--json] [--sheets]
        Pending events, oldest first, with absolute frame paths, the
        verification question and the verdict schema. The session then views
        the frames (Read tool) alongside backend/reference_images/.
        --sheets also writes one contact sheet per event (sheet.jpg, the 4
        frames side by side) and staging/reference-sheet.jpg, so each event
        costs one image read instead of four.

    scripts/run.sh detect record <key-or-event-id> --verdict '<json>'
    scripts/run.sh detect record <key> --verdict-file verdict.json
        Record one verdict (JSON in the DETECTION_SCHEMA shape). It goes
        through WinstonDetector.to_observation() (unchanged signal fusion) and
        is ingested via POST /winston/observation on the running API, so the
        in-memory tracker, transitions and notifications all fire. Falls back
        to in-process ingest if the API is down. The event dir is archived.

    scripts/run.sh detect record-batch verdicts.json
        Many at once: [{"key": "...", "verdict": {...}} | {"key": "...", "skip": "reason"}, ...]

    scripts/run.sh detect skip <key> --reason "frames are black"
        Unusable frames. Recorded as `skipped` — NOT as "not Winston".

    scripts/run.sh detect requeue <key> [--force]
    scripts/run.sh detect requeue --list
        Move an archived event back to pending/ and reset its ledger row to
        `staged`, so a session can re-record the verdict after a threshold or
        fusion change (P1-16). The frames and the question are unchanged; only
        the arithmetic moved. If the event already produced an observation the
        command refuses without --force, because re-recording inserts a *new*
        observation and leaves the old one in place — both would be counted
        when measuring calibration.

    scripts/run.sh detect status
        Counts of pending / archived / ledger states.

Rules the session must follow (CLAUDE.md "one rule"): answer only whether the
animal in the frames is Winston, the enrolled dog in the reference images.
Never record a verdict for frames you did not view. Never touch transitions
or the tracker directly; the tracker derives location from observations.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from src.api import AppContext, load_settings  # noqa: E402
from src.db import Database  # noqa: E402
from src.observation import Observation, parse_timestamp, utcnow  # noqa: E402
from src.pipeline import reference_dir, staging_dirs  # noqa: E402
from src.dog_detector import DogGate, GateSettings  # noqa: E402
from src.local_detector import LocalDetector, LocalSettings  # noqa: E402
from src.local_pipeline import LocalPipeline, LocalPipelineSettings, Outcome  # noqa: E402
from src.staging import (  # noqa: E402
    DETECTION_SCHEMA,
    archive_event,
    archived_local_decisions,
    contact_sheet,
    SKIP_CATEGORIES,
    StagingDirs,
    event_sheet,
    find_archived,
    find_pending,
    list_archived,
    list_pending,
    record_verdict,
    reference_sheet,
    verdict_to_observation,
    requeue_event,
    skip_event,
    stamp_local,
)
from src.winston_detector import FusionWeights, load_reference_images  # noqa: E402


def _load_env() -> None:
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def db_path(settings: dict[str, Any]) -> Path:
    path = Path((settings.get("database") or {}).get("path", "./winston.db"))
    return path if path.is_absolute() else BACKEND / path


class Recorder:
    """Ingest via the running API (keeps its tracker current); in-process fallback."""

    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        api = settings.get("api") or {}
        self.base = f"http://127.0.0.1:{int(api.get('port', 8420))}"
        self.token = os.environ.get("WINSTON_API_TOKEN")
        self._ctx: AppContext | None = None
        self._db: Database | None = None
        self.via = "api"

    def _api_up(self) -> bool:
        import httpx

        try:
            return httpx.get(f"{self.base}/healthz", timeout=3.0).status_code == 200
        except httpx.HTTPError:
            return False

    def ingest(self, obs: Observation) -> dict[str, Any]:
        if self.via == "api" and self._api_up():
            import httpx

            headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
            r = httpx.post(f"{self.base}/winston/observation", json=obs.to_dict(), headers=headers, timeout=15.0)
            if r.status_code == 401:
                sys.exit("API rejected the observation: set WINSTON_API_TOKEN in .env (it must match the server)")
            r.raise_for_status()
            return r.json()
        if self._ctx is None:
            print("API not running; ingesting in-process (its tracker will catch up on next start)", file=sys.stderr)
            self.via = "in-process"
            os.environ.setdefault("WINSTON_PIPELINE", "0")
            self._ctx = AppContext.build(self.settings)
        return self._ctx.ingest(obs)

    @property
    def db(self) -> Database:
        if self._ctx is not None:
            return self._ctx.db
        if self._db is None:
            self._db = Database(db_path(self.settings))
        return self._db

    def marker(self, device_id: str, event_id: str):
        def mark(status: str, observation_id: int | None, error: str | None) -> None:
            self.db.mark_event_by_id(device_id, event_id, status, observation_id, error)
        return mark


def cmd_list(args: argparse.Namespace, settings: dict[str, Any], dirs: StagingDirs) -> int:
    pending = list_pending(dirs, limit=args.limit)
    refs = load_reference_images(reference_dir(settings))
    ref_paths = sorted(str(p) for p in reference_dir(settings).iterdir()
                       if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}) if refs else []
    ref_sheet = reference_sheet(dirs, reference_dir(settings)) if args.sheets and refs else None
    sheets = {e.key: event_sheet(e) for e in pending} if args.sheets else {}
    if args.json:
        rows = []
        for e in pending:
            d = e.to_dict()
            if sheets.get(e.key):
                d["sheet"] = str(sheets[e.key])
            rows.append(d)
        print(json.dumps({
            "reference_images": ref_paths,
            "reference_sheet": str(ref_sheet) if ref_sheet else None,
            "pending": rows,
            "verdict_schema": DETECTION_SCHEMA,
            "record_with": "scripts/run.sh detect record <key> --verdict '<json>'  |  record-batch verdicts.json",
        }, indent=2))
        return 0
    print(f"{len(pending)} pending event(s)  |  {len(ref_paths)} reference image(s) in {reference_dir(settings)}")
    if ref_sheet:
        print(f"reference sheet: {ref_sheet}")
    for e in pending:
        d = e.sidecar
        print(f"\n[{e.key}]\n  camera={d['camera_id']}  time={d['timestamp']}  ring={d.get('ring_classification')}"
              f"  temporal_prior={d.get('temporal_likelihood')}")
        if sheets.get(e.key):
            print(f"  sheet: {sheets[e.key]}")
        for p in e.frame_paths:
            print(f"  {p}")
    if pending:
        print("\nverdict schema keys:", ", ".join(DETECTION_SCHEMA["required"]))
    return 0


def _resolve(dirs: StagingDirs, key: str):
    e = find_pending(dirs, key)
    if e is None:
        sys.exit(f"no pending event matching {key!r} (run `detect list`)")
    return e


def _record_one(rec: Recorder, dirs: StagingDirs, key: str, verdict: dict[str, Any] | str,
                weights: FusionWeights) -> dict[str, Any]:
    e = _resolve(dirs, key)
    result = record_verdict(dirs, e, verdict, rec.ingest, rec.marker(e.sidecar["device_id"], e.sidecar["event_id"]),
                            weights)
    out = {"key": e.key, "camera_id": e.sidecar["camera_id"], "observation_id": result.get("observation_id"),
           "accepted": result.get("accepted"), "rejection_reason": result.get("rejection_reason"),
           "transition": result.get("transition"), "notification": result.get("notification"),
           "state": result.get("state"), "via": rec.via}
    print(json.dumps(out, default=str))
    return out


def cmd_record(args: argparse.Namespace, settings: dict[str, Any], dirs: StagingDirs) -> int:
    verdict = Path(args.verdict_file).read_text() if args.verdict_file else args.verdict
    if not verdict:
        sys.exit("give --verdict '<json>' or --verdict-file path")
    _record_one(Recorder(settings), dirs, args.key, verdict, FusionWeights.from_settings(settings.get("detector")))
    return 0


def cmd_record_batch(args: argparse.Namespace, settings: dict[str, Any], dirs: StagingDirs) -> int:
    items = json.loads(Path(args.file).read_text())
    rec = Recorder(settings)
    weights = FusionWeights.from_settings(settings.get("detector"))
    # Oldest first so the tracker sees events in time order.
    order = {e.key: i for i, e in enumerate(list_pending(dirs))}
    items.sort(key=lambda it: order.get(it.get("key"), 10**9))
    n_ok = n_skip = n_missing = 0
    for it in items:
        key = it["key"]
        e = find_pending(dirs, key)
        if e is None:
            # Already recorded (e.g. by an earlier, interrupted run) or never staged: report, don't abort.
            print(json.dumps({"key": key, "not_pending": True}))
            n_missing += 1
            continue
        if it.get("skip"):
            category = it.get("skip_category", "unspecified")
            skip_event(dirs, e, str(it["skip"]), rec.marker(e.sidecar["device_id"], e.sidecar["event_id"]),
                       category=category)
            print(json.dumps({"key": e.key, "skipped": it["skip"], "skip_category": category}))
            n_skip += 1
        else:
            _record_one(rec, dirs, key, it["verdict"], weights)
            n_ok += 1
    print(f"recorded {n_ok} verdict(s), skipped {n_skip}, not pending {n_missing}", file=sys.stderr)
    return 0


def cmd_skip(args: argparse.Namespace, settings: dict[str, Any], dirs: StagingDirs) -> int:
    rec = Recorder(settings)
    e = _resolve(dirs, args.key)
    dest = skip_event(dirs, e, args.reason, rec.marker(e.sidecar["device_id"], e.sidecar["event_id"]),
                      category=args.category)
    print(json.dumps({"key": e.key, "skipped": args.reason, "skip_category": args.category,
                      "archived_to": str(dest)}))
    return 0


def cmd_requeue(args: argparse.Namespace, settings: dict[str, Any], dirs: StagingDirs) -> int:
    """Move an archived event back to pending so its verdict can be re-fused."""
    if args.list:
        rows = [{"key": p.name, "day": p.parent.name} for p in list_archived(dirs)]
        print(json.dumps(rows, indent=2))
        return 0
    if not args.key:
        sys.exit("give a key (or --list to see archived events)")
    archived = find_archived(dirs, args.key)
    if archived is None:
        sys.exit(f"no archived event matching {args.key!r} (run `detect requeue --list`)")
    sidecar = json.loads((archived / "event.json").read_text())
    rec = Recorder(settings)
    row = rec.db.get_event(sidecar["device_id"], sidecar["event_id"])
    prior_obs = row.get("observation_id") if row else None
    if prior_obs is not None and not args.force:
        sys.exit(
            f"{archived.name} already produced observation {prior_obs}. Re-recording inserts a NEW "
            f"observation; audit and tracking history remain unchanged. Use detect calibration "
            f"for latest-per-event tuning data. Re-run with --force to re-review."
        )
    pending = requeue_event(dirs, archived)
    rec.db.requeue_event(sidecar["device_id"], sidecar["event_id"])
    if prior_obs is not None:
        pending.sidecar["superseded_observation_id"] = prior_obs
        (pending.path / "event.json").write_text(json.dumps(pending.sidecar, indent=2))
    print(json.dumps({
        "key": pending.key,
        "requeued_to": str(pending.path),
        "ledger_status": "staged",
        "superseded_observation_id": prior_obs,
        "note": "observation left in place; re-recording adds another" if prior_obs else None,
    }, indent=2))
    return 0


def cmd_calibration(args: argparse.Namespace, settings: dict[str, Any], dirs: StagingDirs) -> int:
    """Export one latest committed verdict per Ring device/event, without mutations."""
    rec = Recorder(settings)
    for obs in rec.db.iter_calibration_observations():
        print(obs.to_json())
    return 0


def cmd_local(args: argparse.Namespace, settings: dict[str, Any], dirs: StagingDirs) -> int:
    """Run the local pipeline (P4-27) over the pending queue.

    Dry run by default: prints the outcome it would record for each event and
    the totals. `--apply` acts on them — WINSTON becomes an observation through
    the running API, NO_ANIMAL/NOT_WINSTON become ledger skips, and REVIEW is
    left in the queue with the decision written into its sidecar.
    """
    gate = DogGate(GateSettings.from_settings(settings))
    local = LocalDetector(LocalSettings.from_settings(settings), reference_dir(settings),
                          dirs.root / "gallery.pt")
    pipe = LocalPipeline(gate, local, LocalPipelineSettings.from_settings(settings))
    if not local.load():
        sys.exit(f"local detector unavailable: {local._load_error}")
    pending = list_pending(dirs, limit=args.limit)          # oldest first
    if not pending:
        print("nothing pending")
        return 0
    rec = Recorder(settings) if args.apply else None
    weights = FusionWeights.from_settings(settings.get("detector"))
    counts: dict[str, int] = {}
    transitions = 0
    t0 = time.perf_counter()
    for n, e in enumerate(pending, 1):
        frames = e.frame_paths
        res = pipe.classify(frames)
        counts[res.outcome.value] = counts.get(res.outcome.value, 0) + 1
        line = f"{e.timestamp.astimezone().strftime('%m-%d %H:%M:%S')} {e.sidecar['camera_id']:16} {res.outcome.value:11} {res.score:.3f}"
        if not args.apply:
            print(line + ("  " + res.reason[:70] if args.verbose else ""))
            continue
        dev, eid = e.sidecar["device_id"], e.sidecar["event_id"]
        try:
            if res.outcome is Outcome.REVIEW:
                e.sidecar["local"] = res.to_dict()
                (e.path / "event.json").write_text(json.dumps(e.sidecar, indent=2))
                print(line + "  (left for review)")
                continue
            if res.outcome in (Outcome.NO_ANIMAL, Outcome.NOT_WINSTON):
                rec.db.mark_event_by_id(dev, eid, "skipped", None,
                                        f"local {res.outcome.value}: {res.reason}"[:500])
                stamp_local(e, res)
                archive_event(dirs, e, f"skipped: local {res.outcome.value}")
                print(line)
                continue
            obs = verdict_to_observation(e, pipe.detection_result(res), weights,
                                         reviewer=f"local:{local.settings.model}")
            obs.extra["detector"] = "local"
            obs.extra["local_pipeline"] = res.to_dict()
            out = rec.ingest(obs) or {}
            rec.db.mark_event_by_id(dev, eid, "analyzed", out.get("observation_id"), None)
            stamp_local(e, res, out.get("observation_id"))
            archive_event(dirs, e, "analyzed: local winston")
            t = out.get("transition")
            if t:
                transitions += 1
            print(line + f"  p={obs.winston_probability:.2f}"
                  + (f"  ** {t['from_zone']} -> {t['to_zone']} ({t['confidence']:.2f})" if t else ""))
        except Exception as ex:
            print(f"{line}  ERROR {type(ex).__name__}: {ex}", file=sys.stderr)
    dt = time.perf_counter() - t0
    print(f"\n{'applied' if args.apply else 'DRY RUN'}: {len(pending)} event(s) in {dt:.1f}s "
          f"({dt*1000/max(1,len(pending)):.0f} ms each)")
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if args.apply:
        print(f"  transitions recorded: {transitions}")
        print(f"  still pending: {len(list_pending(dirs))}")
    else:
        print("  re-run with --apply to record these.")
    return 0


# --------------------------------------------------------------------------- #
# Audit: spot-check the local models' automatic verdicts (P4-29)
# --------------------------------------------------------------------------- #

AUDIT_LABELS = ("winston", "not_winston", "no_animal", "uncertain")


def audit_sample(dirs: StagingDirs, db, size: int, hours: float,
                 rng: random.Random) -> list[dict[str, Any]]:
    """Pick events for review, balanced across what the models decided.

    A plain random sample of recent work is dominated by whichever outcome is
    most common, so the rarer one goes unchecked for days. Both directions of
    error matter and they are not symmetrical: a wrong `winston` invents a
    sighting, a wrong `no_animal` destroys one silently. Each outcome therefore
    gets half the sample, and events already audited are never re-offered.
    """
    since = utcnow() - timedelta(hours=hours)
    done = db.audited_keys()
    pools: dict[str, list[dict[str, Any]]] = {}
    for d in archived_local_decisions(dirs, since=since):
        if d["key"] in done or not d["frames"]:
            continue
        pools.setdefault(d["local_outcome"], []).append(d)
    if not pools:
        return []
    picked: list[dict[str, Any]] = []
    order = sorted(pools)
    share = max(1, size // len(order))
    for name in order:
        pool = pools[name]
        rng.shuffle(pool)
        picked += pool[:share]
    # Backfill from whatever is left if a pool was too small to fill its share.
    if len(picked) < size:
        rest = [d for name in order for d in pools[name] if d not in picked]
        rng.shuffle(rest)
        picked += rest[: size - len(picked)]
    picked.sort(key=lambda d: d["timestamp"], reverse=True)
    return picked[:size]


def cmd_audit(args, settings: dict[str, Any], dirs: StagingDirs) -> int:
    """Hand a session a sample of the local models' recent automatic verdicts.

    Read-only. Prints the frames to look at and what the model concluded; the
    session answers with `audit record`.
    """
    db = Database(db_path(settings))
    sample = audit_sample(dirs, db, args.sample, args.hours, random.Random(args.seed))
    if not sample:
        print(f"no un-audited local decisions in the last {args.hours:g}h")
        return 0
    if args.sheets:
        for d in sample:
            sheet = contact_sheet([Path(f) for f in d["frames"]],
                                  Path(d["path"]) / "sheet.jpg",
                                  f"{d['camera_id']}  {d['timestamp']}")
            if sheet:
                d["sheet"] = str(sheet)
        ref = reference_sheet(dirs, reference_dir(settings))
        if ref:
            print(json.dumps({"reference_sheet": str(ref)}))
    if args.json:
        print(json.dumps(sample, indent=2))
        return 0
    print(f"{len(sample)} event(s) to spot-check — the local model's answer is a claim to test, "
          f"not a hint to agree with:\n")
    for d in sample:
        when = parse_timestamp(d["timestamp"]).astimezone().strftime("%m-%d %H:%M:%S")
        score = "n/a" if d["local_score"] is None else f"{d['local_score']:.3f}"
        print(f"  {d['key']}\n    {when}  {d['camera_id']}  -> {d['local_outcome']} "
              f"(score {score}, gate {d['gate']})")
        for f in d.get("sheet") and [d["sheet"]] or d["frames"]:
            print(f"      {f}")
    print("\nAnswer with what you SAW, one entry per event:")
    print('  [{"key": "...", "saw": "winston|not_winston|no_animal|uncertain", "notes": "..."}]')
    print("  scripts/run.sh detect audit-record answers.json")
    return 0


def cmd_audit_record(args, settings: dict[str, Any], dirs: StagingDirs) -> int:
    """Record a session's spot-check answers and report every disagreement."""
    answers = json.loads(Path(args.file).read_text())
    if isinstance(answers, dict):
        answers = [answers]
    db = Database(db_path(settings))
    index = {d["key"]: d for d in archived_local_decisions(dirs)}
    agreed = disagreed = 0
    misses: list[dict[str, Any]] = []
    for a in answers:
        key, saw = a.get("key"), a.get("saw")
        d = index.get(key)
        if d is None:
            print(f"  {key}: not an archived local decision — skipped", file=sys.stderr)
            continue
        if saw not in AUDIT_LABELS:
            print(f"  {key}: 'saw' must be one of {AUDIT_LABELS}", file=sys.stderr)
            continue
        rec = db.insert_local_audit(
            staging_key=key, device_id=d["device_id"] or "", event_id=d["event_id"] or "",
            camera_id=d["camera_id"] or "", event_at=d["timestamp"],
            local_outcome=d["local_outcome"], local_score=d["local_score"],
            reviewer_label=saw, reviewer=args.reviewer, notes=a.get("notes", "") or "(none)",
            observation_id=d["observation_id"])
        if rec["agrees"]:
            agreed += 1
            print(f"  {key}: agreed ({d['local_outcome']})")
            continue
        disagreed += 1
        print(f"  {key}: DISAGREE — model said {d['local_outcome']} "
              f"(score {d['local_score']}), session saw {saw}")
        if d["local_outcome"] != "winston" and saw == "winston":
            misses.append(d)
    print(f"\n{agreed} agreed, {disagreed} disagreed")
    if misses:
        print(f"\n{len(misses)} missed sighting(s) — the model discarded frames "
              f"a session says show Winston:")
        for d in misses:
            print(f"  {d['key']}  {d['camera_id']}  score {d['local_score']}")
        if args.requeue:
            for d in misses:
                try:
                    requeue_event(dirs, Path(d["path"]))
                    print(f"  requeued {d['key']} for a verdict")
                except (FileExistsError, OSError) as e:
                    print(f"  could not requeue {d['key']}: {e}", file=sys.stderr)
            print("NOTE: a requeued event is older than the tracker's current state. "
                  "Recording its verdict now ingests a stale sighting and can move the "
                  "reported location backwards. Review before running record-batch.")
        else:
            print("  (left archived. `--requeue` moves them back to pending; read the "
                  "warning it prints before recording those verdicts.)")
    print("\nscripts/run.sh detect audit-summary   # agreement over time")
    return 0


def cmd_audit_summary(args, settings: dict[str, Any], dirs: StagingDirs) -> int:
    """Agreement rates and the threshold evidence behind each disagreement."""
    db = Database(db_path(settings))
    since = utcnow() - timedelta(days=args.days) if args.days else None
    s = db.local_audit_summary(since=since)
    if not s["audited"]:
        print("no audits recorded yet")
        return 0
    window = f"last {args.days:g}d" if args.days else "all time"
    print(f"local model spot-checks ({window}): {s['agreed']}/{s['audited']} agreed "
          f"({s['agreement_rate']:.1%})\n")
    for outcome, b in sorted(s["by_outcome"].items()):
        print(f"  {outcome:11} {b['agreed']:3}/{b['n']:<3} ({b['agreement_rate']:.0%})")
        for d in b["disagreed"]:
            score = "n/a" if d["score"] is None else f"{d['score']:.3f}"
            print(f"      {d['key']}  {d['camera_id']}  score {score}  session saw {d['said']}")
    cfg = LocalPipelineSettings.from_settings(settings)
    misses = [d for d in s["by_outcome"].get("no_animal", {}).get("disagreed", [])
              if d["said"] == "winston" and d["score"] is not None]
    print(f"\nthresholds in force: accept {cfg.accept_threshold}, rescue {cfg.rescue_threshold}")
    if misses:
        hi = max(d["score"] for d in misses)
        print(f"  {len(misses)} missed sighting(s) scored up to {hi:.3f}. Lowering "
              f"rescue_threshold below {hi:.3f} would have sent them to review "
              f"instead of discarding them — at the cost of reviewing more empty events. "
              f"Re-run scripts/calibrate_local.py before changing it.")
    else:
        print("  no missed sightings recorded; nothing here argues for a change.")
    bad = [d for d in s["by_outcome"].get("winston", {}).get("disagreed", [])
           if d["score"] is not None]
    if bad:
        lo = min(d["score"] for d in bad)
        print(f"  {len(bad)} invented sighting(s), lowest score {lo:.3f}. Raising "
              f"accept_threshold above {lo:.3f} would have sent them to review.")
    return 0


def cmd_confirm(args, settings: dict[str, Any], dirs: StagingDirs) -> int:
    """Apply owner ground truth to one observation and rebuild the tracker.

    The tracker is deterministic over the stored observations, so correcting one
    and replaying is how a confirmation reaches the reported location. Re-recording
    the event would not: a revision cannot confirm movement (ADR-017).
    """
    db = Database(db_path(settings))
    try:
        res = db.apply_owner_confirmation(args.observation_id, args.label,
                                          args.reviewer, args.notes)
    except (KeyError, ValueError) as e:
        sys.exit(str(e))
    print(f"observation {res['observation_id']}: {res['was']:.2f} -> {res['winston_probability']:.2f} "
          f"({res['label']}, confirmed by {args.reviewer})")
    print("Restart the API (scripts/run.sh api) so the tracker replays the corrected history.")
    return 0


def cmd_status(args: argparse.Namespace, settings: dict[str, Any], dirs: StagingDirs) -> int:
    pending = list_pending(dirs)
    archived = sum(1 for d in dirs.archive.glob("*/*") if d.is_dir()) if dirs.archive.is_dir() else 0
    rec = Recorder(settings)
    print(json.dumps({
        "mode": (settings.get("detector") or {}).get("mode", "session"),
        "pending": len(pending),
        "oldest_pending": pending[0].timestamp.isoformat() if pending else None,
        "archived": archived,
        "ledger": rec.db.count_events_by_status(),
        "reference_images": len(load_reference_images(reference_dir(settings))),
    }, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", help="path to settings.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--limit", type=int); p.add_argument("--json", action="store_true")
    p.add_argument("--sheets", action="store_true", help="write sheet.jpg per event + reference-sheet.jpg")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("record"); p.add_argument("key"); p.add_argument("--verdict"); p.add_argument("--verdict-file")
    p.set_defaults(fn=cmd_record)
    p = sub.add_parser("record-batch"); p.add_argument("file"); p.set_defaults(fn=cmd_record_batch)
    p = sub.add_parser("skip"); p.add_argument("key"); p.add_argument("--reason", required=True)
    p.add_argument("--category", choices=SKIP_CATEGORIES, default="unspecified",
                   help="why identification was impossible; omitted legacy reasons remain unspecified")
    p.set_defaults(fn=cmd_skip)
    p = sub.add_parser("requeue", help="move an archived event back to pending (re-fuse after a threshold change)")
    p.add_argument("key", nargs="?"); p.add_argument("--list", action="store_true", help="list archived keys")
    p.add_argument("--force", action="store_true", help="proceed even though an observation already exists")
    p.set_defaults(fn=cmd_requeue)
    p = sub.add_parser("calibration", help="export latest verdict per Ring event as JSONL (read-only)")
    p.set_defaults(fn=cmd_calibration)
    p = sub.add_parser("local", help="classify pending events with the local models (P4-27)")
    p.add_argument("--apply", action="store_true", help="record the outcomes (default is a dry run)")
    p.add_argument("--limit", type=int)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_local)
    p = sub.add_parser("audit", help="sample recent local-model verdicts for a spot-check (P4-29)")
    p.add_argument("--sample", type=int, default=8, help="how many events to review (default 8)")
    p.add_argument("--hours", type=float, default=24.0, help="how far back to sample (default 24)")
    p.add_argument("--seed", type=int, default=None, help="fix the sample for reproducibility")
    p.add_argument("--sheets", action="store_true", help="write one contact sheet per event")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_audit)
    p = sub.add_parser("audit-record", help="record spot-check answers and flag disagreements")
    p.add_argument("file", help="JSON: [{key, saw, notes}]")
    p.add_argument("--reviewer", default="claude-session")
    p.add_argument("--requeue", action="store_true",
                   help="move missed sightings back to pending (reads the stale-ingest warning)")
    p.set_defaults(fn=cmd_audit_record)
    p = sub.add_parser("audit-summary", help="agreement rate and threshold evidence")
    p.add_argument("--days", type=float, default=14.0)
    p.set_defaults(fn=cmd_audit_summary)
    p = sub.add_parser("confirm", help="apply owner ground truth to one observation")
    p.add_argument("observation_id", type=int)
    p.add_argument("--label", choices=("winston", "not_winston"), required=True)
    p.add_argument("--reviewer", default="owner")
    p.add_argument("--notes", required=True)
    p.set_defaults(fn=cmd_confirm)
    p = sub.add_parser("status"); p.set_defaults(fn=cmd_status)
    args = ap.parse_args(argv)
    settings = load_settings(args.settings)
    return args.fn(args, settings, staging_dirs(settings))


if __name__ == "__main__":
    raise SystemExit(main())
