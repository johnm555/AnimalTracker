#!/usr/bin/env python
"""Retention cleanup for frames, clips and thumbnails (dry run by default).

    scripts/run.sh cleanup              show what would be deleted and current usage
    scripts/run.sh cleanup --apply      delete it
    scripts/run.sh cleanup --json       machine-readable report (with or without --apply)
    scripts/run.sh cleanup --usage      just the storage numbers, no plan

Retention periods come from `storage:` in backend/config/settings.yaml:
archive_retention_hours (reviewed frames + raw clips), pending_retention_hours
(unreviewed frames; expired ones are recorded as `skipped`, never as a verdict),
thumbnail_retention_days, max_storage_gb (warning threshold). SQLite metadata
is never touched. The poller runs the same code hourly inside `run.sh api`;
this script is the daily belt-and-braces (launchd, 03:00 — see
scripts/install-launchd.sh cleanup) and works while the API is down.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from src.api import load_settings  # noqa: E402
from src.db import Database  # noqa: E402
from src.storage import StorageSettings, human, log_report, run_cleanup, usage  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", help="path to settings.yaml")
    ap.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    ap.add_argument("--usage", action="store_true", help="only report storage usage")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every planned action")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    log = logging.getLogger("cleanup")
    settings = load_settings(args.settings)
    cfg = StorageSettings.from_settings(settings)

    if args.usage:
        u = usage(cfg)
        print(json.dumps(u, indent=2) if args.json else
              f"total {human(u['total_bytes'])} of max {cfg.max_storage_gb} GB"
              f"{'  ** OVER LIMIT **' if u['over_limit'] else ''}\n"
              + "\n".join(f"  {k:10} {human(v)}" for k, v in u["bytes"].items())
              + f"\n  disk free  {u['disk_free_gb']} GB")
        return 2 if u["over_limit"] else 0

    from src.paths import db_path
    db = Database(db_path(settings))

    def mark_expired(device_id: str, event_id: str, reason: str) -> None:
        db.mark_event_by_id(device_id, event_id, "skipped", None, reason)

    try:
        report = run_cleanup(cfg, apply=args.apply, mark_expired=mark_expired)
    finally:
        db.close()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        log_report(report, log)
        if args.verbose or not args.apply:
            for a in report.actions:
                print(f"  {a.kind:16} {human(a.bytes):>10}  {a.age_hours:6.1f} h  {a.path.relative_to(BACKEND)}"
                      + (f"  ({a.note})" if a.note else ""))
        if not args.apply and report.actions:
            print("\nDry run. Re-run with --apply to delete.")
    return 2 if report.usage_after.get("over_limit") else 0


if __name__ == "__main__":
    raise SystemExit(main())
