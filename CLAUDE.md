# CLAUDE.md — Animal Tracker

## What this is

A camera-based animal location tracker: Ring cameras → local vision models →
deterministic state machine → notifications (iMessage / APNs / Pushover) →
Apple Watch. The system answers one question: "where is my animal right now?"

**The one rule: never hallucinate a location.** The system reports only what
cameras have observed and what the deterministic tracker derived. If nothing
has seen the animal, the answer is `unknown` or `last_seen(zone, N minutes ago)`.
No defaults, no "probably inside", no guesses. This applies to code, API
responses, notification copy, and anything you write.

## Layout

```
backend/
  config/*.example.yaml       templates — real configs live in the data dir
  src/paths.py                DATA_DIR resolution (~/Library/Application Support/AnimalTracker)
  src/observation.py          Observation dataclass — the vision layer's only output
  src/winston_detector.py     Claude vision verification → Observation (never a location)
  src/local_detector.py       DINOv2 embedding similarity (local, ~8ms/frame)
  src/dog_detector.py         Apple Vision animal gate (local, ~7ms/frame)
  src/local_pipeline.py       gate → embeddings → outcome, inline, no API call
  src/state_machine.py        Topology + LocationTracker (source of truth for location)
  src/notification.py         policy + senders (log, iMessage, Pushover, APNs)
  src/db.py                   SQLite: cameras, zones, observations, transitions, notifications
  src/api.py                  FastAPI; AppContext wires db + tracker + notifier
  src/pipeline.py             Poller: Ring events → frames → local models → tracker
  src/staging.py              session-mode: pending/ frames + sidecars for review
  src/animals.py              visiting animals: species record beside the tracker, never inside it
  src/storage.py              disk retention
  src/capture.py              one-off evidence capture
  src/ring_client.py          ring_doorbell wrapper
  src/frame_extractor.py      MP4 → JPEG frames
  tests/                      pytest; no network, no credentials needed
ios/WinstonWatch/             Swift: WinstonCore (models, API client), watchOS app, WidgetKit
scripts/run.sh                unified entry point for all commands
docs/                         design decisions, dependency policy, research
```

## Running things

```bash
scripts/setup.sh                       # venv + deps + tests
scripts/run.sh test                    # pytest
scripts/run.sh watch-test              # Swift Testing for WinstonCore
scripts/run.sh api                     # API + Ring poller (the whole system)
scripts/run.sh ring-login              # one-time Ring 2FA
scripts/run.sh detect list --sheets    # session-mode: pending review queue
scripts/run.sh detect local [--apply]  # local models classify pending
scripts/run.sh detect audit --sheets   # spot-check local model verdicts
scripts/run.sh animals report          # visiting animal sightings
scripts/run.sh cleanup [--apply]       # disk retention
scripts/run.sh calibrate-local         # recalibrate DINOv2 thresholds
scripts/run.sh findmy-login            # iCloud auth for AirTag polling
scripts/run.sh findmy-test             # verify AirTag location fetch
```

## Site data vs framework code

Site-specific data lives in `~/Library/Application Support/AnimalTracker/`
(override with `ANIMAL_TRACKER_DATA` env var). The repo contains only
framework code and example configs.

```
DATA_DIR/
  config/settings.yaml        thresholds, backends, Ring device IDs
  config/cameras.yaml          property topology (zones, cameras, travel windows)
  reference_images/            enrolled animal photos for the vision layer
  secrets/                     FindMy keys, APNs certs
  tracker.db                   SQLite database
  ring_token.cache             Ring auth
  .env                         secrets (Ring credentials, API tokens)
  staging/                     pending/archive frames
  ring_downloads/              clips and captures
```

## Key design decisions

**Observations vs. locations.** The vision layer answers "is this my enrolled
animal?" and returns an `Observation`. It has no notion of zones. Only
`state_machine.LocationTracker` turns observations into zones via topology rules.

**Deterministic tracking.** The tracker is plain code, fully unit-tested, no
model in the loop. Given the same observations it always produces the same
transitions. Add tests in `tests/test_state_machine.py` before changing rules.

**Local-first detection.** Apple Vision animal gate → DINOv2 ViT-S embeddings
vs reference photos, ~80ms per event total. Handles ~77% of events with no
external call. Uncertain events go to a session review queue.

**Session detection, not API calls.** There is no `ANTHROPIC_API_KEY` by
default. A scheduled Claude or Codex session reviews uncertain frames,
answering the same verification question. This uses the AI subscription
you already pay for — no per-token API billing.

**Visiting animals are a record, not a location.** Non-target animals
(raccoons, cats, etc.) are stored in `animal_sightings`, never in the
tracker. The topology is calibrated for one animal on known routes.

## Working with LLM agents (Claude, Codex, etc.)

This project is designed for **tag-team development** between the owner and
AI coding agents. The local vision pipeline was built to leverage AI
subscriptions (Claude Pro, Codex) for training data and model improvement
without per-token API costs.

### Session detection workflow

The scheduled detection task (`scripts/run.sh detect`) is the primary way
AI sessions contribute:

1. **Review uncertain frames**: `detect list --sheets` shows events the local
   models couldn't decide. The agent views the frames, answers "is this the
   enrolled animal?", and records verdicts with `detect record-batch`.
2. **Audit local decisions**: `detect audit --sheets` samples events the local
   models decided on their own. The agent says what it *sees* (never what it
   *thinks the answer should be*); the tool computes agreement.
3. **Species labelling**: `animals candidates` shows non-target sightings
   awaiting species identification.

### Training the local models

The local vision stack (Apple Vision gate + DINOv2 embeddings) improves from
the verdicts AI sessions produce:

- **Reference gallery**: `scripts/build_reference_set.py` harvests crops from
  confirmed sightings. More diverse references (angles, lighting, IR) improve
  DINOv2 recall. Only session/owner verdicts are eligible — never the local
  models' own output.
- **Threshold calibration**: `scripts/run.sh calibrate-local` replays the
  archive against recorded verdicts. Run after gallery changes.
- **Eval set**: `scripts/run.sh eval-set build` freezes labelled frames for
  offline evaluation. Labels come from session verdicts and owner reviews.
- **Future**: Create ML classifier trained on the accumulated verdicts;
  Core ML conversion for Neural Engine acceleration.

### Agent coordination rules

- **Read the data dir configs** before making changes. `settings.yaml` and
  `cameras.yaml` are in Application Support, not the repo.
- **Never commit site-specific data** to this repo. No Ring device IDs,
  property addresses, email addresses, or reference photos.
- **Tests must stay hermetic**: fake external clients, use `tmp_path` for
  SQLite, never call Ring or any external API.
- **Don't add location logic to the detector.** The detector answers "is this
  my animal?" — only the state machine knows about zones.
- **Don't hallucinate locations.** If you're writing notification copy, API
  responses, or Siri check-in text: if the tracker says `unknown`, say
  `unknown`. Never infer, interpolate, or guess.

### What each agent type is good at

| Task | Best agent | Why |
|------|-----------|-----|
| Review pending frames | Claude (session) | Multimodal; sees the frames directly |
| Audit local verdicts | Claude (session) | Same — visual verification |
| Code changes, tests | Claude or Codex | Both work; Codex is good for multi-file refactors |
| Threshold tuning | Claude (session) | Needs to run calibration + interpret results |
| Architecture decisions | Claude | Benefits from long-context docs reading |
| Species labelling | Claude (session) | Visual identification from frames |
| Bulk file operations | Codex | Sandbox + parallel execution |

## Conventions

- Python 3.11+, type hints, dataclasses, `from __future__ import annotations`
- All timestamps are timezone-aware UTC; local time only for `quiet_hours`
- Camera IDs normalized: lowercase, spaces → hyphens
- Secrets in `.env` / environment; configs in `settings.yaml`
- Don't commit reference images, databases, ring tokens, or `.env`

## Dependency policy

See `docs/Dependencies_and_References.md`. Key points:
- **Ring**: `python-ring-doorbell[listen]` only
- **APNs**: `httpx[http2]` + `PyJWT`; no PyAPNs2
- **Vision**: local models + Claude sessions; no API key needed
- **State machine**: hand-written, no FSM library
- **iMessage**: `osascript` → Messages.app; no third-party library
