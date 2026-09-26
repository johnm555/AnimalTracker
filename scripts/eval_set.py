#!/usr/bin/env python
"""Frozen labelled eval set (P4-08): build | verify | summary.

    scripts/run.sh eval-set build [--dry-run] [--out backend/evalset/v1] [--db PATH]
    scripts/run.sh eval-set verify [--out ...]
    scripts/run.sh eval-set summary [--out ...]

`build` reads the DB read-only, labels events from owner reviews, session
audits and session verdicts only (never local-only decisions), splits them by
sequence into test/dev/excluded, and hard-links their frames out of
staging/archive/ before retention deletes them. Re-running it is safe and
additive. Rules and caveats: docs/evaluations/Eval_Set.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import yaml  # noqa: E402

from src import evalset  # noqa: E402


def _settings() -> dict:
    p = Path(os.environ.get("WINSTON_SETTINGS", BACKEND / "config" / "settings.yaml"))
    return yaml.safe_load(p.read_text()) or {}


def _resolve(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else BACKEND / p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(BACKEND / "evalset" / "v1"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--db", help="SQLite path (default: settings database.path); opened read-only")
    b.add_argument("--archive", help="staging archive dir (default: <staging_dir>/archive)")
    b.add_argument("--gap-seconds", type=float, default=300.0)
    b.add_argument("--dry-run", action="store_true")
    sub.add_parser("verify")
    sub.add_parser("summary")
    args = ap.parse_args()
    out = Path(args.out)

    if args.cmd == "build":
        s = _settings()
        db = _resolve(args.db or (s.get("database") or {}).get("path", "./winston.db"))
        staging = _resolve((s.get("detector") or {}).get("staging_dir", "./staging"))
        archive = Path(args.archive) if args.archive else staging / "archive"
        gallery = evalset.load_gallery_event_ids(
            _resolve((s.get("detector") or {}).get("reference_images_dir", "./reference_images"))
            / "cam_MANIFEST.json")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            summary = evalset.build(conn, archive, out, gallery,
                                    gap_seconds=args.gap_seconds, dry_run=args.dry_run)
        finally:
            conn.close()
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    if args.cmd == "verify":
        r = evalset.verify(out)
        print(json.dumps({**r, "frames_missing": len(r["frames_missing"]),
                          "frames_mismatched": r["frames_mismatched"][:20]}, indent=2))
        return 0 if not r["frames_missing"] and not r["frames_mismatched"] else 1
    rows = evalset.load_manifest(out)
    print(json.dumps(evalset.summarise(rows.values()), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
