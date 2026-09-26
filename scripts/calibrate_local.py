#!/usr/bin/env python
"""Calibrate the local detector (P4-11) against verdicts already in the database.

    scripts/run.sh calibrate-local [--db winston.db] [--json out.json] [--limit N]

Replays every archived event that has a recorded verdict, scores it with
DINOv2 (cropping to the P4-12 gate box when one is found), and prints what
each threshold choice would have done:

  * ACCEPT precision  — of the events a threshold would auto-accept, how many
    were really Winston. This has to be ~100%: an accepted event becomes a
    sighting with no human in the loop.
  * SKIP loss         — of the events a threshold would skip, how many were
    really Winston. Every one is a sighting the system would never see.
  * review volume     — what fraction still needs a session, i.e. how much of
    the backlog actually goes away.

Ground truth is the session verdict stored with each observation
(`animal_present` + `vision_confidence`), matched to the archived frames by
`extra.staging_key`.
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
os.chdir(BACKEND)

from src.dog_detector import DogGate, GateSettings  # noqa: E402
from src.local_detector import LocalDetector, LocalSettings  # noqa: E402


def load_truth(db_path: Path) -> dict[str, dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    out = {}
    for r in con.execute("SELECT id, winston_probability, vision_confidence, animal_present, extra "
                         "FROM observations WHERE extra LIKE '%staging_key%'"):
        try:
            key = json.loads(r["extra"])["staging_key"]
        except (ValueError, KeyError):
            continue
        conf = r["vision_confidence"]
        animal = bool(r["animal_present"])
        out[key] = {
            "winston": bool(animal and (conf or 0) >= 0.70),
            "animal": animal,
            "confidence": conf,
            "probability": r["winston_probability"],
        }
    con.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="winston.db")
    ap.add_argument("--archive", default="staging/archive")
    ap.add_argument("--references", default="reference_images")
    ap.add_argument("--json", help="write per-event scores here")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-gate", action="store_true", help="score full frames, no crop")
    args = ap.parse_args(argv)

    truth = load_truth(Path(args.db))
    if not truth:
        sys.exit(f"no verdicts found in {args.db}")
    det = LocalDetector(LocalSettings(enabled=True), reference_dir=Path(args.references),
                        cache_path=Path("staging/gallery.pt"))
    if not det.load():
        sys.exit(f"local detector unavailable: {det._load_error}")
    gate = None if args.no_gate else DogGate(GateSettings())
    print(f"gallery: {det.gallery_size} reference images | device {det._device}", file=sys.stderr)

    events = sorted(p for p in Path(args.archive).glob("*/*") if p.is_dir() and p.name in truth)
    if args.limit:
        events = events[: args.limit]
    rows = []
    for n, d in enumerate(events, 1):
        frames = sorted(d.glob("frame-*.jpg"))
        if not frames:
            continue
        boxes = [None] * len(frames)
        if gate is not None:
            for i, f in enumerate(frames):
                fv = gate.detect_frame(f)
                if fv.best:
                    boxes[i] = fv.best.box
        v = det.evaluate(frames, boxes)
        t = truth[d.name]
        rows.append({"key": d.name, "score": v.score, "cropped": v.cropped_any,
                     "winston": t["winston"], "animal": t["animal"],
                     "confidence": t["confidence"], "ms": v.total_ms})
        if n % 50 == 0:
            print(f"  {n}/{len(events)}", file=sys.stderr)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))

    pos = [r for r in rows if r["winston"]]
    neg = [r for r in rows if not r["winston"]]
    print(f"\nscored {len(rows)} archived events "
          f"({len(pos)} Winston / {len(neg)} not) at {sum(r['ms'] for r in rows)/max(1,len(rows)):.0f} ms/event")
    cropped = [r for r in rows if r["cropped"]]
    print(f"gate localised an animal in {len(cropped)}/{len(rows)} events "
          f"({sum(1 for r in cropped if r['winston'])} of them Winston)")

    def band(rs):
        return (f"n={len(rs):3d} winston={sum(1 for r in rs if r['winston']):3d} "
                f"other={sum(1 for r in rs if not r['winston']):3d}")

    print("\nscore distribution:")
    for lo in [round(x * 0.05, 2) for x in range(0, 20)]:
        rs = [r for r in rows if lo <= r["score"] < lo + 0.05]
        if rs:
            print(f"  {lo:.2f}-{lo+0.05:.2f}  {band(rs)}")

    print("\nACCEPT threshold (cropped events only — require_gate_box):")
    print(f"  {'thr':>5} {'accepted':>9} {'precision':>10} {'of all Winston':>15}")
    for thr in [round(0.30 + 0.05 * i, 2) for i in range(14)]:
        acc = [r for r in cropped if r["score"] >= thr]
        if not acc:
            continue
        good = sum(1 for r in acc if r["winston"])
        print(f"  {thr:5.2f} {len(acc):9d} {good/len(acc):9.1%} {good/max(1,len(pos)):14.1%}"
              + ("   <- clean" if good == len(acc) else f"   ({len(acc)-good} wrong)"))

    print("\nSKIP threshold (all events):")
    print(f"  {'thr':>5} {'skipped':>8} {'winston lost':>13} {'review left':>12}")
    for thr in [round(0.05 * i, 2) for i in range(1, 13)]:
        sk = [r for r in rows if r["score"] <= thr]
        lost = sum(1 for r in sk if r["winston"])
        print(f"  {thr:5.2f} {len(sk):8d} {lost:13d} {1 - len(sk)/max(1,len(rows)):11.0%}"
              + ("   <- lossless" if lost == 0 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
