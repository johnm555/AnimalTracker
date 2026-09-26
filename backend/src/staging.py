"""Session-mode detection: frames are staged on disk for a Claude session to verify.

There is no Anthropic API key in this deployment (subscription plan only), so
the "vision model" is a Claude Code session that runs on a schedule. The
contract between the poller and that session is a directory of pending
events:

    backend/staging/pending/<camera>__<UTC stamp>__<event_id>/
        frame-0.jpg … frame-3.jpg     sampled frames (same ones the API path would send)
        event.json                    sidecar: camera, device, timestamp, ring label,
                                      temporal prior, the verification question, the
                                      verdict schema

The session views the frames (Read tool), answers the *verification* question
— "is the animal in these frames Winston, the enrolled Great Dane in
backend/reference_images/?" — as a JSON verdict in the same schema the API
path uses, and records it with `scripts/detect_pending.py record`. Recording
runs the verdict through the unchanged `WinstonDetector.to_observation()`
(signal fusion) and ingests the resulting Observation through the API, so the
tracker, transitions and notifications behave exactly as with the API model.
The event directory is then moved to `staging/archive/<YYYY-MM-DD>/`.

Nothing here decides a location. The session only answers "is it Winston";
the deterministic tracker decides the zone. A verdict is never invented for
an event the session did not look at: unusable frames are recorded as
`skipped`, which is not an observation.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from .observation import Observation, parse_timestamp, utcnow
from .ring_client import MotionEvent
from .winston_detector import (
    DETECTION_SCHEMA,
    DetectionResult,
    FusionWeights,
    WinstonDetector,
    build_user_text,
    parse_detection_response,
)

log = logging.getLogger(__name__)

SIDECAR = "event.json"
SIDECAR_VERSION = 1
SKIP_CATEGORIES = ("unusable_frames", "occluded_subject", "unspecified")


def event_key(event: MotionEvent) -> str:
    stamp = event.timestamp.strftime("%Y%m%dT%H%M%SZ")
    return f"{event.camera_id}__{stamp}__{event.event_id}"


@dataclass
class StagingDirs:
    root: Path

    @property
    def pending(self) -> Path:
        return self.root / "pending"

    @property
    def archive(self) -> Path:
        return self.root / "archive"

    def ensure(self) -> None:
        self.pending.mkdir(parents=True, exist_ok=True)
        self.archive.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Writer side (poller)
# --------------------------------------------------------------------------- #

def stage_event(dirs: StagingDirs, event: MotionEvent, frames: list[Any],
                n_reference: int, temporal_likelihood: float | None = None,
                gate: dict[str, Any] | None = None) -> Path:
    """Write frames + sidecar for one event. Atomic: written to a temp dir, then renamed."""
    dirs.ensure()
    final = dirs.pending / event_key(event)
    if final.exists():
        return final
    tmp = dirs.pending / (final.name + ".part")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    frame_files = []
    frame_offsets = []
    for i, frame in enumerate(frames):
        image = getattr(frame, "image", frame)
        name = f"frame-{i}.jpg"
        (tmp / name).write_bytes(image.data)
        frame_files.append(name)
        frame_offsets.append(getattr(frame, "offset_seconds", None))
    sidecar = {
        "version": SIDECAR_VERSION,
        "event_id": event.event_id,
        "device_id": event.device_id,
        "camera_id": event.camera_id,
        "timestamp": event.timestamp.isoformat(),
        "ring_classification": event.ring_classification,
        "frames": frame_files,
        "frame_offsets_seconds": frame_offsets,
        "temporal_likelihood": temporal_likelihood,
        "staged_at": utcnow().isoformat(),
        "question": build_user_text(n_reference, len(frame_files), event.camera_id, event.ring_classification),
        "verdict_schema": DETECTION_SCHEMA,
    }
    if gate:
        # Local dog gate (P4-12). A hint for the reviewer — where an animal was
        # seen and how sure the detector was. Never a verdict: `present` does not
        # mean Winston, and `absent` does not mean no animal (see dog_detector).
        sidecar["gate"] = gate
    (tmp / SIDECAR).write_text(json.dumps(sidecar, indent=2))
    tmp.rename(final)
    return final


# --------------------------------------------------------------------------- #
# Reader side (Claude session via scripts/detect_pending.py)
# --------------------------------------------------------------------------- #

@dataclass
class PendingEvent:
    path: Path
    sidecar: dict[str, Any]

    @property
    def key(self) -> str:
        return self.path.name

    @property
    def timestamp(self) -> datetime:
        return parse_timestamp(self.sidecar["timestamp"])

    @property
    def frame_paths(self) -> list[Path]:
        return [self.path / f for f in self.sidecar.get("frames", [])]

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.sidecar)
        d.pop("verdict_schema", None)
        d["key"] = self.key
        d["frames"] = [str(p) for p in self.frame_paths]
        return d


def list_pending(dirs: StagingDirs, limit: int | None = None) -> list[PendingEvent]:
    """Oldest first. Partial (`.part`) and sidecar-less directories are ignored."""
    if not dirs.pending.is_dir():
        return []
    out: list[PendingEvent] = []
    for p in sorted(dirs.pending.iterdir()):
        if not p.is_dir() or p.name.endswith(".part") or not (p / SIDECAR).is_file():
            continue
        try:
            out.append(PendingEvent(p, json.loads((p / SIDECAR).read_text())))
        except (OSError, ValueError) as e:
            log.warning("unreadable sidecar in %s: %s", p, e)
    out.sort(key=lambda e: e.timestamp)
    return out[:limit] if limit else out


def find_pending(dirs: StagingDirs, key: str) -> PendingEvent | None:
    """Look up by directory name or by Ring event id."""
    for e in list_pending(dirs):
        if e.key == key or e.sidecar.get("event_id") == key:
            return e
    return None


def verdict_to_observation(pending: PendingEvent, verdict: dict[str, Any] | str,
                           weights: FusionWeights | None = None,
                           reviewer: str = "claude-session") -> Observation:
    """Run a session verdict through the unchanged detector fusion.

    `verdict` is the JSON object (or text containing it) in DETECTION_SCHEMA.
    The staging-time prior is audit data only: intervening session verdicts
    can change tracker state before ingestion. Let the deterministic tracker
    check movement against the state it actually has at ingestion (ADR-015).
    """
    if isinstance(verdict, DetectionResult):
        result = verdict            # already parsed (local detector)
    else:
        result = parse_detection_response(verdict if isinstance(verdict, str) else json.dumps(verdict))
    detector = WinstonDetector(reference_images=[], weights=weights, model=reviewer)
    return detector.to_observation(
        result,
        camera_id=pending.sidecar["camera_id"],
        timestamp=pending.timestamp,
        frames_analyzed=len(pending.frame_paths),
        ring_classification=pending.sidecar.get("ring_classification"),
        temporal_likelihood=None,
        extra={
            "ring_event_id": pending.sidecar["event_id"],
            "ring_device_id": pending.sidecar["device_id"],
            "detector": "session",
            "staging_key": pending.key,
            "staged_temporal_likelihood": pending.sidecar.get("temporal_likelihood"),
            "temporal_policy": "tracker_only",
            **{key: pending.sidecar[key] for key in ("requeued_at", "superseded_observation_id")
               if key in pending.sidecar},
        },
    )


def stamp_local(pending: PendingEvent, result: Any, observation_id: int | None = None) -> None:
    """Persist a local-model decision into the event's sidecar.

    Auto-decided events are archived without a session ever seeing them, so the
    sidecar is the only record of *why*. The audit path (P4-29) reads it back to
    ask a session whether the model was right, so it must carry the score, not
    just the outcome.
    """
    pending.sidecar["local"] = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    pending.sidecar["local"]["decided_at"] = utcnow().isoformat()
    if observation_id is not None:
        pending.sidecar["local"]["observation_id"] = observation_id
    try:
        (pending.path / SIDECAR).write_text(json.dumps(pending.sidecar, indent=2))
    except OSError as e:                       # never cost an event its verdict
        log.warning("could not stamp local decision on %s: %s", pending.key, e)


def archived_local_decisions(dirs: StagingDirs, since: datetime | None = None,
                             outcomes: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Archived events the local models decided on their own, newest first.

    Only events carrying a `local` sidecar block with a settled outcome are
    returned: events a session decided, and events still awaiting review, are
    not the local model's work and are not auditable as such.
    """
    out: list[dict[str, Any]] = []
    for path in list_archived(dirs):
        try:
            car = json.loads((path / SIDECAR).read_text())
        except (OSError, ValueError):
            continue
        local = car.get("local") or {}
        outcome = local.get("outcome")
        if outcome in (None, "review"):
            continue
        if outcomes and outcome not in outcomes:
            continue
        ts = parse_timestamp(car["timestamp"])
        if since is not None and ts < since:
            continue
        frames = [str(path / f) for f in car.get("frames", []) if (path / f).is_file()]
        out.append({
            "key": path.name, "path": str(path), "event_id": car.get("event_id"),
            "device_id": car.get("device_id"), "camera_id": car.get("camera_id"),
            "timestamp": car["timestamp"], "frames": frames,
            "local_outcome": outcome, "local_score": local.get("score"),
            "local_reason": local.get("reason", ""),
            "gate": (local.get("gate") or {}).get("result"),
            "observation_id": local.get("observation_id"),
        })
    out.sort(key=lambda d: d["timestamp"], reverse=True)
    return out


def archive_event(dirs: StagingDirs, pending: PendingEvent, outcome: str) -> Path:
    """Move a processed event out of pending/ into archive/<date>/. Never deletes frames."""
    day = pending.timestamp.strftime("%Y-%m-%d")
    dest_dir = dirs.archive / day
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / pending.key
    if dest.exists():
        shutil.rmtree(dest)
    sidecar = dict(pending.sidecar)
    sidecar["outcome"] = outcome
    sidecar["archived_at"] = utcnow().isoformat()
    (pending.path / SIDECAR).write_text(json.dumps(sidecar, indent=2))
    shutil.move(str(pending.path), str(dest))
    return dest


def list_archived(dirs: StagingDirs) -> list[Path]:
    """Every archived event directory, oldest archive day first."""
    if not dirs.archive.is_dir():
        return []
    return [p for day in sorted(dirs.archive.iterdir()) if day.is_dir()
            for p in sorted(day.iterdir()) if p.is_dir() and (p / SIDECAR).is_file()]


def find_archived(dirs: StagingDirs, key: str) -> Path | None:
    """Look up an archived event by directory name or by Ring event id."""
    for p in list_archived(dirs):
        if p.name == key:
            return p
        try:
            if json.loads((p / SIDECAR).read_text()).get("event_id") == key:
                return p
        except (OSError, ValueError):
            continue
    return None


def requeue_event(dirs: StagingDirs, archived: Path) -> PendingEvent:
    """Move an archived event back into pending/ so its verdict can be re-fused.

    Used after a threshold or fusion change (P1-16): the frames and the
    verification question are unchanged, only the arithmetic applied to a
    verdict has moved. The sidecar keeps a breadcrumb — `requeued_at` and, when
    the ledger had one, `superseded_observation_id` — so the re-recorded
    observation can be told apart from the original during calibration.

    This does not delete the earlier observation row; re-recording inserts a new
    one. The caller is responsible for deciding what to do with the superseded
    row (see the warning in scripts/detect_pending.py).
    """
    dirs.ensure()
    dest = dirs.pending / archived.name
    if dest.exists():
        raise FileExistsError(f"{archived.name} is already pending")
    sidecar = json.loads((archived / SIDECAR).read_text())
    sidecar.pop("outcome", None)
    sidecar.pop("archived_at", None)
    sidecar.pop("skip_category", None)
    sidecar.pop("skip_reason", None)
    sidecar["requeued_at"] = utcnow().isoformat()
    (archived / SIDECAR).write_text(json.dumps(sidecar, indent=2))
    shutil.move(str(archived), str(dest))
    # A stale contact sheet would be reused as-is; drop it so it is rebuilt.
    sheet = dest / "sheet.jpg"
    if sheet.is_file():
        sheet.unlink()
    return PendingEvent(dest, sidecar)


def contact_sheet(image_paths: list[Path], out: Path, label: str, height: int = 480) -> Path | None:
    """Tile images side by side into one JPEG so a session views an event in one Read."""
    try:
        import cv2
    except ImportError:  # pragma: no cover - cv2 is a runtime dependency
        return None
    imgs = [cv2.imread(str(p)) for p in image_paths]
    imgs = [i for i in imgs if i is not None]
    if not imgs:
        return None
    scaled = [cv2.resize(i, (max(1, int(i.shape[1] * height / i.shape[0])), height)) for i in imgs]
    row = cv2.hconcat(scaled) if len(scaled) > 1 else scaled[0]
    cv2.putText(row, label, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
    cv2.imwrite(str(out), row, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return out


def event_sheet(pending: PendingEvent) -> Path | None:
    """`sheet.jpg` inside the event directory (cached; regenerated only if missing)."""
    out = pending.path / "sheet.jpg"
    if out.is_file():
        return out
    d = pending.sidecar
    label = f"{d['camera_id']}  {d['timestamp'][:19]}Z  ring={d.get('ring_classification')}"
    return contact_sheet(pending.frame_paths, out, label)


def reference_sheet(dirs: StagingDirs, reference_dir: Path, max_images: int = 8) -> Path | None:
    """One sheet of the enrolled photos, rebuilt when the reference set changes."""
    files = sorted(p for p in reference_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})[:max_images]
    if not files:
        return None
    dirs.ensure()
    out = dirs.root / "reference-sheet.jpg"
    newest = max(p.stat().st_mtime for p in files)
    if out.is_file() and out.stat().st_mtime >= newest:
        return out
    return contact_sheet(files, out, f"REFERENCE: Winston ({len(files)} enrolled photos)", height=420)


def prune_archive(dirs: StagingDirs, keep_days: float) -> int:
    """Delete archived event directories older than keep_days (by archive date folder)."""
    if keep_days <= 0 or not dirs.archive.is_dir():
        return 0
    cutoff = utcnow().timestamp() - keep_days * 86400
    removed = 0
    for day_dir in dirs.archive.iterdir():
        if day_dir.is_dir() and day_dir.stat().st_mtime < cutoff:
            shutil.rmtree(day_dir, ignore_errors=True)
            removed += 1
    return removed


Ingest = Callable[[Observation], dict[str, Any] | None]


def record_verdict(dirs: StagingDirs, pending: PendingEvent, verdict: dict[str, Any] | str,
                   ingest: Ingest, mark: Callable[[str, int | None, str | None], None] | None = None,
                   weights: FusionWeights | None = None) -> dict[str, Any]:
    """Verdict -> Observation -> ingest -> ledger -> archive. Returns the ingest result."""
    obs = verdict_to_observation(pending, verdict, weights)
    result = ingest(obs) or {}
    if mark is not None:
        mark("analyzed", result.get("observation_id"), None)
    archive_event(dirs, pending, "analyzed")
    return result


def skip_event(dirs: StagingDirs, pending: PendingEvent, reason: str,
               mark: Callable[[str, int | None, str | None], None] | None = None,
               *, category: str = "unspecified") -> Path:
    """Record why no verdict is possible, never a negative animal observation.

    Legacy callers stay unspecified: prose must not be guessed into a camera
    fault or an occlusion. The ledger keeps the category in its error prefix;
    the archived sidecar carries separate machine-readable fields.
    """
    if category not in SKIP_CATEGORIES:
        raise ValueError(f"skip category must be one of {SKIP_CATEGORIES}")
    if not reason.strip():
        raise ValueError("skip reason must not be blank")
    if mark is not None:
        mark("skipped", None, f"[{category}] {reason}")
    pending.sidecar["skip_category"] = category
    pending.sidecar["skip_reason"] = reason
    return archive_event(dirs, pending, f"skipped: {reason}")
