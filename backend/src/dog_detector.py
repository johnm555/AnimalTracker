"""Local dog-detection gate (P4-12): is there an animal in this frame at all?

This runs on the Mac Mini before any identity work. Most Ring events contain
a person, a car or nothing — reviewing them costs a Claude session round trip
(session mode) or a vision API call (api mode). The gate answers the cheap
question locally so only frames with an animal go forward.

It never answers "is this Winston". It answers "is there an animal here":

    animal found            -> PRESENT   (trusted; fast-path to identity)
    nothing found           -> ABSENT    (NOT trusted as evidence of absence)
    backend error/ambiguous -> UNKNOWN

**Measured on 575 archived events with recorded verdicts (2026-09-22):**

    session said "animal"    223 events -> gate PRESENT 133, ABSENT  90  (40% missed)
    session said "no animal" 352 events -> gate ABSENT  352, PRESENT   0  (0% wrong)

PRESENT is reliable: not one false positive in 352 animal-free events.
ABSENT is **not** reliable. 77 of the misses were confident Winston
sightings, and 55 of those are the `side-deck` camera — Winston curled on
his bed, seen through a high fisheye lens, often in night IR.
`VNRecognizeAnimalsRequest` finds standing and walking dogs; it does not
find a black dog lying still on a dark bed.

So ABSENT does **not** skip an event unless `skip_on_absent` is explicitly
turned on (default false). Dropping on ABSENT would have silently discarded
those 77 sightings, which is exactly the failure the project's one rule
forbids. What the gate is for, at this accuracy:

  * fast-path: PRESENT means an animal is really there, so identity work can
    run locally without a review round trip;
  * a crop: the bounding box focuses the DINOv2 embedding (P4-11) on the
    animal instead of the whole scene;
  * a hint: the verdict is written into the staging sidecar so a reviewing
    session knows where to look.

Backends (`detector.gate.backend` in settings.yaml):

    apple_vision  VNRecognizeAnimalsRequest through PyObjC. Neural Engine,
                  6-8 ms/frame after warm-up, no model download, no torch.
                  Labels are Dog and Cat — both count as an animal here,
                  because "a cat, not Winston" is an identity decision and
                  this layer does not make identity decisions.
    yolo          ultralytics YOLO (11n/8n). Optional; `pip install
                  ultralytics`. COCO classes 15 cat / 16 dog / 17 horse /
                  18 sheep / 19 cow, cropped to the animal classes.
    disabled      always UNKNOWN; the gate is a no-op.

`detect_frames()` is the event-level entry point used by the poller.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)

ANIMAL_LABELS = {"dog", "cat", "horse", "sheep", "cow", "bird", "bear"}
"""Anything four-legged counts. Narrowing this to dogs would make the gate an
identity filter, and a cat wrongly called "no animal" hides a real event."""


class GateResult(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass
class Detection:
    label: str
    confidence: float
    box: tuple[float, float, float, float] | None = None
    """(x, y, w, h) in normalized image coordinates, origin bottom-left (Vision convention)."""

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "confidence": round(self.confidence, 3),
                "box": [round(v, 4) for v in self.box] if self.box else None}


@dataclass
class FrameVerdict:
    result: GateResult
    detections: list[Detection] = field(default_factory=list)
    ms: float = 0.0
    error: str | None = None

    @property
    def best(self) -> Detection | None:
        return max(self.detections, key=lambda d: d.confidence, default=None)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"result": self.result.value, "ms": round(self.ms, 1),
                             "detections": [x.to_dict() for x in self.detections]}
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class EventVerdict:
    """The gate's answer for one Ring event (all of its frames)."""

    result: GateResult
    frames: list[FrameVerdict] = field(default_factory=list)
    backend: str = "disabled"
    skip_allowed: bool = False

    @property
    def total_ms(self) -> float:
        return sum(f.ms for f in self.frames)

    @property
    def best(self) -> Detection | None:
        return max((f.best for f in self.frames if f.best), key=lambda d: d.confidence, default=None)

    @property
    def frames_with_animal(self) -> int:
        return sum(1 for f in self.frames if f.result is GateResult.PRESENT)

    @property
    def skippable(self) -> bool:
        """Whether this verdict may drop the event. See GateSettings.skip_on_absent."""
        return self.result is GateResult.ABSENT and self.skip_allowed

    def reason(self) -> str:
        if self.result is GateResult.ABSENT:
            return (f"no_animal_detected ({self.backend}, {len(self.frames)} frame(s), "
                    f"{self.total_ms:.0f}ms)")
        b = self.best
        if self.result is GateResult.PRESENT and b:
            return f"{b.label} {b.confidence:.2f} in {self.frames_with_animal}/{len(self.frames)} frame(s)"
        errs = {f.error for f in self.frames if f.error}
        return "gate inconclusive" + (f": {'; '.join(sorted(errs))[:120]}" if errs else "")

    def to_dict(self) -> dict[str, Any]:
        return {"result": self.result.value, "backend": self.backend, "reason": self.reason(),
                "total_ms": round(self.total_ms, 1), "frames_with_animal": self.frames_with_animal,
                "best": self.best.to_dict() if self.best else None,
                "frames": [f.to_dict() for f in self.frames]}


@dataclass
class GateSettings:
    enabled: bool = True
    backend: str = "apple_vision"
    skip_on_absent: bool = False
    """Allow an ABSENT verdict to skip the event outright. Default false: the
    measured false-negative rate for `apple_vision` is 40% on real animal
    events (see the module docstring). Only turn this on for a backend whose
    ABSENT has been measured on `staging/archive/`."""
    min_confidence: float = 0.30
    """A detection below this is not evidence of an animal, but it does make the
    frame UNKNOWN rather than ABSENT: the backend saw *something*."""
    absent_confidence: float = 0.15
    """Any detection at or above this, however weak, blocks an ABSENT verdict."""
    model: str = "yolo11n.pt"
    """yolo backend only."""

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> "GateSettings":
        d = ((settings or {}).get("detector") or {}).get("gate") or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            backend=str(d.get("backend", "apple_vision")),
            skip_on_absent=bool(d.get("skip_on_absent", False)),
            min_confidence=float(d.get("min_confidence", 0.30)),
            absent_confidence=float(d.get("absent_confidence", 0.15)),
            model=str(d.get("model", "yolo11n.pt")),
        )


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

class Backend:
    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def detect(self, image: bytes | str | Path) -> list[Detection]:
        """Return every animal detection found. Raising means UNKNOWN, not ABSENT."""
        raise NotImplementedError


class AppleVisionBackend(Backend):
    """VNRecognizeAnimalsRequest via PyObjC. Runs on the Neural Engine."""

    name = "apple_vision"

    def __init__(self) -> None:
        self._vision: Any = None
        self._quartz: Any = None

    def _load(self) -> bool:
        if self._vision is not None:
            return True
        try:
            import Quartz  # type: ignore
            import Vision  # type: ignore
        except ImportError as e:  # pragma: no cover - depends on the host
            log.warning("apple_vision backend unavailable (%s); install pyobjc-framework-Vision", e)
            return False
        self._vision, self._quartz = Vision, Quartz
        return True

    def available(self) -> bool:
        return self._load()

    def _cg_image(self, image: bytes | str | Path) -> Any:
        Quartz = self._quartz
        if isinstance(image, (str, Path)):
            from Foundation import NSURL  # type: ignore

            src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(image)), None)
        else:
            from Foundation import NSData  # type: ignore

            src = Quartz.CGImageSourceCreateWithData(NSData.dataWithBytes_length_(image, len(image)), None)
        if src is None:
            raise ValueError("could not decode image")
        cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        if cg is None:
            raise ValueError("no image at index 0")
        return cg

    def detect(self, image: bytes | str | Path) -> list[Detection]:
        if not self._load():
            raise RuntimeError("pyobjc Vision not installed")
        Vision = self._vision
        cg = self._cg_image(image)
        request = Vision.VNRecognizeAnimalsRequest.alloc().init()
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        ok, err = handler.performRequests_error_([request], None)
        if not ok:
            raise RuntimeError(f"Vision request failed: {err}")
        out: list[Detection] = []
        for obs in request.results() or []:
            rect = obs.boundingBox()
            box = (float(rect.origin.x), float(rect.origin.y),
                   float(rect.size.width), float(rect.size.height))
            for label in obs.labels() or []:
                ident = str(label.identifier()).lower()
                if ident in ANIMAL_LABELS:
                    out.append(Detection(ident, float(label.confidence()), box))
        return out


class YoloBackend(Backend):
    """ultralytics YOLO. Optional; only loaded when selected."""

    name = "yolo"

    def __init__(self, model: str = "yolo11n.pt") -> None:
        self.model_name = model
        self._model: Any = None

    def _load(self) -> bool:
        if self._model is not None:
            return True
        try:
            from ultralytics import YOLO  # type: ignore
        except ImportError as e:  # pragma: no cover - optional dependency
            log.warning("yolo backend unavailable (%s); pip install ultralytics", e)
            return False
        self._model = YOLO(self.model_name)
        return True

    def available(self) -> bool:
        return self._load()

    def detect(self, image: bytes | str | Path) -> list[Detection]:
        if not self._load():
            raise RuntimeError("ultralytics not installed")
        import numpy as np

        src: Any = str(image) if isinstance(image, (str, Path)) else None
        if src is None:
            import cv2

            src = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
            if src is None:
                raise ValueError("could not decode image")
        results = self._model.predict(src, verbose=False)
        out: list[Detection] = []
        for r in results:
            names = r.names
            for b in r.boxes:
                label = str(names[int(b.cls)]).lower()
                if label in ANIMAL_LABELS:
                    x1, y1, x2, y2 = (float(v) for v in b.xyxyn[0])
                    # Convert to Vision's bottom-left origin so boxes are comparable.
                    out.append(Detection(label, float(b.conf), (x1, 1.0 - y2, x2 - x1, y2 - y1)))
        return out


class DisabledBackend(Backend):
    name = "disabled"

    def available(self) -> bool:
        return True

    def detect(self, image: bytes | str | Path) -> list[Detection]:  # noqa: ARG002
        raise RuntimeError("gate disabled")


def make_backend(settings: GateSettings) -> Backend:
    if not settings.enabled or settings.backend in ("disabled", "none", "off"):
        return DisabledBackend()
    if settings.backend == "apple_vision":
        return AppleVisionBackend()
    if settings.backend == "yolo":
        return YoloBackend(settings.model)
    raise ValueError(f"unknown gate backend {settings.backend!r} (apple_vision | yolo | disabled)")


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #

class DogGate:
    """Event-level gate. Construct once (backends are warm after the first call)."""

    def __init__(self, settings: GateSettings | None = None, backend: Backend | None = None) -> None:
        self.settings = settings or GateSettings()
        self.backend = backend or make_backend(self.settings)
        self.frames_seen = 0
        self.events_skipped = 0
        self.total_ms = 0.0

    @property
    def enabled(self) -> bool:
        return not isinstance(self.backend, DisabledBackend)

    def detect_frame(self, image: bytes | str | Path) -> FrameVerdict:
        t0 = time.perf_counter()
        try:
            detections = self.backend.detect(image)
        except Exception as e:
            # Unavailable backend, unreadable frame, Vision error: say UNKNOWN so
            # the event still gets looked at. Never turn an error into ABSENT.
            return FrameVerdict(GateResult.UNKNOWN, ms=(time.perf_counter() - t0) * 1000,
                                error=f"{type(e).__name__}: {e}"[:200])
        ms = (time.perf_counter() - t0) * 1000
        self.frames_seen += 1
        self.total_ms += ms
        strong = [d for d in detections if d.confidence >= self.settings.min_confidence]
        weak = [d for d in detections if d.confidence >= self.settings.absent_confidence]
        if strong:
            return FrameVerdict(GateResult.PRESENT, strong, ms)
        if weak:
            # Something animal-shaped, too faint to call. Not evidence of absence.
            return FrameVerdict(GateResult.UNKNOWN, weak, ms)
        return FrameVerdict(GateResult.ABSENT, [], ms)

    def detect_frames(self, images: Sequence[bytes | str | Path]) -> EventVerdict:
        """PRESENT if any frame has an animal; ABSENT only if every frame is clean."""
        skip = self.settings.skip_on_absent
        if not self.enabled or not images:
            return EventVerdict(GateResult.UNKNOWN, [], self.backend.name, skip)
        frames = [self.detect_frame(i) for i in images]
        verdict = EventVerdict(GateResult.UNKNOWN, frames, self.backend.name, skip)
        if any(f.result is GateResult.PRESENT for f in frames):
            verdict.result = GateResult.PRESENT
        elif all(f.result is GateResult.ABSENT for f in frames):
            verdict.result = GateResult.ABSENT
            if skip:
                self.events_skipped += 1
        return verdict

    def stats(self) -> dict[str, Any]:
        return {"backend": self.backend.name, "enabled": self.enabled,
                "skip_on_absent": self.settings.skip_on_absent,
                "frames_seen": self.frames_seen, "events_skipped": self.events_skipped,
                "avg_ms": round(self.total_ms / self.frames_seen, 1) if self.frames_seen else None}
