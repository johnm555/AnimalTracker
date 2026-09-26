# Detection quality review (P4-04)

The terminal dashboard and JSON endpoint measure Winston verification scores
against explicit reviews. They do not change live tracking or correct past
transitions. No production labels are created automatically.

## Review workflow

Inspect the event frames alongside enrolled reference photos first. Use the
observation ID from the detection result or `detect calibration` export:

```sh
scripts/run.sh quality review 123 --label winston --reviewer john --notes "Visible head, ears and body match enrolled Winston references."
scripts/run.sh quality report --hours 24
scripts/run.sh quality report --hours 168 --json
```

Labels: `winston`, `not_winston`, `uncertain`. `not_winston` requires sufficient
visible evidence; covered cameras, missing frames and occluded animals should
remain uncertain. Reviewer names are self-reported audit provenance, not
verified identities. An agent review should use its agent name, never claim
to be the owner. This is a review sample, not independent ground truth or a
production accuracy certification.

`--url http://mini-host:8420` before `review` or `report` selects a backend.
Writes require the configured `WINSTON_API_TOKEN`; the wrapper loads `.env`.
The CLI only writes through the running API, with no offline tracker fallback.
An unreachable server or HTTP error exits nonzero without claiming success.

## API and persistence

- `POST /winston/observations/{id}/reviews`: JSON `{label, reviewer, notes}`;
  returns 201 and the review record. Bearer-authenticated like other writes.
- `GET /winston/observations/{id}/reviews`: full append-only review history.
- `GET /winston/quality?hours=24`: overall/per-camera counts and rates, capture
  time window and the current configured detection threshold. Hours: >0–8760.

`observation_reviews` stores observation ID, label, reviewer, evidence notes,
and server review timestamp. Corrections append a record; latest insertion
wins for reporting. A missing observation returns 404. Blank reviewer/notes
and unsupported labels return 422. Reviews survive restart.

## Counting rules

Only the newest verdict for each Ring device/event is counted, matching the
calibration export. Missing event identities remain independent observations.
A review applies to its exact observation ID; a revised detector verdict
requires a new explicit review. Old reviews remain available for audit.
The capture-time window is applied after choosing the newest verdict.

A predicted positive means `winston_probability >= confidence_threshold`.
This measures detector score decisions, not the tracker's subsequent travel
checks or confirmation logic. Same-named Winston 5000 devices are reported
separately by stable Ring device ID.

| Metric | Calculation |
|---|---|
| Precision | TP / (TP + FP) |
| Recall | TP / (TP + FN) |
| False-positive rate | FP / (FP + TN) |
| False-negative rate | FN / (FN + TP) |

Unreviewed and uncertain observations have separate counts and are excluded
from all four denominators. An empty denominator returns null, never 100%.
Repeated reviews and superseded verdicts do not inflate the sample size.

These metrics cannot measure motion events Ring never captured, expired frames
without an observation, or Winston being off screen. Rates only describe the
explicitly reviewed sample; threshold changes reclassify the same stored scores
and the response reports that threshold. No automatic tuning is performed.
