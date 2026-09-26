"""Deterministic location tracker.

The tracker is the single source of truth for "where is Winston". It consumes
Observations (camera, timestamp, probability) and, using the camera topology,
decides whether each one is believable given where Winston was last seen and
how long ago. It never guesses: if no camera has seen Winston, the state is
UNKNOWN or LAST_SEEN, never a fabricated zone.

State kinds
-----------
UNKNOWN        no confident sighting yet
SEEN           confidently in `zone` as of `timestamp` (within seen_timeout)
TRANSITIONING  was in `from_zone`; a not-yet-confirmed sighting says `to_zone`
LAST_SEEN      last confident sighting was in `zone`, more than seen_timeout ago

Transition rules
----------------
* An observation below `confidence_threshold` never changes state.
* Same zone as current -> debounce: refresh the timestamp, no event.
* Different zone:
    - compute the shortest plausible travel time from the current zone using
      the topology's min_seconds windows (summed along the shortest path);
    - if the elapsed time is shorter than that -> REJECT (implausible teleport);
    - direct neighbor within [min, max]      -> accept, high confidence;
    - direct neighbor slower than max        -> accept, reduced confidence;
    - non-neighbor but plausible via a path  -> accept, reduced confidence
      (intermediate cameras missed him);
    - observations below `strong_threshold` need a second confirming sighting
      in the same candidate zone within `confirmation_window_seconds`
      (this is the TRANSITIONING state).
* Once LAST_SEEN has decayed past `stale_after_seconds`, any confident sighting
  is accepted without a topology check (he could be anywhere by then).
"""

from __future__ import annotations

import heapq
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import yaml

from .observation import Observation, parse_timestamp

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Topology
# --------------------------------------------------------------------------- #

def normalize_camera_id(name: str) -> str:
    return "-".join(name.strip().lower().split())


@dataclass(frozen=True)
class TransitionWindow:
    min_seconds: float
    max_seconds: float


@dataclass
class Zone:
    id: str
    cameras: list[str]
    description: str = ""
    neighbors: dict[str, TransitionWindow] = field(default_factory=dict)


class Topology:
    """Zones, the cameras that cover them, and plausible travel times between them."""

    def __init__(self, zones: dict[str, Zone]) -> None:
        self.zones = zones
        self._camera_to_zone: dict[str, str] = {}
        for zone in zones.values():
            for cam in zone.cameras:
                self._camera_to_zone[normalize_camera_id(cam)] = zone.id
        self._make_symmetric()
        self._validate()

    # -- construction ------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Topology":
        zones: dict[str, Zone] = {}
        for zone_id, spec in (data.get("zones") or {}).items():
            spec = spec or {}
            neighbors = {
                nid: TransitionWindow(float(w["min_seconds"]), float(w["max_seconds"]))
                for nid, w in (spec.get("neighbors") or {}).items()
            }
            zones[zone_id] = Zone(
                id=zone_id,
                cameras=[normalize_camera_id(c) for c in spec.get("cameras", [])],
                description=spec.get("description", ""),
                neighbors=neighbors,
            )
        return cls(zones)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Topology":
        with open(path) as f:
            return cls.from_dict(yaml.safe_load(f) or {})

    def _make_symmetric(self) -> None:
        # If A lists B but B doesn't list A, copy A's window over.
        for zone in list(self.zones.values()):
            for nid, window in zone.neighbors.items():
                other = self.zones.get(nid)
                if other is None:
                    raise ValueError(f"zone '{zone.id}' lists unknown neighbor '{nid}'")
                other.neighbors.setdefault(zone.id, window)

    def _validate(self) -> None:
        seen: dict[str, str] = {}
        for zone in self.zones.values():
            for cam in zone.cameras:
                if cam in seen and seen[cam] != zone.id:
                    raise ValueError(f"camera '{cam}' assigned to both '{seen[cam]}' and '{zone.id}'")
                seen[cam] = zone.id
            for nid, w in zone.neighbors.items():
                if w.min_seconds < 0 or w.max_seconds < w.min_seconds:
                    raise ValueError(f"bad window {zone.id}->{nid}: {w}")

    # -- queries -----------------------------------------------------------

    def zone_for_camera(self, camera_id: str) -> str | None:
        return self._camera_to_zone.get(normalize_camera_id(camera_id))

    def window(self, from_zone: str, to_zone: str) -> TransitionWindow | None:
        z = self.zones.get(from_zone)
        return z.neighbors.get(to_zone) if z else None

    def are_neighbors(self, a: str, b: str) -> bool:
        return self.window(a, b) is not None

    def min_travel_seconds(self, from_zone: str, to_zone: str) -> float | None:
        """Shortest plausible travel time along any path (sum of min_seconds).

        Returns 0 for the same zone and None when the zones are disconnected.
        """
        if from_zone == to_zone:
            return 0.0
        if from_zone not in self.zones or to_zone not in self.zones:
            return None
        dist = {from_zone: 0.0}
        heap = [(0.0, from_zone)]
        while heap:
            d, z = heapq.heappop(heap)
            if z == to_zone:
                return d
            if d > dist.get(z, float("inf")):
                continue
            for nid, w in self.zones[z].neighbors.items():
                nd = d + w.min_seconds
                if nd < dist.get(nid, float("inf")):
                    dist[nid] = nd
                    heapq.heappush(heap, (nd, nid))
        return None


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

class StateKind(str, Enum):
    UNKNOWN = "unknown"
    SEEN = "seen"
    TRANSITIONING = "transitioning"
    LAST_SEEN = "last_seen"


@dataclass
class TrackerState:
    kind: StateKind
    zone: str | None = None
    """Current (SEEN) or last confirmed (LAST_SEEN / TRANSITIONING) zone."""
    timestamp: datetime | None = None
    """Time of the most recent confident sighting in `zone`."""
    confidence: float = 0.0
    from_zone: str | None = None
    to_zone: str | None = None
    """Populated only for TRANSITIONING: the unconfirmed candidate zone."""

    def minutes_ago(self, now: datetime) -> float | None:
        if self.timestamp is None:
            return None
        return max(0.0, (parse_timestamp(now) - self.timestamp).total_seconds() / 60.0)

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {
            "state": self.kind.value,
            "zone": self.zone,
            "last_seen_at": self.timestamp.isoformat() if self.timestamp else None,
            "confidence": round(self.confidence, 3),
        }
        if now is not None:
            d["minutes_ago"] = None if self.timestamp is None else round(self.minutes_ago(now), 1)
        if self.kind is StateKind.TRANSITIONING:
            d["from_zone"] = self.from_zone
            d["to_zone"] = self.to_zone
        return d


@dataclass
class TransitionEvent:
    """Emitted when Winston is confirmed to have moved to a new zone."""

    to_zone: str
    arrived_at: datetime
    confidence: float
    from_zone: str | None = None
    departed_at: datetime | None = None
    observation_ids: list[int] = field(default_factory=list)
    reason: str = ""
    id: int | None = None

    @property
    def is_initial_sighting(self) -> bool:
        return self.from_zone is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from_zone": self.from_zone,
            "to_zone": self.to_zone,
            "departed_at": self.departed_at.isoformat() if self.departed_at else None,
            "arrived_at": self.arrived_at.isoformat(),
            "confidence": round(self.confidence, 3),
            "observation_ids": list(self.observation_ids),
            "reason": self.reason,
        }


@dataclass
class Rejection:
    """Why an observation did not change state (useful for debugging/tuning)."""

    observation: Observation
    reason: str
    zone: str | None = None


@dataclass
class TrackerConfig:
    confidence_threshold: float = 0.70
    strong_threshold: float = 0.90
    debounce_seconds: float = 20.0
    confirmation_window_seconds: float = 90.0
    seen_timeout_seconds: float = 120.0
    stale_after_seconds: float = 6 * 3600.0
    """After this long without a sighting, skip the topology check entirely."""
    slow_transition_penalty: float = 0.85
    """Confidence multiplier for a neighbor move slower than max_seconds."""
    multi_hop_penalty: float = 0.70
    """Confidence multiplier for a move that skipped intermediate cameras."""

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "TrackerConfig":
        d = d or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class _Pending:
    zone: str
    first_seen: datetime
    last_seen: datetime
    observation_ids: list[int]
    confidence: float


# --------------------------------------------------------------------------- #
# Tracker
# --------------------------------------------------------------------------- #

class LocationTracker:
    """Deterministic state machine: observations in, transition events out."""

    def __init__(
        self,
        topology: Topology,
        config: TrackerConfig | None = None,
        on_transition: Callable[[TransitionEvent], None] | None = None,
        on_rejection: Callable[[Rejection], None] | None = None,
    ) -> None:
        self.topology = topology
        self.config = config or TrackerConfig()
        self.on_transition = on_transition
        self.on_rejection = on_rejection
        self._zone: str | None = None
        self._zone_since: datetime | None = None   # arrived_at of current zone
        self._last_seen: datetime | None = None    # most recent confident sighting
        self._confidence: float = 0.0
        self._pending: _Pending | None = None
        self._observation_ids: list[int] = []      # sightings supporting current zone
        self.history: list[TransitionEvent] = []
        self.rejections: list[Rejection] = []
        self._processed_ring_events: set[tuple[str, str]] = set()

    # -- public API --------------------------------------------------------

    def restore(self, zone: str, last_seen: datetime, confidence: float = 1.0,
                zone_since: datetime | None = None) -> None:
        """Seed the tracker from persisted state (e.g. last transition in the DB)."""
        self._zone = zone
        self._last_seen = parse_timestamp(last_seen)
        self._zone_since = parse_timestamp(zone_since) if zone_since else self._last_seen
        self._confidence = confidence
        self._pending = None

    def current_state(self, now: datetime | None = None) -> TrackerState:
        now = parse_timestamp(now) if now else (self._last_seen or datetime.now().astimezone())
        if self._zone is None:
            return TrackerState(kind=StateKind.UNKNOWN)
        if self._pending is not None and not self._pending_expired(now):
            return TrackerState(
                kind=StateKind.TRANSITIONING, zone=self._zone, timestamp=self._last_seen,
                confidence=self._confidence, from_zone=self._zone, to_zone=self._pending.zone,
            )
        age = (now - self._last_seen).total_seconds()
        kind = StateKind.SEEN if age <= self.config.seen_timeout_seconds else StateKind.LAST_SEEN
        return TrackerState(kind=kind, zone=self._zone, timestamp=self._last_seen,
                            confidence=self._confidence)

    @property
    def current_zone(self) -> str | None:
        return self._zone

    def temporal_likelihood(self, camera_id: str, at: datetime) -> float:
        """Prior plausibility (0-1) that Winston is at `camera_id` at time `at`.

        Fed back into the detector so the vision model's verdict can be tempered
        by physics: a sighting at the front door two seconds after a confirmed
        backyard sighting is almost certainly a different dog.
        """
        zone = self.topology.zone_for_camera(camera_id)
        if zone is None:
            return 0.0
        # "No prior knowledge" is neutral (1.0), never a penalty. The fusion
        # multiplies by this value, so anything below 1.0 lowers the score; a
        # 0.5 here used to make the very first sighting from `unknown`
        # unreachable below ~0.93 model confidence (observed live 2026-09-20).
        if self._zone is None or self._last_seen is None:
            return 1.0
        at = parse_timestamp(at)
        elapsed = (at - self._last_seen).total_seconds()
        if elapsed >= self.config.stale_after_seconds:
            return 1.0
        if zone == self._zone:
            return 1.0
        min_t = self.topology.min_travel_seconds(self._zone, zone)
        if min_t is None:
            return 0.05  # disconnected zone
        if elapsed < min_t:
            return 0.05
        window = self.topology.window(self._zone, zone)
        if window is not None and elapsed <= window.max_seconds:
            return 0.9
        return 0.6

    def process(self, obs: Observation) -> TransitionEvent | None:
        """Feed one observation. Returns a TransitionEvent if Winston moved."""
        cfg = self.config
        zone = self.topology.zone_for_camera(obs.camera_id)
        if zone is None:
            return self._reject(obs, f"unknown camera '{obs.camera_id}'")
        # A revised verdict is audit/calibration evidence, not another sighting.
        # Consume identity even for a low-confidence first verdict so live state
        # and append-only restart replay follow the same first-verdict policy.
        device_id = obs.extra.get("ring_device_id")
        event_id = obs.extra.get("ring_event_id")
        if device_id is not None and event_id is not None and str(device_id) and str(event_id):
            identity = (str(device_id), str(event_id))
            if identity in self._processed_ring_events:
                return self._reject(obs, "duplicate Ring event; revision retained for calibration only", zone)
            self._processed_ring_events.add(identity)
        if obs.winston_probability < cfg.confidence_threshold:
            return self._reject(obs, f"probability {obs.winston_probability:.2f} below threshold", zone)

        now = obs.timestamp
        if self._last_seen is not None and now < self._last_seen - timedelta(seconds=cfg.debounce_seconds):
            return self._reject(obs, "observation older than current state (out of order)", zone)

        if self._pending is not None and self._pending_expired(now):
            log.debug("pending move to %s expired", self._pending.zone)
            self._pending = None

        # --- first ever sighting ------------------------------------------
        if self._zone is None:
            return self._commit(zone, obs, confidence=obs.winston_probability,
                                reason="initial sighting")

        # --- same zone: debounce -------------------------------------------
        if zone == self._zone:
            self._last_seen = max(self._last_seen, now)
            self._confidence = max(self._confidence, obs.winston_probability)
            if obs.id is not None:
                self._observation_ids.append(obs.id)
            # A sighting back in the current zone cancels any pending move.
            self._pending = None
            return None

        # --- different zone: plausibility check ---------------------------
        elapsed = (now - self._last_seen).total_seconds()
        plausibility, reason = self._plausibility(self._zone, zone, elapsed)
        if plausibility == 0.0:
            return self._reject(obs, reason, zone)

        confidence = obs.winston_probability * plausibility

        # --- confirmation / TRANSITIONING ----------------------------------
        if self._pending is not None and self._pending.zone == zone:
            # Second sighting in the candidate zone: confirm the move.
            ids = self._pending.observation_ids + ([obs.id] if obs.id is not None else [])
            confidence = max(confidence, self._pending.confidence)
            self._pending = None
            return self._commit(zone, obs, confidence=confidence, reason=reason + " (confirmed)",
                                observation_ids=ids)

        if obs.winston_probability >= cfg.strong_threshold:
            self._pending = None
            return self._commit(zone, obs, confidence=confidence, reason=reason)

        # Not strong enough on its own: hold as pending and wait for a second look.
        self._pending = _Pending(
            zone=zone, first_seen=now, last_seen=now,
            observation_ids=[obs.id] if obs.id is not None else [],
            confidence=confidence,
        )
        log.debug("pending move %s -> %s (p=%.2f)", self._zone, zone, obs.winston_probability)
        return None

    # -- internals ---------------------------------------------------------

    def _plausibility(self, from_zone: str, to_zone: str, elapsed: float) -> tuple[float, str]:
        """Return (multiplier in [0,1], human reason). 0 means reject."""
        cfg = self.config
        if elapsed >= cfg.stale_after_seconds:
            return 1.0, "stale prior; accepted without topology check"
        min_t = self.topology.min_travel_seconds(from_zone, to_zone)
        if min_t is None:
            return 0.0, f"no path from {from_zone} to {to_zone}"
        if elapsed < min_t:
            return 0.0, (f"implausible: {from_zone} -> {to_zone} in {elapsed:.0f}s "
                         f"(minimum {min_t:.0f}s)")
        window = self.topology.window(from_zone, to_zone)
        if window is not None:
            if elapsed <= window.max_seconds:
                return 1.0, f"{from_zone} -> {to_zone} in {elapsed:.0f}s (within window)"
            return cfg.slow_transition_penalty, (
                f"{from_zone} -> {to_zone} in {elapsed:.0f}s (slower than {window.max_seconds:.0f}s window)")
        return cfg.multi_hop_penalty, f"{from_zone} -> {to_zone} via intermediate zones ({elapsed:.0f}s)"

    def _pending_expired(self, now: datetime) -> bool:
        assert self._pending is not None
        return (now - self._pending.first_seen).total_seconds() > self.config.confirmation_window_seconds

    def _commit(self, zone: str, obs: Observation, confidence: float, reason: str,
                observation_ids: list[int] | None = None) -> TransitionEvent:
        event = TransitionEvent(
            from_zone=self._zone,
            to_zone=zone,
            departed_at=self._last_seen,
            arrived_at=obs.timestamp,
            confidence=max(0.0, min(1.0, confidence)),
            observation_ids=observation_ids if observation_ids is not None
                            else ([obs.id] if obs.id is not None else []),
            reason=reason,
        )
        self._zone = zone
        self._zone_since = obs.timestamp
        self._last_seen = obs.timestamp
        self._confidence = event.confidence
        self._observation_ids = list(event.observation_ids)
        self.history.append(event)
        log.info("transition %s -> %s (%.2f): %s", event.from_zone, event.to_zone,
                 event.confidence, event.reason)
        if self.on_transition:
            self.on_transition(event)
        return event

    def _reject(self, obs: Observation, reason: str, zone: str | None = None) -> None:
        rej = Rejection(observation=obs, reason=reason, zone=zone)
        self.rejections.append(rej)
        log.debug("rejected observation from %s: %s", obs.camera_id, reason)
        if self.on_rejection:
            self.on_rejection(rej)
        return None
