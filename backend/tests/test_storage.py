"""Retention cleanup: dry run by default, thumbnails before deletion, expired pending -> skipped."""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.db import Database
from src.storage import StorageSettings, human, run_cleanup, usage

H = 3600.0


def _jpeg(path, w=64, h=48):
    import cv2
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.full((h, w, 3), 90, dtype=np.uint8))


def _event(root, age_hours, camera="backyard", now=None, event_id=None):
    """An event directory with 4 frames and a sidecar whose timestamp is age_hours old."""
    now = now or time.time()
    ts = datetime.fromtimestamp(now - age_hours * H, tz=timezone.utc)
    event_id = event_id or f"{int(ts.timestamp())}"
    key = f"{camera}__{ts.strftime('%Y%m%dT%H%M%SZ')}__{event_id}"
    d = root / ts.strftime("%Y-%m-%d") / key if root.name == "archive" else root / key
    for i in range(4):
        _jpeg(d / f"frame-{i}.jpg")
    (d / "event.json").write_text(json.dumps({
        "event_id": event_id, "device_id": f"dev-{camera}", "camera_id": camera,
        "timestamp": ts.isoformat(), "frames": [f"frame-{i}.jpg" for i in range(4)]}))
    return d


def _touch_age(path, age_hours, now):
    os.utime(path, (now - age_hours * H, now - age_hours * H))


@pytest.fixture
def tree(tmp_path):
    now = time.time()
    cfg = StorageSettings(staging_dir=tmp_path / "staging", downloads_dir=tmp_path / "ring_downloads",
                          archive_retention_hours=72, pending_retention_hours=24,
                          thumbnail_retention_days=7, max_storage_gb=10)
    # Clips: one fresh, one old.
    cfg.downloads_dir.mkdir()
    fresh_clip = cfg.downloads_dir / "fresh.mp4"; fresh_clip.write_bytes(b"x" * 1000)
    old_clip = cfg.downloads_dir / "old.mp4"; old_clip.write_bytes(b"x" * 5000); _touch_age(old_clip, 100, now)
    # Archive: recent (keep), 4 days old (delete + thumbnail), 10 days old (delete, no thumbnail).
    keep = _event(cfg.archive, 10, "backyard", now, "keep")
    four_days = _event(cfg.archive, 96, "backyard", now, "fourdays")
    ten_days = _event(cfg.archive, 240, "front-door", now, "tendays")
    # Pending: fresh (keep) and stale (expire).
    pend_fresh = _event(cfg.pending, 2, "backyard", now, "pfresh")
    pend_stale = _event(cfg.pending, 30, "backyard", now, "pstale")
    (cfg.pending / "half.part").mkdir()
    # Thumbnails: one inside the window, one outside.
    t_new = cfg.thumbnails / "x" / "new.jpg"; _jpeg(t_new)
    t_old = cfg.thumbnails / "y" / "old.jpg"; _jpeg(t_old); _touch_age(t_old, 24 * 9, now)
    return dict(cfg=cfg, now=now, fresh_clip=fresh_clip, old_clip=old_clip, keep=keep, four_days=four_days,
                ten_days=ten_days, pend_fresh=pend_fresh, pend_stale=pend_stale, t_new=t_new, t_old=t_old)


def test_dry_run_plans_but_deletes_nothing(tree):
    cfg, now = tree["cfg"], tree["now"]
    report = run_cleanup(cfg, apply=False, now=now)
    kinds = sorted((a.kind, a.path.name) for a in report.actions)
    assert kinds == sorted([
        ("delete_clip", "old.mp4"),
        ("make_thumbnail", tree["four_days"].name),
        ("delete_archive", tree["four_days"].name),
        ("delete_archive", tree["ten_days"].name),
        ("expire_pending", tree["pend_stale"].name),
        ("delete_thumbnail", "old.jpg"),
    ])
    assert report.freed_bytes > 5000
    assert not report.applied
    # Nothing touched.
    for p in ("old_clip", "four_days", "ten_days", "pend_stale", "t_old"):
        assert tree[p].exists()
    assert "would free" in report.summary()


def test_apply_deletes_thumbnails_and_marks_expired(tree, tmp_path):
    cfg, now = tree["cfg"], tree["now"]
    db = Database(tmp_path / "s.db")
    from types import SimpleNamespace
    stale_ts = datetime.now(timezone.utc) - timedelta(hours=30)
    db.mark_event(SimpleNamespace(device_id="dev-backyard", event_id="pstale", camera_id="backyard",
                                  timestamp=stale_ts), "staged")
    marks = []

    def mark_expired(device_id, event_id, reason):
        marks.append((device_id, event_id, reason))
        db.mark_event_by_id(device_id, event_id, "skipped", None, reason)

    report = run_cleanup(cfg, apply=True, now=now, mark_expired=mark_expired)
    assert report.applied and report.freed_bytes > 5000
    # Deleted.
    assert not tree["old_clip"].exists()
    assert not tree["four_days"].exists() and not tree["ten_days"].exists()
    assert not tree["pend_stale"].exists() and not tree["t_old"].exists()
    # Kept.
    assert tree["fresh_clip"].exists() and tree["keep"].exists()
    assert tree["pend_fresh"].exists() and tree["t_new"].exists()
    assert (cfg.pending / "half.part").exists()  # in-flight staging is never touched
    # Thumbnail written for the 4-day-old event only (10-day-old is past the 7-day window).
    thumbs = sorted(p.name for p in cfg.thumbnails.rglob("*.jpg"))
    assert thumbs == sorted(["new.jpg", tree["four_days"].name + ".jpg"])
    assert report.thumbnails_written == 1
    import cv2
    t = cv2.imread(str(next(cfg.thumbnails.rglob(tree["four_days"].name + ".jpg"))))
    assert t.shape[1] == cfg.thumbnail_width_px
    # Expired pending event recorded as skipped, never as a verdict.
    assert marks == [("dev-backyard", "pstale", "expired unreviewed after 24h")]
    assert db.count_events_by_status() == {"skipped": 1}
    assert db.list_observations() == []
    assert report.expired_events == [tree["pend_stale"].name]
    # Empty day folders are removed.
    assert all(any(d.iterdir()) for d in cfg.archive.iterdir() if d.is_dir())


def test_idempotent_second_run(tree):
    cfg, now = tree["cfg"], tree["now"]
    run_cleanup(cfg, apply=True, now=now)
    again = run_cleanup(cfg, apply=True, now=now)
    assert again.actions == [] and again.freed_bytes == 0


def test_usage_and_over_limit_warning(tree):
    cfg, now = tree["cfg"], tree["now"]
    u = usage(cfg)
    assert set(u["bytes"]) == {"clips", "pending", "archive", "thumbnails"}
    assert u["total_bytes"] == sum(u["bytes"].values()) and not u["over_limit"]
    cfg.max_storage_gb = 0.000001  # ~1 KB
    report = run_cleanup(cfg, apply=False, now=now)
    assert any("exceeds max_storage_gb" in w for w in report.warnings)
    assert usage(cfg)["over_limit"]


def test_zero_retention_disables_that_area(tree):
    cfg, now = tree["cfg"], tree["now"]
    cfg.archive_retention_hours = 0
    cfg.pending_retention_hours = 0
    report = run_cleanup(cfg, apply=False, now=now)
    assert {a.kind for a in report.actions} == {"delete_thumbnail"}


def test_settings_from_yaml_dict():
    cfg = StorageSettings.from_settings({"storage": {"archive_retention_hours": 48, "max_storage_gb": 2},
                                         "ring": {"download_dir": "./dl"}, "detector": {"staging_dir": "./st"}})
    assert cfg.archive_retention_hours == 48 and cfg.max_storage_gb == 2
    assert cfg.pending_retention_hours == 24  # default
    assert cfg.downloads_dir.name == "dl" and cfg.staging_dir.name == "st" and cfg.pending.name == "pending"


def test_human_sizes():
    assert human(512) == "512 B" and human(2048) == "2.0 KB" and human(3 * 1024 ** 3) == "3.0 GB"
