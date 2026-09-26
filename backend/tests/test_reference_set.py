"""Harvesting reference frames (P4-07): the guards that keep the result honest."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import build_reference_set as brs


# --------------------------------------------------------------------------- #
# the holdout split
# --------------------------------------------------------------------------- #

def test_holdout_is_stable_for_an_event():
    assert brs.is_holdout("7688021813082169916") == brs.is_holdout("7688021813082169916")


def test_holdout_is_roughly_the_declared_fraction():
    ids = [str(7688000000000000000 + i) for i in range(3000)]
    frac = sum(brs.is_holdout(i) for i in ids) / len(ids)
    assert abs(frac - brs.HOLDOUT) < 0.04


def test_holdout_splits_events_not_frames():
    """Four frames of one event must land on the same side, or the eval leaks."""
    eid = "7688021813082169916"
    assert len({brs.is_holdout(eid) for _ in range(4)}) == 1


# --------------------------------------------------------------------------- #
# eligibility: never harvest what DINOv2 chose
# --------------------------------------------------------------------------- #

class FakeRow(dict):
    def __getitem__(self, k):
        return dict.__getitem__(self, k)


def fake_db(rows):

    class Cursor:
        def __init__(self, rows): self._rows = rows
        def fetchall(self): return self._rows

    class C:
        def execute(self, q, args=()):
            return Cursor([FakeRow(r) for r in rows])

    class DB:
        _conn = C()
    return DB()


def test_local_pipeline_verdicts_are_excluded():
    rows = [
        {"id": 1, "camera_id": "deck-cam", "timestamp": "t", "winston_probability": 0.9,
         "extra": '{"detector": "local"}', "event_id": "e1", "device_id": "d"},
        {"id": 2, "camera_id": "deck-cam", "timestamp": "t", "winston_probability": 0.9,
         "extra": '{}', "event_id": "e2", "device_id": "d"},
    ]
    got = brs.eligible_events(fake_db(rows), 0.70)
    assert [g["event_id"] for g in got] == ["e2"]
    assert got[0]["source"] == "session"


def test_an_owner_confirmation_is_eligible_even_if_the_pipeline_first_judged_it():
    rows = [{"id": 1, "camera_id": "doorbell", "timestamp": "t", "winston_probability": 0.95,
             "extra": '{"detector": "local", "owner_confirmation": {"label": "winston"}}',
             "event_id": "e1", "device_id": "d"}]
    got = brs.eligible_events(fake_db(rows), 0.70)
    assert [g["source"] for g in got] == ["owner"]


# --------------------------------------------------------------------------- #
# lighting and distance
# --------------------------------------------------------------------------- #

def test_infrared_is_detected_from_absent_saturation():
    gray = np.full((80, 80, 3), 90, dtype=np.uint8)          # equal channels = no saturation
    assert brs.is_infrared(gray)
    colour = np.zeros((80, 80, 3), dtype=np.uint8)
    colour[:, :, 2] = 200                                     # strong red
    assert not brs.is_infrared(colour)


@pytest.mark.parametrize("area,bucket", [(0.20, "near"), (0.08, "mid"), (0.01, "far")])
def test_distance_buckets(area, bucket):
    assert brs.distance_bucket(area) == bucket


def test_quality_rewards_a_sharp_well_filled_frame():
    noise = np.random.default_rng(0).integers(0, 255, (120, 120, 3), dtype=np.uint8)
    flat = np.full((120, 120, 3), 120, dtype=np.uint8)
    big = brs.frame_quality(noise, (0.1, 0.1, 0.4, 0.4))
    small = brs.frame_quality(noise, (0.1, 0.1, 0.05, 0.05))
    assert big["score"] > small["score"]                      # closer is better
    assert brs.frame_quality(noise, (0.1, 0.1, 0.4, 0.4))["score"] > \
           brs.frame_quality(flat, (0.1, 0.1, 0.4, 0.4))["score"]   # sharper is better


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #

def cand(cam, light, ev, score=0.5, prob=0.8, holdout=False, localised=True):
    return {"camera_id": cam, "lighting": light, "event_id": ev, "score": score,
            "probability": prob, "holdout": holdout, "localised": localised,
            "distance": "mid", "path": f"/tmp/{ev}.jpg"}


def test_holdout_frames_never_enter_the_gallery():
    pool = [cand("deck-cam", "day", f"e{i}", holdout=(i % 2 == 0)) for i in range(10)]
    chosen = brs.select(pool, 10)
    assert all(not c["holdout"] for c in chosen)


def test_unlocalised_frames_are_not_gallery_material():
    pool = [cand("deck-cam", "day", "e1", localised=False),
            cand("deck-cam", "day", "e2", localised=True)]
    assert [c["event_id"] for c in brs.select(pool, 5)] == ["e2"]


def test_one_frame_per_event():
    """Four frames of a single moment is redundancy, not variety."""
    pool = [cand("deck-cam", "day", "same", score=0.9 - i * 0.01) for i in range(4)]
    assert len(brs.select(pool, 4)) == 1


def test_a_prolific_camera_cannot_crowd_out_the_others():
    pool = [cand("deck-cam", "day", f"s{i}", score=0.99) for i in range(40)]
    pool += [cand("garden-cam", "day", "d1", score=0.2)]
    pool += [cand("yard-cam", "day", "o1", score=0.2)]
    chosen = brs.select(pool, 6)
    cams = {c["camera_id"] for c in chosen}
    assert "garden-cam" in cams and "yard-cam" in cams


def test_each_cameras_most_confident_frame_is_included():
    pool = [cand("deck-cam", "day", "low", score=0.99, prob=0.72),
            cand("deck-cam", "day", "best", score=0.10, prob=0.96)]
    assert "best" in {c["event_id"] for c in brs.select(pool, 2)}


def test_ir_and_day_are_separate_buckets():
    pool = [cand("deck-cam", "day", f"d{i}", score=0.9) for i in range(10)]
    pool += [cand("deck-cam", "ir", "ir1", score=0.1)]
    assert "ir1" in {c["event_id"] for c in brs.select(pool, 4)}


def test_selection_stops_at_the_target():
    pool = [cand("deck-cam", "day", f"e{i}") for i in range(50)]
    assert len(brs.select(pool, 12)) == 12


def test_selection_of_an_empty_pool_is_empty_not_an_error():
    assert brs.select([], 10) == []


# --------------------------------------------------------------------------- #
# the comparison arithmetic
# --------------------------------------------------------------------------- #

def test_separation_auc_is_one_when_classes_do_not_overlap():
    s = brs._separation([0.8, 0.9], [0.1, 0.2])
    assert s["auc"] == 1.0


def test_separation_auc_is_half_for_identical_distributions():
    s = brs._separation([0.5, 0.5], [0.5, 0.5])
    assert s["auc"] == 0.5


def test_separation_is_unmoved_by_a_constant_shift():
    """The whole point: adding 0.1 to every score is not an improvement."""
    a = brs._separation([0.30, 0.40, 0.50], [0.20, 0.25, 0.35])
    b = brs._separation([0.40, 0.50, 0.60], [0.30, 0.35, 0.45])
    assert a["auc"] == b["auc"]
    assert b["pos_mean"] > a["pos_mean"]        # naive "scores went up" would claim a win


def test_sweep_reports_the_threshold_needed_for_a_low_false_positive_rate():
    s = brs._separation([0.45] * 10, [0.25] * 10)
    assert s["best_threshold_at_fpr<=0.10"]["threshold"] <= 0.45
    assert s["best_f1"]["recall"] == 1.0
