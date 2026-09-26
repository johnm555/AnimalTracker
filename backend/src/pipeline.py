"""End-to-end poller: Ring events -> frames -> detector -> tracker/API.

    python -m src.pipeline                # live Ring polling, standalone process
    python -m src.pipeline --fixtures dir # replay clips offline
    python -m src.pipeline --post http://127.0.0.1:8420   # send to a running API instead of in-process

In production the same `Poller` runs as a background thread inside the API
process (see `api.create_app`, enabled by `pipeline.enabled` in settings.yaml),
so `scripts/run.sh api` is the whole system: one process, one tracker, one DB.
The standalone entry point remains for offline replay and for debugging.

Two detector modes (`detector.mode` in settings.yaml):

  session (default)  No API key. The poller stages frames + a JSON sidecar per
                     event under backend/staging/pending/ and marks the event
                     `staged`. A scheduled Claude session views the frames,
                     answers "is this Winston?" and records the verdict with
                     scripts/detect_pending.py, which ingests through the API.
                     See src/staging.py and docs/Session_Detection.md.
  api                The poller calls the vision model itself (WinstonDetector
                     + ANTHROPIC_API_KEY) and ingests immediately.

Restart safety: every Ring event the poller touches is recorded in the
`processed_events` table. On restart the poller resumes from the newest
processed timestamp (or `startup_lookback_minutes` on a fresh DB) and skips
anything already analyzed, so a restart never re-bills the vision model for
clips it has seen. Failed events are retried up to `max_attempts`, then
skipped with the error recorded. A failure is never a sighting.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .api import AppContext, load_settings
from .db import Database
from .frame_extractor import FrameExtractor, FrameExtractorConfig
from .observation import Observation, utcnow
from .ring_client import FixtureRingClient, MotionEvent, RingClient
from .dog_detector import DogGate, GateSettings
from .local_detector import Decision, LocalDetector, LocalSettings
from .local_pipeline import LocalPipeline, LocalPipelineSettings, Outcome
from .staging import StagingDirs, stage_event
from .storage import StorageSettings, log_report, run_cleanup
from .winston_detector import FusionWeights, WinstonDetector, load_reference_images

log = logging.getLogger("pipeline")

Sink = Callable[[Observation], dict[str, Any] | None]


@dataclass
class PipelineSettings:
    enabled: bool = True
    startup_lookback_minutes: float = 30.0
    max_attempts: int = 3
    max_backoff_seconds: float = 600.0
    cleanup_interval_seconds: float = 3600.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "PipelineSettings":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            startup_lookback_minutes=float(d.get("startup_lookback_minutes", 30)),
            max_attempts=int(d.get("max_attempts", 3)),
            max_backoff_seconds=float(d.get("max_backoff_seconds", 600)),
            cleanup_interval_seconds=float(d.get("cleanup_interval_seconds", 3600)),
        )


@dataclass
class PollerStatus:
    """What /healthz reports. Everything here is observable fact, never a location."""

    state: str = "stopped"
    """stopped | starting | running | error"""
    started_at: str | None = None
    last_poll_at: str | None = None
    last_event_at: str | None = None
    """Timestamp of the newest Ring event handled (its own time, not ours)."""
    polls: int = 0
    events_seen: int = 0
    events_analyzed: int = 0
    events_staged: int = 0
    events_gated: int = 0
    events_local_accepted: int = 0
    events_local_skipped: int = 0
    local: dict[str, Any] = field(default_factory=dict)
    pipeline: dict[str, Any] = field(default_factory=dict)
    """Events the local gate dropped without review (only when skip_on_absent)."""
    gate: dict[str, Any] = field(default_factory=dict)
    events_skipped: int = 0
    events_failed: int = 0
    consecutive_errors: int = 0
    last_error: str | None = None
    cameras: list[str] = field(default_factory=list)
    mode: str = "session"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detector_mode(settings: dict[str, Any]) -> str:
    mode = str((settings.get("detector") or {}).get("mode", "session")).lower()
    if mode not in ("session", "api"):
        raise RuntimeError(f"detector.mode must be 'session' or 'api', not {mode!r}")
    return mode


def reference_dir(settings: dict[str, Any]) -> Path:
    d = settings.get("detector") or {}
    ref_dir = Path(d.get("reference_images_dir", "./reference_images"))
    return ref_dir if ref_dir.is_absolute() else Path(__file__).resolve().parent.parent / ref_dir


def staging_dirs(settings: dict[str, Any]) -> StagingDirs:
    d = settings.get("detector") or {}
    root = Path(d.get("staging_dir", "./staging"))
    return StagingDirs(root if root.is_absolute() else Path(__file__).resolve().parent.parent / root)


def build_detector(settings: dict[str, Any], ctx: AppContext | None) -> WinstonDetector:
    """API-mode detector. Session mode never constructs one (no key needed)."""
    d = settings.get("detector") or {}
    ref_dir = reference_dir(settings)
    detector = WinstonDetector(
        reference_dir=ref_dir,
        max_reference_images=int(d.get("max_reference_images", 6)),
        model=d.get("model", "claude-opus-5"),
        max_tokens=int(d.get("max_tokens", 4096)),
        weights=FusionWeights.from_settings(d),
        temporal_prior=ctx.tracker.temporal_likelihood if ctx else None,
    )
    if not detector.reference_images:
        raise RuntimeError(f"no reference images found in {ref_dir}; add a few clear photos of Winston first")
    # Fail at startup, not on the first clip. The SDK also accepts an `ant auth
    # login` profile (~/.config/anthropic), so only insist on the env var when
    # there is no profile either.
    has_env = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    has_profile = (Path.home() / ".config" / "anthropic").is_dir()
    if not has_env and not has_profile:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (put it in .env); the detector cannot run without it")
    return detector


def build_extractor(settings: dict[str, Any]) -> FrameExtractor:
    return FrameExtractor(FrameExtractorConfig(**{
        k: v for k, v in (settings.get("frames") or {}).items()
        if k in FrameExtractorConfig.__dataclass_fields__}))


class Poller:
    """Polls a Ring client and pushes each new event through detector -> sink.

    `sink(obs)` is `AppContext.ingest` in-process or an HTTP POST in --post
    mode; it returns the ingest result dict (with `observation_id`) or None.
    `db` is used only for the processed_events ledger, so a --post poller can
    share the API's SQLite file (WAL) without touching its tracker.
    """

    def __init__(
        self,
        ring: Any,
        extractor: FrameExtractor,
        detector: WinstonDetector | None,
        sink: Sink,
        db: Database,
        settings: PipelineSettings | None = None,
        poll_interval: float = 60.0,
        since: datetime | None = None,
        clip_dir: Path | None = None,
        mode: str = "api",
        staging: StagingDirs | None = None,
        n_reference: int = 0,
        temporal_prior: Callable[[str, datetime], float] | None = None,
        storage: StorageSettings | None = None,
        gate: DogGate | None = None,
        local: LocalDetector | None = None,
        pipeline: LocalPipeline | None = None,
    ) -> None:
        if mode == "api" and detector is None:
            raise ValueError("api mode needs a detector")
        if mode == "session" and staging is None:
            raise ValueError("session mode needs staging dirs")
        self.ring = ring
        self.extractor = extractor
        self.detector = detector
        self.sink = sink
        self.db = db
        self.settings = settings or PipelineSettings()
        self.poll_interval = poll_interval
        self.clip_dir = clip_dir
        self.mode = mode
        self.staging = staging
        self.n_reference = n_reference
        self.temporal_prior = temporal_prior
        self.storage = storage
        """Retention settings; None disables the hourly cleanup (tests, --post pollers)."""
        self.gate = gate or DogGate(GateSettings(enabled=False))
        """Local animal gate (P4-12). Annotates every event; only drops one when
        `detector.gate.skip_on_absent` is on — see dog_detector for why that is
        off by default."""
        self.local = local or LocalDetector(LocalSettings(enabled=False))
        """Local identity pre-filter (P4-11). ACCEPT writes an observation with no
        review; SKIP only declines to review; everything else is staged as before."""
        self.pipeline = pipeline
        """Combined gate+embedding decision (P4-27). When set and enabled this is
        the default path: only its REVIEW outcome reaches the session queue."""
        self.status = PollerStatus(mode=mode)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cleanup = 0.0
        self.cursor = since or self._initial_cursor()

    # -- lifecycle ---------------------------------------------------------

    def _initial_cursor(self) -> datetime:
        latest = self.db.latest_processed_timestamp()
        lookback = utcnow() - timedelta(minutes=self.settings.startup_lookback_minutes)
        if latest is None:
            return lookback
        # Resume just before the last processed event so a clip that arrived
        # out of order is not lost; the ledger dedupes the overlap.
        return max(lookback, latest - timedelta(minutes=5))

    def start(self) -> None:
        """Run in a daemon thread (used by the API process)."""
        self._thread = threading.Thread(target=self.run_forever, name="ring-poller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def run_forever(self) -> None:
        self.status.state = "starting"
        self.status.started_at = utcnow().isoformat()
        backoff = self.poll_interval
        try:
            self.ring.authenticate()
            self.status.cameras = [c["camera_id"] for c in self.ring.get_cameras()]
        except Exception as e:
            self.status.state = "error"
            self.status.last_error = f"ring auth: {type(e).__name__}: {e}"
            log.error("ring authentication failed; poller stopped: %s", e)
            return
        self.status.state = "running"
        log.info("poller running; %d camera(s), resume cursor %s", len(self.status.cameras), self.cursor.isoformat())
        while not self._stop.is_set():
            try:
                self.run_once()
                backoff = self.poll_interval
            except Exception as e:  # Ring outage, rate limit, network: keep going, back off
                self.status.consecutive_errors += 1
                self.status.last_error = f"{type(e).__name__}: {e}"
                backoff = min(backoff * 2, self.settings.max_backoff_seconds)
                log.warning("poll failed (%s); retrying in %.0fs", self.status.last_error, backoff)
            self._stop.wait(backoff)
        self.status.state = "stopped"

    # -- one cycle ---------------------------------------------------------

    def run_once(self) -> int:
        """One poll: fetch events newer than the cursor, handle each. Returns count handled."""
        events = self.ring.poll_events(since=self.cursor)
        self.status.polls += 1
        self.status.last_poll_at = utcnow().isoformat()
        self.status.consecutive_errors = 0
        handled = 0
        advance = True
        for event in events:  # oldest first
            if self._stop.is_set():
                break
            outcome = self.handle_event(event)
            if outcome in ("analyzed", "staged"):
                handled += 1
            elif outcome == "failed":
                # poll_events(since=cursor) is exclusive, so the cursor must not
                # move past a failed event or it would never be retried.
                advance = False
            if advance and event.timestamp > self.cursor:
                self.cursor = event.timestamp
        self._maybe_cleanup()
        return handled

    def handle_event(self, event: MotionEvent) -> str:
        """Analyze (api) or stage (session) one event. Returns analyzed | staged | duplicate | skipped | failed."""
        if self.db.event_processed(event.device_id, event.event_id):
            return "duplicate"
        self.status.events_seen += 1
        self.status.last_event_at = event.timestamp.isoformat()
        attempts = self.db.event_attempts(event.device_id, event.event_id)
        if attempts >= self.settings.max_attempts:
            self.db.mark_event(event, "skipped", error=f"gave up after {attempts} attempts")
            self.status.events_skipped += 1
            log.warning("skipping %s on %s after %d failed attempts", event.event_id, event.camera_id, attempts)
            return "skipped"

        log.info("event %s on %s at %s (%s)", event.event_id, event.camera_id,
                 event.timestamp.isoformat(), event.ring_classification)
        try:
            clip = self.ring.download_video(event)
            frames = self.extractor.extract(clip) if clip.suffix.lower() == ".mp4" else self.extractor.from_snapshot(clip)
            if not frames:
                raise RuntimeError("no frames decoded from clip")
            gate_verdict = self.gate.detect_frames([f.image.data for f in frames])
            self.status.gate = self.gate.stats()
            if gate_verdict.skippable:
                self.db.mark_event(event, "skipped", error=f"gate: {gate_verdict.reason()}"[:500])
                self.status.events_gated += 1
                log.info("  -> gate skipped: %s", gate_verdict.reason())
                return "skipped"
            if gate_verdict.result.value != "unknown":
                log.info("  -> gate: %s", gate_verdict.reason())
            if self.mode == "session":
                assert self.staging is not None
                prior = self.temporal_prior(event.camera_id, event.timestamp) if self.temporal_prior else None
                path = stage_event(self.staging, event, frames, self.n_reference, prior,
                                   gate=gate_verdict.to_dict() if self.gate.enabled else None)
                self.db.mark_event(event, "staged")
                outcome = self._local_triage(event, path, gate_verdict, prior)
                if outcome is not None:
                    return outcome
                self.status.events_staged += 1
                log.info("  -> staged %d frame(s) for session review: %s", len(frames), path.name)
                return "staged"
            assert self.detector is not None
            obs = self.detector.analyze(
                FrameExtractor.images(frames), event.camera_id, event.timestamp,
                ring_classification=event.ring_classification,
                extra={"ring_event_id": event.event_id, "ring_device_id": event.device_id,
                       "gate": gate_verdict.to_dict() if self.gate.enabled else None},
            )
        except Exception as e:
            # Never let one bad clip or one API error stop the loop, and never
            # record a failure as "not Winston".
            self.db.mark_event(event, "failed", error=f"{type(e).__name__}: {e}"[:500])
            self.status.events_failed += 1
            self.status.last_error = f"{event.camera_id}/{event.event_id}: {type(e).__name__}: {e}"
            log.warning("could not analyze %s on %s: %s: %s", event.event_id, event.camera_id, type(e).__name__, e)
            return "failed"

        log.info("  -> p(Winston)=%.2f  %s", obs.winston_probability, obs.extra.get("reasoning", ""))
        result = self.sink(obs) or {}
        self.db.mark_event(event, "analyzed", observation_id=result.get("observation_id"))
        self.status.events_analyzed += 1
        t = result.get("transition")
        if t:
            log.info("  ** %s -> %s (%.2f)", t["from_zone"], t["to_zone"], t["confidence"])
        return "analyzed"

    def _local_triage(self, event: MotionEvent, path: Path, gate_verdict: Any,
                      prior: float | None) -> str | None:
        """Classify a freshly staged event locally (P4-27).

        Returns the poller outcome when the local models settled it, or None to
        leave the event staged for the session path. The frames stay on disk
        either way: an auto-decided event is archived with its reasoning so it
        can be audited (P4-04) or requeued (P1-24) later.
        """
        if self.pipeline is None or not self.pipeline.settings.enabled:
            return None
        from .staging import (archive_event, find_pending, stamp_local as _stamp_local,
                              verdict_to_observation)  # local: cycle

        try:
            frames = sorted(path.glob("frame-*.jpg"))
            res = self.pipeline.classify(frames)
            self.status.local = self.local.stats()
            self.status.pipeline = self.pipeline.stats()
            pending = find_pending(self.staging, path.name)

            if res.outcome is Outcome.REVIEW:
                if pending is not None:
                    pending.sidecar["local"] = res.to_dict()
                    (pending.path / "event.json").write_text(json.dumps(pending.sidecar, indent=2))
                log.info("  -> review: %s", res.reason)
                return None

            if res.outcome in (Outcome.NO_ANIMAL, Outcome.NOT_WINSTON):
                # A skip, never a sighting and never a stored "not Winston"
                # observation: the ledger records the reason, the frames are kept.
                self.db.mark_event_by_id(event.device_id, event.event_id, "skipped",
                                         None, f"local {res.outcome.value}: {res.reason}"[:500])
                if pending is not None:
                    _stamp_local(pending, res)
                    archive_event(self.staging, pending, f"skipped: local {res.outcome.value}")
                self.status.events_skipped += 1
                self.status.events_local_skipped += 1
                log.info("  -> %s: %s", res.outcome.value, res.reason)
                return "skipped"

            # WINSTON
            if pending is None:
                return None
            obs = verdict_to_observation(pending, self.pipeline.detection_result(res),
                                         reviewer=f"local:{self.local.settings.model}")
            obs.extra["detector"] = "local"
            obs.extra["local_pipeline"] = res.to_dict()
            result = self.sink(obs) or {}
            self.db.mark_event_by_id(event.device_id, event.event_id, "analyzed",
                                     result.get("observation_id"), None)
            _stamp_local(pending, res, result.get("observation_id"))
            archive_event(self.staging, pending, "analyzed: local winston")
            self.status.events_local_accepted += 1
            self.status.events_analyzed += 1
            log.info("  -> WINSTON p=%.2f (%s)", obs.winston_probability, res.reason)
            t = result.get("transition")
            if t:
                log.info("  ** %s -> %s (%.2f)", t["from_zone"], t["to_zone"], t["confidence"])
            return "analyzed"
        except Exception as e:
            # A broken local model must never cost an event: fall back to review.
            log.warning("local triage failed for %s (%s: %s); staging for review",
                        path.name, type(e).__name__, e)
            return None

    # -- housekeeping ------------------------------------------------------

    def _maybe_cleanup(self) -> None:
        """Hourly retention pass (same code as scripts/run.sh cleanup). Never fatal."""
        if self.storage is None:
            return
        now = time.time()
        if now - self._last_cleanup < self.settings.cleanup_interval_seconds:
            return
        self._last_cleanup = now
        try:
            report = run_cleanup(self.storage, apply=True, mark_expired=self._mark_expired)
            if report.actions or report.warnings:
                log_report(report, log)
        except Exception as e:  # disk trouble must not stop polling
            log.warning("cleanup failed: %s: %s", type(e).__name__, e)

    def _mark_expired(self, device_id: str, event_id: str, reason: str) -> None:
        self.db.mark_event_by_id(device_id, event_id, "skipped", None, reason)


def build_poller(settings: dict[str, Any], ctx: AppContext, ring: Any | None = None,
                 since: datetime | None = None) -> Poller:
    """Wire a live poller into an existing AppContext (shared tracker + DB)."""
    ring_cfg = settings.get("ring") or {}
    ring = ring or RingClient.from_settings(ring_cfg)
    download_dir = Path(ring_cfg.get("download_dir", "./ring_downloads"))
    if not download_dir.is_absolute():
        download_dir = Path(__file__).resolve().parent.parent / download_dir
    mode = detector_mode(settings)
    common: dict[str, Any] = dict(
        ring=ring, extractor=build_extractor(settings), sink=ctx.ingest, db=ctx.db,
        settings=PipelineSettings.from_dict(settings.get("pipeline")),
        poll_interval=float(ring_cfg.get("poll_interval_seconds", 60)),
        since=since, clip_dir=download_dir, mode=mode,
    )
    common["storage"] = StorageSettings.from_settings(settings)
    common["gate"] = DogGate(GateSettings.from_settings(settings))
    _local = LocalDetector(LocalSettings.from_settings(settings), reference_dir(settings),
                           staging_dirs(settings).root / "gallery.pt")
    common["local"] = _local
    common["pipeline"] = LocalPipeline(common["gate"], _local, LocalPipelineSettings.from_settings(settings))
    if mode == "session":
        n_ref = len(load_reference_images(reference_dir(settings)))
        if n_ref == 0:
            log.warning("no reference images in %s; the session will have nothing to compare against",
                        reference_dir(settings))
        return Poller(detector=None, staging=staging_dirs(settings), n_reference=n_ref,
                      temporal_prior=ctx.tracker.temporal_likelihood, **common)
    return Poller(detector=build_detector(settings, ctx), **common)


# --------------------------------------------------------------------------- #
# Standalone entry point
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", help="path to settings.yaml")
    ap.add_argument("--fixtures", help="replay clips from this directory instead of Ring")
    ap.add_argument("--post", metavar="URL", help="POST observations to a running API instead of in-process")
    ap.add_argument("--since", help="ISO timestamp; ignore events before this")
    ap.add_argument("--once", action="store_true", help="one poll cycle, then exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.settings)
    since = datetime.fromisoformat(args.since) if args.since else None

    ctx: AppContext | None = None
    if args.post:
        import httpx

        base = args.post.rstrip("/")

        headers = {}
        if os.environ.get("WINSTON_API_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['WINSTON_API_TOKEN']}"

        def sink(obs: Observation) -> dict[str, Any]:
            r = httpx.post(f"{base}/winston/observation", json=obs.to_dict(), headers=headers, timeout=10.0)
            r.raise_for_status()
            return r.json()

        db_path = Path(settings.get("database", {}).get("path", "./winston.db"))
        if not db_path.is_absolute():
            db_path = Path(__file__).resolve().parent.parent / db_path
        db = Database(db_path)
    else:
        ctx = AppContext.build(settings)
        sink = ctx.ingest
        db = ctx.db

    mode = detector_mode(settings)
    detector = None
    if mode == "api":
        try:
            detector = build_detector(settings, ctx)
        except RuntimeError as e:
            sys.exit(str(e))
    ring: Any = FixtureRingClient(args.fixtures) if args.fixtures else RingClient.from_settings(settings.get("ring"))
    ring_cfg = settings.get("ring") or {}
    _cli_gate = DogGate(GateSettings.from_settings(settings))
    _cli_local = LocalDetector(LocalSettings.from_settings(settings), reference_dir(settings),
                               staging_dirs(settings).root / "gallery.pt")
    poller = Poller(
        ring=ring, extractor=build_extractor(settings), detector=detector, sink=sink, db=db,
        settings=PipelineSettings.from_dict(settings.get("pipeline")),
        poll_interval=float(ring_cfg.get("poll_interval_seconds", 60)),
        since=since or (datetime.min.replace(tzinfo=utcnow().tzinfo) if args.fixtures else None),
        mode=mode, staging=staging_dirs(settings) if mode == "session" else None,
        gate=_cli_gate,
        local=_cli_local,
        pipeline=LocalPipeline(_cli_gate, _cli_local, LocalPipelineSettings.from_settings(settings)),
        n_reference=len(load_reference_images(reference_dir(settings))),
        temporal_prior=ctx.tracker.temporal_likelihood if ctx else None,
    )
    if args.once or args.fixtures:
        ring.authenticate()
        n = poller.run_once()
        log.info("done: %d event(s) %s", n, "staged for session review" if mode == "session" else "analyzed")
        return 0
    try:
        poller.run_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
