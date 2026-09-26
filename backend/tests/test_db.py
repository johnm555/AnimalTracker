from datetime import timedelta

import pytest

from src.db import Database
from src.observation import Observation
from tests.conftest import T0


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


def test_schema_creates_all_tables(db):
    names = {r["name"] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"cameras", "zones", "observations", "transitions", "notifications", "reference_images"} <= names


def test_sync_topology_populates_zones_and_cameras(db, topology):
    db.sync_topology(topology)
    zones = {z["id"] for z in db.list_zones()}
    cams = {c["id"]: c["zone_id"] for c in db.list_cameras()}
    assert zones == {"house", "backyard", "side-yard", "driveway", "front"}
    assert cams["kitchen-door"] == "house" and cams["front-door"] == "front"
    # idempotent
    db.sync_topology(topology)
    assert len(db.list_cameras()) == len(cams)


def test_observation_roundtrip(db):
    obs = Observation(camera_id="backyard", timestamp=T0, winston_probability=0.87,
                      ring_classification="animal", vision_similarity=0.9, vision_confidence=0.85,
                      size_appearance_compatible=True, temporal_likelihood=0.6, animal_present=True,
                      frames_analyzed=4, raw_response='{"x":1}', extra={"reasoning": "floppy ears"})
    obs_id = db.insert_observation(obs)
    assert obs.id == obs_id
    got = db.get_observation(obs_id)
    assert got is not None
    assert got.camera_id == "backyard"
    assert got.timestamp == T0
    assert got.winston_probability == pytest.approx(0.87)
    assert got.size_appearance_compatible is True
    assert got.animal_present is True
    assert got.extra == {"reasoning": "floppy ears"}
    assert db.get_observation(9999) is None


def test_list_observations_filters(db):
    for i, (cam, p) in enumerate([("backyard", 0.9), ("front-door", 0.3), ("backyard", 0.95)]):
        db.insert_observation(Observation(camera_id=cam, timestamp=T0 + timedelta(minutes=i), winston_probability=p))
    assert len(db.list_observations()) == 3
    assert len(db.list_observations(camera_id="backyard")) == 2
    assert len(db.list_observations(min_probability=0.5)) == 2
    assert len(db.list_observations(since=T0 + timedelta(minutes=1))) == 2
    assert len(db.list_observations(since=T0, until=T0 + timedelta(minutes=1))) == 1
    latest = db.latest_observation(min_probability=0.5)
    assert latest.timestamp == T0 + timedelta(minutes=2)


def test_transition_crud(db):
    tid = db.insert_transition(to_zone="backyard", arrived_at=T0, confidence=0.9,
                               from_zone=None, observation_ids=[1])
    tid2 = db.insert_transition(to_zone="side-yard", arrived_at=T0 + timedelta(seconds=30), confidence=0.8,
                                from_zone="backyard", departed_at=T0, observation_ids=[2, 3])
    t = db.get_transition(tid2)
    assert t["from_zone"] == "backyard" and t["to_zone"] == "side-yard"
    assert t["observation_ids"] == [2, 3]
    assert t["departed_at"] == T0.isoformat()
    assert db.latest_transition()["id"] == tid2
    assert [x["id"] for x in db.list_transitions()] == [tid, tid2]
    assert [x["id"] for x in db.list_transitions(since=T0 + timedelta(seconds=1))] == [tid2]
    assert len(db.transitions_for_day(T0)) == 2
    assert db.transitions_for_day(T0 + timedelta(days=1)) == []


def test_notification_records_and_cooldown_lookup(db):
    tid = db.insert_transition(to_zone="front", arrived_at=T0, confidence=0.9, from_zone="driveway")
    payload = {"aps": {"alert": {"title": "t", "body": "b"}}, "winston": {"zone": "front"}}
    db.insert_notification(tid, "high_priority", payload, backend="log", sent_at=T0)
    db.insert_notification(tid, "suppressed", payload, backend="log", sent_at=T0 + timedelta(seconds=5))
    rows = db.list_notifications()
    assert len(rows) == 2
    assert rows[0]["payload"] == payload and rows[0]["success"] is True
    last = db.last_notification_for("driveway", "front")
    assert last["type"] == "high_priority"  # suppressed rows don't count
    assert db.last_notification_for("backyard", "front") is None
    with pytest.raises(Exception):
        db.insert_notification(tid, "bogus-type", payload)


def test_reference_images(db, tmp_path):
    p = tmp_path / "winston1.jpg"
    rid = db.add_reference_image(p, label="side profile", sha256="abc")
    assert db.add_reference_image(p, label="updated") == rid  # upsert by path
    imgs = db.list_reference_images()
    assert len(imgs) == 1 and imgs[0]["label"] == "updated"
    db.remove_reference_image(p)
    assert db.list_reference_images() == []


def test_transaction_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        with db.transaction() as c:
            c.execute("INSERT INTO zones(id) VALUES('tmp')")
            raise RuntimeError("boom")
    assert db.list_zones() == []


def test_calibration_uses_latest_revision_not_highest_probability(db):
    identity = {"ring_device_id": "5000-a", "ring_event_id": "event-1"}
    old = Observation("winston-5000", T0, 0.99, extra=identity)
    db.insert_observation(old)
    new = Observation("winston-5000", T0, 0.1,
                      extra={**identity, "superseded_observation_id": old.id, "requeued_at": T0.isoformat()})
    db.insert_observation(new)
    assert [o.id for o in db.iter_calibration_observations()] == [new.id]
    assert [o.id for o in db.iter_observations_in_ingestion_order()] == [old.id, new.id]
    assert db.get_observation(old.id).winston_probability == 0.99


def test_calibration_keeps_distinct_devices_and_missing_identities(db):
    identities = [
        {"ring_device_id": "a", "ring_event_id": "same"},
        {"ring_device_id": "b", "ring_event_id": "same"},
        {"ring_device_id": "a", "ring_event_id": "different"},
        {}, {}, {"ring_event_id": "same"}, {"ring_event_id": "same"},
        {"ring_device_id": "", "ring_event_id": "same"},
        {"ring_device_id": "", "ring_event_id": "same"},
    ]
    for identity in identities:
        db.insert_observation(Observation("winston-5000", T0, 0.8, extra=identity))
    assert len(list(db.iter_calibration_observations())) == len(identities)


def test_calibration_handles_multiple_revisions_and_numeric_ids(db):
    for device in [123, "123", 123]:
        db.insert_observation(Observation("backyard", T0, 0.8,
                                         extra={"ring_device_id": device, "ring_event_id": "42"}))
    rows = list(db.iter_calibration_observations())
    assert len(rows) == 1 and rows[0].id == 3
    assert len(db.list_observations()) == 3
