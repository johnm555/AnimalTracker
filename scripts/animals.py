#!/usr/bin/env python3
"""Visiting animals (P4-30): record and report what else the cameras see.

    scripts/run.sh animals report [--days 7]        what has been visiting
    scripts/run.sh animals candidates               non-Winston animals awaiting a species
    scripts/run.sh animals record --species raccoon --camera side-deck --at <iso>
    scripts/run.sh animals label <observation_id> --species raccoon

`label` is the normal path: an observation already says an animal was there and
was not Winston; this says what it was. `record` is for a sighting with no
observation behind it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from src.animals import KNOWN_SPECIES, AnimalSighting, normalize_species, summarize  # noqa: E402
from src.api import load_settings  # noqa: E402
from src.db import Database  # noqa: E402
from src.observation import parse_timestamp, utcnow  # noqa: E402


def open_db(settings) -> Database:
    p = Path((settings.get("database") or {}).get("path", "./winston.db"))
    return Database(p if p.is_absolute() else BACKEND / p)


def cmd_report(args, settings) -> int:
    db = open_db(settings)
    since = utcnow() - timedelta(days=args.days)
    rows = db.list_animal_sightings(since=since,
                                    species=normalize_species(args.species) if args.species else None)
    if args.json:
        print(json.dumps({"sightings": rows, "summary": summarize(rows)}, indent=2))
        return 0
    if not rows:
        print(f"no visiting animals recorded in the last {args.days:g} day(s).")
        print("`animals candidates` shows non-Winston animals still awaiting a species.")
        return 0
    s = summarize(rows)
    print(f"{s['sightings']} sighting(s) in the last {args.days:g} day(s) "
          f"({s['note']}):\n")
    for sp, b in s["species"].items():
        print(f"  {sp:12} {b['sightings']:3}  on {', '.join(b['cameras'])}")
        print(f"               first {b['first'][:19]}  last {b['last'][:19]}")
    print("\n  by camera: " + "  ".join(f"{k}={v}" for k, v in s["by_camera"].items()))
    if s["by_hour_utc"]:
        print("  by hour (UTC): " + " ".join(f"{h:02d}:{n}" for h, n in s["by_hour_utc"].items()))
    print("\nmost recent:")
    for r in rows[:10]:
        print(f"  {r['timestamp'][:19]}  {r['species']:10} {r['camera_id']:16} "
              f"[{r['source']}] {r['notes'][:50]}")
    return 0


def cmd_candidates(args, settings) -> int:
    db = open_db(settings)
    thr = float(((settings.get("detector") or {}).get("confidence_threshold")
                 or settings.get("tracker", {}).get("confidence_threshold", 0.70)))
    rows = db.unlabelled_animal_observations(thr, limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("nothing awaiting a species.")
        return 0
    print(f"{len(rows)} observation(s) saw an animal that was not Winston, with no species yet.")
    print("The species is genuinely unknown — the reviewer said what it was NOT.")
    print("Look at the frames before labelling; do not infer from the camera.\n")
    for r in rows:
        print(f"  obs {r['observation_id']:<5} {r['timestamp'][:19]}  {r['camera_id']:16} "
              f"p={r['winston_probability']:.3f}")
        if r["mismatched_features"]:
            print(f"        not-Winston features: {', '.join(r['mismatched_features'])[:110]}")
    print(f"\n  scripts/run.sh animals label <observation_id> --species <{'|'.join(KNOWN_SPECIES[:5])}|...>")
    return 0


def _record(db, *, camera_id, timestamp, species, source, confidence, notes,
            observation_id=None) -> int:
    sighting = AnimalSighting(camera_id=camera_id, timestamp=timestamp, species=species,
                              source=source, confidence=confidence, notes=notes,
                              observation_id=observation_id)
    row = db.record_animal_sighting(sighting)
    print(f"recorded {row['species']} on {row['camera_id']} at {row['timestamp'][:19]} "
          f"[{row['source']}]" + (f" (observation {observation_id})" if observation_id else ""))
    if row["species"] not in KNOWN_SPECIES:
        print(f"  note: '{row['species']}' is not in the known-species list; kept as given.")
    return 0


def cmd_label(args, settings) -> int:
    db = open_db(settings)
    row = db._conn.execute(
        "SELECT id,camera_id,timestamp,winston_probability,animal_present FROM observations WHERE id=?",
        (args.observation_id,)).fetchone()
    if row is None:
        sys.exit(f"no observation {args.observation_id}")
    if not row["animal_present"]:
        sys.exit(f"observation {args.observation_id} recorded no animal at all — nothing to label")
    return _record(db, camera_id=row["camera_id"], timestamp=parse_timestamp(row["timestamp"]),
                   species=args.species, source=args.source, confidence=args.confidence,
                   notes=args.notes, observation_id=args.observation_id)


def cmd_record(args, settings) -> int:
    return _record(open_db(settings), camera_id=args.camera,
                   timestamp=parse_timestamp(args.at) if args.at else utcnow(),
                   species=args.species, source=args.source,
                   confidence=args.confidence, notes=args.notes)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("report", help="what has been visiting")
    p.add_argument("--days", type=float, default=7)
    p.add_argument("--species")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("candidates", help="non-Winston animals awaiting a species")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_candidates)

    p = sub.add_parser("label", help="give an existing observation's animal a species")
    p.add_argument("observation_id", type=int)
    p.add_argument("--species", required=True)
    p.add_argument("--source", default="session", choices=("session", "owner", "backfill"))
    p.add_argument("--confidence", type=float)
    p.add_argument("--notes", default="")
    p.set_defaults(fn=cmd_label)

    p = sub.add_parser("record", help="record a sighting with no observation behind it")
    p.add_argument("--species", required=True)
    p.add_argument("--camera", required=True)
    p.add_argument("--at", help="ISO timestamp (default: now)")
    p.add_argument("--source", default="owner", choices=("session", "owner", "backfill"))
    p.add_argument("--confidence", type=float)
    p.add_argument("--notes", default="")
    p.set_defaults(fn=cmd_record)

    args = ap.parse_args(argv)
    return args.fn(args, load_settings(args.settings))


if __name__ == "__main__":
    raise SystemExit(main())
