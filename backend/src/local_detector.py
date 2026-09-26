"""Local identity pre-filter (P4-11): DINOv2 embedding similarity to the gallery.

The question this layer answers is the same one the detector prompt asks —
"is the animal in this frame Winston, the enrolled dog?" — but it answers it
with a self-supervised image embedding instead of a language model, on the
Mac Mini, in about 8 ms a frame.

    reference_images/*.jpg  --DINOv2 ViT-S-->  gallery (N x 384, L2-normalised)
    frame (cropped to the P4-12 box when there is one)
                            --DINOv2 ViT-S-->  vector
    score = max cosine similarity against the gallery

Decision bands (`detector.local.*` in settings.yaml), applied to the event's
best frame:

    score >= accept_threshold   ACCEPT   local observation, no review needed
    score <= reject_threshold   SKIP     recorded as skipped, NEVER "not Winston"
    otherwise                   REVIEW   staged for a session exactly as before

The asymmetry is deliberate and is the whole reason this is safe to run
unattended. ACCEPT creates a sighting, so it has to be precise. SKIP only
declines to look further, so the cost of being wrong is a missed event — the
same cost the system already pays when a camera doesn't fire. Neither band
ever writes "this was not Winston"; a low score is an absence of evidence,
which `skip` says honestly and a low-probability observation would not.

Thresholds are not guesses. `scripts/calibrate_local.py` replays
`staging/archive/` against the verdicts already in the database and prints
the precision of each band; the defaults here are what that script measured.
Re-run it whenever the reference set changes.

Cropping matters more than anything else here. A full 1024 px frame of the
deck embeds mostly as "the deck", whether or not a dog is on it, so
uncropped scores carry much less signal than cropped ones. When the P4-12
gate supplies a box, this module embeds the animal; when it does not, the
frame is scored whole and will usually land in REVIEW, which is the correct
outcome for a frame nothing has localised.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)

MODEL_NAME = "dinov2_vits14"
EMBED_DIM = 384
CACHE_VERSION = 1


class Decision(str, Enum):
    ACCEPT = "accept"
    REVIEW = "review"
    SKIP = "skip"


@dataclass
class LocalSettings:
    enabled: bool = False
    """Off until calibrated on this property's own archive."""
    model: str = MODEL_NAME
    device: str = "auto"
    """auto | mps | cpu"""
    accept_threshold: float = 0.50
    """Calibrated 2026-09-22 on 575 archived events: every one of the 45 events
    at or above 0.50 with a gate box was Winston (100% precision). 0.45 admits
    one more event whose session confidence was 0.68 — probably Winston too,
    but the margin is not worth a fabricated sighting."""
    reject_threshold: float = 0.10
    """Deliberately tiny. Measured loss at each level: 0.10 -> 0 sightings lost
    (7 events skipped), 0.15 -> 1 lost, 0.20 -> 10 lost, 0.30 -> 40 lost. A
    full-frame embedding scores the scene more than the dog, so rejection has
    almost no headroom; anything above 0.10 trades real sightings for review
    volume and must not be set without re-running the calibration."""
    crop_padding: float = 0.15
    """Fraction of the box size added on each side before cropping."""
    require_gate_box: bool = True
    """Only ACCEPT when the P4-12 gate localised an animal. A full-frame score
    is dominated by the background, so it must not create a sighting on its own."""
    max_frames: int = 4

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> "LocalSettings":
        d = ((settings or {}).get("detector") or {}).get("local") or {}
        return cls(
            enabled=bool(d.get("enabled", False)),
            model=str(d.get("model", MODEL_NAME)),
            device=str(d.get("device", "auto")),
            accept_threshold=float(d.get("accept_threshold", 0.50)),
            reject_threshold=float(d.get("reject_threshold", 0.10)),
            crop_padding=float(d.get("crop_padding", 0.15)),
            require_gate_box=bool(d.get("require_gate_box", True)),
            max_frames=int(d.get("max_frames", 4)),
        )


@dataclass
class FrameScore:
    score: float
    cropped: bool
    ms: float = 0.0
    best_reference: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {"score": round(self.score, 4), "cropped": self.cropped, "ms": round(self.ms, 1)}
        if self.best_reference:
            d["best_reference"] = self.best_reference
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class LocalVerdict:
    decision: Decision
    score: float = 0.0
    frames: list[FrameScore] = field(default_factory=list)
    gallery_size: int = 0
    cropped_any: bool = False
    error: str | None = None

    @property
    def total_ms(self) -> float:
        return sum(f.ms for f in self.frames)

    def reason(self) -> str:
        if self.error:
            return f"local detector unavailable: {self.error}"
        crop = "cropped" if self.cropped_any else "full-frame"
        return (f"similarity {self.score:.3f} ({crop}, best of {len(self.frames)} frame(s), "
                f"gallery {self.gallery_size}, {self.total_ms:.0f}ms)")

    def to_dict(self) -> dict[str, Any]:
        return {"decision": self.decision.value, "score": round(self.score, 4),
                "reason": self.reason(), "cropped_any": self.cropped_any,
                "gallery_size": self.gallery_size, "total_ms": round(self.total_ms, 1),
                "frames": [f.to_dict() for f in self.frames]}


class LocalDetector:
    """DINOv2 similarity against the enrolled reference photos.

    Construct once; the model loads lazily on first use and the gallery is
    cached on disk so restarts do not re-embed the reference set.
    """

    def __init__(self, settings: LocalSettings | None = None, reference_dir: Path | None = None,
                 cache_path: Path | None = None) -> None:
        self.settings = settings or LocalSettings()
        self.reference_dir = Path(reference_dir) if reference_dir else None
        self.cache_path = Path(cache_path) if cache_path else None
        self._model: Any = None
        self._device: str | None = None
        self._gallery: Any = None
        self._gallery_names: list[str] = []
        self._load_error: str | None = None
        self.frames_seen = 0
        self.total_ms = 0.0
        self.decisions: dict[str, int] = {d.value: 0 for d in Decision}

    # -- model -------------------------------------------------------------

    def _pick_device(self) -> str:
        import torch

        if self.settings.device != "auto":
            return self.settings.device
        return "mps" if torch.backends.mps.is_available() else "cpu"

    def load(self) -> bool:
        """Load the model and gallery. Returns False (and records why) on failure."""
        if self._model is not None:
            return True
        if self._load_error is not None:
            return False
        try:
            import torch

            self._device = self._pick_device()
            model = torch.hub.load("facebookresearch/dinov2", self.settings.model,
                                   pretrained=True, trust_repo=True, verbose=False)
            model.eval().to(self._device)
            self._model = model
            self._build_gallery()
        except Exception as e:  # torch missing, no network for the weights, bad ref dir
            self._load_error = f"{type(e).__name__}: {e}"[:300]
            log.warning("local detector unavailable: %s", self._load_error)
            return False
        return True

    @property
    def available(self) -> bool:
        return self.settings.enabled and self.load() and self.gallery_size > 0

    @property
    def gallery_size(self) -> int:
        return 0 if self._gallery is None else int(self._gallery.shape[0])

    # -- embedding ---------------------------------------------------------

    def _preprocess(self, image: Any) -> Any:
        """BGR ndarray -> normalised 1x3x224x224 tensor on the model's device."""
        import cv2
        import torch

        rgb = cv2.cvtColor(cv2.resize(image, (224, 224), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
        t = torch.from_numpy(rgb).float().div_(255.0).permute(2, 0, 1)
        mean = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
        std = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)
        return ((t - mean) / std).unsqueeze(0).to(self._device)

    def embed(self, image: Any) -> Any:
        """One L2-normalised embedding for a BGR ndarray."""
        import torch

        with torch.no_grad():
            v = self._model(self._preprocess(image))
        return torch.nn.functional.normalize(v, dim=-1)[0].cpu()

    def _read(self, path: Path) -> Any:
        import cv2

        img = cv2.imread(str(path))
        if img is None:
            raise ValueError(f"could not read {path}")
        return img

    # -- gallery -----------------------------------------------------------

    def _reference_files(self) -> list[Path]:
        if not self.reference_dir or not self.reference_dir.is_dir():
            return []
        return sorted(p for p in self.reference_dir.iterdir()
                      if p.is_file() and not p.name.startswith(".")
                      and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})

    def _gallery_key(self, files: Sequence[Path]) -> str:
        h = hashlib.sha256(f"{CACHE_VERSION}:{self.settings.model}".encode())
        for p in files:
            st = p.stat()
            h.update(f"{p.name}:{st.st_size}:{int(st.st_mtime)}".encode())
        return h.hexdigest()

    def _build_gallery(self) -> None:
        import torch

        files = self._reference_files()
        if not files:
            raise RuntimeError(f"no reference images in {self.reference_dir}")
        key = self._gallery_key(files)
        if self.cache_path and self.cache_path.is_file():
            try:
                blob = torch.load(self.cache_path, map_location="cpu", weights_only=False)
                if blob.get("key") == key:
                    self._gallery, self._gallery_names = blob["gallery"], blob["names"]
                    log.info("local detector: gallery cache hit (%d references)", self.gallery_size)
                    return
            except Exception as e:
                log.warning("gallery cache unreadable (%s); rebuilding", e)
        t0 = time.perf_counter()
        vecs = [self.embed(self._read(p)) for p in files]
        self._gallery = torch.stack(vecs)
        self._gallery_names = [p.name for p in files]
        log.info("local detector: embedded %d reference image(s) in %.1fs on %s",
                 len(files), time.perf_counter() - t0, self._device)
        if self.cache_path:
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                torch.save({"key": key, "gallery": self._gallery, "names": self._gallery_names},
                           self.cache_path)
            except OSError as e:
                log.warning("could not write gallery cache: %s", e)

    # -- scoring -----------------------------------------------------------

    @staticmethod
    def crop(image: Any, box: Sequence[float] | None, padding: float = 0.15) -> tuple[Any, bool]:
        """Crop a BGR frame to a Vision-style (x, y, w, h) box, origin bottom-left."""
        if not box:
            return image, False
        h, w = image.shape[:2]
        x, y, bw, bh = (float(v) for v in box)
        px, py = bw * padding, bh * padding
        x0 = max(0, int((x - px) * w))
        x1 = min(w, int((x + bw + px) * w))
        # Vision's origin is bottom-left; OpenCV's is top-left.
        y0 = max(0, int((1.0 - y - bh - py) * h))
        y1 = min(h, int((1.0 - y + py) * h))
        if x1 - x0 < 16 or y1 - y0 < 16:
            return image, False
        return image[y0:y1, x0:x1], True

    def score_frame(self, image_path: Path | str, box: Sequence[float] | None = None) -> FrameScore:
        t0 = time.perf_counter()
        try:
            img = self._read(Path(image_path))
            crop, cropped = self.crop(img, box, self.settings.crop_padding)
            v = self.embed(crop)
            sims = self._gallery @ v
            i = int(sims.argmax())
            ms = (time.perf_counter() - t0) * 1000
            self.frames_seen += 1
            self.total_ms += ms
            return FrameScore(float(sims[i]), cropped, ms, self._gallery_names[i])
        except Exception as e:
            return FrameScore(0.0, False, (time.perf_counter() - t0) * 1000,
                              error=f"{type(e).__name__}: {e}"[:200])

    def evaluate(self, frame_paths: Sequence[Path | str],
                 boxes: Sequence[Sequence[float] | None] | None = None) -> LocalVerdict:
        """Score an event's frames and pick a band. Any failure lands in REVIEW."""
        if not self.settings.enabled:
            return LocalVerdict(Decision.REVIEW, error="disabled")
        if not self.load():
            return LocalVerdict(Decision.REVIEW, error=self._load_error)
        if self.gallery_size == 0:
            return LocalVerdict(Decision.REVIEW, error="empty gallery")
        paths = list(frame_paths)[: self.settings.max_frames]
        if not paths:
            return LocalVerdict(Decision.REVIEW, error="no frames")
        boxes = list(boxes or [])[: len(paths)] + [None] * max(0, len(paths) - len(boxes or []))
        scores = [self.score_frame(p, b) for p, b in zip(paths, boxes)]
        usable = [s for s in scores if s.error is None]
        verdict = LocalVerdict(Decision.REVIEW, frames=scores, gallery_size=self.gallery_size,
                               cropped_any=any(s.cropped for s in usable))
        if not usable:
            verdict.error = "; ".join(sorted({s.error for s in scores if s.error}))[:200]
            self.decisions[Decision.REVIEW.value] += 1
            return verdict
        best = max(usable, key=lambda s: s.score)
        verdict.score = best.score
        cfg = self.settings
        if best.score >= cfg.accept_threshold and (best.cropped or not cfg.require_gate_box):
            verdict.decision = Decision.ACCEPT
        elif best.score <= cfg.reject_threshold:
            # Not "this is not Winston" — only "nothing here resembles him
            # enough to spend a review on".
            verdict.decision = Decision.SKIP
        self.decisions[verdict.decision.value] += 1
        return verdict

    # -- observation -------------------------------------------------------

    def to_detection_result(self, verdict: LocalVerdict) -> Any:
        """Turn an ACCEPT into the same DetectionResult shape the model path returns.

        `is_winston_confidence` is the calibrated precision of the accept band,
        not the raw cosine similarity: 45/45 correct gives roughly a 0.92 lower
        bound at 95%, so 0.90 is an honest, slightly conservative statement of
        "how sure is this decision" and it feeds the unchanged fusion.
        """
        from .winston_detector import DetectionResult

        return DetectionResult(
            animal_present=True,
            is_winston_confidence=0.90,
            visual_similarity=round(float(verdict.score), 4),
            size_appearance_compatible=True,
            matched_features=[f"DINOv2 {self.settings.model} cosine {verdict.score:.3f} "
                              f"vs {verdict.gallery_size} reference photos"],
            mismatched_features=[],
            frame_quality="good",
            reasoning=(f"Local embedding match: {verdict.reason()}. Accepted without review "
                       f"because the score is at or above the calibrated accept threshold "
                       f"({self.settings.accept_threshold}), which measured 100% precision "
                       f"on the archived set."),
            raw=json.dumps(verdict.to_dict()),
        )

    def stats(self) -> dict[str, Any]:
        return {"enabled": self.settings.enabled, "model": self.settings.model,
                "device": self._device, "gallery": self.gallery_size,
                "frames_seen": self.frames_seen,
                "avg_ms": round(self.total_ms / self.frames_seen, 1) if self.frames_seen else None,
                "decisions": dict(self.decisions), "error": self._load_error}
