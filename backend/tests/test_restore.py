from datetime import datetime, timedelta, timezone
from src.api import AppContext
from src.observation import Observation
from src.state_machine import Topology

T = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


def topology():
    return Topology.from_dict({"zones": {
        "a": {"cameras": ["cam-a"], "neighbors": {"b": {"min_seconds": 3, "max_seconds": 120}}},
        "b": {"cameras": ["cam-b"]}, "isolated": {"cameras": ["cam-isolated"]}}})


def context(path):
    return AppContext.build({"notifications": {"backend": "log"}}, db_path=str(path), topology=topology())


def obs(camera, seconds, p=.99):
    return Observation(camera_id=camera, timestamp=T + timedelta(seconds=seconds), winston_probability=p)


def test_rejected_sighting_does_not_refresh_old_zone_on_restart(tmp_path):
    path = tmp_path / "tracker.db"
    ctx = context(path)
    ctx.ingest(obs("cam-a", 0))
    assert not ctx.ingest(obs("cam-isolated", 600))["accepted"]
    before = ctx.tracker.current_state(T + timedelta(seconds=600)).to_dict()
    transitions = ctx.db._conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
    notifications = ctx.db._conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    ctx.db.close()
    restored = context(path)
    assert restored.tracker.current_state(T + timedelta(seconds=600)).to_dict() == before
    assert before["state"] == "last_seen"
    assert restored.db._conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0] == transitions
    assert restored.db._conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == notifications
    restored.db.close()


def test_pending_confirmation_survives_restart(tmp_path):
    path = tmp_path / "tracker.db"
    ctx = context(path)
    ctx.ingest(obs("cam-a", 0))
    ctx.ingest(obs("cam-b", 10, .8))
    before = ctx.tracker.current_state(T + timedelta(seconds=10)).to_dict()
    ctx.db.close()
    restored = context(path)
    assert restored.tracker.current_state(T + timedelta(seconds=10)).to_dict() == before
    result = restored.ingest(obs("cam-b", 20, .8))
    assert result["transition"]["to_zone"] == "b"
    restored.db.close()


def test_same_zone_refresh_and_out_of_order_evidence_replay(tmp_path):
    path = tmp_path / "tracker.db"
    ctx = context(path)
    ctx.ingest(obs("cam-a", 0))
    ctx.ingest(obs("cam-a", 60))
    ctx.ingest(obs("cam-b", 1))
    before = ctx.tracker.current_state(T + timedelta(seconds=65)).to_dict()
    ctx.db.close()
    restored = context(path)
    assert restored.tracker.current_state(T + timedelta(seconds=65)).to_dict() == before
    restored.db.close()


def ring_obs(camera, seconds, event_id, device_id="device", p=.8):
    item = obs(camera, seconds, p)
    item.extra = {"ring_device_id": device_id, "ring_event_id": event_id}
    return item


def test_duplicate_event_cannot_confirm_move_before_or_after_restart(tmp_path):
    path = tmp_path / "dedupe.db"
    ctx = context(path)
    ctx.ingest(ring_obs("cam-a", 0, "initial", p=.99))
    first = ctx.ingest(ring_obs("cam-b", 10, "candidate"))
    assert first["transition"] is None and first["state"]["state"] == "transitioning"
    duplicate = ctx.ingest(ring_obs("cam-b", 10, "candidate", p=.99))
    assert not duplicate["accepted"] and duplicate["transition"] is None
    assert "duplicate Ring event" in duplicate["rejection_reason"]
    assert len(ctx.db.list_observations()) == 3  # calibration/audit revisions persist
    before = ctx.tracker.current_state(T + timedelta(seconds=10)).to_dict()
    ctx.db.close()
    restored = context(path)
    assert restored.tracker.current_state(T + timedelta(seconds=10)).to_dict() == before
    again = restored.ingest(ring_obs("cam-b", 10, "candidate"))
    assert not again["accepted"] and again["transition"] is None
    confirmed = restored.ingest(ring_obs("cam-b", 15, "independent"))
    assert confirmed["transition"]["to_zone"] == "b"
    assert len(restored.db.list_transitions()) == 2
    assert restored.db._conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2


def test_revised_rejected_event_is_calibration_only(tmp_path):
    ctx = context(tmp_path / "rejected.db")
    assert not ctx.ingest(ring_obs("cam-a", 0, "event", p=.1))["accepted"]
    revised = ctx.ingest(ring_obs("cam-a", 0, "event", p=.99))
    assert not revised["accepted"] and revised["state"]["state"] == "unknown"
    assert len(ctx.db.list_observations()) == 2
    assert list(ctx.db.iter_calibration_observations())[0].winston_probability == .99


def test_same_event_id_on_different_devices_is_independent(tmp_path):
    ctx = context(tmp_path / "devices.db")
    ctx.ingest(ring_obs("cam-a", 0, "initial", p=.99))
    ctx.ingest(ring_obs("cam-b", 10, "shared-id", device_id="one"))
    result = ctx.ingest(ring_obs("cam-b", 15, "shared-id", device_id="two"))
    assert result["transition"]["to_zone"] == "b"


def test_ring_identity_normalizes_numbers_and_duplicate_does_not_refresh_time(tmp_path):
    ctx = context(tmp_path / "numeric.db")
    ctx.ingest(ring_obs("cam-a", 0, 42, device_id=123, p=.99))
    result = ctx.ingest(ring_obs("cam-a", 100, "42", device_id="123", p=.99))
    assert not result["accepted"]
    assert ctx.tracker.current_state(T + timedelta(seconds=100)).timestamp == T
