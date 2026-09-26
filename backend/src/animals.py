"""Visiting animals (P4-30): what else the cameras see, as a record of its own.

Winston is the subject of this system; everything else the cameras catch used to
be a dead end. `LocalPipeline` sent a non-Winston animal to REVIEW, a session
recorded a low `is_winston_confidence`, the tracker dropped it below threshold
and nothing was ever queryable. The frames and the reviewer's mismatched-feature
notes survived, but "a raccoon was on the deck at 03:56" was not a fact the
system could answer.

This module keeps that fact, and deliberately keeps it *beside* the tracker
rather than inside it:

**No zones, no topology, no state.** `state_machine` encodes travel windows,
debounce and confirmation for one animal that lives here and moves between known
zones on known routes. A raccoon crossing the deck once is not a journey, and
running it through that machinery would produce confident nonsense — plausible
"transitions" for an animal with no home zone. A visiting-animal sighting is a
flat record: species, camera, time, who said so. It is never a location claim,
about the visitor or about Winston.

**Species comes from a reviewer, never from a model.** Apple Vision's animal
detector knows exactly two labels, Dog and Cat, so it cannot recognise a raccoon,
a deer or a skunk — it reports those as nothing at all, or as a low-confidence
Dog. DINOv2 only answers "how close to Winston is this", which is a similarity
score and not an identity. Nothing in the local stack can name a species, so
nothing in this module infers one: `species` is written only when a human or a
session that looked at the frames says what it saw. An unlabelled animal stays
unlabelled and shows up in `candidates()`.

**It does not notify.** Knowing a raccoon visits at 4am is useful in the morning;
being woken for it is not. The notification policy is untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Species seen or expected on this property. The list is guidance for the CLI
# and the reports, NOT a validation whitelist: refusing an unexpected species
# would mean discarding a real sighting because it was not anticipated.
KNOWN_SPECIES = (
    "raccoon", "cat", "deer", "skunk", "opossum", "coyote", "fox",
    "turkey", "squirrel", "bird", "rabbit", "dog-other", "unknown",
)

#: What a session/reviewer may state about a visitor. `unknown` is a real answer
#: — "an animal, but I cannot tell what" is worth recording and is not a gap.
UNKNOWN = "unknown"

_SLUG = re.compile(r"[^a-z0-9]+")

#: Common ways a reviewer might write a species, mapped to the canonical slug.
_ALIASES = {
    "racoon": "raccoon", "raccoons": "raccoon", "trash-panda": "raccoon",
    "cats": "cat", "kitty": "cat", "housecat": "cat", "house-cat": "cat",
    "possum": "opossum", "opossums": "opossum", "deers": "deer", "doe": "deer",
    "buck": "deer", "fawn": "deer", "wild-turkey": "turkey", "crow": "bird",
    "other-dog": "dog-other", "another-dog": "dog-other", "strange-dog": "dog-other",
    "unidentified": UNKNOWN, "unsure": UNKNOWN, "unclear": UNKNOWN, "": UNKNOWN,
}


def normalize_species(raw: str | None) -> str:
    """Fold a reviewer's wording to a canonical slug, without rejecting it.

    An unrecognised species normalises to its own slug rather than to `unknown`:
    the property may get a visitor nobody anticipated, and losing its name would
    be worse than carrying a slug the report has never seen before.
    """
    s = _SLUG.sub("-", (raw or "").strip().lower()).strip("-")
    return _ALIASES.get(s, s or UNKNOWN)


@dataclass
class AnimalSighting:
    """One visiting animal, seen once, on one camera, at one time."""

    camera_id: str
    timestamp: datetime
    species: str
    source: str
    """Who said so: `session`, `owner`, or `backfill`. Never a model."""
    confidence: float | None = None
    """The reviewer's own certainty about the species, if they gave one. This is
    not a detector score and is not comparable to `winston_probability`."""
    notes: str = ""
    observation_id: int | None = None
    staging_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from .state_machine import normalize_camera_id

        self.camera_id = normalize_camera_id(self.camera_id)
        self.species = normalize_species(self.species)
        if self.source not in ("session", "owner", "backfill"):
            raise ValueError(f"source must be session/owner/backfill, not {self.source!r}")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {"camera_id": self.camera_id, "timestamp": self.timestamp.isoformat(),
                "species": self.species, "source": self.source,
                "confidence": self.confidence, "notes": self.notes,
                "observation_id": self.observation_id, "staging_key": self.staging_key,
                "extra": self.extra}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Group sightings for the report: by species, by camera, by hour of day.

    Counts are of *sightings*, not individuals. Six raccoon events on one camera
    in ten minutes are far more likely to be one raccoon than six, and this
    module has no way to tell them apart, so it does not pretend to.
    """
    by_species: dict[str, dict[str, Any]] = {}
    by_camera: dict[str, int] = {}
    by_hour: dict[int, int] = {}
    for r in rows:
        sp = r["species"]
        b = by_species.setdefault(sp, {"sightings": 0, "cameras": set(),
                                       "first": r["timestamp"], "last": r["timestamp"]})
        b["sightings"] += 1
        b["cameras"].add(r["camera_id"])
        b["first"] = min(b["first"], r["timestamp"])
        b["last"] = max(b["last"], r["timestamp"])
        by_camera[r["camera_id"]] = by_camera.get(r["camera_id"], 0) + 1
        try:
            hour = int(str(r["timestamp"])[11:13])
        except ValueError:
            continue
        by_hour[hour] = by_hour.get(hour, 0) + 1
    for b in by_species.values():
        b["cameras"] = sorted(b["cameras"])
    return {
        "sightings": len(rows),
        "species": dict(sorted(by_species.items(), key=lambda kv: -kv[1]["sightings"])),
        "by_camera": dict(sorted(by_camera.items(), key=lambda kv: -kv[1])),
        "by_hour_utc": dict(sorted(by_hour.items())),
        "note": "counts are sightings, not individual animals",
    }
