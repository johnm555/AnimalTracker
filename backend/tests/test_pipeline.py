"""Poller: restart-safe dedupe, failure handling, and the in-process API wiring."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import AppContext, create_app
from src.db import Database
from src.notification import LogSender, NotificationPolicy, NotificationService, PolicyConfig
from src.observation import Observation, utcnow
from src.pipeline import PipelineSettings, Poller
from src.ring_client import MotionEvent
from src.state_machine import LocationTracker, TrackerConfig

T0 = (utcnow() - timedelta(minutes=10)).replace(microsecond=0)


class FakeRing:
    """Serves a fixed event list; records which clips were downloaded."""

    def __init__(self, events, fail_download=()):
        self.events = events
        self.downloads = []
        self.fail_download = set(fail_download)
        self.authenticated = False

    def authenticate(self, otp_callback=None):
        self.authenticated = True

    def get_cameras(self):
        return [{"device_id": e.device_id, "camera_id": e.camera_id} for e in self.events]

    def poll_events(self, since=None, limit=20):
        return sorted((e for e in self.events if since is None or e.timestamp > since), key=lambda e: e.timestamp)

    def download_video(self, event, dest=None, **_):
        self.downloads.append(event.event_id)
        if event.event_id in self.fail_download:
            raise RuntimeError("recording not ready")
        return Path(f"/nonexistent/{event.event_id}.mp4")


class FakeExtractor:
    def extract(self, path):
        return [SimpleNamespace(index=0, image=SimpleNamespace(data=b"jpg", media_type="image/jpeg"))]

    from_snapshot = extract


class FakeDetector:
    """Returns a fixed probability per camera; counts model calls (the expensive part)."""

    def __init__(self, probabilities, fail_on=()):
        self.probabilities = probabilities
        self.calls = 0
        self.fail_on = set(fail_on)

    def analyze(self, frames, camera_id, timestamp, ring_classification=None, extra=None):
        self.calls += 1
        if camera_id in self.fail_on:
            raise RuntimeError("vision API 529")
        return Observation(camera_id=camera_id, timestamp=timestamp,
                           winston_probability=self.probabilities.get(camera_id, 0.0),
                           frames_analyzed=len(frames), extra=dict(extra or {}))


def event(event_id, camera, seconds, device=None):
    return MotionEvent(event_id=event_id, camera_id=camera, device_id=device or f"dev-{camera}",
                       timestamp=T0 + timedelta(seconds=seconds), ring_classification="human")


@pytest.fixture
def ctx(tmp_path, topology):
    settings = {"tracker": {"confidence_threshold": 0.7, "strong_threshold": 0.9}}
    db = Database(tmp_path / "p.db")
    db.sync_topology(topology)
    tracker = LocationTracker(topology, TrackerConfig.from_dict(settings["tracker"]))
    notifier = NotificationService(NotificationPolicy(PolicyConfig()), LogSender(), db)
    return AppContext(settings, topology, db, tracker, notifier)


def make_poller(ctx, ring, detector, **kw):
    kw.setdefault("since", T0 - timedelta(seconds=1))
    return Poller(ring=ring, extractor=FakeExtractor(), detector=detector, sink=ctx.ingest,
                  db=ctx.db, settings=PipelineSettings(max_attempts=2), **kw)


def test_events_flow_to_tracker_and_are_deduped_across_restart(ctx):
    events = [event("e1", "backyard", 0), event("e2", "backyard", 30), event("e3", "kitchen-door", 400)]
    detector = FakeDetector({"backyard": 0.95, "kitchen-door": 0.95})
    poller = make_poller(ctx, FakeRing(events), detector)

    assert poller.run_once() == 3
    assert detector.calls == 3
    assert ctx.tracker.current_state(T0 + timedelta(seconds=401)).zone == "house"
    assert len(ctx.db.list_observations()) == 3
    ledger = {r["event_id"]: r for r in ctx.db.list_processed_events()}
    assert set(ledger) == {"e1", "e2", "e3"}
    assert all(r["status"] == "analyzed" and r["observation_id"] for r in ledger.values())

    # Same events again (a second poll, or a process restart): no new model calls, no duplicates.
    assert poller.run_once() == 0
    restarted = make_poller(ctx, FakeRing(events), detector, since=None)
    assert restarted.run_once() == 0
    assert detector.calls == 3
    assert len(ctx.db.list_observations()) == 3


def test_failed_event_is_retried_then_skipped_never_a_sighting(ctx):
    events = [event("bad", "backyard", 0), event("good", "backyard", 60)]
    ring = FakeRing(events, fail_download={"bad"})
    detector = FakeDetector({"backyard": 0.95})
    poller = make_poller(ctx, ring, detector)

    poller.run_once()
    # The good event still went through; the bad one is recorded as failed, not as an observation.
    assert detector.calls == 1
    assert [o.extra["ring_event_id"] for o in ctx.db.list_observations()] == ["good"]
    assert ctx.db.list_processed_events(limit=10)[-1]["status"] == "failed"
    assert poller.status.events_failed == 1
    # The cursor must not have moved past the failure, so it is retried...
    assert poller.cursor < events[0].timestamp + timedelta(seconds=1)
    poller.run_once()
    assert ring.downloads.count("bad") == 2
    # ...and after max_attempts it is skipped with the reason recorded.
    poller.run_once()
    rows = {r["event_id"]: r for r in ctx.db.list_processed_events()}
    assert rows["bad"]["status"] == "skipped" and "attempts" in rows["bad"]["error"]
    assert ring.downloads.count("bad") == 2
    assert len(ctx.db.list_observations()) == 1


def test_detector_error_does_not_stop_the_loop(ctx):
    events = [event("k1", "kitchen-door", 0), event("b1", "backyard", 60)]
    detector = FakeDetector({"backyard": 0.95}, fail_on={"kitchen-door"})
    poller = make_poller(ctx, FakeRing(events), detector)
    assert poller.run_once() == 1
    assert poller.status.events_analyzed == 1 and poller.status.events_failed == 1
    assert "vision API" in poller.status.last_error


def test_fresh_db_starts_from_lookback_not_history(ctx):
    old = event("old", "backyard", -3 * 3600)  # three hours before T0
    new = event("new", "backyard", 0)
    detector = FakeDetector({"backyard": 0.95})
    poller = Poller(ring=FakeRing([old, new]), extractor=FakeExtractor(), detector=detector,
                    sink=ctx.ingest, db=ctx.db, settings=PipelineSettings(startup_lookback_minutes=30))
    poller.run_once()
    assert [o.extra["ring_event_id"] for o in ctx.db.list_observations()] == ["new"]


def test_run_forever_reports_auth_failure_without_raising(ctx):
    class BadRing(FakeRing):
        def authenticate(self, otp_callback=None):
            raise RuntimeError("no cached token")

    poller = make_poller(ctx, BadRing([]), FakeDetector({}))
    poller.run_forever()
    assert poller.status.state == "error" and "no cached token" in poller.status.last_error


def test_healthz_and_device_registration(ctx, monkeypatch):
    # Device registration is independent of the real machine's archive size.
    monkeypatch.setattr("src.storage.usage", lambda _: {
        "total_gb": 0, "max_storage_gb": 10, "over_limit": False,
        "disk_free_gb": 100, "bytes": {},
    })
    with TestClient(create_app(ctx)) as c:
        h = c.get("/healthz").json()
        assert h["status"] == "ok" and h["pipeline"]["state"] == "disabled"
        assert h["notifications"] == {"backend": "log", "device_tokens": 0}

        token = "ab" * 32
        r = c.post("/winston/devices", json={"token": token.upper(), "name": "John's watch"})
        assert r.status_code == 201 and r.json()["device_tokens"] == 1
        c.post("/winston/devices", json={"token": token})  # idempotent
        assert ctx.db.list_device_tokens()[0]["token"] == token
        assert c.get("/healthz").json()["notifications"]["device_tokens"] == 1

        assert c.post("/winston/devices", json={"token": "not-hex!"}).status_code == 422
        assert c.delete(f"/winston/devices/{token}").status_code == 200
        assert c.delete(f"/winston/devices/{token}").status_code == 404

        # A live poller's status shows up verbatim.
        ctx.poller = SimpleNamespace(status=SimpleNamespace(
            state="running", to_dict=lambda: {"state": "running", "events_analyzed": 4}), stop=lambda: None)
        assert c.get("/healthz").json()["pipeline"]["events_analyzed"] == 4


def test_write_endpoints_require_bearer_token_when_configured(ctx, monkeypatch):
    monkeypatch.setenv("WINSTON_API_TOKEN", "s3cret")
    with TestClient(create_app(ctx)) as c:
        body = {"camera_id": "backyard", "winston_probability": 0.95}
        assert c.post("/winston/observation", json=body).status_code == 401
        assert c.post("/winston/observation", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.post("/winston/observation", json=body, headers={"Authorization": "Bearer s3cret"}).status_code == 201
        assert c.post("/winston/devices", json={"token": "ab" * 16}).status_code == 401
        assert c.get("/winston/location").status_code == 200  # reads stay open
        assert c.get("/healthz").status_code == 200
