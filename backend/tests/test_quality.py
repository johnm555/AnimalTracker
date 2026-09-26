from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from src.api import AppContext, create_app
from src.db import Database
from src.observation import Observation, utcnow
from src.quality import quality_report


def add(db, score, label=None, *, device="1", event=None, camera="backyard", at=None):
    extra = {"ring_device_id": device, "ring_event_id": event} if event else {}
    obs = Observation(camera_id=camera, timestamp=at or utcnow(), winston_probability=score, extra=extra)
    db.insert_observation(obs)
    if label:
        db.insert_observation_review(obs.id, label, "human-reviewer", "Inspected frames against enrolled references.")
    return obs


def report(db):
    now = utcnow()
    return quality_report(db, 0.7, now - timedelta(days=1), now)


def test_confusion_counts_uncertainty_and_camera_identity():
    db = Database()
    try:
        add(db, 0.7, "winston", event="a")  # boundary is accepted
        add(db, 0.9, "not_winston", event="b")
        add(db, 0.2, "not_winston", event="c")
        add(db, 0.6, "winston", event="d")
        add(db, 0.95, "uncertain", event="e")
        add(db, 0.99, event="f")
        add(db, 0.8, "winston", device="2", event="a")
        result = report(db)
        t = result["totals"]
        assert (t["true_positive"], t["false_positive"], t["true_negative"], t["false_negative"]) == (2, 1, 1, 1)
        assert t["uncertain"] == 1 and t["unreviewed"] == 1 and t["reviewed"] == 5
        assert t["precision"] == pytest.approx(2 / 3)
        assert t["recall"] == pytest.approx(2 / 3)
        assert t["false_positive_rate"] == 0.5
        assert t["false_negative_rate"] == pytest.approx(1 / 3)
        assert len(result["cameras"]) == 2  # same logical camera, independent Ring devices
    finally:
        db.close()


def test_latest_review_and_verdict_do_not_duplicate_evidence(tmp_path):
    path = tmp_path / "reviews.db"
    db = Database(path)
    old = add(db, 0.9, "not_winston", event="a")
    db.insert_observation_review(old.id, "uncertain", "owner", "View was occluded; withdrawing definitive negative.")
    assert report(db)["totals"]["uncertain"] == 1
    assert len(db.list_observation_reviews(old.id)) == 2
    newer = add(db, 0.8, event="a")
    assert report(db)["totals"]["observations"] == 1
    assert report(db)["totals"]["unreviewed"] == 1  # old label is not silently inherited
    db.insert_observation_review(newer.id, "winston", "owner", "Confirmed visible head and body against references.")
    db.close()
    db = Database(path)
    try:
        assert report(db)["totals"]["true_positive"] == 1
        assert report(db)["totals"]["false_positive"] == 0
        assert len(list(db.iter_observations_in_ingestion_order())) == 2
        assert len(db.list_observation_reviews(old.id)) == 2
        assert db.list_transitions() == [] and db.list_notifications() == []
    finally:
        db.close()


def test_empty_denominators_and_capture_time_window():
    db = Database()
    try:
        add(db, 0.9, "uncertain")
        add(db, 0.9, "not_winston", at=utcnow() - timedelta(days=2))
        add(db, 0.9, "not_winston", at=utcnow() + timedelta(days=1))
        t = report(db)["totals"]
        assert t["observations"] == 1
        assert t["reviewed"] == 0
        assert all(t[key] is None for key in ("precision", "recall", "false_positive_rate", "false_negative_rate"))
    finally:
        db.close()


def test_review_api_auth_validation_and_no_tracker_side_effects(tmp_path, topology, monkeypatch):
    monkeypatch.setenv("ANIMAL_TRACKER_API_TOKEN", "review-token")
    ctx = AppContext.build(settings={"notifications": {"backend": "log"}},
                           topology=topology, db_path=str(tmp_path / "api.db"))
    obs = add(ctx.db, 0.95)
    path = f"/tracker/observations/{obs.id}/reviews"
    body = {"label": "winston", "reviewer": "owner", "notes": "Reference-matched visual evidence."}
    with TestClient(create_app(ctx)) as client:
        assert client.post(path, json=body).status_code == 401
        client.headers.update({"Authorization": "Bearer review-token"})
        assert client.post(path, json={**body, "label": "bear"}).status_code == 422
        assert client.post(path, json={**body, "notes": "  "}).status_code == 422
        assert client.post("/tracker/observations/999/reviews", json=body).status_code == 404
        assert client.get("/tracker/observations/999/reviews").status_code == 404
        created = client.post(path, json=body)
        assert created.status_code == 201
        assert created.json()["observation_id"] == obs.id
        assert len(client.get(path).json()["reviews"]) == 1
        assert client.get("/tracker/quality").json()["totals"]["true_positive"] == 1
        for hours in (0, -1, 8761):
            assert client.get("/tracker/quality", params={"hours": hours}).status_code == 422
        assert client.get("/tracker/location").json()["state"] == "unknown"
        assert ctx.db.list_transitions() == [] and ctx.db.list_notifications() == []
