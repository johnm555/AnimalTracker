"""Spot-checking the local models (P4-29): sampling, agreement, and the feedback loop."""

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from src.db import Database
from src.observation import utcnow
from src.staging import StagingDirs, archived_local_decisions

import detect_pending as cli


# --------------------------------------------------------------------------- #
# fixtures: an archive of events the local models decided on their own
# --------------------------------------------------------------------------- #

def archive(dirs: StagingDirs, key: str, outcome: str, score: float,
            camera: str = "deck-cam", age_hours: float = 1.0,
            observation_id: int | None = None, frames: int = 2) -> Path:
    ts = utcnow() - timedelta(hours=age_hours)
    d = dirs.archive / ts.strftime("%Y-%m-%d") / key
    d.mkdir(parents=True)
    for i in range(frames):
        (d / f"frame-{i}.jpg").write_bytes(b"\xff\xd8jpeg")
    local = {"outcome": outcome, "score": score, "reason": f"local said {outcome}",
             "gate": {"result": "present" if outcome == "winston" else "absent"}}
    if observation_id:
        local["observation_id"] = observation_id
    (d / "event.json").write_text(json.dumps({
        "event_id": f"ev-{key}", "device_id": "100000001", "camera_id": camera,
        "timestamp": ts.isoformat(), "frames": [f"frame-{i}.jpg" for i in range(frames)],
        "outcome": f"analyzed: local {outcome}", "local": local}))
    return d


@pytest.fixture
def dirs(tmp_path):
    d = StagingDirs(tmp_path / "staging")
    d.ensure()
    return d


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


# --------------------------------------------------------------------------- #
# reading the archive
# --------------------------------------------------------------------------- #

def test_only_events_the_models_settled_are_auditable(dirs):
    archive(dirs, "a", "winston", 0.5)
    archive(dirs, "b", "no_animal", 0.1)
    # An event a session still has to review is not the local model's work.
    p = archive(dirs, "c", "review", 0.25)
    car = json.loads((p / "event.json").read_text())
    car["local"]["outcome"] = "review"
    (p / "event.json").write_text(json.dumps(car))
    # An event with no local block at all (session-decided, or pre-P4-27).
    q = archive(dirs, "d", "winston", 0.9)
    car = json.loads((q / "event.json").read_text())
    del car["local"]
    (q / "event.json").write_text(json.dumps(car))

    keys = {d["key"] for d in archived_local_decisions(dirs)}
    assert keys == {"a", "b"}


def test_decisions_carry_the_score_and_the_observation(dirs):
    archive(dirs, "a", "winston", 0.42, observation_id=7)
    d = archived_local_decisions(dirs)[0]
    assert d["local_outcome"] == "winston" and d["local_score"] == 0.42
    assert d["observation_id"] == 7 and len(d["frames"]) == 2
    assert d["camera_id"] == "deck-cam"


def test_since_filters_by_event_time(dirs):
    archive(dirs, "old", "winston", 0.5, age_hours=50)
    archive(dirs, "new", "winston", 0.5, age_hours=2)
    recent = archived_local_decisions(dirs, since=utcnow() - timedelta(hours=24))
    assert [d["key"] for d in recent] == ["new"]


# --------------------------------------------------------------------------- #
# sampling
# --------------------------------------------------------------------------- #

def test_sample_is_balanced_across_outcomes_not_proportional(dirs, db):
    """20 skips and 2 sightings must not yield a sample of 10 skips."""
    for i in range(20):
        archive(dirs, f"skip{i}", "no_animal", 0.05)
    for i in range(2):
        archive(dirs, f"win{i}", "winston", 0.6)
    import random
    got = cli.audit_sample(dirs, db, size=8, hours=24, rng=random.Random(0))
    outcomes = [d["local_outcome"] for d in got]
    assert len(got) == 8
    assert outcomes.count("winston") == 2            # both of the rare ones
    assert outcomes.count("no_animal") == 6


def test_already_audited_events_are_never_offered_again(dirs, db):
    for i in range(4):
        archive(dirs, f"e{i}", "winston", 0.6)
    import random
    db.insert_local_audit(staging_key="e0", device_id="d", event_id="x", camera_id="c",
                          event_at=utcnow().isoformat(), local_outcome="winston",
                          local_score=0.6, reviewer_label="winston",
                          reviewer="claude-session", notes="ok")
    got = cli.audit_sample(dirs, db, size=8, hours=24, rng=random.Random(0))
    assert "e0" not in {d["key"] for d in got} and len(got) == 3


def test_sample_skips_events_whose_frames_are_gone(dirs, db):
    d = archive(dirs, "gone", "winston", 0.6)
    for f in d.glob("frame-*.jpg"):
        f.unlink()
    archive(dirs, "here", "winston", 0.6)
    import random
    got = cli.audit_sample(dirs, db, size=8, hours=24, rng=random.Random(0))
    assert [x["key"] for x in got] == ["here"]


# --------------------------------------------------------------------------- #
# agreement is derived, never asserted
# --------------------------------------------------------------------------- #

def test_agreement_is_computed_from_what_the_reviewer_saw(db):
    def rec(outcome, saw):
        return db.insert_local_audit(
            staging_key=f"{outcome}-{saw}", device_id="d", event_id="e", camera_id="c",
            event_at=utcnow().isoformat(), local_outcome=outcome, local_score=0.3,
            reviewer_label=saw, reviewer="claude-session", notes="n")["agrees"]

    assert rec("winston", "winston") is True
    assert rec("winston", "not_winston") is False
    assert rec("no_animal", "no_animal") is True
    assert rec("no_animal", "winston") is False        # the destroyed sighting
    assert rec("winston", "uncertain") is False        # uncertainty is not agreement


def test_a_review_outcome_cannot_be_wrong(db):
    """REVIEW makes no claim, so no answer contradicts it."""
    for saw in ("winston", "no_animal", "uncertain"):
        r = db.insert_local_audit(staging_key=f"r-{saw}", device_id="d", event_id="e",
                                  camera_id="c", event_at=utcnow().isoformat(),
                                  local_outcome="review", local_score=0.25,
                                  reviewer_label=saw, reviewer="s", notes="n")
        assert r["agrees"] is True


def test_reviewer_and_notes_are_required(db):
    with pytest.raises(ValueError):
        db.insert_local_audit(staging_key="k", device_id="d", event_id="e", camera_id="c",
                              event_at=utcnow().isoformat(), local_outcome="winston",
                              local_score=0.5, reviewer_label="winston", reviewer="  ", notes="n")


def test_re_auditing_updates_rather_than_double_counting(db):
    for saw in ("winston", "no_animal"):
        db.insert_local_audit(staging_key="k", device_id="d", event_id="e", camera_id="c",
                              event_at=utcnow().isoformat(), local_outcome="no_animal",
                              local_score=0.1, reviewer_label=saw, reviewer="s", notes="n")
    s = db.local_audit_summary()
    assert s["audited"] == 1 and s["agreed"] == 1


def test_summary_reports_rates_per_outcome_and_names_the_disagreements(db):
    for i in range(3):
        db.insert_local_audit(staging_key=f"w{i}", device_id="d", event_id="e", camera_id="deck",
                              event_at=utcnow().isoformat(), local_outcome="winston",
                              local_score=0.5, reviewer_label="winston", reviewer="s", notes="n")
    db.insert_local_audit(staging_key="m", device_id="d", event_id="e", camera_id="garden-cam",
                          event_at=utcnow().isoformat(), local_outcome="no_animal",
                          local_score=0.21, reviewer_label="winston", reviewer="s",
                          notes="he is asleep on the bed")
    s = db.local_audit_summary()
    assert s["agreement_rate"] == 0.75
    assert s["by_outcome"]["winston"]["agreement_rate"] == 1.0
    assert s["by_outcome"]["no_animal"]["agreement_rate"] == 0.0
    assert s["by_outcome"]["no_animal"]["disagreed"][0]["score"] == 0.21


def test_summary_window_excludes_older_audits(db):
    db.insert_local_audit(staging_key="k", device_id="d", event_id="e", camera_id="c",
                          event_at=utcnow().isoformat(), local_outcome="winston",
                          local_score=0.5, reviewer_label="winston", reviewer="s", notes="n")
    assert db.local_audit_summary(since=utcnow() + timedelta(days=1))["audited"] == 0
    assert db.local_audit_summary(since=utcnow() - timedelta(days=1))["audited"] == 1


# --------------------------------------------------------------------------- #
# the CLI round trip
# --------------------------------------------------------------------------- #

def settings_for(tmp_path, dirs):
    return {"database": {"path": str(tmp_path / "t.db")},
            "staging": {"path": str(dirs.root)},
            "detector": {"pipeline": {"accept_threshold": 0.30, "rescue_threshold": 0.23}}}


def run(fn, tmp_path, dirs, **kw):
    import argparse
    args = argparse.Namespace(**kw)
    return fn(args, settings_for(tmp_path, dirs), dirs)


def test_audit_record_flags_a_missed_sighting_and_leaves_it_archived(tmp_path, dirs, capsys):
    archive(dirs, "miss", "no_animal", 0.21)
    answers = tmp_path / "a.json"
    answers.write_text(json.dumps([{"key": "miss", "saw": "winston",
                                    "notes": "asleep on the dog bed, clearly him"}]))
    assert run(cli.cmd_audit_record, tmp_path, dirs, file=str(answers),
               reviewer="claude-session", requeue=False) == 0
    out = capsys.readouterr().out
    assert "DISAGREE" in out and "missed sighting" in out
    assert (dirs.archive / sorted(p.name for p in dirs.archive.iterdir())[0] / "miss").is_dir()
    assert not list(dirs.pending.iterdir())          # not requeued without --requeue


def test_requeue_moves_the_miss_back_and_warns_about_stale_ingestion(tmp_path, dirs, capsys):
    archive(dirs, "miss", "no_animal", 0.21)
    answers = tmp_path / "a.json"
    answers.write_text(json.dumps([{"key": "miss", "saw": "winston", "notes": "him"}]))
    run(cli.cmd_audit_record, tmp_path, dirs, file=str(answers),
        reviewer="claude-session", requeue=True)
    out = capsys.readouterr().out
    assert (dirs.pending / "miss").is_dir()
    assert "backwards" in out                        # the honest caveat, not a silent replay


def test_audit_record_rejects_unknown_keys_and_labels(tmp_path, dirs, capsys):
    archive(dirs, "real", "winston", 0.5)
    answers = tmp_path / "a.json"
    answers.write_text(json.dumps([
        {"key": "ghost", "saw": "winston", "notes": "n"},
        {"key": "real", "saw": "probably", "notes": "n"},
    ]))
    run(cli.cmd_audit_record, tmp_path, dirs, file=str(answers),
        reviewer="s", requeue=False)
    cap = capsys.readouterr()
    assert "not an archived local decision" in cap.err and "must be one of" in cap.err
    assert Database(tmp_path / "t.db").local_audit_summary()["audited"] == 0


def test_summary_points_at_the_threshold_that_would_have_caught_the_miss(tmp_path, dirs, capsys):
    archive(dirs, "miss", "no_animal", 0.21)
    answers = tmp_path / "a.json"
    answers.write_text(json.dumps([{"key": "miss", "saw": "winston", "notes": "him"}]))
    run(cli.cmd_audit_record, tmp_path, dirs, file=str(answers), reviewer="s", requeue=False)
    capsys.readouterr()
    run(cli.cmd_audit_summary, tmp_path, dirs, days=14.0)
    out = capsys.readouterr().out
    assert "rescue 0.23" in out and "below 0.210" in out
    assert "calibrate_local" in out                  # measure before moving a threshold


def test_audit_lists_the_sample_without_recording_anything(tmp_path, dirs, capsys):
    archive(dirs, "a", "winston", 0.55)
    archive(dirs, "b", "no_animal", 0.08)
    run(cli.cmd_audit, tmp_path, dirs, sample=8, hours=24.0, seed=1, sheets=False, json=False)
    out = capsys.readouterr().out
    assert "a" in out and "b" in out and "not a hint to agree with" in out
    assert Database(tmp_path / "t.db").local_audit_summary()["audited"] == 0


# --------------------------------------------------------------------------- #
# owner ground truth
# --------------------------------------------------------------------------- #

def observation(db, prob=0.75, camera="doorbell"):
    from src.observation import Observation
    return db.insert_observation(Observation(
        camera_id=camera, timestamp=utcnow(), winston_probability=prob,
        vision_confidence=prob, vision_similarity=0.72, animal_present=True,
        size_appearance_compatible=True, frames_analyzed=4))


def test_confirmation_lifts_the_probability_above_the_strong_threshold(db):
    oid = observation(db, 0.75)
    r = db.apply_owner_confirmation(oid, "winston", "owner", "it is him")
    assert r["was"] == 0.75 and r["winston_probability"] == 0.95
    row = db._conn.execute("SELECT winston_probability FROM observations WHERE id=?", (oid,)).fetchone()
    assert row["winston_probability"] == 0.95          # > strong_threshold 0.90, confirms alone


def test_confirmation_preserves_what_it_overwrote(db):
    oid = observation(db, 0.75)
    db.apply_owner_confirmation(oid, "winston", "owner", "him")
    extra = json.loads(db._conn.execute(
        "SELECT extra FROM observations WHERE id=?", (oid,)).fetchone()["extra"])
    c = extra["owner_confirmation"]
    assert c["original_winston_probability"] == 0.75 and c["reviewer"] == "owner"
    assert c["notes"] == "him" and c["label"] == "winston"


def test_reconfirming_keeps_the_pre_confirmation_original(db):
    """Otherwise the second confirmation records 0.95 as the model's own answer."""
    oid = observation(db, 0.75)
    db.apply_owner_confirmation(oid, "winston", "owner", "him")
    db.apply_owner_confirmation(oid, "winston", "owner", "still him")
    extra = json.loads(db._conn.execute(
        "SELECT extra FROM observations WHERE id=?", (oid,)).fetchone()["extra"])
    assert extra["owner_confirmation"]["original_winston_probability"] == 0.75


def test_confirming_not_winston_zeroes_it_out(db):
    oid = observation(db, 0.82)
    db.apply_owner_confirmation(oid, "not_winston", "owner", "that is the raccoon")
    row = db._conn.execute("SELECT winston_probability FROM observations WHERE id=?", (oid,)).fetchone()
    assert row["winston_probability"] == 0.0           # below threshold: the tracker ignores it


def test_confirmation_writes_a_matching_review_row(db):
    oid = observation(db)
    db.apply_owner_confirmation(oid, "winston", "owner", "him")
    reviews = db.list_observation_reviews(oid)
    assert len(reviews) == 1 and reviews[0]["label"] == "winston"


def test_confirmation_requires_provenance_and_a_real_observation(db):
    oid = observation(db)
    for bad in ({"reviewer": " ", "notes": "n"}, {"reviewer": "o", "notes": "  "}):
        with pytest.raises(ValueError):
            db.apply_owner_confirmation(oid, "winston", **bad)
    with pytest.raises(ValueError):
        db.apply_owner_confirmation(oid, "maybe", "owner", "n")
    with pytest.raises(KeyError):
        db.apply_owner_confirmation(9999, "winston", "owner", "n")
