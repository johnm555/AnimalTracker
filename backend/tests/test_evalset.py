"""P4-08: frozen labelled eval set — labels, sequences, splits, freezing."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import evalset
from src.db import Database
from src.observation import Observation

# After the calibration archive, so the default split is decided by the other rules.
T = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def _obs(db: Database, *, event: str, device: str = "111", camera: str = "deck-cam",
         seconds: float = 0, detector: str = "session", conf: float | None = 0.9,
         animal: bool | None = True, **extra) -> int:
    return db.insert_observation(Observation(
        camera_id=camera, timestamp=T + timedelta(seconds=seconds),
        winston_probability=conf or 0.0, vision_confidence=conf, animal_present=animal,
        extra={"ring_event_id": event, "ring_device_id": device, "detector": detector,
               "staging_key": f"{camera}__{event}", **extra}))


def _test_event_id(prefix: str) -> str:
    """An event id outside P4-18's held-out split, so it can land in test."""
    for i in range(1000):
        eid = f"{prefix}{i}"
        if not evalset.is_refset_holdout(eid):
            return eid
    raise AssertionError


def _holdout_event_id(prefix: str) -> str:
    for i in range(1000):
        eid = f"{prefix}{i}"
        if evalset.is_refset_holdout(eid):
            return eid
    raise AssertionError


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "w.db")
    d.init_schema()
    return d


def _archive(root: Path, camera: str, event: str, device: str = "111", n: int = 4) -> Path:
    d = root / "2026-09-24" / f"{camera}__{event}"
    d.mkdir(parents=True)
    (d / "event.json").write_text(json.dumps({"event_id": event, "device_id": device,
                                              "camera_id": camera}))
    for i in range(n):
        (d / f"frame-{i}.jpg").write_bytes(f"jpeg-{event}-{i}".encode())
    return d


@pytest.mark.parametrize("animal,conf,label", [
    (False, 0.0, "no_animal"), (True, 0.95, "winston"), (True, 0.70, "winston"),
    (True, 0.5, "uncertain"), (True, 0.1, "not_winston"), (True, None, "uncertain"),
])
def test_session_label_bands(animal, conf, label):
    assert evalset.session_label(animal, conf) == label


def test_local_only_decisions_are_not_labels(db):
    _obs(db, event="e1", detector="local", conf=0.9)
    _obs(db, event="e2", detector="session", conf=0.9, seconds=10)
    labels = evalset.collect_labels(db._conn)
    assert [e.event_id for e in labels] == ["e2"]


def test_latest_session_revision_wins(db):
    _obs(db, event="e1", conf=0.9)
    _obs(db, event="e1", conf=0.1)
    (ev,) = evalset.collect_labels(db._conn)
    assert ev.label == "not_winston"


def test_owner_review_beats_audit_beats_session(db):
    oid = _obs(db, event="e1", conf=0.5)                       # session: uncertain
    db.insert_local_audit(staging_key="deck-cam__e1", device_id="111", event_id="e1",
                          camera_id="deck-cam", event_at=T.isoformat(), local_outcome="review",
                          local_score=0.3, reviewer_label="no_animal", reviewer="claude-session",
                          notes="saw nothing", observation_id=oid)
    (ev,) = evalset.collect_labels(db._conn)
    assert (ev.label, ev.label_source) == ("no_animal", "audit")
    db.insert_observation_review(oid, "winston", "owner", "that's him")
    (ev,) = evalset.collect_labels(db._conn)
    assert (ev.label, ev.label_source) == ("winston", "owner")


def test_audit_labels_a_local_decision(db):
    oid = _obs(db, event="e1", detector="local", conf=0.9)
    db.insert_local_audit(staging_key="deck-cam__e1", device_id="111", event_id="e1",
                          camera_id="deck-cam", event_at=T.isoformat(), local_outcome="winston",
                          local_score=0.5, reviewer_label="winston", reviewer="claude-session",
                          notes="collar visible", observation_id=oid)
    (ev,) = evalset.collect_labels(db._conn)
    assert (ev.label, ev.label_source) == ("winston", "audit")


def test_sequences_chain_on_gap_and_device():
    E = lambda eid, dev, s: evalset.LabelledEvent(  # noqa: E731
        device_id=dev, event_id=eid, camera_id="c", timestamp=(T + timedelta(seconds=s)).isoformat(),
        label="winston", label_source="session")
    evs = [E("a", "1", 0), E("b", "1", 200), E("c", "1", 700), E("d", "2", 100)]
    evalset.assign_sequences(evs, gap_seconds=300)
    seq = {e.event_id: e.sequence_id for e in evs}
    assert seq["a"] == seq["b"] != seq["c"]
    assert seq["d"] not in (seq["a"], seq["c"])


def test_splits_keep_sequences_whole_and_tuned_data_out_of_test():
    E = lambda eid, s, dev="1": evalset.LabelledEvent(  # noqa: E731
        device_id=dev, event_id=eid, camera_id="c", timestamp=(T + timedelta(seconds=s)).isoformat(),
        label="winston", label_source="session")
    clean, gal, hold = _test_event_id("x"), _test_event_id("g"), _holdout_event_id("h")
    old = evalset.LabelledEvent(device_id="4", event_id=_test_event_id("o"), camera_id="c",
                                timestamp="2026-09-21T12:00:00+00:00", label="winston",
                                label_source="session")
    evs = [E(clean, 0, "1"), E(gal, 0, "2"), E(_test_event_id("n"), 60, "2"),
           E(hold, 0, "3"), E(_test_event_id("m"), 60, "3"), old]
    evalset.assign_sequences(evs)
    evalset.assign_splits(evs, gallery_event_ids={gal})
    split = {e.device_id: {x.split for x in evs if x.device_id == e.device_id} for e in evs}
    assert split == {"1": {"test"}, "2": {"excluded"}, "3": {"dev"}, "4": {"dev"}}


def test_build_freezes_frames_and_survives_archive_deletion(db, tmp_path):
    eid = _test_event_id("e")
    _obs(db, event=eid, conf=0.95)
    _obs(db, event=_test_event_id("z"), device="222", conf=0.95)   # no frames on disk
    archive, out = tmp_path / "archive", tmp_path / "evalset"
    src = _archive(archive, "deck-cam", eid)

    s = evalset.build(db._conn, archive, out, set())
    assert s["events"] == 1 and s["frames_unavailable"] == 1
    assert s["by_split"] == {"test": {"winston": 1}}
    frozen = out / "frames" / f"deck-cam__{eid}"
    assert sorted(p.name for p in frozen.iterdir()) == [f"frame-{i}.jpg" for i in range(4)]

    for p in src.iterdir():                     # retention sweep
        p.unlink()
    src.rmdir()
    db.insert_observation_review(1, "not_winston", "owner", "a visiting dog")
    s = evalset.build(db._conn, archive, out, set())
    (row,) = evalset.load_manifest(out).values()
    assert s["events"] == 1 and row["label"] == "not_winston" and row["label_source"] == "owner"
    assert evalset.verify(out)["frames_ok"] == 4

    (frozen / "frame-2.jpg").write_bytes(b"tampered")
    assert evalset.verify(out)["frames_mismatched"] == [str(frozen / "frame-2.jpg")]


def test_dry_run_writes_nothing(db, tmp_path):
    eid = _test_event_id("e")
    _obs(db, event=eid)
    archive, out = tmp_path / "archive", tmp_path / "evalset"
    _archive(archive, "deck-cam", eid)
    s = evalset.build(db._conn, archive, out, set(), dry_run=True)
    assert s["events"] == 1 and not out.exists()


def test_holdout_split_matches_reference_set_script():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "brs", Path(__file__).resolve().parents[2] / "scripts" / "build_reference_set.py")
    src = spec.origin and Path(spec.origin).read_text()
    # The script imports cv2 and the API; compare the constants textually instead.
    assert "HOLDOUT = 0.30" in src and 'f"refset:{event_id}"' in src
    assert evalset.HOLDOUT == 0.30
