from datetime import timedelta

import pytest

from src.state_machine import (
    LocationTracker, StateKind, Topology, TrackerConfig, TransitionEvent, normalize_camera_id,
)
from tests.conftest import T0


# --------------------------------------------------------------------------- #
# Topology
# --------------------------------------------------------------------------- #

def test_topology_maps_cameras_to_zones(topology):
    assert topology.zone_for_camera("backyard") == "backyard"
    assert topology.zone_for_camera("kitchen-door") == "house"
    assert topology.zone_for_camera("Back Door") == "house"  # normalized
    assert topology.zone_for_camera("garage") is None


def test_topology_adjacency_is_symmetric(topology):
    # house never declares neighbors in cameras.yaml, but backyard -> house exists.
    assert topology.are_neighbors("house", "backyard")
    assert topology.window("house", "backyard").min_seconds == 3
    assert not topology.are_neighbors("house", "front")


def test_min_travel_time_sums_shortest_path(topology):
    # house -> backyard (3) -> side-yard (5) -> driveway (8) -> front (5) = 21
    assert topology.min_travel_seconds("house", "front") == 21
    assert topology.min_travel_seconds("front", "front") == 0


def test_topology_rejects_camera_in_two_zones():
    with pytest.raises(ValueError):
        Topology.from_dict({"zones": {"a": {"cameras": ["cam"]}, "b": {"cameras": ["cam"]}}})


def test_topology_rejects_unknown_neighbor():
    with pytest.raises(ValueError):
        Topology.from_dict({"zones": {"a": {"cameras": ["cam"], "neighbors": {"zzz": {"min_seconds": 1, "max_seconds": 2}}}}})


def test_normalize_camera_id():
    assert normalize_camera_id("  Front   Door ") == "front-door"


# --------------------------------------------------------------------------- #
# Basic state
# --------------------------------------------------------------------------- #

def test_initial_state_unknown(topology):
    t = LocationTracker(topology)
    assert t.current_state(T0).kind is StateKind.UNKNOWN


def test_first_sighting_emits_initial_transition(topology, obs_factory):
    t = LocationTracker(topology)
    ev = t.process(obs_factory("backyard", 0, 0.95))
    assert isinstance(ev, TransitionEvent)
    assert ev.from_zone is None and ev.to_zone == "backyard"
    assert ev.is_initial_sighting
    st = t.current_state(T0)
    assert st.kind is StateKind.SEEN and st.zone == "backyard"


def test_low_confidence_observation_ignored(topology, obs_factory):
    t = LocationTracker(topology)
    assert t.process(obs_factory("backyard", 0, 0.4)) is None
    assert t.current_state(T0).kind is StateKind.UNKNOWN
    assert t.rejections[-1].reason.startswith("probability")


def test_unknown_camera_ignored(topology, obs_factory):
    t = LocationTracker(topology)
    assert t.process(obs_factory("garage", 0, 0.99)) is None
    assert "unknown camera" in t.rejections[-1].reason


def test_seen_decays_to_last_seen(topology, obs_factory):
    t = LocationTracker(topology, TrackerConfig(seen_timeout_seconds=120))
    t.process(obs_factory("backyard", 0))
    assert t.current_state(T0 + timedelta(seconds=60)).kind is StateKind.SEEN
    later = t.current_state(T0 + timedelta(minutes=10))
    assert later.kind is StateKind.LAST_SEEN
    assert later.zone == "backyard"
    assert later.minutes_ago(T0 + timedelta(minutes=10)) == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #

def test_valid_neighbor_transition(topology, obs_factory):
    t = LocationTracker(topology)
    t.process(obs_factory("backyard", 0))
    ev = t.process(obs_factory("side-yard", 20, 0.95))
    assert ev is not None
    assert (ev.from_zone, ev.to_zone) == ("backyard", "side-yard")
    assert ev.departed_at == T0 and ev.arrived_at == T0 + timedelta(seconds=20)
    assert ev.confidence == pytest.approx(0.95)
    assert t.current_zone == "side-yard"


def test_too_fast_neighbor_transition_rejected(topology, obs_factory):
    t = LocationTracker(topology)
    t.process(obs_factory("backyard", 0))
    # side-yard needs >= 5s from the backyard
    assert t.process(obs_factory("side-yard", 2, 0.99)) is None
    assert "implausible" in t.rejections[-1].reason
    assert t.current_zone == "backyard"


def test_teleport_across_property_rejected(topology, obs_factory):
    t = LocationTracker(topology)
    t.process(obs_factory("back-door", 0))  # house
    # house -> front minimum is 21s along the path; 10s is impossible.
    assert t.process(obs_factory("front-door", 10, 0.99)) is None
    assert t.current_zone == "house"
    assert "implausible" in t.rejections[-1].reason


def test_multi_hop_transition_accepted_with_penalty(topology, obs_factory):
    cfg = TrackerConfig(multi_hop_penalty=0.7)
    t = LocationTracker(topology, cfg)
    t.process(obs_factory("back-door", 0))
    ev = t.process(obs_factory("front-door", 60, 1.0))
    assert ev is not None and ev.to_zone == "front"
    assert ev.confidence == pytest.approx(0.7)
    assert "intermediate" in ev.reason


def test_slow_neighbor_transition_accepted_with_penalty(topology, obs_factory):
    cfg = TrackerConfig(slow_transition_penalty=0.85)
    t = LocationTracker(topology, cfg)
    t.process(obs_factory("backyard", 0))
    ev = t.process(obs_factory("side-yard", 300, 1.0))  # window max is 60s
    assert ev is not None
    assert ev.confidence == pytest.approx(0.85)


def test_stale_state_accepts_anything(topology, obs_factory):
    cfg = TrackerConfig(stale_after_seconds=3600)
    t = LocationTracker(topology, cfg)
    t.process(obs_factory("front-door", 0))
    # Would be "too fast"/impossible if recent, but it's been 2 hours.
    ev = t.process(obs_factory("back-door", 7200, 0.95))
    assert ev is not None and ev.to_zone == "house"
    assert "stale" in ev.reason


def test_disconnected_zone_rejected(obs_factory):
    topo = Topology.from_dict({"zones": {
        "a": {"cameras": ["cam-a"]},
        "b": {"cameras": ["cam-b"]},
    }})
    t = LocationTracker(topo)
    t.process(obs_factory("cam-a", 0))
    assert t.process(obs_factory("cam-b", 30, 0.99)) is None
    assert "no path" in t.rejections[-1].reason


# --------------------------------------------------------------------------- #
# Debouncing & confirmation
# --------------------------------------------------------------------------- #

def test_repeated_sightings_in_same_zone_collapse(topology, obs_factory):
    t = LocationTracker(topology)
    t.process(obs_factory("backyard", 0))
    for s in (5, 12, 30, 58):
        assert t.process(obs_factory("backyard", s, 0.9)) is None
    assert len(t.history) == 1
    st = t.current_state(T0 + timedelta(seconds=58))
    assert st.kind is StateKind.SEEN
    assert st.timestamp == T0 + timedelta(seconds=58)  # refreshed


def test_weak_sighting_requires_confirmation(topology, obs_factory):
    cfg = TrackerConfig(confidence_threshold=0.7, strong_threshold=0.9, confirmation_window_seconds=90)
    t = LocationTracker(topology, cfg)
    t.process(obs_factory("backyard", 0, 0.95))
    # A single 0.75 sighting at the side yard isn't enough on its own...
    assert t.process(obs_factory("side-yard", 20, 0.75)) is None
    st = t.current_state(T0 + timedelta(seconds=20))
    assert st.kind is StateKind.TRANSITIONING
    assert (st.from_zone, st.to_zone) == ("backyard", "side-yard")
    assert t.current_zone == "backyard"
    # ...but a second one confirms the move.
    ev = t.process(obs_factory("side-yard", 35, 0.8))
    assert ev is not None and ev.to_zone == "side-yard"
    assert "confirmed" in ev.reason
    assert len(ev.observation_ids) == 2


def test_pending_move_expires(topology, obs_factory):
    cfg = TrackerConfig(strong_threshold=0.9, confirmation_window_seconds=60)
    t = LocationTracker(topology, cfg)
    t.process(obs_factory("backyard", 0, 0.95))
    t.process(obs_factory("side-yard", 20, 0.75))
    assert t.current_state(T0 + timedelta(seconds=30)).kind is StateKind.TRANSITIONING
    # Long after the window the pending candidate is dropped.
    assert t.current_state(T0 + timedelta(seconds=200)).kind is StateKind.LAST_SEEN
    # A fresh weak sighting starts a new pending move rather than confirming the stale one.
    assert t.process(obs_factory("side-yard", 200, 0.75)) is None
    assert t.current_zone == "backyard"


def test_sighting_in_current_zone_cancels_pending(topology, obs_factory):
    cfg = TrackerConfig(strong_threshold=0.9)
    t = LocationTracker(topology, cfg)
    t.process(obs_factory("backyard", 0, 0.95))
    t.process(obs_factory("side-yard", 20, 0.75))  # pending
    t.process(obs_factory("backyard", 25, 0.95))   # still in the backyard after all
    assert t.current_state(T0 + timedelta(seconds=25)).kind is StateKind.SEEN
    assert t.process(obs_factory("side-yard", 30, 0.75)) is None  # not a confirmation


def test_out_of_order_observation_ignored(topology, obs_factory):
    t = LocationTracker(topology, TrackerConfig(debounce_seconds=20))
    t.process(obs_factory("backyard", 100))
    assert t.process(obs_factory("side-yard", 10, 0.99)) is None
    assert "out of order" in t.rejections[-1].reason


# --------------------------------------------------------------------------- #
# Callbacks, restore, temporal prior
# --------------------------------------------------------------------------- #

def test_on_transition_callback(topology, obs_factory):
    seen = []
    t = LocationTracker(topology, on_transition=seen.append)
    t.process(obs_factory("backyard", 0))
    t.process(obs_factory("side-yard", 20))
    assert [e.to_zone for e in seen] == ["backyard", "side-yard"]


def test_restore_seeds_state(topology, obs_factory):
    t = LocationTracker(topology)
    t.restore("driveway", T0, confidence=0.9)
    assert t.current_state(T0).zone == "driveway"
    # driveway -> front needs >= 5s
    assert t.process(obs_factory("front-door", 2, 0.99)) is None
    assert t.process(obs_factory("front-door", 10, 0.99)) is not None


def test_temporal_likelihood(topology, obs_factory):
    t = LocationTracker(topology)
    # No prior knowledge is neutral (1.0), not a penalty: the first sighting
    # ever must be able to clear the confidence threshold on its own merits.
    assert t.temporal_likelihood("backyard", T0) == 1.0
    t.process(obs_factory("backyard", 0))
    assert t.temporal_likelihood("backyard", T0 + timedelta(seconds=10)) == 1.0
    assert t.temporal_likelihood("front-door", T0 + timedelta(seconds=5)) == pytest.approx(0.05)
    assert t.temporal_likelihood("side-yard", T0 + timedelta(seconds=20)) == pytest.approx(0.9)
    assert t.temporal_likelihood("side-yard", T0 + timedelta(seconds=600)) == pytest.approx(0.6)
    assert t.temporal_likelihood("garage", T0) == 0.0
    # Once the last sighting is stale, physics says nothing either way.
    assert t.temporal_likelihood("front-door", T0 + timedelta(seconds=t.config.stale_after_seconds + 1)) == 1.0


def test_first_sighting_from_unknown_is_not_penalized_by_fusion(topology, obs_factory):
    from src.winston_detector import DetectionResult, FusionWeights, fuse_signals
    t = LocationTracker(topology)
    r = DetectionResult(animal_present=True, is_winston_confidence=0.85, visual_similarity=0.85,
                        size_appearance_compatible=True)
    p = fuse_signals(r, t.temporal_likelihood("backyard", T0), FusionWeights())
    assert p == pytest.approx(0.6 * 0.85 + 0.25 * 0.85 + 0.15)
    assert p >= t.config.confidence_threshold
