"""Local identity pre-filter (P4-11): bands, cropping, and the safety asymmetry."""

import numpy as np
import pytest

from src.local_detector import Decision, FrameScore, LocalDetector, LocalSettings


class FakeDetector(LocalDetector):
    """Real banding logic, scripted scores — no torch, no model download."""

    def __init__(self, scores, settings=None, gallery=3):
        super().__init__(settings or LocalSettings(enabled=True))
        self._scores = list(scores)
        self._fake_gallery = gallery
        self.loaded = 0

    def load(self):
        self.loaded += 1
        return True

    @property
    def gallery_size(self):
        return self._fake_gallery

    def score_frame(self, image_path, box=None):
        s = self._scores[min(len(self._scores) - 1, self.frames_seen)]
        self.frames_seen += 1
        if isinstance(s, str):
            return FrameScore(0.0, False, 1.0, error=s)
        score, cropped = s if isinstance(s, tuple) else (s, box is not None)
        return FrameScore(score, cropped, 1.0, "01_standing_front.jpg")


def test_accept_needs_both_a_high_score_and_a_gate_crop():
    d = FakeDetector([(0.62, True)])
    v = d.evaluate(["f0.jpg"], [(0.1, 0.1, 0.2, 0.2)])
    assert v.decision is Decision.ACCEPT and v.score == pytest.approx(0.62)
    # Same score, no crop: a full-frame match mostly measures the background.
    d2 = FakeDetector([(0.62, False)])
    assert d2.evaluate(["f0.jpg"], [None]).decision is Decision.REVIEW


def test_full_frame_accept_allowed_only_when_explicitly_configured():
    d = FakeDetector([(0.62, False)], LocalSettings(enabled=True, require_gate_box=False))
    assert d.evaluate(["f0.jpg"], [None]).decision is Decision.ACCEPT


def test_best_frame_wins():
    d = FakeDetector([(0.2, True), (0.55, True), (0.3, True)])
    v = d.evaluate(["a", "b", "c"], [(0, 0, 1, 1)] * 3)
    assert v.decision is Decision.ACCEPT and v.score == pytest.approx(0.55)
    assert len(v.frames) == 3


def test_middle_band_goes_to_review():
    for score in (0.11, 0.3, 0.49):
        d = FakeDetector([(score, True)])
        assert d.evaluate(["f"], [(0, 0, 1, 1)]).decision is Decision.REVIEW, score


def test_skip_band_is_a_skip_not_a_negative_verdict():
    d = FakeDetector([(0.05, False)])
    v = d.evaluate(["f"], [None])
    assert v.decision is Decision.SKIP
    # The verdict carries a score and a reason, never a "not Winston" claim.
    assert "similarity" in v.reason() and "not Winston" not in v.reason()


def test_frame_errors_fall_back_to_review_never_skip():
    d = FakeDetector(["cv2 read failed"])
    v = d.evaluate(["f"], [None])
    assert v.decision is Decision.REVIEW and "cv2 read failed" in v.error


def test_disabled_and_empty_gallery_are_review():
    assert LocalDetector(LocalSettings(enabled=False)).evaluate(["f"]).decision is Decision.REVIEW
    d = FakeDetector([(0.9, True)], gallery=0)
    assert d.evaluate(["f"], [(0, 0, 1, 1)]).decision is Decision.REVIEW


def test_max_frames_caps_work():
    d = FakeDetector([(0.2, True)] * 10, LocalSettings(enabled=True, max_frames=2))
    assert len(d.evaluate(["a", "b", "c", "d"], [None] * 4).frames) == 2


def test_crop_converts_vision_coordinates_and_rejects_slivers():
    img = np.zeros((100, 200, 3), dtype=np.uint8)
    img[10:30, 40:80] = 255                      # a bright patch, top-left area
    # Vision box origin is bottom-left: y=0.7 h=0.2 -> rows 10..30 from the top.
    crop, cropped = LocalDetector.crop(img, (0.2, 0.7, 0.2, 0.2), padding=0.0)
    assert cropped and crop.shape[0] == 20 and crop.shape[1] == 40
    assert crop.mean() > 200                     # we cropped onto the patch
    whole, cropped = LocalDetector.crop(img, None)
    assert not cropped and whole.shape == img.shape
    _, cropped = LocalDetector.crop(img, (0.5, 0.5, 0.01, 0.01))
    assert not cropped                            # too small to be useful


def test_to_detection_result_states_calibrated_confidence_not_raw_similarity():
    d = FakeDetector([(0.58, True)])
    v = d.evaluate(["f"], [(0, 0, 1, 1)])
    r = d.to_detection_result(v)
    assert r.animal_present is True
    assert r.is_winston_confidence == 0.90        # band precision, not the cosine
    assert r.visual_similarity == pytest.approx(0.58)
    assert "DINOv2" in r.matched_features[0] and r.mismatched_features == []


def test_settings_defaults_are_the_calibrated_ones():
    s = LocalSettings.from_settings({})
    assert (s.accept_threshold, s.reject_threshold) == (0.50, 0.10)
    assert s.require_gate_box is True
    s2 = LocalSettings.from_settings({"detector": {"local": {"enabled": True, "accept_threshold": 0.7}}})
    assert s2.enabled is True and s2.accept_threshold == 0.7


def test_stats_counts_decisions():
    d = FakeDetector([(0.9, True), (0.01, True), (0.3, True)])
    for _ in range(3):
        d.evaluate(["f"], [(0, 0, 1, 1)])
    st = d.stats()
    assert st["decisions"] == {"accept": 1, "skip": 1, "review": 1}
    assert st["gallery"] == 3 and st["frames_seen"] == 3
