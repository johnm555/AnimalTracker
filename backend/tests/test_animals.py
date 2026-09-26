"""Visiting animals (P4-30): a record of its own, kept away from the tracker."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from src.animals import KNOWN_SPECIES, UNKNOWN, AnimalSighting, normalize_species, summarize
from src.db import Database
from src.observation import Observation, utcnow

import animals as cli

UTC = timezone.utc


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


def sighting(species="raccoon", camera="side-deck", hour=3, **kw):
    kw.setdefault("source", "session")
    return AnimalSighting(camera_id=camera, timestamp=datetime(2026, 9, 23, hour, 0, tzinfo=UTC),
                          species=species, **kw)


# --------------------------------------------------------------------------- #
# species normalisation — fold wording, never discard a sighting
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("Raccoon", "raccoon"), ("racoon", "raccoon"), ("  RACCOONS ", "raccoon"),
    ("possum", "opossum"), ("other dog", "dog-other"), ("Wild Turkey", "turkey"),
    ("", UNKNOWN), (None, UNKNOWN), ("unsure", UNKNOWN),
])
def test_normalize_species_folds_common_wording(raw, expected):
    assert normalize_species(raw) == expected


def test_an_unexpected_species_keeps_its_name_rather_than_becoming_unknown():
    """Refusing a surprise visitor would discard the most interesting sighting."""
    assert normalize_species("Mountain Lion") == "mountain-lion"
    assert normalize_species("mountain-lion") not in KNOWN_SPECIES   # still recorded


def test_camera_id_is_normalized_like_everywhere_else():
    assert sighting(camera="Side Deck").camera_id == "side-deck"


def test_source_must_be_a_person_not_a_model():
    for ok in ("session", "owner", "backfill"):
        assert sighting(source=ok).source == ok
    with pytest.raises(ValueError):
        sighting(source="dinov2")
    with pytest.raises(ValueError):
        sighting(source="gate")


def test_confidence_is_bounded():
    assert sighting(confidence=0.5).confidence == 0.5
    with pytest.raises(ValueError):
        sighting(confidence=1.4)


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

def test_record_and_list(db):
    db.record_animal_sighting(sighting("Racoon", hour=3))
    db.record_animal_sighting(sighting("cat", camera="deck-stairs", hour=5))
    rows = db.list_animal_sightings()
    assert [r["species"] for r in rows] == ["cat", "raccoon"]        # newest first
    assert rows[0]["camera_id"] == "deck-stairs"


def test_filter_by_species_and_window(db):
    db.record_animal_sighting(sighting("raccoon", hour=3))
    db.record_animal_sighting(sighting("deer", hour=9))
    assert len(db.list_animal_sightings(species="raccoon")) == 1
    since = datetime(2026, 9, 23, 6, tzinfo=UTC)
    assert [r["species"] for r in db.list_animal_sightings(since=since)] == ["deer"]


def test_one_sighting_per_observation_relabelling_updates(db):
    oid = db.insert_observation(Observation(camera_id="side-deck", timestamp=utcnow(),
                                            winston_probability=0.02, animal_present=True))
    db.record_animal_sighting(sighting("cat", observation_id=oid))
    db.record_animal_sighting(sighting("raccoon", observation_id=oid, notes="looked again"))
    rows = db.list_animal_sightings()
    assert len(rows) == 1 and rows[0]["species"] == "raccoon"


def test_sightings_without_an_observation_are_not_deduplicated(db):
    """Two raccoons on two nights are two sightings, not one."""
    db.record_animal_sighting(sighting("raccoon", hour=3))
    db.record_animal_sighting(sighting("raccoon", hour=4))
    assert len(db.list_animal_sightings()) == 2


# --------------------------------------------------------------------------- #
# the labelling queue
# --------------------------------------------------------------------------- #

def test_candidates_are_animals_that_were_not_winston(db):
    winston = db.insert_observation(Observation(camera_id="side-deck", timestamp=utcnow(),
                                                winston_probability=0.91, animal_present=True))
    visitor = db.insert_observation(Observation(camera_id="side-deck", timestamp=utcnow(),
                                                winston_probability=0.02, animal_present=True))
    empty = db.insert_observation(Observation(camera_id="side-deck", timestamp=utcnow(),
                                              winston_probability=0.0, animal_present=False))
    ids = [c["observation_id"] for c in db.unlabelled_animal_observations(0.70)]
    assert visitor in ids
    assert winston not in ids and empty not in ids


def test_labelled_observations_leave_the_queue(db):
    oid = db.insert_observation(Observation(camera_id="side-deck", timestamp=utcnow(),
                                            winston_probability=0.02, animal_present=True))
    assert [c["observation_id"] for c in db.unlabelled_animal_observations(0.70)] == [oid]
    db.record_animal_sighting(sighting("raccoon", observation_id=oid))
    assert db.unlabelled_animal_observations(0.70) == []


def test_candidates_carry_the_reviewers_not_winston_evidence(db):
    db.insert_observation(Observation(
        camera_id="side-deck", timestamp=utcnow(), winston_probability=0.03, animal_present=True,
        extra={"mismatched_features": ["cat-sized", "cat body shape"]}))
    c = db.unlabelled_animal_observations(0.70)[0]
    assert c["mismatched_features"] == ["cat-sized", "cat body shape"]


def test_candidates_never_guess_a_species(db):
    db.insert_observation(Observation(
        camera_id="side-deck", timestamp=utcnow(), winston_probability=0.03, animal_present=True,
        extra={"mismatched_features": ["cat-sized"]}))
    assert "species" not in db.unlabelled_animal_observations(0.70)[0]


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #

def test_summary_groups_by_species_camera_and_hour():
    rows = [{"species": "raccoon", "camera_id": "side-deck", "timestamp": "2026-09-23T03:10:00+00:00"},
            {"species": "raccoon", "camera_id": "deck-stairs", "timestamp": "2026-09-23T03:40:00+00:00"},
            {"species": "cat", "camera_id": "side-deck", "timestamp": "2026-09-22T19:00:00+00:00"}]
    s = summarize(rows)
    assert s["sightings"] == 3
    assert s["species"]["raccoon"]["sightings"] == 2
    assert s["species"]["raccoon"]["cameras"] == ["deck-stairs", "side-deck"]
    assert s["species"]["raccoon"]["first"].startswith("2026-09-23T03:10")
    assert s["by_camera"] == {"side-deck": 2, "deck-stairs": 1}
    assert s["by_hour_utc"] == {3: 2, 19: 1}


def test_summary_says_it_counts_sightings_not_animals():
    """Six events in ten minutes is probably one raccoon; the report must not imply six."""
    assert "not individual animals" in summarize([])["note"]


def test_summary_of_nothing_is_empty_not_an_error():
    s = summarize([])
    assert s["sightings"] == 0 and s["species"] == {}


# --------------------------------------------------------------------------- #
# the tracker must not be able to see any of this
# --------------------------------------------------------------------------- #

def test_recording_a_visitor_leaves_winstons_location_untouched(db, topology):
    from src.state_machine import LocationTracker, TrackerConfig
    tracker = LocationTracker(topology, TrackerConfig())
    before = tracker.current_state()
    db.record_animal_sighting(sighting("raccoon", camera="kitchen-door"))
    assert tracker.current_state() == before
    assert tracker.current_state().kind == "unknown"        # still nothing has seen Winston


def test_a_visitor_sighting_creates_no_observation_and_no_transition(db):
    db.record_animal_sighting(sighting("raccoon"))
    assert db._conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0] == 0


def test_labelling_does_not_alter_the_observations_own_verdict(db):
    oid = db.insert_observation(Observation(camera_id="side-deck", timestamp=utcnow(),
                                            winston_probability=0.02, animal_present=True))
    db.record_animal_sighting(sighting("raccoon", observation_id=oid))
    row = db._conn.execute("SELECT winston_probability, animal_present FROM observations WHERE id=?",
                           (oid,)).fetchone()
    assert row["winston_probability"] == 0.02 and row["animal_present"] == 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def settings_for(tmp_path):
    return {"database": {"path": str(tmp_path / "t.db")}}


def run(fn, tmp_path, **kw):
    import argparse
    return fn(argparse.Namespace(**kw), settings_for(tmp_path))


def test_cli_label_refuses_an_observation_with_no_animal(tmp_path, capsys):
    db = Database(tmp_path / "t.db")
    oid = db.insert_observation(Observation(camera_id="side-deck", timestamp=utcnow(),
                                            winston_probability=0.0, animal_present=False))
    with pytest.raises(SystemExit) as e:
        run(cli.cmd_label, tmp_path, observation_id=oid, species="raccoon", source="session",
            confidence=None, notes="")
    assert "no animal" in str(e.value)


def test_cli_label_refuses_an_unknown_observation(tmp_path):
    Database(tmp_path / "t.db")
    with pytest.raises(SystemExit):
        run(cli.cmd_label, tmp_path, observation_id=4242, species="raccoon", source="session",
            confidence=None, notes="")


def test_cli_report_is_empty_without_inventing_anything(tmp_path, capsys):
    Database(tmp_path / "t.db")
    run(cli.cmd_report, tmp_path, days=7, species=None, json=False)
    out = capsys.readouterr().out
    assert "no visiting animals recorded" in out and "candidates" in out


def test_cli_round_trip_label_then_report(tmp_path, capsys):
    db = Database(tmp_path / "t.db")
    oid = db.insert_observation(Observation(camera_id="side-deck", timestamp=utcnow(),
                                            winston_probability=0.02, animal_present=True))
    run(cli.cmd_label, tmp_path, observation_id=oid, species="Racoon", source="session",
        confidence=0.9, notes="ringed tail")
    capsys.readouterr()
    run(cli.cmd_report, tmp_path, days=7, species=None, json=False)
    out = capsys.readouterr().out
    assert "raccoon" in out and "side-deck" in out
    assert "not individual animals" in out


def test_cli_warns_when_a_species_is_outside_the_known_list(tmp_path, capsys):
    db = Database(tmp_path / "t.db")
    oid = db.insert_observation(Observation(camera_id="side-deck", timestamp=utcnow(),
                                            winston_probability=0.02, animal_present=True))
    run(cli.cmd_label, tmp_path, observation_id=oid, species="mountain lion", source="owner",
        confidence=None, notes="")
    out = capsys.readouterr().out
    assert "mountain-lion" in out and "not in the known-species list" in out
