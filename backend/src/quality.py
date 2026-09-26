"""Review-based detection quality; never changes evidence or tracker state."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .db import Database


def _counts() -> dict[str, Any]:
    return dict(observations=0, unreviewed=0, uncertain=0, true_positive=0,
                false_positive=0, true_negative=0, false_negative=0)


def _rates(counts: dict[str, Any]) -> dict[str, Any]:
    tp, fp = counts["true_positive"], counts["false_positive"]
    tn, fn = counts["true_negative"], counts["false_negative"]
    counts["reviewed"] = tp + fp + tn + fn
    counts["precision"] = tp / (tp + fp) if tp + fp else None
    counts["recall"] = tp / (tp + fn) if tp + fn else None
    counts["false_positive_rate"] = fp / (fp + tn) if fp + tn else None
    counts["false_negative_rate"] = fn / (fn + tp) if fn + tp else None
    return counts


def quality_report(db: Database, threshold: float, since: datetime, until: datetime) -> dict[str, Any]:
    reviews = db.latest_observation_reviews()
    groups: dict[tuple[str, str | None], dict[str, Any]] = {}
    total = _counts()
    # Select newest verdict before filtering capture time. Never count a re-review
    # as another independent event, or borrow a label from a superseded verdict.
    for obs in db.iter_calibration_observations():
        if not since <= obs.timestamp < until:
            continue
        device = obs.extra.get("ring_device_id")
        device = str(device) if device is not None and str(device) else None
        group = groups.setdefault((obs.camera_id, device), _counts())
        review = reviews.get(obs.id)
        if review is None:
            bucket = "unreviewed"
        elif review["label"] == "uncertain":
            bucket = "uncertain"
        else:
            predicted = obs.winston_probability >= threshold
            positive = review["label"] == "winston"
            bucket = ("true_positive" if positive else "false_positive") if predicted else (
                "false_negative" if positive else "true_negative")
        for counts in (group, total):
            counts["observations"] += 1
            counts[bucket] += 1
    return {
        "since": since.isoformat(), "until": until.isoformat(), "threshold": threshold,
        "basis": "latest verdict per Ring event; latest explicit review of that observation",
        "scope": "reviewed detector scores, not tracker acceptance or missed uncaptured events",
        "totals": _rates(total),
        "cameras": [{"camera_id": camera, "ring_device_id": device, **_rates(counts)}
                    for (camera, device), counts in sorted(groups.items(), key=lambda item: (
                        item[0][0], item[0][1] or ""))],
    }
