# Training Guide

How the tracker gets better at recognising *your* animal, using the AI
subscription you already have instead of per-token API billing.

## The loop

```
Ring event ──▶ local models (Apple Vision gate + DINOv2 vs reference gallery)
                 │ confident                    │ unsure
                 ▼                              ▼
            observation                   review queue
                                                │  Claude / Codex session
                                                ▼  (scripts/review_session.sh)
                                          reviewer verdicts ──────┐
                                                                  ▼
          new reference crops  ◀── train harvest      train calibrate ──▶ threshold bands
          frozen eval set      ◀── train eval-set     train export    ──▶ Create ML / fine-tuning
```

One rule makes this honest: **only verdicts from someone who looked at the
frames are training data** — owner confirmations, session verdicts, session
audits. A local model's own accept or skip is never fed back, or the model would
learn what it already believes and every evaluation would measure it against
itself.

## Where you stand

```bash
scripts/run.sh train status
```

Shows verdict counts by source, the share of this week's events the local
models decided on their own, the review queue, audit agreement, gallery size and
thresholds — and a ranked list of what to do next.

| Metric | Healthy | If not |
|---|---|---|
| Review queue | drains within an hour | install the review schedule (`install-launchd.sh review claude`) |
| Audits | 20+ per week | sessions run `detect audit`; the review session does 6 per run |
| Audit agreement | ≥ 90% | `detect audit-summary` shows which band is wrong → recalibrate |
| Decided locally | rises over the first weeks | more varied references (night, lying down, far away) |

## Steps, and when to run them

**Every day (automatic):** the review session drains the queue and audits a
sample of local decisions. Nothing to do.

**After ~50 new reviewed sightings, or a new camera:** grow the gallery.

```bash
scripts/run.sh train harvest       # plan: which frames, stratified by camera and day/IR
scripts/run.sh train evaluate      # held-out separation, current vs proposed gallery
scripts/run.sh train harvest --apply   # only if evaluate says it separates better
```

**After a gallery change, or when audit agreement drops:** recalibrate.

```bash
scripts/run.sh train calibrate
```

It replays the archive against reviewer verdicts and prints precision/recall at
each threshold. It never writes settings: you pick the band and edit
`detector.pipeline` in the data-dir `settings.yaml`, then restart the API.
Trade-offs to weigh:

- **accept_threshold** — above it (with the gate seeing an animal) a sighting is
  recorded with no review. Too low invents sightings; too high sends more to review.
- **rescue_threshold** — the gate misses still animals, so a clean gate with a
  score above this goes to review instead of being skipped. Lowering it costs
  review time; raising it loses real sightings.
- **not_winston_threshold** — leave `null` until you have enough labelled
  *other* animals to measure it. Uncertain animals going to review is correct.

**Seasonally, or after moving cameras:** lighting changes what the cameras see.
Re-run `train full` (status → harvest plan → evaluate → calibrate).

**Before trusting any change:** freeze a yardstick.

```bash
scripts/run.sh train eval-set      # labelled frames, split by sequence, checksummed
```

Frames in `staging/archive` expire after 72 h; the eval set keeps labelled ones
permanently, split so that nothing a threshold was fitted on is used to test it.

## Train your own classifier

```bash
scripts/run.sh train eval-set
scripts/run.sh train export        # → <data dir>/exports/classifier/{Training,Testing}/<label>/
```

Labels: `target` (your animal), `other_animal`, `no_animal`. Open **Create ML →
Image Classifier**, choose `Training` and `Testing`, train, and export the
`.mlmodel` — it runs on the Neural Engine in milliseconds. The same folders work
for fine-tuning a local vision-language model or any image classifier.

## Using Claude and Codex

| Job | How |
|---|---|
| Drain the queue, audit (scheduled) | `scripts/install-launchd.sh review claude` or `review codex` |
| Same, by hand | ask "review the pending frames" (`review-frames` skill) |
| Decide what to train next | ask "how are the local models doing?" (`train-local-models` skill) |
| Label visiting animals | `scripts/run.sh animals candidates` in a session |

Sessions follow the same contract as the code: answer only "is this the
enrolled animal?", skip unusable frames instead of guessing, never record a
verdict for frames they didn't view, and never touch thresholds without the
owner.
