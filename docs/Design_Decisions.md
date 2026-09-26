# Design Decisions

Short ADR-style records. Newest at the bottom. When you change one of these,
update the record rather than silently diverging.

---

## ADR-001 — The vision model produces observations, never locations

**Decision.** `winston_detector.py` answers one question per event: *is the
animal in these frames the enrolled animal in the reference
photos?* It returns an `Observation` bound to a camera. It has no concept of
zones and is never told where the animal was last seen in words.

**Why.** A language model asked "where is the animal?" will happily answer even
when no camera has seen it. Splitting perception (probabilistic, model-based)
from tracking (deterministic, code-based) means every location claim can be
traced to a specific camera at a specific time, and the model's failure mode
is a wrong *probability*, not a fabricated *place*.

**Consequences.** The only cross-talk is numeric: the tracker supplies a
`temporal_likelihood` prior which the detector fuses into
`winston_probability`. Tests assert the `Observation` has no zone field.

## ADR-002 — Deterministic, topology-aware state machine

**Decision.** `LocationTracker` is plain code driven by `cameras.yaml`. Given
the same sequence of observations it always yields the same transitions.

**Why.** Debuggability and trust. When the watch says "driveway" you can
replay the observations and see exactly which rule fired. Physical
plausibility (a large dog can't get from the house to the front door in
10 s) is a far stronger signal than anything a vision model can offer for
rejecting look-alike dogs.

**Consequences.** All thresholds and timings live in YAML. Every rejection
carries a reason string. New rules require a test first.

## ADR-003 — Verification prompt, not classification

**Decision.** The prompt explicitly says: "Determine whether the animal
visible in these frames is the enrolled animal shown in the
reference images. Do not perform generic animal classification."

**Why.** Generic "is this a dog?" is near-useless in a neighborhood with
dogs; the value is individual identification. Anchoring on reference photos
and asking for matched/mismatched features produces calibrated, explainable
output.

**Consequences.** Reference photos are a hard requirement (pipeline refuses
to start without them). They go first in the message so they form a stable
prefix with a `cache_control` breakpoint; frames and the question follow.

## ADR-004 — Structured output over free-text parsing

**Decision.** Use `output_config.format` with a strict JSON schema
(`DETECTION_SCHEMA`, `additionalProperties: false`). `parse_detection_response`
still tolerates fences/prose as a fallback.

**Why.** Deterministic downstream code needs deterministic input shape.

## ADR-005 — Two thresholds and a confirmation step

**Decision.** `confidence_threshold` (0.70) gates any effect;
`strong_threshold` (0.90) allows a zone change on a single sighting; between
the two, a second sighting in the same candidate zone within
`confirmation_window_seconds` is required. Meanwhile the state reads
TRANSITIONING(from, to).

**Why.** Single mid-confidence frames are the main false-positive source
(neighbor's dog, shadows). The animal moving through a zone almost always
triggers ≥2 events. TRANSITIONING is an honest description of "we think it
moved, not sure yet" instead of flipping state back and forth.

## ADR-006 — Debounce in the tracker, cooldown in the notifier

**Decision.** Repeated sightings inside one zone never leave the tracker
(timestamp refresh only). Repeated *transitions* between the same two zones
within `cooldown_seconds` are downgraded by the notifier to silent pushes.

**Why.** They are different phenomena: pacing around the backyard is not a
location change; pacing between backyard and side yard is, but nobody wants
a buzz every 40 s. Keeping the second concern in the notifier means the
history/stats endpoints still record every real transition.

## ADR-007 — Front and driveway are high priority

**Decision.** Arrivals in street-adjacent zones use APNs
`interruption-level: time-sensitive`, bypass quiet hours, and get Pushover
priority 1.

**Why.** Those are the only transitions with a safety consequence.

## ADR-008 — SQLite, single process, no queue

**Decision.** One SQLite file in WAL mode; pipeline and API either share a
process or talk over HTTP (`--post`).

**Why.** Volume is tens of events per day. Anything heavier is ceremony.

## ADR-009 — Poll Ring history first, push later

**Decision.** v0 polls `history()` every 15 s. FCM push
(`RingEventListener`) is the planned upgrade.

**Why.** Polling is sync and simple; push needs an asyncio task and FCM
credential persistence. Latency of 15–60 s is acceptable for v0 because clips
aren't downloadable for ~20 s anyway.

## ADR-010 — Log every decision, including the ones that did nothing

**Decision.** Low-confidence observations, rejected observations (with
reason), and suppressed notifications are all persisted.

**Why.** Tuning thresholds and topology windows on real footage is the whole
game after day one; the "nothing happened" rows are the dataset.

## ADR-011 — Model choice: `claude-opus-5`, adaptive thinking

**Decision.** Default to `claude-opus-5` with `thinking: {type: "adaptive"}`
and `max_tokens: 4096`; configurable in `settings.yaml`.

**Why.** Individual-dog identification from mediocre night-time frames is a
hard visual task; get accuracy first, then measure whether a cheaper model
holds up on the logged dataset (ADR-010 makes that possible). Server-side
refusal fallbacks were deliberately left out for now (dog photos don't
trigger them; keeps the injectable-client test setup simple).

## ADR-012 — Event clips only; no continuous streaming, no WebRTC in Python

**Decision.** Perception runs on Ring's per-event recordings (and, later, FCM
push + optional single snapshots). We never hold a live stream open, and we
don't implement Ring's WebRTC signaling ourselves. If an on-demand frame is
ever needed, run [go2rtc](https://github.com/AlexxIT/go2rtc) as a sidecar
(native `ring:` source, `GET /api/frame.jpeg`) and rate-limit pulls.

**Why.** Ring cameras are event devices: while streaming they stop emitting
motion events — the very signal this system depends on — and battery models
drain and overheat (documented at length by the ring-mqtt maintainer). The
WebRTC path is Ring-specific signaling on top of `werift`/`aiortc`; go2rtc
already maintains it. Survey in
[Dependencies_and_References.md](Dependencies_and_References.md) §1/§4.

**Consequences.** Latency floor is Ring's clip-processing delay (~20 s), which
is acceptable for zone tracking. Frigate-style continuous detection is off the
table unless the house gets non-Ring cameras, in which case Frigate becomes
the perception layer and our tracker consumes its zone events.

## ADR-013 — Session-mode detection: a scheduled Claude session is the vision model

**Decision (2026-09-20).** There is no Anthropic API key (subscription plan
only). `detector.mode: session` is the default: the poller stages each
event's frames plus a JSON sidecar under `backend/staging/pending/`, and a
scheduled Claude Code session views them, answers the same verification
question, and records the verdict with `scripts/run.sh detect record-batch`.
The verdict is the same `DETECTION_SCHEMA` JSON the API path returns and
goes through the unchanged `WinstonDetector.to_observation()` fusion, then
`POST /winston/observation`. `detector.mode: api` keeps the original
in-process model call for the day a key exists. Details: `scripts/detect_pending.py` and
[the review-frames skill](../.claude/skills/review-frames/SKILL.md).

**Why.** Everything downstream — fusion, tracker, notifications, watch —
is agnostic to *who* produced the verdict. Staging on disk decouples the
24/7 poller from the intermittent session, is restart-safe through the
`processed_events` ledger (`staged` → `analyzed`/`skipped`), and costs
nothing per event beyond the subscription. It also gives a free
human-reviewable archive of every frame set with its verdict.

**Consequences.** Detection latency is the schedule interval (30 min) plus
polling, not seconds; acceptable for zone tracking, not for "it's at the
gate right now". The session must obey the same rules as the model
(verification, not classification; `skip` for unusable frames; never a
location). ADR-011's model choice and the API request shape remain valid
for `api` mode only.

## ADR-014 — "No prior knowledge" is neutral in the temporal prior

**Decision (2026-09-20).** `LocationTracker.temporal_likelihood()` returns
`1.0` when the tracker is `unknown` or the last sighting is stale, not
`0.5`. Penalties (`0.9`, `0.6`, `0.05`) apply only when physics actually
has an opinion.

**Why.** `fuse_signals()` multiplies the visual score by
`(1 − w) + w · prior`; with `w = 0.5` a prior of `0.5` is a 25 % haircut.
Observed live: three genuine sightings at model confidence 0.80–0.90
fused to 0.61–0.69 and were all rejected below the 0.70 threshold, so the
system could never leave `unknown`. A prior meaning "I know nothing" must
not lower the score.

**Consequences.** The first sighting after a restart or a long gap is
judged on the visual evidence alone. `test_state_machine.py` pins both the
neutral values and the "first sighting clears the threshold" case.

## ADR-015 — Session fusion excludes the staging-time movement prior

**Decision (2026-09-21, P1-27).** Session verdict conversion passes no temporal
prior to visual fusion. Preserve the original sidecar value in
`extra.staged_temporal_likelihood` and mark `extra.temporal_policy` as
`tracker_only`. The deterministic tracker checks movement at ingestion.

**Why.** A session batch is staged before earlier events in that batch have
updated tracker state. Applying that frozen prior at review time penalized
plausible sightings: visual 0.865 became 0.692 with prior 0.6 and failed the
0.70 threshold. Recomputing in the CLI would still race API ingestion; the
tracker already checks plausibility under its ingestion lock.

**Consequences.** Existing pending sidecars work unchanged. Frame-quality
penalties, thresholds, impossible-travel rejection and confirmation still
apply. API-mode temporal fusion is unchanged. Event timestamps are preserved.
No historical observations are rewritten; P1-26 must address supersession
before requeue-based recalibration. Tests cover delayed batched confirmations,
impossible movement, poor-frame rejection and persisted audit metadata.


## ADR-016 — Preserve revisions; deduplicate calibration by Ring event identity

**Decision (2026-09-21, P1-26).** Keep every observation and derived transition.
Use `Database.iter_calibration_observations()` / `detect calibration` to select
the latest committed verdict by insertion ID per Ring device/event pair.
Carry requeue provenance into new observation metadata. No schema migration.

**Why.** Deleting or retroactively filtering tracker input would invalidate
references in existing transitions and change restart behavior relative to the
running tracker. Counting every revision would bias threshold calibration.
Event identity handles existing duplicates and multi-revision chains, including
two cameras that share one name. Latest means newest verdict,
not highest confidence. Failed reviews and skips cannot erase prior evidence.

**Limits.** This does not correct historical transitions or retract alerts.
Calibration must explicitly use the revision-aware view; raw history remains
raw. Corrected chronological tracker evaluation belongs in the offline replay
harness (P4-08). No live observations are requeued by this change.


## ADR-017 — One Ring event supplies at most one live sighting (P1-29)

The deterministic tracker consumes each `(ring_device_id, ring_event_id)` pair
once, after camera validation and before the confidence check. Later verdicts
are persisted but rejected for tracking as duplicate Ring events. Numeric/string
IDs normalize to strings; different device IDs stay independent. Observations
without both identity fields retain existing behavior.

This prevents a forced re-review from confirming its own pending move, generating
another alert, or refreshing the sighting time. A rejected first verdict is also
consumed: corrections belong in the latest-per-event calibration view and an
explicit offline replay, not retroactive live notifications. Restart reconstructs
the identity set by replaying observations in ingestion order. The set grows with
unique event history, like existing replay/history costs; a future checkpoint
must preserve deduplication state rather than forget old identities silently.

Historical transition and notification records are not rewritten. If older code
already created a duplicate-derived transition, restart under this rule can yield
a different current state. Old records remain an audit of what was emitted then;
no retraction or new alert is sent on restart. Replay tests verify the corrected
policy is identical before/after restart for newly processed evidence.

## ADR-018 — The session audits the local models rather than replacing them (P4-29)

**Status:** accepted, 2026-09-23.

**Context.** After ADR-013 the scheduled Claude session was the detector: it
reviewed every staged event. P4-27 moved that work to local models running in
the poller, which settle about 77% of events in ~80 ms with no session at all.
That leaves the session with spare capacity and the local path with a problem:
**nothing downstream contradicts it.** A wrong `winston` produces a plausible
transition and a notification; a wrong `no_animal` produces silence. Neither
error announces itself, and the thresholds behind them were fitted to a 575-event
archive that will drift as the seasons, the reference set and the cameras change.

**Decision.** Keep the scheduled session running, with its job inverted. Each
run now spot-checks a bounded sample of the local models' automatic verdicts
before draining the review queue. The results accumulate in a `local_audits`
table and drive threshold changes by hand.

Four properties make it a measurement rather than a rubber stamp:

1. **The sample is balanced across outcomes, not drawn at random.** Skips
   outnumber sightings roughly 1:1 now and will not stay that way; a
   proportional sample would spend itself on the majority outcome and leave the
   other untested for days.
2. **Agreement is derived, not reported.** The session records the label it saw;
   the comparison to the stored outcome happens in `db.insert_local_audit`.
   There is no code path by which a session writes "agreed".
3. **`uncertain` is a first-class answer** and counts as disagreement with any
   confident outcome. A reviewer who cannot tell is evidence that the model
   should not have been sure either.
4. **Nothing auto-tunes.** `audit-summary` names the threshold each miss would
   have required and stops. A handful of audits cannot price the trade a
   threshold makes against the whole distribution; `scripts/calibrate_local.py`
   over the archive can, and is the gate for any change.

**Consequences.** Quality assurance costs ~8 image reads a run, and detection of
a systematic local failure now takes hours rather than being indefinite. A
missed sighting found by an audit is *not* automatically recovered: requeueing it
would ingest an hour-old observation, and the tracker consumes observations in
ingestion order, so a stale one can move the reported location backwards —
precisely the confident wrong answer the one rule forbids. `--requeue` exists,
prints the warning, and is never the default. Audits are also not a ground-truth
set: they are one reviewer's reading of frames the models already judged, and
the sampling is deliberately biased toward balance, so the agreement rate is a
drift signal rather than an accuracy estimate.

**Alternatives rejected.** Auditing through the existing `observation_reviews`
table (P4-04): it requires an `observation_id`, which a skipped event does not
have — the most important error class would have had nowhere to live. Having the
session re-decide every event as a shadow run: that is the cost P4-27 removed.
Auto-adjusting thresholds from audit disagreements: a feedback loop with four
samples and no held-out set tunes itself into whatever the last run happened to
see.

## ADR-019 — Visiting animals get their own record, not a place in the tracker (P4-30)

**Status:** accepted, 2026-09-23.

**Context.** Owner request: the property gets visitors — a raccoon was confirmed
on the side deck on 2026-09-23, a cat earlier the same day — and John wants to
know what comes by and when. Until now a non-target animal was a dead end.
`LocalPipeline` routed it to REVIEW, a session recorded a low
`is_winston_confidence`, the tracker dropped it below threshold, and nothing was
queryable afterwards. The evidence survived in the frames and in the reviewer's
`mismatched_features`, but "a raccoon was on the deck at 03:56" was not a fact
the system could answer.

**Decision.** Store visiting animals in a separate `animal_sightings` table,
served from `/animals` (outside the `/winston/` namespace), with four
constraints:

1. **It never touches `LocationTracker`.** The state machine encodes travel
   windows, debounce and confirmation for one animal that lives here and moves
   between known zones on known routes. A visitor has no home zone and no route.
   Feeding it through that machinery would manufacture confident nonsense —
   plausible-looking "transitions" for an animal passing through once. A
   visiting-animal sighting is flat: species, camera, time, who said so.
2. **Species comes from a reviewer, never from a model.** Apple Vision's animal
   request knows exactly two labels, Dog and Cat, so it cannot recognise a
   raccoon, a deer or a skunk — it reports them as nothing, or as a
   low-confidence Dog. DINOv2 answers only "how close to the animal is this", which
   is similarity, not identity. Nothing in the local stack can name a species,
   so `source` is constrained to `session`/`owner`/`backfill` and a model cannot
   be recorded as the author of one.
3. **An unrecognised species keeps its name.** `normalize_species` folds
   wording (`racoon` → `raccoon`, `possum` → `opossum`) but does not validate
   against a whitelist: refusing an unexpected visitor would discard the single
   most interesting sighting the system could ever record.
4. **Visitors do not notify.** Knowing a raccoon visits at 4am is useful in the
   morning; being woken for it is not. The notification policy is untouched.

**Consequences.** `animal_sightings.observation_id` is uniquely indexed where
non-null, so an observation has at most one species and relabelling updates in
place; sightings with no observation behind them are not deduplicated, because
two raccoons on two nights are two sightings. Reports count *sightings, not
individuals* and say so — six events in ten minutes on one camera are far more
likely to be one raccoon than six, and nothing here can tell them apart.

The labelling queue (`animals candidates`) is `animal_present = 1 AND
winston_probability < threshold` minus anything already labelled. It is
deliberately noisy: most of its 31 current rows are low-confidence *target-animal*
(backlit, coat washed out), not other species. It surfaces candidates for a
human to look at and never guesses a species from the row.

**Alternatives rejected.** A `species` column on `observations`: an observation
is the vision layer's answer to one question — is this the enrolled animal? — and widening it
would put two different claims in one row. Reusing `winston_probability` as a
generic animal score: it is calibrated for one identity and a second meaning
would silently corrupt every threshold that reads it. Auto-labelling from the
Apple Vision gate: it would label the confirmed raccoon a "dog" or nothing at
all, and a wrong species is worse than an absent one.
