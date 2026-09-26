"""Winston detector: "is the animal in these frames Winston?"

This module asks Claude's vision API a *verification* question, not a
classification question. The model is shown enrolled reference photos of
Winston alongside the frames from a Ring event and asked whether they are the
same individual dog. It returns a structured JSON verdict, which we fuse with
a temporal prior from the tracker into a single Observation.

The detector NEVER returns a location. It returns an Observation for a camera.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .observation import Observation, parse_timestamp

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

SYSTEM_PROMPT = """You are a visual verification system for a single enrolled dog named Winston, \
a Great Dane. You will be shown reference photographs of Winston followed by frames captured by a \
home security camera.

Your only job is to determine whether the animal visible in the camera frames is Winston, the \
enrolled Great Dane shown in the reference images. Do not perform generic animal classification. \
"This is a dog" is not an answer; "this is Winston" or "this is not Winston" is.

Compare the animal in the frames against Winston's specific characteristics: coat color and \
pattern, markings, ear shape and set (cropped or natural), build, height relative to fixed objects, \
tail, collar or harness, gait. Use the reference images, not your general knowledge of Great Danes.

Be calibrated. If frames are dark, blurry, partial, or show only a silhouette, say so with lower \
confidence rather than guessing. If no animal is visible at all, report that. Other dogs, cats, \
deer, raccoons, and people are common in these frames; a large dark dog that is not Winston should \
score low. If the camera sees Winston through glass or a screen door, that still counts as Winston.

Respond only with the JSON object described by the schema."""

DETECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "animal_present": {
            "type": "boolean",
            "description": "True if any animal is visible in at least one frame.",
        },
        "is_winston_confidence": {
            "type": "number",
            "description": "0-1 confidence that the animal is Winston specifically (0 if no animal).",
        },
        "visual_similarity": {
            "type": "number",
            "description": "0-1 visual similarity between the animal and the reference images.",
        },
        "size_appearance_compatible": {
            "type": "boolean",
            "description": "True if the animal's apparent size and build are compatible with a Great Dane.",
        },
        "matched_features": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific features that match Winston (e.g. 'natural floppy ears', 'white chest blaze').",
        },
        "mismatched_features": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific features that do NOT match Winston.",
        },
        "frame_quality": {
            "type": "string",
            "enum": ["good", "partial", "poor"],
            "description": "Overall usability of the frames for identification.",
        },
        "reasoning": {
            "type": "string",
            "description": "One or two sentences explaining the verdict.",
        },
    },
    "required": [
        "animal_present",
        "is_winston_confidence",
        "visual_similarity",
        "size_appearance_compatible",
        "matched_features",
        "mismatched_features",
        "frame_quality",
        "reasoning",
    ],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ImageData:
    data: bytes
    media_type: str
    label: str = ""

    @property
    def base64(self) -> str:
        return base64.standard_b64encode(self.data).decode("ascii")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @classmethod
    def from_path(cls, path: str | Path, label: str | None = None) -> "ImageData":
        p = Path(path)
        media_type, _ = mimetypes.guess_type(p.name)
        if media_type == "image/jpg":
            media_type = "image/jpeg"
        if media_type not in SUPPORTED_IMAGE_TYPES:
            raise ValueError(f"unsupported image type for {p}: {media_type}")
        return cls(data=p.read_bytes(), media_type=media_type, label=label or p.stem)


def load_reference_images(directory: str | Path, limit: int | None = None) -> list[ImageData]:
    """Load Winston's enrolled photos (jpg/png/webp) from a directory, sorted by name."""
    d = Path(directory)
    if not d.is_dir():
        return []
    files = sorted(
        p for p in d.iterdir()
        if p.is_file() and not p.name.startswith(".")
        and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    )
    if limit is not None:
        files = files[:limit]
    return [ImageData.from_path(p) for p in files]


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #

def build_user_text(n_reference: int, n_frames: int, camera_id: str,
                    ring_classification: str | None = None) -> str:
    ring = f" Ring's own motion classifier labelled this event '{ring_classification}'; treat that as a weak hint only." \
        if ring_classification else ""
    return (
        f"The first {n_reference} image(s) are reference photographs of Winston, the enrolled Great Dane. "
        f"The following {n_frames} image(s) are consecutive frames from the '{camera_id}' security camera "
        f"during a single motion event.{ring}\n\n"
        "Determine whether the animal visible in these frames is Winston, the enrolled Great Dane shown "
        "in the reference images. Do not perform generic animal classification. "
        "Return the JSON verdict."
    )


def build_messages(
    reference_images: Sequence[ImageData],
    frames: Sequence[ImageData],
    camera_id: str,
    ring_classification: str | None = None,
) -> list[dict[str, Any]]:
    """Build the Messages API `messages` array: refs first, then frames, then the question.

    Reference images come first so they form a stable, cacheable prefix across calls.
    """
    if not reference_images:
        raise ValueError("at least one reference image of Winston is required")
    if not frames:
        raise ValueError("at least one camera frame is required")

    content: list[dict[str, Any]] = []
    for i, ref in enumerate(reference_images, 1):
        content.append({"type": "text", "text": f"Reference image {i} of Winston:"})
        content.append(_image_block(ref))
    # Cache breakpoint after the (stable) reference set.
    content[-1]["cache_control"] = {"type": "ephemeral"}

    for i, frame in enumerate(frames, 1):
        content.append({"type": "text", "text": f"Camera frame {i}:"})
        content.append(_image_block(frame))

    content.append({
        "type": "text",
        "text": build_user_text(len(reference_images), len(frames), camera_id, ring_classification),
    })
    return [{"role": "user", "content": content}]


def _image_block(img: ImageData) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": img.media_type, "data": img.base64},
    }


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #

@dataclass
class DetectionResult:
    animal_present: bool
    is_winston_confidence: float
    visual_similarity: float
    size_appearance_compatible: bool
    matched_features: list[str] = field(default_factory=list)
    mismatched_features: list[str] = field(default_factory=list)
    frame_quality: str = "good"
    reasoning: str = ""
    raw: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any], raw: str = "") -> "DetectionResult":
        return cls(
            animal_present=bool(d.get("animal_present", False)),
            is_winston_confidence=_clamp(d.get("is_winston_confidence", 0.0)),
            visual_similarity=_clamp(d.get("visual_similarity", 0.0)),
            size_appearance_compatible=bool(d.get("size_appearance_compatible", False)),
            matched_features=[str(x) for x in d.get("matched_features", []) or []],
            mismatched_features=[str(x) for x in d.get("mismatched_features", []) or []],
            frame_quality=str(d.get("frame_quality", "good")),
            reasoning=str(d.get("reasoning", "")),
            raw=raw,
        )


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_detection_response(text: str) -> DetectionResult:
    """Parse the model's JSON verdict. Tolerates code fences and leading prose."""
    candidate = text.strip()
    m = _FENCE_RE.search(candidate)
    if m:
        candidate = m.group(1)
    else:
        # Fall back to the first {...} span.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            candidate = candidate[start:end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise ValueError(f"detector returned non-JSON response: {e}: {text[:200]!r}") from e
    if not isinstance(data, dict):
        raise ValueError("detector response is not a JSON object")
    missing = [k for k in ("animal_present", "is_winston_confidence") if k not in data]
    if missing:
        raise ValueError(f"detector response missing keys: {missing}")
    return DetectionResult.from_dict(data, raw=text)


# --------------------------------------------------------------------------- #
# Signal fusion
# --------------------------------------------------------------------------- #

@dataclass
class FusionWeights:
    vision_confidence: float = 0.60
    visual_similarity: float = 0.25
    size_compatible: float = 0.15
    temporal_weight: float = 0.5
    """0 = ignore the temporal prior; 1 = fully multiply by it."""

    @classmethod
    def from_settings(cls, detector_settings: dict[str, Any] | None) -> "FusionWeights":
        s = detector_settings or {}
        w = s.get("weights") or {}
        return cls(
            vision_confidence=float(w.get("vision_confidence", 0.60)),
            visual_similarity=float(w.get("visual_similarity", 0.25)),
            size_compatible=float(w.get("size_compatible", 0.15)),
            temporal_weight=float(s.get("temporal_weight", 0.5)),
        )


def fuse_signals(result: DetectionResult, temporal_likelihood: float | None,
                 weights: FusionWeights | None = None) -> float:
    """Blend the model's signals and the tracker's temporal prior into one probability."""
    w = weights or FusionWeights()
    if not result.animal_present:
        return 0.0
    total = w.vision_confidence + w.visual_similarity + w.size_compatible
    visual = (
        w.vision_confidence * result.is_winston_confidence
        + w.visual_similarity * result.visual_similarity
        + w.size_compatible * (1.0 if result.size_appearance_compatible else 0.0)
    ) / total
    if result.frame_quality == "poor":
        visual *= 0.8
    if temporal_likelihood is None:
        return _clamp(visual)
    # Interpolate between "ignore prior" (1.0) and "fully apply prior".
    factor = (1.0 - w.temporal_weight) + w.temporal_weight * temporal_likelihood
    return _clamp(visual * factor)


# --------------------------------------------------------------------------- #
# Detector
# --------------------------------------------------------------------------- #

class _MessagesClient(Protocol):
    """The slice of anthropic.Anthropic we use; makes the detector easy to fake in tests."""

    def create(self, **kwargs: Any) -> Any: ...


class WinstonDetector:
    def __init__(
        self,
        reference_images: Sequence[ImageData] | None = None,
        reference_dir: str | Path | None = None,
        max_reference_images: int = 6,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
        weights: FusionWeights | None = None,
        client: Any | None = None,
        temporal_prior: Callable[[str, datetime], float] | None = None,
    ) -> None:
        if reference_images is None and reference_dir is not None:
            reference_images = load_reference_images(reference_dir, limit=max_reference_images)
        self.reference_images = list(reference_images or [])
        self.model = model
        self.max_tokens = max_tokens
        self.weights = weights or FusionWeights()
        self.temporal_prior = temporal_prior
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic  # imported lazily so tests don't need credentials
            self._client = anthropic.Anthropic()
        return self._client

    def build_request(self, frames: Sequence[ImageData], camera_id: str,
                      ring_classification: str | None = None) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "thinking": {"type": "adaptive"},
            "system": SYSTEM_PROMPT,
            "messages": build_messages(self.reference_images, frames, camera_id, ring_classification),
            "output_config": {"format": {"type": "json_schema", "schema": DETECTION_SCHEMA}},
        }

    def detect(self, frames: Sequence[ImageData], camera_id: str,
               ring_classification: str | None = None) -> DetectionResult:
        """Call the vision model and return its parsed verdict."""
        request = self.build_request(frames, camera_id, ring_classification)
        response = self.client.messages.create(**request)
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise RuntimeError(f"vision model refused the request: {details}")
        text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
        return parse_detection_response(text)

    def analyze(
        self,
        frames: Sequence[ImageData],
        camera_id: str,
        timestamp: datetime | str,
        ring_classification: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Observation:
        """Full pipeline for one camera event: model verdict + temporal prior -> Observation."""
        ts = parse_timestamp(timestamp)
        temporal = self.temporal_prior(camera_id, ts) if self.temporal_prior else None
        result = self.detect(frames, camera_id, ring_classification)
        return self.to_observation(result, camera_id, ts, len(frames), ring_classification, temporal, extra)

    def to_observation(
        self,
        result: DetectionResult,
        camera_id: str,
        timestamp: datetime,
        frames_analyzed: int,
        ring_classification: str | None = None,
        temporal_likelihood: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Observation:
        probability = fuse_signals(result, temporal_likelihood, self.weights)
        obs_extra = {
            "matched_features": result.matched_features,
            "mismatched_features": result.mismatched_features,
            "frame_quality": result.frame_quality,
            "reasoning": result.reasoning,
            "model": self.model,
        }
        if extra:
            obs_extra.update(extra)
        return Observation(
            camera_id=camera_id,
            timestamp=timestamp,
            winston_probability=probability,
            ring_classification=ring_classification,
            vision_similarity=result.visual_similarity,
            vision_confidence=result.is_winston_confidence,
            size_appearance_compatible=result.size_appearance_compatible,
            temporal_likelihood=temporal_likelihood,
            animal_present=result.animal_present,
            frames_analyzed=frames_analyzed,
            raw_response=result.raw or None,
            extra=obs_extra,
        )


def _clamp(x: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return lo
