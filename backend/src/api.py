"""FastAPI server: the query surface for the Watch app and anything else.

    GET  /tracker/location                 current state + confidence + minutes since seen
    GET  /tracker/history?hours=N          movement history (transitions) for the last N hours
    GET  /tracker/transitions?date=YYYY-MM-DD
    POST /tracker/observation              ingest a new Observation (from the detector/pipeline)
    GET  /tracker/stats?hours=N            activity metrics
    GET  /tracker/trends?days=N            per-day inside/outside minutes + zone visits
    GET|POST /tracker/mute                 notification mute
    POST /tracker/devices                  register an APNs device token {token, platform?, name?}
    DELETE /tracker/devices/{token}        unregister
    GET  /healthz                          liveness + poller status (never a location)

Every /tracker/* route is also served at its old /winston/* path, hidden from
the OpenAPI schema and marked deprecated, until clients have moved.

Run with:  uvicorn src.api:app --host 127.0.0.1 --port 8420

When `pipeline.enabled` is true in settings.yaml (the default) the Ring
poller runs as a background thread in this process, sharing the tracker and
DB, so this one command is the whole system. Set ANIMAL_TRACKER_PIPELINE=0 to serve
without polling (tests, replay, a second read-only instance).
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from .db import Database
from .notification import NotificationPolicy, NotificationService, PolicyConfig, sender_from_settings
from .observation import Observation, parse_timestamp, utcnow
from .paths import data_dir, settings_path, cameras_path, db_path as resolve_db_path, env, resolve
from .state_machine import LocationTracker, Topology, TrackerConfig

log = logging.getLogger(__name__)

# Legacy aliases — only used by capture.py; prefer paths module elsewhere.
BACKEND_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = data_dir() / "config"


def configure_logging() -> None:
    """Make the app's own loggers visible under uvicorn/launchd.

    uvicorn configures only its own loggers, so `src.*` INFO lines (poller
    activity, notification decisions) would otherwise be dropped. Idempotent.
    """
    root = logging.getLogger()
    if not any(getattr(h, "_winston", False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler._winston = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    level = (env("LOG_LEVEL") or "INFO").upper()
    for name in ("src", "pipeline"):
        logging.getLogger(name).setLevel(level)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


def load_settings(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path or env("SETTINGS") or settings_path())
    with open(p) as f:
        return yaml.safe_load(f) or {}


class AppContext:
    """Everything the endpoints need, built once at startup (or by tests)."""

    def __init__(self, settings: dict[str, Any], topology: Topology, db: Database,
                 tracker: LocationTracker, notifier: NotificationService | None) -> None:
        self.settings = settings
        self.topology = topology
        self.db = db
        self.tracker = tracker
        self.notifier = notifier
        self.lock = threading.Lock()
        self.house_zones = set(settings.get("stats", {}).get("inside_zones", ["house"]))
        self.poller: Any | None = None
        """Background Ring poller (pipeline.Poller) when running in-process, else None."""
        self.poller_error: str | None = None

    @classmethod
    def build(cls, settings: dict[str, Any] | None = None, db_path: str | None = None,
              topology: Topology | None = None, notifier: NotificationService | None = None) -> "AppContext":
        settings = settings if settings is not None else load_settings()
        topology = topology or Topology.from_yaml(
            env("CAMERAS") or cameras_path())
        db = Database(db_path or resolve_db_path(settings))
        db.sync_topology(topology)
        tracker = LocationTracker(topology, TrackerConfig.from_dict(settings.get("tracker")))
        # Re-run the deterministic tracker against original ingestion order.
        # A merely high-scoring observation may have been rejected or pending;
        # it cannot safely supply a last-seen timestamp for the last transition.
        # No persistence or notification callbacks are attached during replay.
        for observation in db.iter_observations_in_ingestion_order():
            tracker.process(observation)
        tracker.history.clear()
        tracker.rejections.clear()
        if notifier is None:
            policy = NotificationPolicy(PolicyConfig.from_settings(settings.get("notifications")))
            notifier = NotificationService(policy, sender_from_settings(settings.get("notifications"), db), db)
        return cls(settings, topology, db, tracker, notifier)

    # -- core ingest -------------------------------------------------------

    def ingest(self, obs: Observation) -> dict[str, Any]:
        with self.lock:
            self.db.insert_observation(obs)
            # Session-mode verdicts carry the Ring event identity: close the
            # ledger row here, on the connection that just committed the
            # observation, so no second process has to race our snapshot.
            dev_id, ev_id = obs.extra.get("ring_device_id"), obs.extra.get("ring_event_id")
            if dev_id and ev_id:
                self.db.mark_event_by_id(str(dev_id), str(ev_id), "analyzed", obs.id, None)
            n_rejections = len(self.tracker.rejections)
            event = self.tracker.process(obs)
            rejected = len(self.tracker.rejections) > n_rejections
            result: dict[str, Any] = {
                "observation_id": obs.id,
                "accepted": not rejected,
                "rejection_reason": self.tracker.rejections[-1].reason if rejected else None,
                "transition": None,
                "notification": None,
                "state": self.tracker.current_state(obs.timestamp).to_dict(obs.timestamp),
            }
            if event is not None:
                event.id = self.db.insert_transition(
                    to_zone=event.to_zone, arrived_at=event.arrived_at, confidence=event.confidence,
                    from_zone=event.from_zone, departed_at=event.departed_at,
                    observation_ids=event.observation_ids,
                )
                result["transition"] = event.to_dict()
                if self.notifier is not None:
                    decision = self.notifier.handle_transition(event, now=obs.timestamp)
                    result["notification"] = {"type": decision.type, "title": decision.title,
                                              "body": decision.body, "reason": decision.reason}
            return result


def _resolve(p: str) -> str:
    return str(resolve(p))


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #

class ObservationIn(BaseModel):
    camera_id: str
    timestamp: datetime | None = None
    winston_probability: float = Field(ge=0.0, le=1.0)
    ring_classification: str | None = None
    vision_similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    vision_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    size_appearance_compatible: bool | None = None
    temporal_likelihood: float | None = Field(default=None, ge=0.0, le=1.0)
    animal_present: bool | None = None
    frames_analyzed: int = 0
    raw_response: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_observation(self) -> Observation:
        d = self.model_dump()
        d["timestamp"] = d["timestamp"] or utcnow()
        return Observation(**d)


class DetectionRunIn(BaseModel):
    run_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")
    reviewer: str = Field(min_length=1, max_length=200)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    status: Literal["completed", "partial", "failed"]
    events_reviewed: int = Field(ge=0, strict=True)
    sheets_viewed: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_run(self) -> "DetectionRunIn":
        self.reviewer = self.reviewer.strip()
        if not self.reviewer:
            raise ValueError("reviewer is required")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        if self.finished_at > utcnow() + timedelta(minutes=5):
            raise ValueError("finished_at is in the future")
        return self

    def record(self) -> dict[str, Any]:
        d = self.model_dump()
        d["started_at"] = parse_timestamp(self.started_at).isoformat()
        d["finished_at"] = parse_timestamp(self.finished_at).isoformat()
        d["wall_seconds"] = (self.finished_at - self.started_at).total_seconds()
        return d


class ReviewIn(BaseModel):
    label: Literal["winston", "not_winston", "uncertain"]
    reviewer: str = Field(min_length=1, max_length=200)
    notes: str = Field(min_length=1, max_length=4000)


class DeviceIn(BaseModel):
    token: str = Field(min_length=16, max_length=512, pattern=r"^[0-9a-fA-F]+$")
    platform: str = "watchos"
    name: str | None = None


class LocationOut(BaseModel):
    state: str
    zone: str | None
    zone_label: str | None
    confidence: float
    last_seen_at: datetime | None
    minutes_ago: float | None
    from_zone: str | None = None
    to_zone: str | None = None
    as_of: datetime


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

def create_app(ctx: AppContext | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if ctx is None:
            configure_logging()
        app.state.ctx = ctx or AppContext.build()
        log.info("Animal Tracker API ready; tracker state: %s",
                 app.state.ctx.tracker.current_state(utcnow()).to_dict())
        if ctx is None:  # production build: start the Ring poller alongside the API
            start_poller(app.state.ctx)
        yield
        if app.state.ctx.poller is not None:
            app.state.ctx.poller.stop()
        app.state.ctx.db.close()

    app = FastAPI(title="Animal Tracker", version="0.1.0", lifespan=lifespan)

    def get_ctx(request: Request) -> AppContext:
        return request.app.state.ctx

    def require_write_token(authorization: str | None = Header(default=None)) -> None:
        """Mutations need `Authorization: Bearer $ANIMAL_TRACKER_API_TOKEN` when it is set.

        Reads stay open: the API is meant for a home LAN and the watch polls it.
        Without the env var (development, tests) writes are open too.
        """
        expected = env("API_TOKEN")
        if not expected:
            return
        if authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    @app.get("/healthz")
    def healthz(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        """Liveness plus what the poller has been doing. Reports facts, not a location."""
        out: dict[str, Any] = {"status": "ok", "version": app.version}
        if ctx.poller is not None:
            out["pipeline"] = ctx.poller.status.to_dict()
            if ctx.poller.status.state == "error":
                out["status"] = "degraded"
        else:
            out["pipeline"] = {"state": "disabled", "last_error": ctx.poller_error}
            if ctx.poller_error:
                out["status"] = "degraded"
        out["notifications"] = {
            "backend": ctx.notifier.sender.name if ctx.notifier else None,
            "device_tokens": len(ctx.db.list_device_tokens()),
        }
        # Session-mode detection: how much is waiting for a Claude session to look at.
        from .pipeline import detector_mode, staging_dirs  # local: pipeline imports this module
        from .staging import list_pending

        out["detector"] = {"mode": detector_mode(ctx.settings)}
        if out["detector"]["mode"] == "session":
            pending = list_pending(staging_dirs(ctx.settings))
            out["detector"]["pending_events"] = len(pending)
            out["detector"]["oldest_pending"] = pending[0].timestamp.isoformat() if pending else None
        from .storage import StorageSettings, usage

        u = usage(StorageSettings.from_settings(ctx.settings))
        out["storage"] = {"total_gb": u["total_gb"], "max_storage_gb": u["max_storage_gb"],
                          "over_limit": u["over_limit"], "disk_free_gb": u["disk_free_gb"],
                          "bytes": u["bytes"]}
        if u["over_limit"]:
            out["status"] = "degraded"
        return out

    @app.get("/tracker/mute")
    @app.get("/winston/mute", include_in_schema=False, deprecated=True)
    def mute_status(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        with ctx.lock:
            if ctx.notifier is None:
                raise HTTPException(status_code=503, detail="notification service unavailable")
            return ctx.notifier.mute_status()

    @app.post("/tracker/mute", dependencies=[Depends(require_write_token)])
    @app.post("/winston/mute", dependencies=[Depends(require_write_token)], include_in_schema=False, deprecated=True)
    def mute(minutes: int = Query(default=60, ge=0, le=1440),
             ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        with ctx.lock:
            if ctx.notifier is None:
                raise HTTPException(status_code=503, detail="notification service unavailable")
            return ctx.notifier.set_mute(minutes)

    @app.post("/tracker/devices", status_code=201, dependencies=[Depends(require_write_token)])
    @app.post("/winston/devices", status_code=201, dependencies=[Depends(require_write_token)], include_in_schema=False, deprecated=True)
    def register_device(body: DeviceIn, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        ctx.db.register_device_token(body.token.lower(), body.platform, body.name)
        return {"registered": True, "device_tokens": len(ctx.db.list_device_tokens())}

    @app.delete("/tracker/devices/{token}", dependencies=[Depends(require_write_token)])
    @app.delete("/winston/devices/{token}", dependencies=[Depends(require_write_token)], include_in_schema=False, deprecated=True)
    def unregister_device(token: str, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        removed = ctx.db.remove_device_token(token.lower())
        if not removed:
            raise HTTPException(status_code=404, detail="unknown device token")
        return {"removed": True}

    @app.get("/tracker/location", response_model=LocationOut)
    @app.get("/winston/location", response_model=LocationOut, include_in_schema=False, deprecated=True)
    def location(ctx: AppContext = Depends(get_ctx)) -> LocationOut:
        now = utcnow()
        with ctx.lock:
            st = ctx.tracker.current_state(now)
        d = st.to_dict(now)
        zone = d["zone"]
        label = ctx.notifier.policy.label(zone) if (zone and ctx.notifier) else zone
        return LocationOut(
            state=d["state"], zone=zone, zone_label=label, confidence=d["confidence"],
            last_seen_at=st.timestamp, minutes_ago=d.get("minutes_ago"),
            from_zone=d.get("from_zone"), to_zone=d.get("to_zone"), as_of=now,
        )

    @app.get("/tracker/history")
    @app.get("/winston/history", include_in_schema=False, deprecated=True)
    def history(hours: float = Query(default=24, gt=0, le=24 * 30),
                ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        since = utcnow() - timedelta(hours=hours)
        transitions = ctx.db.list_transitions(since=since)
        return {"since": since.isoformat(), "hours": hours,
                "count": len(transitions), "transitions": transitions}

    @app.get("/tracker/transitions")
    @app.get("/winston/transitions", include_in_schema=False, deprecated=True)
    def transitions(date_: date | None = Query(default=None, alias="date"),
                    ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        day = date_ or datetime.now().astimezone().date()
        # Interpret the date in local time, query in UTC.
        start_local = datetime.combine(day, datetime.min.time()).astimezone()
        start = start_local.astimezone(timezone.utc)
        rows = ctx.db.list_transitions(since=start, until=start + timedelta(days=1))
        return {"date": day.isoformat(), "count": len(rows), "transitions": rows}

    @app.post("/tracker/observation", status_code=201, dependencies=[Depends(require_write_token)])
    @app.post("/winston/observation", status_code=201, dependencies=[Depends(require_write_token)], include_in_schema=False, deprecated=True)
    def post_observation(body: ObservationIn, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        if ctx.topology.zone_for_camera(body.camera_id) is None:
            raise HTTPException(status_code=422, detail=f"unknown camera '{body.camera_id}'")
        return ctx.ingest(body.to_observation())

    @app.post("/tracker/observations/{observation_id}/reviews", status_code=201,
              dependencies=[Depends(require_write_token)])
    @app.post("/winston/observations/{observation_id}/reviews", status_code=201,
              dependencies=[Depends(require_write_token)], include_in_schema=False, deprecated=True)
    def review_observation(observation_id: int, body: ReviewIn,
                           ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        with ctx.lock:
            if ctx.db.get_observation(observation_id) is None:
                raise HTTPException(status_code=404, detail="unknown observation")
            if not body.reviewer.strip() or not body.notes.strip():
                raise HTTPException(status_code=422, detail="reviewer and evidence notes are required")
            return ctx.db.insert_observation_review(observation_id, body.label, body.reviewer, body.notes)

    @app.get("/tracker/observations/{observation_id}/reviews")
    @app.get("/winston/observations/{observation_id}/reviews", include_in_schema=False, deprecated=True)
    def observation_reviews(observation_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        with ctx.lock:
            if ctx.db.get_observation(observation_id) is None:
                raise HTTPException(status_code=404, detail="unknown observation")
            return {"observation_id": observation_id,
                    "reviews": ctx.db.list_observation_reviews(observation_id)}

    @app.post("/tracker/detection-runs", status_code=201, dependencies=[Depends(require_write_token)])
    @app.post("/winston/detection-runs", status_code=201, dependencies=[Depends(require_write_token)], include_in_schema=False, deprecated=True)
    def record_detection_run(body: DetectionRunIn, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        with ctx.lock:
            try:
                return ctx.db.record_detection_run(body.record())
            except ValueError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/tracker/detection-runs")
    @app.get("/winston/detection-runs", include_in_schema=False, deprecated=True)
    def detection_runs(hours: float = Query(default=24, gt=0, le=8760),
                       limit: int = Query(default=100, ge=1, le=1000),
                       ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        now = utcnow()
        with ctx.lock:
            return ctx.db.detection_runs_report(now - timedelta(hours=hours), now, limit)

    @app.get("/tracker/quality")
    @app.get("/winston/quality", include_in_schema=False, deprecated=True)
    def quality(hours: float = Query(default=24, gt=0, le=24 * 365),
                ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        from .quality import quality_report
        now = utcnow()
        with ctx.lock:
            return quality_report(ctx.db, ctx.tracker.config.confidence_threshold,
                                  now - timedelta(hours=hours), now)

    @app.get("/tracker/stats")
    @app.get("/winston/stats", include_in_schema=False, deprecated=True)
    def stats(hours: float = Query(default=24, gt=0, le=24 * 30),
              ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        now = utcnow()
        since = now - timedelta(hours=hours)
        return compute_stats(ctx, since, now)

    # -- visiting animals (P4-30). Deliberately outside /winston/: these are
    # not sightings of Winston and must never be read as his location.
    @app.get("/animals")
    def animals(days: float = Query(default=7, gt=0, le=365),
                species: str | None = None,
                ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        from .animals import normalize_species, summarize
        since = utcnow() - timedelta(days=days)
        rows = ctx.db.list_animal_sightings(
            since=since, species=normalize_species(species) if species else None)
        return {"since": since.isoformat(), "sightings": rows, "summary": summarize(rows)}

    @app.get("/animals/candidates")
    def animal_candidates(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        """Non-Winston animals with no species recorded yet — the labelling queue."""
        rows = ctx.db.unlabelled_animal_observations(ctx.tracker.config.confidence_threshold)
        return {"count": len(rows), "candidates": rows}

    @app.post("/animals", status_code=201, dependencies=[Depends(require_write_token)])
    def record_animal(payload: dict[str, Any],
                      ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        from .animals import AnimalSighting
        from .observation import parse_timestamp
        try:
            sighting = AnimalSighting(
                camera_id=payload["camera_id"],
                timestamp=parse_timestamp(payload["timestamp"]),
                species=payload["species"],
                source=payload.get("source", "session"),
                confidence=payload.get("confidence"),
                notes=payload.get("notes", ""),
                observation_id=payload.get("observation_id"),
                staging_key=payload.get("staging_key"),
                extra=payload.get("extra") or {})
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=422, detail=str(e))
        return ctx.db.record_animal_sighting(sighting)

    @app.get("/tracker/trends")
    @app.get("/winston/trends", include_in_schema=False, deprecated=True)
    def trends(days: int = Query(default=7, ge=1, le=90),
               ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
        return compute_trends(ctx, days, utcnow())

    return app


def start_poller(ctx: AppContext) -> None:
    """Start the in-process Ring poller unless disabled. Failures degrade /healthz, never the API."""
    from .pipeline import PipelineSettings, build_poller  # local import: pipeline imports this module

    if env("PIPELINE", "1") == "0" or not PipelineSettings.from_dict(
            ctx.settings.get("pipeline")).enabled:
        log.info("Ring poller disabled (pipeline.enabled=false or ANIMAL_TRACKER_PIPELINE=0)")
        return
    try:
        ctx.poller = build_poller(ctx.settings, ctx)
    except Exception as e:  # missing key / reference images / bad config
        ctx.poller_error = f"{type(e).__name__}: {e}"
        log.error("Ring poller not started: %s", ctx.poller_error)
        return
    ctx.poller.start()


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC start/end of a local calendar day (same convention as ?date=)."""
    start_local = datetime.combine(day, datetime.min.time()).astimezone()
    start = start_local.astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def compute_trends(ctx: AppContext, days: int, now: datetime) -> dict[str, Any]:
    """Per-day inside/outside minutes and zone visits, for the watch StatsView.

    Days are *local* calendar days (CLAUDE.md: local time is used for the
    `?date=` query and quiet hours), newest last. The current day is partial and
    says so, so the watch never presents a half-finished day as comparable to a
    full one.

    Nothing here invents coverage: a day with no transitions reports zeroes and
    `sightings: 0`, which means "nothing was observed", not "Winston was
    nowhere". `observed_minutes` is the share of the day the tracker could
    attribute to some zone at all — the honest denominator for the other
    numbers.
    """
    today = now.astimezone().date()
    rows: list[dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        start, end = local_day_bounds(day)
        until = min(end, now)
        if until <= start:
            continue
        s = compute_stats(ctx, start, until)
        attributed = sum(s["minutes_by_zone"].values())
        span_minutes = (until - start).total_seconds() / 60.0
        rows.append({
            "date": day.isoformat(),
            "partial": end > now,
            "span_minutes": round(span_minutes, 1),
            "observed_minutes": round(attributed, 1),
            "coverage": round(attributed / span_minutes, 3) if span_minutes else 0.0,
            "inside_minutes": s["inside_minutes"],
            "outside_minutes": s["outside_minutes"],
            "minutes_by_zone": s["minutes_by_zone"],
            "visits_by_zone": s["visits_by_zone"],
            "transitions": s["transitions"],
            "sightings": s["sightings"],
            "street_adjacent_visits": s["street_adjacent_visits"],
        })

    full = [r for r in rows if not r["partial"]]
    totals_zone: dict[str, int] = {}
    for r in rows:
        for z, v in r["visits_by_zone"].items():
            totals_zone[z] = totals_zone.get(z, 0) + v
    return {
        "days": len(rows),
        "generated_at": now.isoformat(),
        "timezone": str(now.astimezone().tzinfo),
        "trends": rows,
        "totals": {
            "transitions": sum(r["transitions"] for r in rows),
            "sightings": sum(r["sightings"] for r in rows),
            "visits_by_zone": totals_zone,
            "street_adjacent_visits": sum(r["street_adjacent_visits"] for r in rows),
        },
        # Averages deliberately exclude the partial day.
        "averages_full_days": {
            "days": len(full),
            "inside_minutes": round(sum(r["inside_minutes"] for r in full) / len(full), 1) if full else None,
            "outside_minutes": round(sum(r["outside_minutes"] for r in full) / len(full), 1) if full else None,
            "transitions": round(sum(r["transitions"] for r in full) / len(full), 2) if full else None,
        },
    }


def compute_stats(ctx: AppContext, since: datetime, now: datetime) -> dict[str, Any]:
    hours = (now - since).total_seconds() / 3600.0
    transitions = ctx.db.list_transitions(since=since, until=now)
    observations = ctx.db.list_observations(since=since, until=now)
    confident = [o for o in observations if o.winston_probability >= ctx.tracker.config.confidence_threshold]

    # Time in each zone: walk the transition list, with the zone before the
    # window taken from the last transition prior to `since`.
    prior = ctx.db.list_transitions(until=since, limit=100000)
    current_zone = prior[-1]["to_zone"] if prior else None
    cursor = since
    zone_seconds: dict[str, float] = {}
    for t in transitions:
        arrived = parse_timestamp(t["arrived_at"])
        if current_zone is not None:
            zone_seconds[current_zone] = zone_seconds.get(current_zone, 0.0) + (arrived - cursor).total_seconds()
        current_zone, cursor = t["to_zone"], arrived
    if current_zone is not None:
        zone_seconds[current_zone] = zone_seconds.get(current_zone, 0.0) + (now - cursor).total_seconds()

    ambiguous = set(ctx.settings.get("stats", {}).get("ambiguous_zones", []))
    outside = sum(s for z, s in zone_seconds.items()
                  if z not in ctx.house_zones and z not in ambiguous)
    inside = sum(s for z, s in zone_seconds.items() if z in ctx.house_zones)
    per_zone_visits: dict[str, int] = {}
    for t in transitions:
        per_zone_visits[t["to_zone"]] = per_zone_visits.get(t["to_zone"], 0) + 1
    rejected = len([o for o in observations if o.winston_probability < ctx.tracker.config.confidence_threshold])

    return {
        "window": {"since": since.isoformat(), "until": now.isoformat(), "hours": round(hours, 2)},
        "transitions": len(transitions),
        "transitions_per_hour": round(len(transitions) / hours, 2) if hours else 0.0,
        "sightings": len(confident),
        "low_confidence_observations": rejected,
        "outside_minutes": round(outside / 60.0, 1),
        "inside_minutes": round(inside / 60.0, 1),
        "minutes_by_zone": {z: round(s / 60.0, 1) for z, s in sorted(zone_seconds.items())},
        "visits_by_zone": per_zone_visits,
        "most_visited_zone": max(per_zone_visits, key=per_zone_visits.get) if per_zone_visits else None,
        "street_adjacent_visits": sum(v for z, v in per_zone_visits.items()
                                      if z in (ctx.notifier.policy.config.high_priority_zones if ctx.notifier else ())),
        "current": ctx.tracker.current_state(now).to_dict(now),
    }


app = create_app()
