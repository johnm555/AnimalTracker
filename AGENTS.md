# AGENTS.md — Animal Tracker

Guidance for any coding agent (Claude, Codex, Cursor, others) working in
this repo. `CLAUDE.md` has Claude-specific detail; if they disagree, this
file wins.

## The one rule

**Never hallucinate a location.** If no camera has seen the animal, the
answer is `unknown` — never a guess.

## Before you start

0. **The plan is the issue tracker:** https://github.com/johnm555/AnimalTracker/issues
   (labels `area:*`). Pick an issue, say so on it, and open a PR that closes it.
   New ideas become issues, not notes in the repo.

1. Check which branch you're on: `main` is the public framework; `internal`
   (if it exists) is the private deployment with site-specific history.
2. Read `CLAUDE.md` for the full layout, commands, and design decisions.
3. Site data lives in `~/Library/Application Support/AnimalTracker/`, not in
   the repo. Read the configs there before changing code that depends on them.

## Task playbooks

Step-by-step playbooks live in `.claude/skills/*/SKILL.md`. Claude Code loads
them automatically; other agents (Codex etc.) should read the matching file
before starting:

| Task | Playbook |
|---|---|
| Configure a property (cameras, zones, travel windows, priorities) | `.claude/skills/setup-property/SKILL.md` |
| Notifications, quiet hours, muting | `.claude/skills/configure-notifications/SKILL.md` |
| Review queued frames, audit local decisions | `.claude/skills/review-frames/SKILL.md` |
| Improve the local models | `.claude/skills/train-local-models/SKILL.md` |
| Something's broken | `.claude/skills/troubleshoot/SKILL.md` — always start with `scripts/run.sh doctor` |

Unattended review with either subscription: `scripts/review_session.sh claude|codex`
(scheduled by `scripts/install-launchd.sh review claude|codex`).

## How AI sessions train the local models

The system is designed so your existing AI subscription (Claude Pro, Codex,
etc.) continuously improves the local vision pipeline — no API key or
per-token billing needed.

### The feedback loop

```
Ring motion event
    ↓
Local models (Apple Vision + DINOv2)  ←── reference gallery + thresholds
    ↓                                          ↑
Confident? ──yes──→ Observation                │
    ↓ no                                       │
Staged for review                              │
    ↓                                          │
AI session reviews frames ──verdict──→ DB      │
    ↓                                          │
build_reference_set.py  ──new crops──→ gallery │
calibrate-local         ──new thresholds──────→┘
```

Each AI session that reviews frames makes the local models better at
deciding on their own next time:

1. **Session reviews uncertain events** → verdicts stored in DB
2. **`build_reference_set.py`** harvests crops from confirmed sightings →
   more diverse reference gallery → better DINOv2 similarity scores
3. **`calibrate-local`** re-fits thresholds against the growing verdict
   archive → the local models handle a larger share of events
4. **`detect audit`** spot-checks what the local models decided → catches
   drift before it accumulates

### What the session agent does

```bash
# 1. Drain the review queue (events local models couldn't decide)
scripts/run.sh detect list --sheets     # see what's pending
scripts/run.sh detect record-batch verdicts.json

# 2. Spot-check local model decisions
scripts/run.sh detect audit --sheets    # sample recent auto-decisions
scripts/run.sh detect audit-summary     # agreement rate

# 3. Label visiting animals
scripts/run.sh animals candidates       # non-target animals awaiting species

# 4. (Periodically) Improve the local models
scripts/run.sh train status             # what to do next
scripts/run.sh train harvest            # plan a gallery from new verdicts (--apply after train evaluate)
scripts/run.sh train calibrate          # threshold bands (printed, never written)
scripts/run.sh train export             # labelled class folders for Create ML / fine-tuning
```

### Rules for session agents

- **Verify, don't classify.** The question is always "is this the enrolled
  animal shown in the reference images?" — never "what animal is this?"
- **`skip` unusable frames.** Blurry, too dark, no animal visible → skip.
  Never guess from context.
- **Say what you see in audits.** Describe the visual evidence. The tool
  computes agreement; you don't assert it.
- **Never answer "where is the animal?" from frames.** Frames show a camera
  view, not a zone. Only the state machine maps cameras to zones.
- **Draining the review queue is not optional.** Events the local models
  refused to decide have no other path to a verdict.

## Coordination protocol

- **Don't duplicate work.** Check if another agent is active before starting.
- **Commit only framework code** to `main`. No site-specific configs, no
  reference photos, no database files, no Ring device IDs.
- **Tests must stay hermetic.** Fake external clients, use `tmp_path`, never
  call Ring or any network service.
- **Update docs in the same commit** when you change a rule, threshold,
  payload field, or endpoint.
- **The state machine is deterministic.** Given the same observations it
  always produces the same transitions. Add a test before changing rules.

## Future local model improvements

The verdict archive produced by AI sessions enables:

- **Create ML classifier**: 50+ confirmed photos → binary classifier on the
  Neural Engine. Near-instant, free, retrain when appearance changes.
- **Core ML DINOv2**: convert the PyTorch ViT-S to Core ML for ANE
  acceleration (currently runs on MPS).
- **Apple Foundation Models** (macOS 27+): on-device multimodal fallback for
  ambiguous frames, replacing any cloud dependency entirely.
- **Per-camera empty-scene baselines**: embed a known-empty frame per camera,
  skip when new frames match — attacks the majority of non-animal events.

The goal: every AI session that reviews frames pushes the local models closer
to handling 100% of events autonomously.
