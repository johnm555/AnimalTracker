"""Session-mode detection: poller stages frames, a session records verdicts."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.api import AppContext
from src.db import Database
from src.notification import LogSender, NotificationPolicy, NotificationService, PolicyConfig
from src.pipeline import PipelineSettings, Poller
from src.ring_client import MotionEvent
from src.staging import (
    StagingDirs, archive_event, find_archived, find_pending, list_archived, list_pending, record_verdict,
    requeue_event, skip_event, verdict_to_observation,
)
from src.state_machine import LocationTracker, TrackerConfig
from src.winston_detector import DETECTION_SCHEMA

from tests.test_pipeline import T0, FakeExtractor, FakeRing, event

# Share test_pipeline's clock: `event()` stamps events from its T0. A second T0
# computed at a different import time can land a second later, which put the
# first event behind the poller cursor (flaky "1 == 2").

WINSTON = {
    "animal_present": True, "is_winston_confidence": 0.95, "visual_similarity": 0.9,
    "size_appearance_compatible": True, "matched_features": ["natural ears", "black coat"],
    "mismatched_features": [], "frame_quality": "good", "reasoning": "Same dog as the reference set.",
}
NOT_WINSTON = {**WINSTON, "is_winston_confidence": 0.05, "visual_similarity": 0.2,
               "size_appearance_compatible": False, "matched_features": [],
               "mismatched_features": ["small", "tan coat"], "reasoning": "A small tan dog."}


@pytest.fixture
def ctx(tmp_path, topology):
    settings = {"tracker": {"confidence_threshold": 0.7, "strong_threshold": 0.9}}
    db = Database(tmp_path / "s.db")
    db.sync_topology(topology)
    tracker = LocationTracker(topology, TrackerConfig.from_dict(settings["tracker"]))
    notifier = NotificationService(NotificationPolicy(PolicyConfig()), LogSender(), db)
    return AppContext(settings, topology, db, tracker, notifier)


@pytest.fixture
def dirs(tmp_path):
    return StagingDirs(tmp_path / "staging")


def session_poller(ctx, dirs, events):
    return Poller(ring=FakeRing(events), extractor=FakeExtractor(), detector=None, sink=ctx.ingest, db=ctx.db,
                  settings=PipelineSettings(), since=T0 - timedelta(seconds=1), mode="session", staging=dirs,
                  n_reference=6, temporal_prior=ctx.tracker.temporal_likelihood)


def test_poller_stages_frames_and_sidecar_without_a_detector(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0), event("e2", "kitchen-door", 90)])
    assert poller.run_once() == 2
    assert poller.status.events_staged == 2 and poller.status.mode == "session"
    pending = list_pending(dirs)
    assert [p.sidecar["event_id"] for p in pending] == ["e1", "e2"]  # oldest first
    side = pending[0].sidecar
    assert side["camera_id"] == "backyard" and side["ring_classification"] == "human"
    assert side["frames"] == ["frame-0.jpg"] and pending[0].frame_paths[0].read_bytes() == b"jpg"
    assert "Winston" in side["question"] and "Do not perform generic animal classification" in side["question"]
    assert side["verdict_schema"] == DETECTION_SCHEMA
    assert 0.0 <= side["temporal_likelihood"] <= 1.0
    # No observation exists yet — staging is not a verdict.
    assert ctx.db.list_observations() == []
    assert ctx.db.count_events_by_status() == {"staged": 2}
    # Re-polling does not stage twice.
    assert poller.run_once() == 0
    assert len(list_pending(dirs)) == 2


def test_verdict_roundtrip_moves_tracker_and_archives(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    poller.run_once()
    pending = find_pending(dirs, "e1")
    assert pending is not None
    mark = lambda status, obs_id, err: ctx.db.mark_event_by_id("dev-backyard", "e1", status, obs_id, err)  # noqa: E731

    result = record_verdict(dirs, pending, WINSTON, ctx.ingest, mark)
    assert result["accepted"] and result["transition"]["to_zone"] == "backyard"
    obs = ctx.db.list_observations()[0]
    assert obs.winston_probability >= 0.7 and obs.vision_confidence == 0.95  # visual fusion; movement is checked by the tracker
    assert obs.extra["detector"] == "session"
    assert obs.extra["ring_event_id"] == "e1" and obs.extra["staging_key"] == pending.key
    assert ctx.tracker.current_state(T0 + timedelta(seconds=1)).zone == "backyard"
    assert ctx.db.count_events_by_status() == {"analyzed": 1}
    assert list_pending(dirs) == []
    archived = list(dirs.archive.glob("*/*"))
    assert len(archived) == 1 and json.loads((archived[0] / "event.json").read_text())["outcome"] == "analyzed"


def test_not_winston_verdict_does_not_move_tracker(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    poller.run_once()
    pending = find_pending(dirs, "e1")
    result = record_verdict(dirs, pending, json.dumps(NOT_WINSTON), ctx.ingest)
    assert result["accepted"] is False and result["transition"] is None
    assert ctx.tracker.current_state(T0 + timedelta(seconds=1)).zone is None
    assert ctx.db.list_observations()[0].winston_probability < 0.3  # recorded, but as a low-probability observation


def test_verdict_text_with_prose_and_fences_is_tolerated(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    poller.run_once()
    pending = find_pending(dirs, "e1")
    text = "Here is my verdict:\n```json\n" + json.dumps(WINSTON) + "\n```"
    obs = verdict_to_observation(pending, text)
    assert obs.camera_id == "backyard" and obs.vision_confidence == 0.95


def test_bad_verdict_is_rejected_and_nothing_is_archived(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    poller.run_once()
    pending = find_pending(dirs, "e1")
    with pytest.raises(ValueError):
        record_verdict(dirs, pending, {"reasoning": "forgot the fields"}, ctx.ingest)
    assert len(list_pending(dirs)) == 1 and ctx.db.list_observations() == []


def test_skip_is_not_an_observation(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    poller.run_once()
    pending = find_pending(dirs, "e1")
    mark = lambda status, obs_id, err: ctx.db.mark_event_by_id("dev-backyard", "e1", status, obs_id, err)  # noqa: E731
    dest = skip_event(dirs, pending, "frames are black", mark)
    assert ctx.db.list_observations() == []
    assert ctx.db.count_events_by_status() == {"skipped": 1}
    assert json.loads((dest / "event.json").read_text())["outcome"] == "skipped: frames are black"
    assert list_pending(dirs) == []


def test_partial_directories_are_ignored(dirs):
    dirs.ensure()
    (dirs.pending / "cam__x__1.part").mkdir()
    (dirs.pending / "no-sidecar").mkdir()
    assert list_pending(dirs) == []


# --------------------------------------------------------------------------- #
# requeue (P1-24): re-fuse an archived verdict after a threshold change
# --------------------------------------------------------------------------- #

def _archived_event(ctx, dirs):
    """Stage one event, record a Winston verdict, return its archived directory."""
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    poller.run_once()
    pending = find_pending(dirs, "e1")
    mark = lambda status, obs_id, err: ctx.db.mark_event_by_id("dev-backyard", "e1", status, obs_id, err)  # noqa: E731
    record_verdict(dirs, pending, WINSTON, ctx.ingest, mark)
    archived = find_archived(dirs, "e1")
    assert archived is not None
    return archived


def test_requeue_moves_an_archived_event_back_to_pending(ctx, dirs):
    archived = _archived_event(ctx, dirs)
    assert len(list_archived(dirs)) == 1 and list_pending(dirs) == []

    pending = requeue_event(dirs, archived)

    assert [p.key for p in list_pending(dirs)] == [pending.key]
    assert list_archived(dirs) == []
    # Frames and the verification question survive the round trip unchanged.
    assert pending.frame_paths[0].read_bytes() == b"jpg"
    assert "Do not perform generic animal classification" in pending.sidecar["question"]
    # Archive bookkeeping is cleared, a breadcrumb is left.
    assert "outcome" not in pending.sidecar and "archived_at" not in pending.sidecar
    assert pending.sidecar["requeued_at"]


def test_requeue_resets_the_ledger_and_reports_the_superseded_observation(ctx, dirs):
    archived = _archived_event(ctx, dirs)
    obs_id = ctx.db.list_observations()[0].id
    assert ctx.db.count_events_by_status() == {"analyzed": 1}

    requeue_event(dirs, archived)
    prior = ctx.db.requeue_event("dev-backyard", "e1")

    assert prior == obs_id
    assert ctx.db.count_events_by_status() == {"staged": 1}
    row = ctx.db.get_event("dev-backyard", "e1")
    assert row["status"] == "staged" and row["observation_id"] is None
    # The observation itself is NOT deleted — re-recording adds another.
    assert len(ctx.db.list_observations()) == 1


def test_requeued_event_can_be_re_recorded_and_re_fused(ctx, dirs):
    """The point of requeue: the same verdict, re-fused, lands as a new observation."""
    archived = _archived_event(ctx, dirs)
    first = ctx.db.list_observations()[0]

    pending = requeue_event(dirs, archived)
    ctx.db.requeue_event("dev-backyard", "e1")
    mark = lambda status, obs_id, err: ctx.db.mark_event_by_id("dev-backyard", "e1", status, obs_id, err)  # noqa: E731
    record_verdict(dirs, pending, WINSTON, ctx.ingest, mark)

    observations = ctx.db.list_observations()
    assert len(observations) == 2
    assert observations[0].vision_confidence == observations[1].vision_confidence == 0.95
    assert ctx.db.count_events_by_status() == {"analyzed": 1}
    assert list_pending(dirs) == [] and len(list_archived(dirs)) == 1
    # Same frames, same verdict — the fusion result is reproducible.
    assert {o.winston_probability for o in observations} == {first.winston_probability}


def test_requeue_refuses_to_clobber_an_already_pending_event(ctx, dirs):
    archived = _archived_event(ctx, dirs)
    requeue_event(dirs, archived)
    # A second copy arriving in archive/ must not overwrite the pending one.
    second = dirs.archive / "day" / (dirs.pending.iterdir().__next__().name)
    second.parent.mkdir(parents=True, exist_ok=True)
    second.mkdir()
    (second / "event.json").write_text(json.dumps({"event_id": "e1", "device_id": "dev-backyard",
                                                   "camera_id": "backyard", "timestamp": T0.isoformat()}))
    with pytest.raises(FileExistsError):
        requeue_event(dirs, second)


def test_requeue_of_a_skipped_event_reports_no_observation(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    poller.run_once()
    mark = lambda status, obs_id, err: ctx.db.mark_event_by_id("dev-backyard", "e1", status, obs_id, err)  # noqa: E731
    skip_event(dirs, find_pending(dirs, "e1"), "occluded behind the recliner", mark)

    requeue_event(dirs, find_archived(dirs, "e1"))
    prior = ctx.db.requeue_event("dev-backyard", "e1")

    assert prior is None  # nothing to supersede: a skip was never an observation
    assert ctx.db.count_events_by_status() == {"staged": 1}
    assert ctx.db.list_observations() == []


def test_find_archived_matches_key_or_event_id(ctx, dirs):
    archived = _archived_event(ctx, dirs)
    assert find_archived(dirs, archived.name) == archived
    assert find_archived(dirs, "e1") == archived
    assert find_archived(dirs, "nope") is None


@pytest.mark.parametrize("staged_prior", [0.6, 0.05, None])
def test_delayed_session_verdict_ignores_staged_prior_but_requires_confirmation(ctx, dirs, staged_prior):
    events = [event("e1", "backyard", 0), event("e2", "kitchen-door", 40),
              event("e3", "kitchen-door", 45)]
    session_poller(ctx, dirs, events).run_once()
    pending = list_pending(dirs)
    # All events are captured before any verdict updates tracker state.
    for item in pending[1:]:
        item.sidecar["temporal_likelihood"] = staged_prior
    record_verdict(dirs, pending[0], WINSTON, ctx.ingest)
    verdict = {**WINSTON, "is_winston_confidence": 0.85, "visual_similarity": 0.82}
    first = record_verdict(dirs, pending[1], verdict, ctx.ingest)
    assert first["accepted"] and first["transition"] is None
    assert first["state"]["zone"] == "backyard"  # a candidate is not a confirmed move
    second = record_verdict(dirs, pending[2], verdict, ctx.ingest)
    assert second["transition"]["to_zone"] == "house"
    stored = next(o for o in ctx.db.list_observations() if o.extra["ring_event_id"] == "e2")
    assert stored.winston_probability == pytest.approx(0.865)  # formerly 0.692 with prior 0.6
    assert stored.temporal_likelihood is None
    assert stored.extra["staged_temporal_likelihood"] == staged_prior
    assert stored.extra["temporal_policy"] == "tracker_only"
    assert stored.timestamp == events[1].timestamp  # capture time, not review time


def test_session_visual_match_cannot_bypass_impossible_travel(ctx, dirs):
    session_poller(ctx, dirs, [event("e1", "backyard", 0),
                               event("e2", "kitchen-door", 1)]).run_once()
    pending = list_pending(dirs)
    record_verdict(dirs, pending[0], WINSTON, ctx.ingest)
    pending[1].sidecar["temporal_likelihood"] = 1.0  # even an optimistic stale prior is ignored
    result = record_verdict(dirs, pending[1], WINSTON, ctx.ingest)
    assert not result["accepted"] and result["transition"] is None
    assert "implausible" in result["rejection_reason"]
    assert result["state"]["zone"] == "backyard"
    assert len(ctx.db.list_observations()) == 2  # rejected evidence still persists


def test_session_poor_frame_confidence_stays_below_threshold(ctx, dirs):
    session_poller(ctx, dirs, [event("e1", "backyard", 0)]).run_once()
    pending = list_pending(dirs)[0]
    verdict = {**WINSTON, "is_winston_confidence": 0.85, "visual_similarity": 0.82,
               "frame_quality": "poor"}
    result = record_verdict(dirs, pending, verdict, ctx.ingest)
    assert not result["accepted"] and result["transition"] is None
    assert ctx.db.list_observations()[0].winston_probability == pytest.approx(0.692)


def test_api_ingest_closes_ledger_row_in_the_same_connection(ctx, dirs):
    """The API owns the DB: a session verdict's observation closes the staged
    ledger row inside ingest(), so a second process never races the snapshot
    (the FOREIGN KEY failure seen live 2026-09-21)."""
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    poller.run_once()
    assert ctx.db.count_events_by_status() == {"staged": 1}
    pending = find_pending(dirs, "e1")
    obs = verdict_to_observation(pending, WINSTON)
    result = ctx.ingest(obs)  # no `mark` callback at all
    row = ctx.db.list_processed_events()[0]
    assert row["status"] == "analyzed" and row["observation_id"] == result["observation_id"]
    # A later best-effort mark from the script is idempotent.
    assert ctx.db.mark_event_by_id("dev-backyard", "e1", "analyzed", result["observation_id"], None) is True
    assert ctx.db.mark_event_by_id("dev-backyard", "nope", "analyzed", None, None) is False


def test_requeue_calibration_replaces_only_after_successful_verdict(ctx, dirs):
    archived = _archived_event(ctx, dirs)
    original = ctx.db.list_observations()[0]
    transitions = ctx.db.list_transitions()
    pending = requeue_event(dirs, archived)
    pending.sidecar["superseded_observation_id"] = original.id
    ctx.db.requeue_event("dev-backyard", "e1")
    assert [o.id for o in ctx.db.iter_calibration_observations()] == [original.id]
    with pytest.raises(ValueError):
        record_verdict(dirs, pending, {"reasoning": "invalid"}, ctx.ingest)
    assert [o.id for o in ctx.db.iter_calibration_observations()] == [original.id]
    record_verdict(dirs, pending, NOT_WINSTON, ctx.ingest)
    latest = list(ctx.db.iter_calibration_observations())
    assert len(latest) == 1 and latest[0].id != original.id
    assert latest[0].extra["superseded_observation_id"] == original.id
    assert latest[0].extra["requeued_at"] == pending.sidecar["requeued_at"]
    assert ctx.db.list_transitions() == transitions  # historical transition evidence remains intact
    assert ctx.db.get_observation(original.id) is not None


@pytest.mark.parametrize("category", ["unusable_frames", "occluded_subject", "unspecified"])
def test_skip_category_persisted_without_an_observation(ctx, dirs, category):
    session_poller(ctx, dirs, [event("e1", "backyard", 0)]).run_once()
    pending = find_pending(dirs, "e1")
    mark = lambda status, obs_id, err: ctx.db.mark_event_by_id("dev-backyard", "e1", status, obs_id, err)
    dest = skip_event(dirs, pending, "cannot identify subject", mark, category=category)
    sidecar = json.loads((dest / "event.json").read_text())
    assert sidecar["skip_category"] == category
    assert sidecar["skip_reason"] == "cannot identify subject"
    row = ctx.db.get_event("dev-backyard", "e1")
    assert row["status"] == "skipped" and row["observation_id"] is None
    assert row["error"] == f"[{category}] cannot identify subject"
    assert ctx.db.list_observations() == [] and ctx.db.list_transitions() == []
    requeued = requeue_event(dirs, dest)
    assert "skip_category" not in requeued.sidecar and "skip_reason" not in requeued.sidecar


@pytest.mark.parametrize("category,reason", [("dog_absent", "reason"), ("occluded_subject", " ")])
def test_invalid_skip_leaves_event_pending(ctx, dirs, category, reason):
    session_poller(ctx, dirs, [event("e1", "backyard", 0)]).run_once()
    pending = find_pending(dirs, "e1")
    mark = lambda status, obs_id, err: ctx.db.mark_event_by_id("dev-backyard", "e1", status, obs_id, err)
    with pytest.raises(ValueError):
        skip_event(dirs, pending, reason, mark, category=category)
    assert len(list_pending(dirs)) == 1
    assert ctx.db.count_events_by_status() == {"staged": 1}
    assert "skip_category" not in pending.sidecar


def test_stage_preserves_frame_offsets_without_inventing_missing_times(dirs):
    from src.staging import stage_event
    frames = [SimpleNamespace(image=SimpleNamespace(data=b"first"), offset_seconds=1.25),
              SimpleNamespace(image=SimpleNamespace(data=b"second"), offset_seconds=7.75),
              SimpleNamespace(data=b"unknown")]
    staged = stage_event(dirs, event("timing", "backyard", 0), frames, 6)
    sidecar = json.loads((staged / "event.json").read_text())
    assert sidecar["frame_offsets_seconds"] == [1.25, 7.75, None]
    assert sidecar["frames"] == ["frame-0.jpg", "frame-1.jpg", "frame-2.jpg"]
    pending = find_pending(dirs, "timing")
    archived = archive_event(dirs, pending, "skipped: test")
    requeued = requeue_event(dirs, archived)
    assert requeued.sidecar["frame_offsets_seconds"] == [1.25, 7.75, None]


# --------------------------------------------------------------------------- #
# Local animal gate wired into the poller (P4-12)
# --------------------------------------------------------------------------- #

def _gate(script, **kw):
    from src.dog_detector import DogGate, GateSettings
    from tests.test_dog_detector import FakeBackend
    return DogGate(GateSettings(**kw), FakeBackend(script))


def test_gate_verdict_is_attached_to_the_sidecar_as_a_hint(ctx, dirs):
    from src.dog_detector import Detection
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    poller.gate = _gate([[Detection("dog", 0.91, (0.1, 0.2, 0.3, 0.4))]])
    assert poller.run_once() == 1
    side = list_pending(dirs)[0].sidecar
    assert side["gate"]["result"] == "present"
    assert side["gate"]["best"]["label"] == "dog" and side["gate"]["best"]["confidence"] == 0.91
    assert side["gate"]["best"]["box"] == [0.1, 0.2, 0.3, 0.4]
    # A hint, not a verdict: the event still goes to review.
    assert ctx.db.count_events_by_status() == {"staged": 1}


def test_gate_absent_still_stages_by_default(ctx, dirs):
    """apple_vision misses 40% of real sightings, so ABSENT must not drop."""
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    poller.gate = _gate([[]])
    assert poller.run_once() == 1
    assert len(list_pending(dirs)) == 1
    assert ctx.db.count_events_by_status() == {"staged": 1}
    assert poller.status.events_gated == 0
    assert list_pending(dirs)[0].sidecar["gate"]["result"] == "absent"


def test_gate_absent_skips_the_event_when_explicitly_enabled(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0), event("e2", "backyard", 60)])
    poller.gate = _gate([[]], skip_on_absent=True)
    assert poller.run_once() == 0                      # nothing staged
    assert list_pending(dirs) == []
    assert ctx.db.count_events_by_status() == {"skipped": 2}
    assert poller.status.events_gated == 2
    row = ctx.db.list_processed_events()[0]
    assert "no_animal_detected" in row["error"]
    # A gate skip is never an observation, and never "not Winston".
    assert ctx.db.list_observations() == []


def test_gate_error_never_drops_an_event(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    poller.gate = _gate(["Vision unavailable"], skip_on_absent=True)
    assert poller.run_once() == 1
    assert ctx.db.count_events_by_status() == {"staged": 1}
    assert poller.status.events_gated == 0


# --------------------------------------------------------------------------- #
# Local identity pre-filter wired into the poller (P4-11)
# --------------------------------------------------------------------------- #

def _local(scores, **kw):
    from src.local_detector import LocalSettings
    from tests.test_local_detector import FakeDetector
    return FakeDetector(scores, LocalSettings(enabled=True, **kw))


def _pipeline(poller, gate, scores, **kw):
    """Attach a real LocalPipeline driven by fake models (P4-27)."""
    from src.local_pipeline import LocalPipeline, LocalPipelineSettings
    poller.gate = gate
    poller.local = _local(scores)
    poller.pipeline = LocalPipeline(gate, poller.local, LocalPipelineSettings(**kw))
    return poller.pipeline


def _gate_present(conf=0.9, box=(0.1, 0.2, 0.3, 0.4)):
    from src.dog_detector import Detection
    return _gate([[Detection("dog", conf, box)]])


def test_local_accept_creates_a_sighting_without_review(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    _pipeline(poller, _gate_present(), [(0.58, True)])
    assert poller.run_once() == 1
    obs = ctx.db.list_observations()
    assert len(obs) == 1
    assert obs[0].extra["detector"] == "local"
    assert obs[0].vision_similarity == pytest.approx(0.58)
    assert obs[0].winston_probability >= ctx.tracker.config.confidence_threshold
    assert ctx.tracker.current_state(T0 + timedelta(seconds=1)).zone == "backyard"
    assert ctx.db.count_events_by_status() == {"analyzed": 1}
    assert list_pending(dirs) == []                     # archived, not left pending
    assert poller.status.events_local_accepted == 1


def test_local_review_band_leaves_the_event_staged(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    _pipeline(poller, _gate_present(), [(0.20, True)], accept_threshold=0.50)
    assert poller.run_once() == 1
    assert len(list_pending(dirs)) == 1
    assert ctx.db.count_events_by_status() == {"staged": 1}
    assert ctx.db.list_observations() == []


def test_local_skip_is_recorded_as_skipped_never_as_not_winston(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    _pipeline(poller, _gate([[]]), [(0.02, False)])
    poller.run_once()
    assert ctx.db.count_events_by_status() == {"skipped": 1}
    row = ctx.db.list_processed_events()[0]
    assert row["error"].startswith("local no_animal:") and row["observation_id"] is None
    assert ctx.db.list_observations() == []             # no fabricated negative
    assert list_pending(dirs) == []


def test_gate_absent_but_high_embedding_goes_to_review_not_skip(ctx, dirs):
    """The gate misses 40% of real animals, so ABSENT alone must not discard."""
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    _pipeline(poller, _gate([[]]), [(0.30, False)], rescue_threshold=0.23)
    assert poller.run_once() == 1
    assert len(list_pending(dirs)) == 1                 # kept for review
    assert ctx.db.count_events_by_status() == {"staged": 1}
    assert ctx.db.list_observations() == []


def test_gate_present_but_low_embedding_is_review_not_a_negative_verdict(ctx, dirs):
    """No automatic 'animal but not Winston' — one counter-example is not a class."""
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    _pipeline(poller, _gate_present(), [(0.05, True)])
    assert poller.run_once() == 1
    assert ctx.db.count_events_by_status() == {"staged": 1}
    assert ctx.db.list_observations() == []


def test_not_winston_band_only_fires_when_explicitly_configured(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    _pipeline(poller, _gate_present(), [(0.05, True)], not_winston_threshold=0.10)
    poller.run_once()
    assert ctx.db.count_events_by_status() == {"skipped": 1}
    row = ctx.db.list_processed_events()[0]
    assert row["error"].startswith("local not_winston:")
    assert ctx.db.list_observations() == []             # still never an observation


def test_local_decision_is_recorded_in_the_sidecar_for_reviewed_events(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    _pipeline(poller, _gate([[]]), [(0.30, False)], rescue_threshold=0.23)
    poller.run_once()
    side = list_pending(dirs)[0].sidecar
    assert side["local"]["outcome"] == "review" and side["local"]["score"] == 0.30
    assert "gate misses 40%" in side["local"]["reason"]


def test_broken_local_detector_falls_back_to_review(ctx, dirs):
    class Broken:
        settings = type("S", (), {"enabled": True, "model": "x"})()
        def evaluate(self, *a, **k): raise RuntimeError("MPS out of memory")
        def stats(self): return {}
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    poller.gate = _gate_present()
    poller.local = Broken()
    poller.pipeline = None
    assert poller.run_once() == 1
    assert ctx.db.count_events_by_status() == {"staged": 1}
    assert len(list_pending(dirs)) == 1


def test_local_disabled_changes_nothing(ctx, dirs):
    poller = session_poller(ctx, dirs, [event("e1", "backyard", 0)])
    poller.gate = _gate_present()
    assert poller.run_once() == 1
    assert ctx.db.count_events_by_status() == {"staged": 1}
