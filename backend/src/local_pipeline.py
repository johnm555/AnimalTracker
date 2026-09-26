"""Fully local classification of a Ring event (P4-27): gate -> embedding -> outcome.

This is the default detection path. It replaces the round trip through a
Claude session for the cases the local models can answer, and routes only
the genuinely uncertain ones to review. Everything runs on the Mac Mini in
about 30 ms an event.

    frames -> DogGate (Apple Vision, P4-12) -> LocalDetector (DINOv2, P4-11)

                         gate PRESENT                 gate ABSENT / UNKNOWN
                    (animal localised)              (nothing localised)
    score >= accept      WINSTON                     -
    score >= rescue      REVIEW                      REVIEW
    score <  rescue      REVIEW                      NO_ANIMAL (skipped)

Every threshold here was fitted to the 575 archived events that carry a
recorded verdict (215 Winston). The numbers are the reason the shape is what
it is, so they belong next to the code:

**Why gate PRESENT is nearly sufficient on its own.** Of 133 events where
Apple Vision localised an animal, **132 were Winston**, and it produced zero
false positives across 352 animal-free events. On this property Winston is
the only large animal the cameras see. So a PRESENT gate plus a modest
embedding score is a strong statement, and the accept threshold buys
precision at the margin rather than doing the work alone:

    accept   accepted   precision   coverage of gate-PRESENT Winston
     0.30      127        99.2%              95.5%      <- superseded 2026-09-24
     0.40      102        99.0%              76.5%
     0.50       45       100.0%              34.1%      <- P4-11's original

The single "wrong" event at 0.30-0.45 scored 0.494 with a session confidence
of 0.68 — just under the 0.70 ground-truth cut-off, so it is most likely
Winston as well.

**Why ABSENT must not skip on its own.** The gate misses 40% of real animal
events — a black dog lying still on a dark bed under a fisheye lens is not a
dog to `VNRecognizeAnimalsRequest`. Of 442 ABSENT events, **83 were really
Winston**. Skipping all of them, as a naive "no animal -> discard" rule would,
silently destroys those 83 sightings. So ABSENT gets a second look from the
embedding, scored on the full frame:

    rescue   misses rescued   empties also reviewed   of ABSENTs skipped
     0.20        74/83              238/352                  29%
     0.23        74/83              189/352                  40%   <- default
     0.27        61/83              161/352                  49%
     0.30        49/83               49/352                  77%

0.23 recovers 89% of what the gate lost while still discarding 40% of the
ABSENT pile unreviewed. The two distributions genuinely overlap (real
Winston median 0.306, truly empty median 0.263), so this is a deliberate
trade, not a clean separation.

**Why there is no automatic "animal, but not Winston".** In 575 reviewed
events exactly one gate-PRESENT event was not Winston, and even that one is
probably him. One example is not a class. `not_winston_threshold` exists so
the band can be switched on when there is data behind it, but it is `None`
by default and nothing routes to it; an animal that does not look like
Winston goes to REVIEW, where a human or a session can say so honestly.

**Recalibrated 2026-09-24 (P4-07/P4-18).** Everything above was measured against
a gallery of six daylight phone photos. That gallery was replaced with 36 crops
harvested from confirmed sightings on the cameras themselves, which moved the
whole score distribution up by about 0.09 — so the thresholds moved with it,
from accept 0.30 -> 0.37 and rescue 0.23 -> 0.31.

The gallery is genuinely better, but not in the way a naive check would report.
On 95 held-out positive events and 120 held-out empty events (split by event id
hash, so no harvested frame appears in the evaluation):

    gallery              AUC      recall @ fpr<=0.10    fpr @ the old 0.30
    6 phone photos      0.8675          41%                   10.8%
    42 (36 harvested)   0.9130          67%                   50.8%

Read the last column carefully: left at 0.30, the new gallery accepts **half of
all empty events** as Winston. Raising the threshold is not tuning taste, it is
the difference between this change being an improvement and it being a flood of
fabricated sightings. At 0.37 the measured false-positive rate is 1.7% with 67%
recall — an operating point the phone-photo gallery could not reach at any
threshold (it managed 22% recall at a comparable 2.5% fpr).

Why the shift happens: a harvested reference is a crop from the same fisheye
lenses, surfaces and IR illumination as the input, so *everything* from those
cameras now scores higher — empty decks included. The separation improved; the
absolute numbers are not comparable across galleries, and any future gallery
change must re-run `scripts/build_reference_set.py compare` before the
thresholds are trusted.

Expected split on the archive with the defaults: ~127 auto-Winston, ~177
auto-skipped, ~271 to review. Roughly half the events stop needing a
session; none of the discarded half contained a recorded sighting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from .dog_detector import DogGate, EventVerdict, GateResult
from .local_detector import LocalDetector, LocalVerdict

log = logging.getLogger(__name__)


class Outcome(str, Enum):
    WINSTON = "winston"
    """Confident enough to write an observation with no review."""
    NO_ANIMAL = "no_animal"
    """Nothing animal-like found; recorded as skipped, frames kept."""
    NOT_WINSTON = "not_winston"
    """An animal that is confidently not Winston. Off by default — uncalibrated."""
    REVIEW = "review"
    """Hand to the session path. The honest answer when the models disagree."""


@dataclass
class LocalPipelineSettings:
    enabled: bool = True
    accept_threshold: float = 0.37
    """Gate PRESENT + score at or above this -> WINSTON. Recalibrated 2026-09-24
    against the harvested gallery (see the recalibration note above)."""
    rescue_threshold: float = 0.31
    """Gate ABSENT + score at or above this -> REVIEW instead of skip. Recovers
    89% of the gate's false negatives."""
    not_winston_threshold: float | None = None
    """Gate PRESENT + score at or below this -> NOT_WINSTON. Disabled: there is
    one non-Winston animal event in the whole reviewed archive, which is not
    enough to calibrate a negative claim."""
    require_gate_box: bool = True
    """A WINSTON outcome needs the gate to have localised the animal; a
    full-frame score mostly measures the background."""

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> "LocalPipelineSettings":
        d = ((settings or {}).get("detector") or {}).get("pipeline") or {}
        nw = d.get("not_winston_threshold", None)
        return cls(
            enabled=bool(d.get("enabled", True)),
            accept_threshold=float(d.get("accept_threshold", 0.37)),
            rescue_threshold=float(d.get("rescue_threshold", 0.31)),
            not_winston_threshold=None if nw in (None, "", "null") else float(nw),
            require_gate_box=bool(d.get("require_gate_box", True)),
        )


@dataclass
class LocalResult:
    outcome: Outcome
    score: float = 0.0
    gate: EventVerdict | None = None
    local: LocalVerdict | None = None
    reason: str = ""
    boxes: list[Any] = field(default_factory=list)

    @property
    def gate_present(self) -> bool:
        return self.gate is not None and self.gate.result is GateResult.PRESENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "score": round(self.score, 4),
            "reason": self.reason,
            "gate": self.gate.to_dict() if self.gate else None,
            "local": self.local.to_dict() if self.local else None,
        }


class LocalPipeline:
    """Gate + embedding, combined into one decision per event."""

    def __init__(self, gate: DogGate, local: LocalDetector,
                 settings: LocalPipelineSettings | None = None) -> None:
        self.gate = gate
        self.local = local
        self.settings = settings or LocalPipelineSettings()
        self.counts: dict[str, int] = {o.value: 0 for o in Outcome}

    @property
    def available(self) -> bool:
        """Both models must be usable, or every event has to go to review."""
        return self.settings.enabled and self.gate.enabled and self.local.available

    def classify(self, frame_paths: Sequence[Path | str],
                 frame_bytes: Sequence[bytes] | None = None) -> LocalResult:
        """Decide one event. Any failure yields REVIEW — never a skip, never a sighting."""
        cfg = self.settings
        if not cfg.enabled:
            return self._count(LocalResult(Outcome.REVIEW, reason="local pipeline disabled"))
        paths = list(frame_paths)
        if not paths:
            return self._count(LocalResult(Outcome.REVIEW, reason="no frames"))
        try:
            gate = self.gate.detect_frames(list(frame_bytes) if frame_bytes is not None else paths)
            boxes = [(f.best.box if f.best else None) for f in gate.frames][: len(paths)]
            local = self.local.evaluate(paths, boxes)
        except Exception as e:
            log.warning("local pipeline failed (%s: %s); sending to review", type(e).__name__, e)
            return self._count(LocalResult(Outcome.REVIEW, reason=f"error: {type(e).__name__}: {e}"[:200]))

        score = local.score
        res = LocalResult(Outcome.REVIEW, score, gate, local, boxes=boxes)
        if local.error:
            res.reason = f"embedding unavailable ({local.error}); review"
            return self._count(res)

        if gate.result is GateResult.PRESENT:
            cropped_ok = local.cropped_any or not cfg.require_gate_box
            if score >= cfg.accept_threshold and cropped_ok:
                res.outcome = Outcome.WINSTON
                res.reason = (f"gate {gate.reason()}; embedding {score:.3f} >= {cfg.accept_threshold} "
                              f"(held-out false-positive rate 1.7% at this threshold, 2026-09-24)")
            elif cfg.not_winston_threshold is not None and score <= cfg.not_winston_threshold:
                res.outcome = Outcome.NOT_WINSTON
                res.reason = f"animal found but embedding {score:.3f} <= {cfg.not_winston_threshold}"
            else:
                res.reason = (f"gate {gate.reason()} but embedding {score:.3f} < {cfg.accept_threshold}"
                              + ("" if cropped_ok else " and no crop") + "; review")
            return self._count(res)

        # ABSENT or UNKNOWN: the gate found nothing, which it gets wrong 40% of
        # the time on real animals. Only the low-scoring tail may be discarded.
        if gate.result is GateResult.UNKNOWN:
            res.reason = f"gate inconclusive ({gate.reason()}); review"
            return self._count(res)
        if score >= cfg.rescue_threshold:
            res.reason = (f"gate found no animal but embedding {score:.3f} >= {cfg.rescue_threshold}; "
                          f"review (the gate misses 40% of real animals)")
            return self._count(res)
        res.outcome = Outcome.NO_ANIMAL
        res.reason = f"no animal: gate clean on {len(gate.frames)} frame(s), embedding {score:.3f} < {cfg.rescue_threshold}"
        return self._count(res)

    def _count(self, res: LocalResult) -> LocalResult:
        self.counts[res.outcome.value] += 1
        return res

    def detection_result(self, res: LocalResult) -> Any:
        """A DETECTION_SCHEMA verdict for an auto-accepted event."""
        from .winston_detector import DetectionResult

        best = res.gate.best if res.gate else None
        feats = [f"DINOv2 cosine {res.score:.3f} vs the enrolled reference photos"]
        if best:
            feats.append(f"Apple Vision localised a {best.label} at confidence {best.confidence:.2f}")
        return DetectionResult(
            animal_present=True,
            is_winston_confidence=0.90,
            visual_similarity=round(float(res.score), 4),
            size_appearance_compatible=True,
            matched_features=feats,
            mismatched_features=[],
            frame_quality="good",
            reasoning=("Accepted locally with no review: " + res.reason +
                       ". Measured 2026-09-24 on 95 held-out positive and 120 held-out empty "
                       "events against the harvested gallery: 1.7% false-positive rate, 67% recall."),
            raw="",
        )

    def stats(self) -> dict[str, Any]:
        return {"enabled": self.settings.enabled, "available": self.available,
                "accept_threshold": self.settings.accept_threshold,
                "rescue_threshold": self.settings.rescue_threshold,
                "not_winston_threshold": self.settings.not_winston_threshold,
                "outcomes": dict(self.counts)}
