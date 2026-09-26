from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api import AppContext, create_app
from src.notification import LogSender, NotificationPolicy, NotificationService, PolicyConfig
from src.state_machine import LocationTracker, TrackerConfig
from src.observation import utcnow

# Anchor to the wall clock so history/stats windows (which are relative to now) stay valid.
T0 = (utcnow() - timedelta(hours=1)).replace(microsecond=0)


@pytest.fixture
def client(tmp_path, topology):
    settings = {"tracker": {"confidence_threshold": 0.7, "strong_threshold": 0.9},
                "notifications": {"cooldown_seconds": 300, "high_priority_zones": ["front", "driveway"]}}
    from src.db import Database
    db = Database(tmp_path / "api.db")
    db.sync_topology(topology)
    tracker = LocationTracker(topology, TrackerConfig.from_dict(settings["tracker"]))
    policy = NotificationPolicy(PolicyConfig.from_settings(settings["notifications"]))
    notifier = NotificationService(policy, LogSender(), db)
    ctx = AppContext(settings, topology, db, tracker, notifier)
    with TestClient(create_app(ctx)) as c:
        yield c


def post(c, camera, seconds, p, **kw):
    body = {"camera_id": camera, "timestamp": (T0 + timedelta(seconds=seconds)).isoformat(),
            "winston_probability": p, **kw}
    r = c.post("/tracker/observation", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_location_unknown_initially(client):
    r = client.get("/tracker/location")
    assert r.status_code == 200
    assert r.json()["state"] == "unknown" and r.json()["zone"] is None


def test_observation_flow_and_notifications(client):
    r1 = post(client, "backyard", 0, 0.95)
    assert r1["accepted"] and r1["transition"]["to_zone"] == "backyard"
    assert r1["notification"]["type"] == "normal"

    r2 = post(client, "backyard", 10, 0.9)  # debounced
    assert r2["accepted"] and r2["transition"] is None

    r3 = post(client, "front-door", 12, 0.99)  # implausible
    assert r3["accepted"] is False and "implausible" in r3["rejection_reason"]

    post(client, "side-yard", 40, 0.95)
    r5 = post(client, "driveway", 70, 0.95)
    assert r5["transition"]["from_zone"] == "side-yard"
    assert r5["notification"]["type"] == "high_priority"

    loc = client.get("/tracker/location").json()
    assert loc["zone"] == "driveway"
    assert loc["state"] == "last_seen"  # T0 is an hour ago
    assert loc["minutes_ago"] >= 55

    hist = client.get("/tracker/history", params={"hours": 24}).json()
    assert [t["to_zone"] for t in hist["transitions"]] == ["backyard", "side-yard", "driveway"]

    day = client.get("/tracker/transitions", params={"date": T0.astimezone().date().isoformat()}).json()
    assert day["count"] == 3

    stats = client.get("/tracker/stats", params={"hours": 24}).json()
    assert stats["transitions"] == 3
    assert stats["street_adjacent_visits"] == 1
    assert stats["low_confidence_observations"] == 0
    assert stats["current"]["zone"] == "driveway"


def test_unknown_camera_rejected(client):
    r = client.post("/tracker/observation", json={"camera_id": "garage", "winston_probability": 0.9})
    assert r.status_code == 422


def test_probability_validation(client):
    r = client.post("/tracker/observation", json={"camera_id": "backyard", "winston_probability": 1.5})
    assert r.status_code == 422


def test_mute_auth_validation_and_delayed_transition(client, monkeypatch):
    monkeypatch.setenv("ANIMAL_TRACKER_API_TOKEN", "test-token")
    assert client.post("/tracker/mute").status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    for minutes in (-1, 1441, "invalid", "1.5"):
        assert client.post("/tracker/mute", params={"minutes": minutes}, headers=headers).status_code == 422
    assert client.get("/tracker/mute").json()["muted"] is False
    response = client.post("/tracker/mute", headers=headers)
    assert response.status_code == 200
    status = response.json()
    assert status["muted"] and status["scope"] == "all_devices"
    from src.observation import parse_timestamp
    assert (parse_timestamp(status["muted_until"]) - parse_timestamp(status["as_of"])).total_seconds() == 3600
    client.headers.update(headers)
    # The clip predates the mute by an hour; delivery is happening now.
    result = post(client, "backyard", 0, 0.95)
    assert result["notification"]["type"] == "silent"
    assert "manual mute" in result["notification"]["reason"]
    assert client.get("/tracker/history").json()["count"] == 1
    ctx = client.app.state.ctx
    assert len(ctx.db.list_observations()) == 1
    row = ctx.db.list_notifications()[0]
    assert row["type"] == "silent"
    assert "alert" not in row["payload"]["aps"]
    assert row["payload"]["aps"]["content-available"] == 1
    assert client.post("/tracker/mute?minutes=0").json()["muted"] is False
    result = post(client, "side-yard", 40, 0.95)
    assert result["notification"]["type"] == "normal"


def test_mute_survives_restart_and_expires(tmp_path, topology):
    from src.notification import SILENT, HIGH_PRIORITY, build_payload
    from src.observation import parse_timestamp
    from src.state_machine import TransitionEvent
    now = utcnow()
    db_path = str(tmp_path / "persist-mute.db")
    settings = {"notifications": {"backend": "log", "high_priority_zones": ["driveway"]}}
    first = AppContext.build(settings=settings, topology=topology, db_path=db_path)
    status = first.notifier.set_mute(60, now)
    until = parse_timestamp(status["muted_until"])
    first.db.close()
    restored = AppContext.build(settings=settings, topology=topology, db_path=db_path)
    try:
        assert restored.notifier.mute_status(now)["muted"]
        event = TransitionEvent(from_zone="side-yard", to_zone="driveway",
                                arrived_at=now - timedelta(hours=2), confidence=0.95)
        muted = restored.notifier.policy.decide(event, delivery_time=until - timedelta(microseconds=1))
        assert muted.type == SILENT  # Explicit mute also covers priority zones.
        assert "sound" not in build_payload(event, muted)["aps"]
        assert restored.notifier.policy.decide(event, delivery_time=until).type == HIGH_PRIORITY
        assert restored.notifier.mute_status(until)["muted"] is False
        restored.notifier.set_mute(0, now)
    finally:
        restored.db.close()
    again = AppContext.build(settings=settings, topology=topology, db_path=db_path)
    assert again.notifier.mute_status(now)["muted"] is False
    again.db.close()


def test_failed_mute_write_does_not_change_policy(client, monkeypatch):
    notifier = client.app.state.ctx.notifier
    before = notifier.policy.muted_until
    def fail(_):
        raise RuntimeError("test storage failure")
    monkeypatch.setattr(notifier.db, "set_muted_until", fail)
    with pytest.raises(RuntimeError, match="storage failure"):
        notifier.set_mute(60)
    assert notifier.policy.muted_until == before


# --------------------------------------------------------------------------- #
# P4-03: GET /tracker/trends
# --------------------------------------------------------------------------- #

def test_trends_empty_history_reports_zeroes_not_absence(client):
    """No data must read as 'nothing observed', never as a location claim."""
    r = client.get("/tracker/trends", params={"days": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 3 and len(body["trends"]) == 3
    assert [d["sightings"] for d in body["trends"]] == [0, 0, 0]
    assert all(d["inside_minutes"] == 0.0 and d["outside_minutes"] == 0.0 for d in body["trends"])
    assert all(d["observed_minutes"] == 0.0 and d["coverage"] == 0.0 for d in body["trends"])
    assert body["totals"]["transitions"] == 0
    # Dates ascend, newest last.
    assert body["trends"] == sorted(body["trends"], key=lambda d: d["date"])


def test_trends_only_today_is_partial(client):
    body = client.get("/tracker/trends", params={"days": 5}).json()
    assert [d["partial"] for d in body["trends"]] == [False, False, False, False, True]
    # A partial day is shorter than a full one and must not be averaged in.
    assert body["trends"][-1]["span_minutes"] <= 24 * 60
    assert body["averages_full_days"]["days"] == 4


def test_trends_counts_todays_activity(client, monkeypatch):
    # Pin the clock to local midday: with the wall clock, "an hour ago" is
    # yesterday for the first hour after midnight and "today" comes out empty.
    noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
    monkeypatch.setitem(globals(), "T0", noon)
    monkeypatch.setattr("src.api.utcnow", lambda: (noon + timedelta(minutes=10)).astimezone(timezone.utc))
    post(client, "backyard", 0, 0.95)
    post(client, "kitchen-door", 120, 0.95)
    post(client, "driveway", 300, 0.95)

    body = client.get("/tracker/trends", params={"days": 2}).json()
    today = body["trends"][-1]
    assert today["partial"] is True
    assert today["transitions"] == 3 and today["sightings"] == 3
    assert today["visits_by_zone"]["driveway"] == 1
    assert today["street_adjacent_visits"] == 1
    assert today["observed_minutes"] > 0 and today["coverage"] > 0
    assert body["totals"]["transitions"] == 3
    assert body["totals"]["visits_by_zone"]["driveway"] == 1


def test_trends_day_count_is_bounded(client):
    assert client.get("/tracker/trends", params={"days": 0}).status_code == 422
    assert client.get("/tracker/trends", params={"days": 91}).status_code == 422
    assert client.get("/tracker/trends", params={"days": 90}).status_code == 200


def test_trends_defaults_to_a_week(client):
    body = client.get("/tracker/trends").json()
    assert body["days"] == 7 and body["timezone"]


# --------------------------------------------------------------------------- #
# iMessage sender
# --------------------------------------------------------------------------- #

def test_imessage_sender_high_priority_sends_immediately(monkeypatch):
    from src.notification import IMessageSender, NotificationDecision, HIGH_PRIORITY, SILENT
    from src.state_machine import TransitionEvent
    from src.notification import build_payload

    sender = IMessageSender(recipient="test@example.com", settle_seconds=300)
    assert sender.name == "imessage"

    calls = []
    import subprocess
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        class FakeResult:
            returncode = 0
            stderr = ""
        return FakeResult()
    monkeypatch.setattr(subprocess, "run", fake_run)

    # High-priority sends immediately
    event = TransitionEvent(id=1, from_zone="backyard", to_zone="exit-door",
                            arrived_at=T0, confidence=0.95)
    decision = NotificationDecision(HIGH_PRIORITY, "Max at the exit door",
                                     "Moved to street-adjacent zone.")
    payload = build_payload(event, decision)
    sender.send(payload, decision)
    assert len(calls) == 1
    assert "osascript" in calls[0][0]
    assert "Max at the exit door" in calls[0][2]

    # Silent decisions should not send
    calls.clear()
    silent = NotificationDecision(SILENT, "Winston → front", "repeat")
    sender.send(payload, silent)
    assert len(calls) == 0


def test_imessage_sender_buffers_normal_transitions(monkeypatch):
    from src.notification import IMessageSender, NotificationDecision, NORMAL
    from src.state_machine import TransitionEvent
    from src.notification import build_payload

    sender = IMessageSender(recipient="test@example.com", settle_seconds=0.1)

    calls = []
    import subprocess
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        class FakeResult:
            returncode = 0
            stderr = ""
        return FakeResult()
    monkeypatch.setattr(subprocess, "run", fake_run)

    # Normal transition should be buffered, not sent immediately
    e1 = TransitionEvent(id=1, from_zone="backyard", to_zone="deck-cam",
                         arrived_at=T0, confidence=0.9)
    d1 = NotificationDecision(NORMAL, "Max → deck cam", "Moved.")
    sender.send(build_payload(e1, d1), d1)
    assert len(calls) == 0  # buffered, not sent

    e2 = TransitionEvent(id=2, from_zone="deck-cam", to_zone="living-room",
                         arrived_at=T0 + timedelta(seconds=60), confidence=0.9)
    d2 = NotificationDecision(NORMAL, "Winston → dog room", "Moved.")
    sender.send(build_payload(e2, d2), d2)
    assert len(calls) == 0  # still buffered

    # Wait for settle timer to fire
    import time as _time
    _time.sleep(0.3)
    assert len(calls) == 1  # journey summary sent
    assert "settled" in calls[0][2].lower()
    assert "deck cam" in calls[0][2]
    assert "living room" in calls[0][2]


def test_imessage_sender_on_off(monkeypatch):
    from src.notification import IMessageSender, NotificationDecision, NORMAL
    from src.state_machine import TransitionEvent
    from src.notification import build_payload

    sender = IMessageSender(recipient="test@example.com", settle_seconds=300)

    calls = []
    import subprocess
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        class FakeResult:
            returncode = 0
            stderr = ""
        return FakeResult()
    monkeypatch.setattr(subprocess, "run", fake_run)

    sender._enabled = False
    event = TransitionEvent(id=1, from_zone="backyard", to_zone="front",
                            arrived_at=T0, confidence=0.95)
    decision = NotificationDecision(NORMAL, "Winston → front", "Moved.")
    sender.send(build_payload(event, decision), decision)
    assert len(calls) == 0  # disabled, nothing buffered or sent

    sender._enabled = True
    # Re-enable would buffer again (no immediate send for normal)


def test_imessage_sender_from_settings():
    from src.notification import IMessageSender
    sender = IMessageSender.from_settings({"recipient": "owner@example.com", "settle_seconds": 120})
    assert sender.recipient == "owner@example.com"
    assert sender.settle_seconds == 120.0


def test_legacy_winston_routes_and_token_still_work(client, monkeypatch):
    """/winston/* and WINSTON_API_TOKEN are deprecated aliases until clients move."""
    old, new = client.get("/winston/location").json(), client.get("/tracker/location").json()
    assert {k: v for k, v in old.items() if k != "as_of"} == {k: v for k, v in new.items() if k != "as_of"}
    schema_paths = client.get("/openapi.json").json()["paths"]
    assert "/tracker/location" in schema_paths and not any(p.startswith("/winston/") for p in schema_paths)
    monkeypatch.delenv("ANIMAL_TRACKER_API_TOKEN", raising=False)
    monkeypatch.setenv("WINSTON_API_TOKEN", "legacy")
    assert client.post("/tracker/mute").status_code == 401
    assert client.post("/winston/mute", headers={"Authorization": "Bearer legacy"}).status_code == 200
