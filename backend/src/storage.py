"""Disk retention for frames, clips and thumbnails on the Mac Mini.

What lives where, and how long it is kept (`storage:` in settings.yaml):

    backend/ring_downloads/*.mp4            raw Ring clips            archive_retention_hours
    backend/ring_downloads/captures/*/      check-camera evidence     archive_retention_hours
    backend/staging/archive/<date>/<key>/   reviewed frames + sheet   archive_retention_hours
    backend/staging/pending/<key>/          frames awaiting a verdict pending_retention_hours
    backend/staging/thumbnails/<date>/      one small JPEG per event  thumbnail_retention_days
    backend/winston.db                      observations, transitions kept forever (tiny)

Before an archived event is deleted, a thumbnail of it is written if it is
still inside the thumbnail window, so the last week of sightings stays
browsable (watch app / status page) after the full-size frames are gone.

A pending event that expires unreviewed is recorded in the ledger as
`skipped` ("expired unreviewed") — never as an observation. The frames are
deleted; the fact that nobody looked is kept.

Everything is dry-run unless `apply=True`. `Poller` calls `run_cleanup()`
hourly; `scripts/cleanup.py` (launchd, 03:00) runs the same code daily so the
disk is protected even when the API process is down.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .observation import parse_timestamp
from .paths import data_dir, staging_dir as default_staging_dir, ring_downloads_dir as default_downloads_dir

log = logging.getLogger(__name__)

GB = 1024 ** 3


@dataclass
class StorageSettings:
    archive_retention_hours: float = 72.0
    pending_retention_hours: float = 24.0
    thumbnail_retention_days: float = 7.0
    max_storage_gb: float = 10.0
    thumbnail_width_px: int = 320
    staging_dir: Path = field(default_factory=default_staging_dir)
    downloads_dir: Path = field(default_factory=default_downloads_dir)

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> "StorageSettings":
        s = settings or {}
        st = s.get("storage") or {}

        return cls(
            archive_retention_hours=float(st.get("archive_retention_hours", 72)),
            pending_retention_hours=float(st.get("pending_retention_hours", 24)),
            thumbnail_retention_days=float(st.get("thumbnail_retention_days", 7)),
            max_storage_gb=float(st.get("max_storage_gb", 10)),
            thumbnail_width_px=int(st.get("thumbnail_width_px", 320)),
            staging_dir=default_staging_dir(s),
            downloads_dir=default_downloads_dir(s),
        )

    @property
    def pending(self) -> Path:
        return self.staging_dir / "pending"

    @property
    def archive(self) -> Path:
        return self.staging_dir / "archive"

    @property
    def thumbnails(self) -> Path:
        return self.staging_dir / "thumbnails"


# --------------------------------------------------------------------------- #
# Measuring
# --------------------------------------------------------------------------- #

def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def usage(cfg: StorageSettings) -> dict[str, Any]:
    """Bytes per area, total, and whether the max_storage_gb ceiling is exceeded."""
    areas = {
        "clips": dir_size(cfg.downloads_dir),
        "pending": dir_size(cfg.pending),
        "archive": dir_size(cfg.archive),
        "thumbnails": dir_size(cfg.thumbnails),
    }
    total = sum(areas.values())
    free = None
    try:
        free = shutil.disk_usage(data_dir()).free
    except OSError:
        pass
    return {
        "bytes": areas,
        "total_bytes": total,
        "total_gb": round(total / GB, 3),
        "max_storage_gb": cfg.max_storage_gb,
        "over_limit": cfg.max_storage_gb > 0 and total > cfg.max_storage_gb * GB,
        "disk_free_gb": round(free / GB, 2) if free is not None else None,
    }


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n} B"


# --------------------------------------------------------------------------- #
# Thumbnails
# --------------------------------------------------------------------------- #

def _event_timestamp(event_dir: Path) -> datetime | None:
    side = event_dir / "event.json"
    try:
        return parse_timestamp(json.loads(side.read_text())["timestamp"])
    except (OSError, ValueError, KeyError):
        pass
    # Fall back to the directory name: <camera>__<YYYYmmddTHHMMSSZ>__<id>
    try:
        stamp = event_dir.name.split("__")[1]
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return None


def make_thumbnail(cfg: StorageSettings, event_dir: Path) -> Path | None:
    """Write thumbnails/<date>/<key>.jpg from the event's middle frame (idempotent)."""
    try:
        import cv2
    except ImportError:  # pragma: no cover
        return None
    ts = _event_timestamp(event_dir)
    if ts is None:
        return None
    out_dir = cfg.thumbnails / ts.strftime("%Y-%m-%d")
    out = out_dir / f"{event_dir.name}.jpg"
    if out.is_file():
        return out
    frames = sorted(event_dir.glob("frame-*.jpg"))
    if not frames:
        return None
    src = frames[len(frames) // 2]
    img = cv2.imread(str(src))
    if img is None:
        return None
    h, w = img.shape[:2]
    tw = cfg.thumbnail_width_px
    small = cv2.resize(img, (tw, max(1, int(h * tw / w))))
    out_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), small, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return out


# --------------------------------------------------------------------------- #
# Planning and applying
# --------------------------------------------------------------------------- #

@dataclass
class Action:
    kind: str
    """delete_clip | delete_capture | delete_archive | expire_pending | delete_thumbnail | make_thumbnail"""
    path: Path
    bytes: int
    age_hours: float
    note: str = ""


@dataclass
class CleanupReport:
    applied: bool
    actions: list[Action] = field(default_factory=list)
    freed_bytes: int = 0
    thumbnails_written: int = 0
    expired_events: list[str] = field(default_factory=list)
    usage_before: dict[str, Any] = field(default_factory=dict)
    usage_after: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        by_kind: dict[str, tuple[int, int]] = {}
        for a in self.actions:
            n, b = by_kind.get(a.kind, (0, 0))
            by_kind[a.kind] = (n + 1, b + a.bytes)
        parts = [f"{k}: {n} ({human(b)})" for k, (n, b) in sorted(by_kind.items())]
        verb = "freed" if self.applied else "would free"
        return f"{'APPLIED' if self.applied else 'DRY RUN'} — {verb} {human(self.freed_bytes)}; " + (
            "; ".join(parts) if parts else "nothing to do")

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "freed_bytes": self.freed_bytes,
            "thumbnails_written": self.thumbnails_written,
            "expired_events": self.expired_events,
            "actions": [{"kind": a.kind, "path": str(a.path), "bytes": a.bytes,
                         "age_hours": round(a.age_hours, 1), "note": a.note} for a in self.actions],
            "usage_before": self.usage_before,
            "usage_after": self.usage_after,
            "warnings": self.warnings,
        }


def _age_hours(path: Path, now: float) -> float:
    try:
        return (now - path.stat().st_mtime) / 3600.0
    except OSError:
        return 0.0


def _rm(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


MarkExpired = Callable[[str, str, str], None]
"""(device_id, event_id, reason) -> None; records an expired pending event as skipped."""


def run_cleanup(cfg: StorageSettings, apply: bool = False, now: float | None = None,
                mark_expired: MarkExpired | None = None) -> CleanupReport:
    """Plan (and with apply=True perform) retention cleanup. Never touches the DB rows."""
    now = now or time.time()
    report = CleanupReport(applied=apply, usage_before=usage(cfg))
    archive_h = cfg.archive_retention_hours
    pending_h = cfg.pending_retention_hours
    thumb_h = cfg.thumbnail_retention_days * 24.0

    # 1. Raw clips and one-off captures.
    if cfg.downloads_dir.is_dir() and archive_h > 0:
        for p in sorted(cfg.downloads_dir.glob("*.mp4")):
            age = _age_hours(p, now)
            if age > archive_h:
                report.actions.append(Action("delete_clip", p, dir_size(p), age))
        captures = cfg.downloads_dir / "captures"
        if captures.is_dir():
            for p in sorted(captures.iterdir()):
                if p.is_dir() and _age_hours(p, now) > archive_h:
                    report.actions.append(Action("delete_capture", p, dir_size(p), _age_hours(p, now)))

    # 2. Archived (reviewed) events: thumbnail first if still inside the window, then delete.
    if cfg.archive.is_dir() and archive_h > 0:
        for day_dir in sorted(cfg.archive.iterdir()):
            if not day_dir.is_dir():
                continue
            for ev in sorted(day_dir.iterdir()):
                if not ev.is_dir():
                    continue
                ts = _event_timestamp(ev)
                age = (now - ts.timestamp()) / 3600.0 if ts else _age_hours(ev, now)
                if age <= archive_h:
                    continue
                if age <= thumb_h:
                    thumb = cfg.thumbnails / (ts.strftime("%Y-%m-%d") if ts else "unknown") / f"{ev.name}.jpg"
                    if not thumb.is_file():
                        report.actions.append(Action("make_thumbnail", ev, 0, age, str(thumb)))
                report.actions.append(Action("delete_archive", ev, dir_size(ev), age))

    # 3. Pending events nobody reviewed in time: expire, never "not Winston".
    if cfg.pending.is_dir() and pending_h > 0:
        for ev in sorted(cfg.pending.iterdir()):
            if not ev.is_dir() or ev.name.endswith(".part"):
                continue
            ts = _event_timestamp(ev)
            age = (now - ts.timestamp()) / 3600.0 if ts else _age_hours(ev, now)
            if age > pending_h:
                report.actions.append(Action("expire_pending", ev, dir_size(ev), age, "expired unreviewed"))

    # 4. Thumbnails past their own window.
    if cfg.thumbnails.is_dir() and thumb_h > 0:
        for p in sorted(cfg.thumbnails.rglob("*.jpg")):
            age = _age_hours(p, now)
            if age > thumb_h:
                report.actions.append(Action("delete_thumbnail", p, dir_size(p), age))

    report.freed_bytes = sum(a.bytes for a in report.actions if a.kind != "make_thumbnail")

    if apply:
        for a in report.actions:
            try:
                if a.kind == "make_thumbnail":
                    if make_thumbnail(cfg, a.path):
                        report.thumbnails_written += 1
                    continue
                if a.kind == "expire_pending":
                    side = a.path / "event.json"
                    if mark_expired is not None and side.is_file():
                        try:
                            d = json.loads(side.read_text())
                            mark_expired(d["device_id"], d["event_id"], "expired unreviewed after "
                                         f"{cfg.pending_retention_hours:.0f}h")
                            report.expired_events.append(a.path.name)
                        except (ValueError, KeyError) as e:
                            report.warnings.append(f"could not mark {a.path.name} expired: {e}")
                _rm(a.path)
            except OSError as e:
                report.warnings.append(f"{a.kind} {a.path}: {e}")
        # Remove empty day folders left behind.
        for root in (cfg.archive, cfg.thumbnails):
            if root.is_dir():
                for d in root.iterdir():
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()
        report.usage_after = usage(cfg)
    else:
        report.usage_after = report.usage_before

    u = report.usage_after
    if u.get("over_limit"):
        report.warnings.append(
            f"storage {u['total_gb']} GB exceeds max_storage_gb={cfg.max_storage_gb}; "
            "lower archive_retention_hours or check what is filling ring_downloads")
    if u.get("disk_free_gb") is not None and u["disk_free_gb"] < 5:
        report.warnings.append(f"only {u['disk_free_gb']} GB free on the volume")
    return report


def log_report(report: CleanupReport, logger: logging.Logger = log) -> None:
    logger.info("cleanup: %s", report.summary())
    u = report.usage_after
    logger.info("storage: total %s (clips %s, pending %s, archive %s, thumbnails %s); free %s GB",
                human(u["total_bytes"]), human(u["bytes"]["clips"]), human(u["bytes"]["pending"]),
                human(u["bytes"]["archive"]), human(u["bytes"]["thumbnails"]), u.get("disk_free_gb"))
    for w in report.warnings:
        logger.warning("storage: %s", w)
    if report.expired_events:
        logger.warning("cleanup: %d pending event(s) expired unreviewed (recorded as skipped): %s",
                       len(report.expired_events), ", ".join(report.expired_events[:5]))
