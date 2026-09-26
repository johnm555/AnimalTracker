from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from src.api import AppContext, create_app
from src.db import Database
from src.observation import utcnow


def payload(run_id="test-run"):
    end = utcnow() - timedelta(minutes=1)
    return {"run_id": run_id, "reviewer": "claude-session", "started_at": (end - timedelta(seconds=90)).isoformat(),
            "finished_at": end.isoformat(), "status": "completed", "events_reviewed": 12, "sheets_viewed": 13}


@pytest.fixture
def client(tmp_path, topology, monkeypatch):
    monkeypatch.setenv("WINSTON_API_TOKEN", "test-runs")
    ctx = AppContext.build(settings={"notifications": {"backend": "log"}}, topology=topology,
                           db_path=str(tmp_path / "runs.db"))
    with TestClient(create_app(ctx)) as c:
        c.headers.update({"Authorization": "Bearer test-runs"})
        yield c


def test_retry_idempotence_conflict_and_no_evidence_side_effects(client):
    data = payload()
    first = client.post("/winston/detection-runs", json=data)
    assert first.status_code == 201
    assert first.json()["wall_seconds"] == 90
    assert client.post("/winston/detection-runs", json=data).json() == first.json()
    assert client.post("/winston/detection-runs", json={**data, "events_reviewed": 99}).status_code == 409
    report = client.get("/winston/detection-runs").json()
    assert report["summary"]["runs"] == 1
    assert report["summary"]["events_reviewed"] == 12
    assert report["summary"]["sheets_viewed"] == 13
    assert report["summary"]["wall_seconds"] == 90
    assert client.app.state.ctx.db.list_observations() == []
    assert client.app.state.ctx.db.list_transitions() == []
    assert client.app.state.ctx.db.list_notifications() == []


@pytest.mark.parametrize("change", [
    {"events_reviewed": -1}, {"events_reviewed": True}, {"sheets_viewed": 1.5},
    {"reviewer": "  "}, {"run_id": "../invalid"}, {"status": "running"},
    {"started_at": "2026-09-20T00:00:00"},
    {"finished_at": "2000-01-01T00:00:00Z"},
    {"finished_at": "2099-01-01T00:00:00Z"},
])
def test_invalid_metrics_rejected(client, change):
    assert client.post("/winston/detection-runs", json={**payload(), **change}).status_code == 422
    assert client.get("/winston/detection-runs").json()["summary"]["runs"] == 0


def test_auth_and_summary_counts_beyond_display_limit(client):
    data = payload()
    client.headers.pop("Authorization")
    assert client.post("/winston/detection-runs", json=data).status_code == 401
    client.headers.update({"Authorization": "Bearer test-runs"})
    for i, status in enumerate(("completed", "partial", "failed")):
        assert client.post("/winston/detection-runs", json={**data, "run_id": str(i), "status": status}).status_code == 201
    report = client.get("/winston/detection-runs?limit=1").json()
    assert report["truncated"] and len(report["runs"]) == 1
    assert report["summary"]["runs"] == 3
    assert report["summary"]["events_reviewed"] == 36
    assert report["summary"]["failed_runs"] == report["summary"]["partial_runs"] == 1


def test_runs_persist_and_empty_window_is_not_an_invented_run(client):
    ctx = client.app.state.ctx
    assert client.post("/winston/detection-runs", json=payload()).status_code == 201
    other = Database(ctx.db.path)
    try:
        now = utcnow()
        assert other.detection_runs_report(now-timedelta(hours=1), now)["summary"]["runs"] == 1
        empty = other.detection_runs_report(now, now+timedelta(hours=1))
        assert empty["summary"]["runs"] == 0 and empty["runs"] == []
    finally:
        other.close()
