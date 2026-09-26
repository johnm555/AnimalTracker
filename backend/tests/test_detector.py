import base64
import json
from types import SimpleNamespace

import pytest

from src.state_machine import LocationTracker
from src.winston_detector import (
    DETECTION_SCHEMA, SYSTEM_PROMPT, DetectionResult, FusionWeights, ImageData, WinstonDetector,
    build_messages, build_user_text, fuse_signals, load_reference_images, parse_detection_response,
)
from tests.conftest import T0

# 1x1 PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def img(label="x", media="image/png") -> ImageData:
    return ImageData(data=PNG, media_type=media, label=label)


class FakeMessages:
    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self.text, self.stop_reason, self.calls = text, stop_reason, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason, stop_details=None,
            content=[SimpleNamespace(type="text", text=self.text)],
        )


class FakeClient:
    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self.messages = FakeMessages(text, stop_reason)


VERDICT = {
    "animal_present": True, "is_winston_confidence": 0.9, "visual_similarity": 0.8,
    "size_appearance_compatible": True, "matched_features": ["natural ears", "white chest"],
    "mismatched_features": [], "frame_quality": "good", "reasoning": "Matches Winston's markings.",
}


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #

def test_system_prompt_is_verification_not_classification():
    assert "Winston" in SYSTEM_PROMPT
    assert "Do not perform generic animal classification" in SYSTEM_PROMPT
    assert "Great Dane" in SYSTEM_PROMPT


def test_user_text_contains_required_instruction():
    text = build_user_text(2, 4, "backyard", ring_classification="animal")
    assert "Determine whether the animal visible in these frames is Winston, the enrolled Great Dane" in text
    assert "Do not perform generic animal classification" in text
    assert "'backyard'" in text and "2 image(s)" in text and "4 image(s)" in text
    assert "animal" in text  # ring hint mentioned as weak signal


def test_build_messages_orders_refs_then_frames_then_question():
    refs = [img("ref1"), img("ref2", "image/jpeg")]
    frames = [img("f1"), img("f2"), img("f3")]
    msgs = build_messages(refs, frames, "side-yard")
    assert len(msgs) == 1 and msgs[0]["role"] == "user"
    content = msgs[0]["content"]
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == 5
    assert images[1]["source"]["media_type"] == "image/jpeg"
    assert images[0]["source"]["data"] == base64.standard_b64encode(PNG).decode()
    labels = [b["text"] for b in content if b["type"] == "text"]
    assert labels[0].startswith("Reference image 1")
    assert labels[2].startswith("Camera frame 1")
    assert "Return the JSON verdict" in labels[-1]
    # cache breakpoint sits on the last reference image, before any frame
    idx_cached = [i for i, b in enumerate(content) if "cache_control" in b]
    idx_first_frame = next(i for i, b in enumerate(content) if b.get("text", "").startswith("Camera frame 1"))
    assert idx_cached == [idx_first_frame - 1]


def test_build_messages_requires_refs_and_frames():
    with pytest.raises(ValueError):
        build_messages([], [img()], "backyard")
    with pytest.raises(ValueError):
        build_messages([img()], [], "backyard")


def test_build_request_uses_structured_output_and_adaptive_thinking():
    d = WinstonDetector(reference_images=[img()], client=FakeClient(json.dumps(VERDICT)))
    req = d.build_request([img()], "front-door")
    assert req["model"] == "claude-opus-5"
    assert req["thinking"] == {"type": "adaptive"}
    assert req["system"] == SYSTEM_PROMPT
    assert req["output_config"]["format"]["schema"] is DETECTION_SCHEMA
    assert DETECTION_SCHEMA["additionalProperties"] is False
    assert set(DETECTION_SCHEMA["required"]) == set(DETECTION_SCHEMA["properties"])


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #

def test_parse_plain_json():
    r = parse_detection_response(json.dumps(VERDICT))
    assert r.animal_present is True
    assert r.is_winston_confidence == pytest.approx(0.9)
    assert r.matched_features == ["natural ears", "white chest"]
    assert r.raw == json.dumps(VERDICT)


def test_parse_tolerates_fences_and_prose():
    text = "Here is the verdict:\n```json\n" + json.dumps(VERDICT) + "\n```\nDone."
    assert parse_detection_response(text).visual_similarity == pytest.approx(0.8)
    text2 = "Sure. " + json.dumps(VERDICT)
    assert parse_detection_response(text2).animal_present is True


def test_parse_clamps_and_defaults():
    r = parse_detection_response(json.dumps({"animal_present": True, "is_winston_confidence": 1.7}))
    assert r.is_winston_confidence == 1.0
    assert r.visual_similarity == 0.0
    assert r.matched_features == [] and r.frame_quality == "good"


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_detection_response("I cannot tell.")
    with pytest.raises(ValueError):
        parse_detection_response(json.dumps({"foo": 1}))
    with pytest.raises(ValueError):
        parse_detection_response("[1, 2, 3]")


# --------------------------------------------------------------------------- #
# Signal fusion
# --------------------------------------------------------------------------- #

def test_fusion_no_animal_is_zero():
    r = DetectionResult(animal_present=False, is_winston_confidence=0.9, visual_similarity=0.9,
                        size_appearance_compatible=True)
    assert fuse_signals(r, 1.0) == 0.0


def test_fusion_weights_and_temporal_prior():
    w = FusionWeights(vision_confidence=0.6, visual_similarity=0.25, size_compatible=0.15, temporal_weight=0.5)
    r = DetectionResult(animal_present=True, is_winston_confidence=1.0, visual_similarity=1.0,
                        size_appearance_compatible=True)
    assert fuse_signals(r, None, w) == pytest.approx(1.0)
    # prior of 0 with temporal_weight 0.5 halves the score
    assert fuse_signals(r, 0.0, w) == pytest.approx(0.5)
    assert fuse_signals(r, 1.0, w) == pytest.approx(1.0)
    # size mismatch drops the 0.15 share
    r2 = DetectionResult(animal_present=True, is_winston_confidence=1.0, visual_similarity=1.0,
                         size_appearance_compatible=False)
    assert fuse_signals(r2, None, w) == pytest.approx(0.85)
    # poor frames are discounted
    r3 = DetectionResult(animal_present=True, is_winston_confidence=1.0, visual_similarity=1.0,
                         size_appearance_compatible=True, frame_quality="poor")
    assert fuse_signals(r3, None, w) == pytest.approx(0.8)


def test_fusion_weights_from_settings():
    w = FusionWeights.from_settings({"weights": {"vision_confidence": 0.5, "visual_similarity": 0.3,
                                                 "size_compatible": 0.2}, "temporal_weight": 0.0})
    assert (w.vision_confidence, w.visual_similarity, w.size_compatible, w.temporal_weight) == (0.5, 0.3, 0.2, 0.0)


# --------------------------------------------------------------------------- #
# End to end (fake client)
# --------------------------------------------------------------------------- #

def test_analyze_returns_observation_not_location(topology):
    tracker = LocationTracker(topology)
    client = FakeClient(json.dumps(VERDICT))
    d = WinstonDetector(reference_images=[img("ref")], client=client,
                        temporal_prior=tracker.temporal_likelihood,
                        weights=FusionWeights(temporal_weight=0.0))
    obs = d.analyze([img("f1"), img("f2")], "backyard", T0, ring_classification="animal")
    assert obs.camera_id == "backyard"
    assert obs.frames_analyzed == 2
    assert obs.ring_classification == "animal"
    assert obs.vision_confidence == pytest.approx(0.9)
    assert obs.vision_similarity == pytest.approx(0.8)
    assert obs.size_appearance_compatible is True
    assert obs.temporal_likelihood == 1.0  # tracker had no prior: neutral, not a penalty
    expected = (0.6 * 0.9 + 0.25 * 0.8 + 0.15 * 1.0)
    assert obs.winston_probability == pytest.approx(expected)
    assert obs.extra["reasoning"] == "Matches Winston's markings."
    assert not hasattr(obs, "zone")  # detector never emits a location
    assert len(client.messages.calls) == 1


def test_detect_raises_on_refusal():
    d = WinstonDetector(reference_images=[img()], client=FakeClient("", stop_reason="refusal"))
    with pytest.raises(RuntimeError, match="refused"):
        d.detect([img()], "backyard")


def test_load_reference_images(tmp_path):
    (tmp_path / "b.png").write_bytes(PNG)
    (tmp_path / "a.png").write_bytes(PNG)
    (tmp_path / ".DS_Store").write_bytes(b"junk")
    (tmp_path / "notes.txt").write_text("not an image")
    refs = load_reference_images(tmp_path)
    assert [r.label for r in refs] == ["a", "b"]
    assert all(r.media_type == "image/png" for r in refs)
    assert load_reference_images(tmp_path, limit=1)[0].label == "a"
    assert load_reference_images(tmp_path / "missing") == []
