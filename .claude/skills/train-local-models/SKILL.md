---
name: train-local-models
description: Improve Animal Tracker's on-device models (reference gallery, DINOv2 thresholds, eval set) from the verdicts sessions have recorded. Use when the user asks how accurate detection is, why events need review, to retrain/recalibrate, or after adding reference photos or cameras.
---

# Train the local models

The local stack is a fixed Apple Vision animal gate plus DINOv2 embeddings compared to a
reference gallery. "Training" means three things, all driven by verdicts that humans or
review sessions recorded — **never** by the local models' own output (that would teach the
model what it already believes):

- **Gallery:** crops of the animal as the cameras actually see it (angles, IR, distance).
- **Thresholds:** the similarity bands that mean accept / review / skip.
- **Eval set:** frozen, labelled frames so a change can be measured before it ships.

## Steps

1. `scripts/run.sh train status` — verdict counts, how many events the local models decided
   this week, review queue, audit agreement, gallery size, current thresholds, and ranked
   next steps. Start from its recommendations.
2. If there's a review queue or fewer than ~20 audits, do the **review-frames** skill first.
   More verdicts are the raw material for everything below.
3. **Gallery:** `scripts/run.sh train harvest` (plan only) shows what would be selected,
   stratified by camera and day/IR. Then `scripts/run.sh train evaluate` measures held-out
   separation old vs new. Only if the new gallery separates better, ask the user, then
   `scripts/run.sh train harvest --apply`.
4. **Thresholds:** `scripts/run.sh train calibrate` replays the archive and prints bands with
   precision/recall. It never writes settings. Propose a change to the user with the numbers
   (e.g. "accept 0.37 → 0.35: recall 67% → 72%, false positives 1.7% → 2.4%"), and edit
   `detector.pipeline.*` in the data-dir `settings.yaml` only when they agree. Restart the
   API afterwards.
5. **Eval set:** `scripts/run.sh train eval-set` after large verdict batches, so future
   changes have a fixed yardstick.

## What moves the numbers

- Night/IR, lying-down and far-away references help more than more daylight close-ups.
- One camera dominating the gallery hurts; harvest is stratified to prevent it.
- The gate misses still animals, which is why a clean gate never auto-skips a
  high-similarity event — don't "fix" that by enabling `skip_on_absent`.
- Enable `not_winston_threshold` only when there are enough labelled other-animal events
  to measure it; until then uncertain animals go to review, which is correct.
