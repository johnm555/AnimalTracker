"""Local animal gate (P4-12): one-sided by construction, never drops a sighting."""


import pytest

from src.dog_detector import (
    Backend, Detection, DisabledBackend, DogGate, GateResult, GateSettings, make_backend,
)


class FakeBackend(Backend):
    """Returns a scripted detection list per call; a string schedules an exception."""

    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def available(self):
        return True

    def detect(self, image):
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, str):
            raise RuntimeError(item)
        return list(item)


DOG = [Detection("dog", 0.82, (0.1, 0.2, 0.3, 0.4))]
CAT = [Detection("cat", 0.77)]
FAINT = [Detection("dog", 0.2)]


def gate(script, **kw):
    return DogGate(GateSettings(**kw), FakeBackend(script))


def test_any_frame_with_an_animal_makes_the_event_present():
    g = gate([[], [], DOG, []])
    v = g.detect_frames([b"a", b"b", b"c", b"d"])
    assert v.result is GateResult.PRESENT
    assert v.frames_with_animal == 1 and v.best.label == "dog"
    assert "dog 0.82 in 1/4 frame(s)" in v.reason()


def test_a_cat_is_an_animal_not_an_identity_call():
    v = gate([CAT]).detect_frames([b"a"])
    assert v.result is GateResult.PRESENT and v.best.label == "cat"


def test_absent_does_not_skip_by_default():
    """The measured false-negative rate makes ABSENT unsafe as a drop signal."""
    g = gate([[], [], [], []])
    v = g.detect_frames([b"a", b"b", b"c", b"d"])
    assert v.result is GateResult.ABSENT
    assert v.skippable is False          # <- the important one
    assert g.events_skipped == 0
    assert "no_animal_detected" in v.reason()


def test_absent_skips_only_when_explicitly_enabled():
    g = gate([[], []], skip_on_absent=True)
    v = g.detect_frames([b"a", b"b"])
    assert v.result is GateResult.ABSENT and v.skippable is True
    assert g.events_skipped == 1


def test_backend_error_is_unknown_never_absent():
    g = gate(["Vision exploded"], skip_on_absent=True)
    v = g.detect_frames([b"a", b"b"])
    assert v.result is GateResult.UNKNOWN and v.skippable is False
    assert "Vision exploded" in v.frames[0].error
    assert g.events_skipped == 0


def test_one_bad_frame_cannot_turn_a_sighting_into_a_skip():
    g = gate([DOG, "decode failure"], skip_on_absent=True)
    v = g.detect_frames([b"a", b"b"])
    assert v.result is GateResult.PRESENT and v.skippable is False


def test_faint_detection_blocks_absent():
    """Something animal-shaped but under threshold is uncertainty, not absence."""
    g = gate([FAINT, []], skip_on_absent=True)
    v = g.detect_frames([b"a", b"b"])
    assert v.frames[0].result is GateResult.UNKNOWN
    assert v.result is GateResult.UNKNOWN and v.skippable is False


def test_no_frames_is_unknown():
    assert gate([DOG], skip_on_absent=True).detect_frames([]).result is GateResult.UNKNOWN


def test_disabled_gate_is_a_noop():
    g = DogGate(GateSettings(enabled=False))
    assert isinstance(g.backend, DisabledBackend) and not g.enabled
    v = g.detect_frames([b"a"])
    assert v.result is GateResult.UNKNOWN and v.skippable is False


def test_settings_from_yaml_and_backend_selection():
    s = GateSettings.from_settings({"detector": {"gate": {"backend": "yolo", "min_confidence": 0.5,
                                                          "skip_on_absent": True}}})
    assert s.backend == "yolo" and s.min_confidence == 0.5 and s.skip_on_absent is True
    assert GateSettings.from_settings({}).backend == "apple_vision"
    assert GateSettings.from_settings({}).skip_on_absent is False   # safe default
    assert isinstance(make_backend(GateSettings(enabled=False)), DisabledBackend)
    with pytest.raises(ValueError):
        make_backend(GateSettings(backend="nope"))


def test_stats_and_serialization():
    g = gate([DOG, []])
    v = g.detect_frames([b"a", b"b"])
    d = v.to_dict()
    assert d["result"] == "present" and d["backend"] == "fake" and len(d["frames"]) == 2
    assert d["best"]["label"] == "dog" and d["best"]["box"] == [0.1, 0.2, 0.3, 0.4]
    assert g.stats()["frames_seen"] == 2 and g.stats()["skip_on_absent"] is False


# --------------------------------------------------------------------------- #
# Apple Vision backend, when this host has it
# --------------------------------------------------------------------------- #

vision = pytest.importorskip("Vision", reason="pyobjc-framework-Vision not installed")


def test_apple_vision_backend_runs_on_a_real_image(tmp_path):
    import cv2
    import numpy as np

    from src.dog_detector import AppleVisionBackend

    backend = AppleVisionBackend()
    if not backend.available():
        pytest.skip("Vision framework unavailable")
    path = tmp_path / "blank.jpg"
    cv2.imwrite(str(path), np.full((240, 320, 3), 128, dtype=np.uint8))
    # A flat grey frame has no animal: real call, real framework, empty result.
    assert backend.detect(path) == []
    assert backend.detect(path.read_bytes()) == []
    g = DogGate(GateSettings(backend="apple_vision"), backend)
    assert g.detect_frames([path]).result is GateResult.ABSENT
