"""Observation data model.

An Observation is the *only* thing the vision layer is allowed to produce:
"camera X saw something at time T that looks like Winston with probability P".
It never carries a location. Turning observations into a location is the job
of the deterministic tracker in state_machine.py.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str | datetime) -> datetime:
    """Parse an ISO-8601 string (or pass through a datetime) as an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class Observation:
    """A single camera event that has been analyzed by the Winston detector."""

    camera_id: str
    timestamp: datetime
    winston_probability: float
    """Fused probability in [0, 1] that the animal in the frames is Winston."""

    ring_classification: str | None = None
    """Ring's own coarse label for the event (animal / person / other_motion ...)."""

    vision_similarity: float | None = None
    """Model-reported visual similarity to the reference images, [0, 1]."""

    vision_confidence: float | None = None
    """Model-reported confidence that this is Winston specifically, [0, 1]."""

    size_appearance_compatible: bool | None = None
    """Is the animal's apparent size/build compatible with a Great Dane?"""

    temporal_likelihood: float | None = None
    """Prior plausibility of Winston being at this camera now, from the tracker."""

    animal_present: bool | None = None
    frames_analyzed: int = 0
    raw_response: str | None = None
    id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.timestamp = parse_timestamp(self.timestamp)
        self.winston_probability = _clamp(self.winston_probability)
        if self.vision_similarity is not None:
            self.vision_similarity = _clamp(self.vision_similarity)
        if self.vision_confidence is not None:
            self.vision_confidence = _clamp(self.vision_confidence)
        if self.temporal_likelihood is not None:
            self.temporal_likelihood = _clamp(self.temporal_likelihood)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Observation":
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))
