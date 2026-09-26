"""Combined local decision (P4-27): the bands, and the things it must never do."""

import pytest

from src.dog_detector import Detection, DogGate, GateSettings
from src.local_detector import LocalSettings
from src.local_pipeline import LocalPipeline, LocalPipelineSettings, Outcome
from tests.test_dog_detector import FakeBackend
from tests.test_local_detector import FakeDetector

DOG = [Detection("dog", 0.8, (0.1, 0.2, 0.3, 0.4))]


def build(gate_script, scores, **kw):
    gate = DogGate(GateSettings(), FakeBackend(gate_script))
    local = FakeDetector(scores, LocalSettings(enabled=True))
    return LocalPipeline(gate, local, LocalPipelineSettings(**kw))


def classify(gate_script, scores, frames=("f0.jpg",), **kw):
    return build(gate_script, scores, **kw).classify(list(frames))


# --------------------------------------------------------------------------- #
# gate PRESENT
# --------------------------------------------------------------------------- #

def test_animal_plus_strong_embedding_is_an_automatic_sighting():
    r = classify([DOG], [(0.42, True)])
    assert r.outcome is Outcome.WINSTON and r.score == pytest.approx(0.42)
    assert "false-positive rate 1.7%" in r.reason


def test_accept_threshold_is_respected():
    assert classify([DOG], [(0.36, True)]).outcome is Outcome.REVIEW
    assert classify([DOG], [(0.37, True)]).outcome is Outcome.WINSTON


def test_accept_needs_a_gate_crop_by_default():
    """A full-frame score mostly measures the background, so it can't create a sighting."""
    assert classify([DOG], [(0.9, False)]).outcome is Outcome.REVIEW
    assert classify([DOG], [(0.9, False)], require_gate_box=False).outcome is Outcome.WINSTON


def test_animal_with_a_weak_score_goes_to_review_not_to_not_winston():
    r = classify([DOG], [(0.05, True)])
    assert r.outcome is Outcome.REVIEW
    assert "not winston" not in r.reason.lower()


def test_not_winston_band_is_off_by_default_and_opt_in():
    assert LocalPipelineSettings().not_winston_threshold is None
    assert LocalPipelineSettings.from_settings({}).not_winston_threshold is None
    r = classify([DOG], [(0.05, True)], not_winston_threshold=0.10)
    assert r.outcome is Outcome.NOT_WINSTON


# --------------------------------------------------------------------------- #
# gate ABSENT — the 40% false-negative problem
# --------------------------------------------------------------------------- #

def test_clean_gate_and_low_score_is_the_only_automatic_skip():
    r = classify([[]], [(0.10, False)])
    assert r.outcome is Outcome.NO_ANIMAL and "no animal" in r.reason


def test_a_clean_gate_alone_never_skips_when_the_embedding_disagrees():
    """83 real Winston sightings in the archive had a clean gate. This is the rescue."""
    r = classify([[]], [(0.31, False)])
    assert r.outcome is Outcome.REVIEW and "misses 40%" in r.reason
    assert classify([[]], [(0.30, False)]).outcome is Outcome.NO_ANIMAL


def test_rescue_threshold_is_configurable():
    assert classify([[]], [(0.30, False)], rescue_threshold=0.40).outcome is Outcome.NO_ANIMAL
    assert classify([[]], [(0.30, False)], rescue_threshold=0.20).outcome is Outcome.REVIEW


# --------------------------------------------------------------------------- #
# failure modes — everything degrades to REVIEW, never to a skip or a sighting
# --------------------------------------------------------------------------- #

def test_gate_error_is_review():
    r = classify(["Vision exploded"], [(0.02, False)])
    assert r.outcome is Outcome.REVIEW and "inconclusive" in r.reason


def test_embedding_error_is_review_even_with_a_confident_gate():
    r = classify([DOG], ["cv2 failed"])
    assert r.outcome is Outcome.REVIEW and "embedding unavailable" in r.reason


def test_no_frames_is_review():
    assert classify([DOG], [(0.9, True)], frames=()).outcome is Outcome.REVIEW


def test_disabled_pipeline_sends_everything_to_review():
    assert classify([DOG], [(0.9, True)], enabled=False).outcome is Outcome.REVIEW


def test_exception_inside_a_model_is_review_not_a_crash():
    class Boom(FakeDetector):
        def evaluate(self, *a, **k):
            raise RuntimeError("MPS out of memory")

    gate = DogGate(GateSettings(), FakeBackend([DOG]))
    p = LocalPipeline(gate, Boom([(0.9, True)], LocalSettings(enabled=True)), LocalPipelineSettings())
    r = p.classify(["f0.jpg"])
    assert r.outcome is Outcome.REVIEW and "MPS out of memory" in r.reason


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #

def test_detection_result_carries_the_evidence_and_a_calibrated_confidence():
    p = build([DOG], [(0.44, True)])
    r = p.classify(["f0.jpg"])
    d = p.detection_result(r)
    assert d.animal_present is True
    assert d.is_winston_confidence == 0.90          # band precision, not the cosine
    assert d.visual_similarity == pytest.approx(0.44)
    assert any("DINOv2" in f for f in d.matched_features)
    assert any("Apple Vision" in f for f in d.matched_features)
    assert d.mismatched_features == []


def test_settings_defaults_are_the_measured_ones():
    s = LocalPipelineSettings.from_settings({})
    assert (s.accept_threshold, s.rescue_threshold) == (0.37, 0.31)
    assert s.require_gate_box is True and s.enabled is True
    s2 = LocalPipelineSettings.from_settings(
        {"detector": {"pipeline": {"accept_threshold": 0.5, "not_winston_threshold": 0.1}}})
    assert s2.accept_threshold == 0.5 and s2.not_winston_threshold == 0.1


def test_stats_track_the_outcome_mix():
    p = build([DOG, DOG, []], [(0.9, True), (0.05, True), (0.01, False)])
    for _ in range(3):
        p.classify(["f0.jpg"])
    assert p.stats()["outcomes"] == {"winston": 1, "review": 1, "no_animal": 1, "not_winston": 0}
    assert p.stats()["accept_threshold"] == 0.37


def test_result_serialises_for_the_sidecar():
    d = classify([DOG], [(0.42, True)]).to_dict()
    assert d["outcome"] == "winston" and d["score"] == 0.42
    assert d["gate"]["result"] == "present" and d["local"]["decision"] in ("accept", "review", "skip")
