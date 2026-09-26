---
name: review-frames
description: Drain the Animal Tracker review queue and audit the local models — view staged camera frames, decide whether the enrolled animal is in them, record verdicts. Use for scheduled detection sessions, when the user asks to "review", "check pending events", "run detection", or when `train status` reports a review queue or few audits.
---

# Review frames (session detection)

Local models (Apple Vision + DINOv2) decide most events. The ones they won't decide wait
in a queue, and **nothing else will ever give those events a verdict** — draining it is
the job. Every verdict you record also becomes training data for the local models, so
accuracy matters more than speed.

## The question

For every event, answer exactly one question: **is the animal in these frames the
enrolled animal shown in the reference images?** Not "what animal is this", not "where is
it". You never decide location; the state machine does that from your observation.

## Drain the queue

1. `scripts/run.sh detect list --sheets` — prints each pending event, the verification
   question and the verdict JSON schema, and writes one contact sheet per event plus a
   reference sheet. **Read the reference sheet first**, then each event's `sheet.jpg`.
2. For each event decide:
   - Frames unusable (black, blurred, animal not visible, only a shadow) → **skip** with a
     specific reason. A skip is not a "no".
   - Otherwise fill the schema from what you can see: `animal_present`,
     `is_winston_confidence` (it is the enrolled animal — the field name is historical),
     `visual_similarity`, `size_appearance_compatible`, matched / mismatched features.
     Name concrete features (coat colour, markings, collar, ear shape, size relative to
     furniture). Low confidence is fine; guessing high is not.
3. Write all answers to one scratch file and record them together:
   `[{"key": "...", "verdict": {...}}, {"key": "...", "skip": "only a dark blur at the edge"}]`
   → `scripts/run.sh detect record-batch verdicts.json`
4. `scripts/run.sh detect status` should show the queue shrinking.

## Audit the local models (spot-check)

1. `scripts/run.sh detect audit --sheets` samples recent events the local models decided
   **without telling you what they decided**. Keep it that way — don't look it up first.
2. For each, say what you *see*: `winston` (the enrolled animal), `not_winston` (a different
   animal), `no_animal`, or `uncertain`. Write `[{"key": "...", "saw": "...", "notes": "..."}]`.
3. `scripts/run.sh detect audit-record audits.json`, then `scripts/run.sh detect audit-summary`.
   Report disagreements to the user; don't change thresholds yourself.

## Other animals

If the frames show a different animal (raccoon, cat, deer), the verdict is still just "not
the enrolled animal". Afterwards `scripts/run.sh animals candidates` lists them for species
labelling.

## Rules

- Never record a verdict for frames you did not view.
- Never answer from context ("it's 3am, so it's probably inside") — only from pixels.
- Never touch transitions, the tracker, or the database directly.
