#!/usr/bin/env python3
"""Build the DINOv2 reference gallery from confirmed camera sightings (P4-07).

The original gallery is six phone photos of Winston: all daylight, all close,
all from a human's eye level. The cameras see something else entirely — fisheye
distortion, night IR, overhead angles, a dog 8 metres away. P4-18 records the
consequence: every threshold in `local_pipeline` was fitted against references
that do not resemble the input.

This script harvests reference frames from events the system has already judged,
and the selection rule that matters is *which* judgements it trusts.

**Only verdicts that did not come from DINOv2 are eligible.** A frame accepted by
`LocalPipeline` was accepted *because* it scored high against the current
gallery. Feeding those back in teaches the model what it already believes, and
any evaluation afterwards measures the model against its own output. So
candidates are restricted to session verdicts (a reviewer looked at the frames
and answered the verification question) and owner confirmations. At the time of
writing that is 340 of the 652 confirmed sightings — enough to build a gallery
without ever consulting the model being improved.

**The gallery and the evaluation set are disjoint.** Events are split before
selection, by a hash of the event id, so a frame that enters the gallery can
never appear in the held-out set. Without that split, "similarity improved" is
arithmetic, not evidence.

**Variety is stratified, not hoped for.** Frames are bucketed by camera and by
day/IR, then ranked within each bucket by a quality score (sharpness, how much
of the frame the animal occupies, exposure sanity). Every bucket contributes
before any bucket contributes twice, so one prolific camera cannot dominate.

Pose (standing / lying / walking / partial) is NOT inferred here. Nothing in the
local stack can tell a lying dog from a standing one, and a filename asserting
otherwise would be a fabricated label attached to training data. `--apply` names
files with what is measured — camera, lighting, distance, confidence — and a
reviewer renames with a pose afterwards if they want it.

    scripts/build_reference_set.py plan [--target 36]
    scripts/build_reference_set.py apply [--target 36]
    scripts/build_reference_set.py evaluate        # held-out, old gallery vs new
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from src.api import load_settings  # noqa: E402
from src.db import Database  # noqa: E402
from src.dog_detector import DogGate, GateSettings  # noqa: E402
from src.pipeline import reference_dir, staging_dirs  # noqa: E402

#: Fraction of eligible events held out of the gallery for evaluation.
HOLDOUT = 0.30


def open_db(settings) -> Database:
    from src.paths import db_path
    return Database(db_path(settings))


def is_holdout(event_id: str) -> bool:
    """Stable per-event split: the same event always lands on the same side."""
    h = hashlib.sha256(f"refset:{event_id}".encode()).digest()
    return (h[0] / 255.0) < HOLDOUT


def eligible_events(db: Database, threshold: float) -> list[dict[str, Any]]:
    """Confirmed Winston events whose verdict did not come from DINOv2."""
    rows = db._conn.execute(
        """SELECT o.id, o.camera_id, o.timestamp, o.winston_probability, o.extra,
                  p.event_id, p.device_id
             FROM observations o
             JOIN processed_events p ON p.observation_id = o.id
            WHERE o.winston_probability >= ?
            ORDER BY o.winston_probability DESC""", (threshold,)).fetchall()
    out = []
    for r in rows:
        extra = json.loads(r["extra"] or "{}")
        owner = bool(extra.get("owner_confirmation"))
        if extra.get("detector") == "local" and not owner:
            continue                     # scored by the model we are improving
        out.append({"observation_id": r["id"], "camera_id": r["camera_id"],
                    "timestamp": r["timestamp"], "probability": r["winston_probability"],
                    "event_id": r["event_id"], "source": "owner" if owner else "session"})
    return out


def find_frames(dirs, event_id: str) -> list[Path]:
    for day in sorted(dirs.archive.iterdir()) if dirs.archive.is_dir() else []:
        if not day.is_dir():
            continue
        for p in day.iterdir():
            if p.is_dir() and p.name.endswith(event_id):
                return sorted(p.glob("frame-*.jpg"))
    return []


def is_infrared(img) -> bool:
    """Ring night frames are grayscale; daylight frames carry real saturation."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    return float(hsv[:, :, 1].mean()) < 18.0


def frame_quality(img, box) -> dict[str, Any]:
    """Score a frame as a *reference*: sharp, well exposed, animal big enough."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean = float(gray.mean())
    area = float(box[2] * box[3]) if box else 0.0
    exposure = 1.0 - abs(mean - 120.0) / 120.0          # 1.0 at mid-grey
    score = (min(sharp / 300.0, 1.0) * 0.4
             + min(area / 0.10, 1.0) * 0.4              # 10% of frame = plenty
             + max(exposure, 0.0) * 0.2)
    return {"sharpness": round(sharp, 1), "mean_luma": round(mean, 1),
            "box_area": round(area, 4), "score": round(score, 4)}


def distance_bucket(area: float) -> str:
    if area >= 0.12:
        return "near"
    if area >= 0.04:
        return "mid"
    return "far"


def scan(db, settings, threshold: float, verbose: bool = False) -> list[dict[str, Any]]:
    """Every eligible frame, with lighting, distance and a quality score."""
    dirs = staging_dirs(settings)
    gate = DogGate(GateSettings.from_settings(settings))
    cands: list[dict[str, Any]] = []
    events = eligible_events(db, threshold)
    for n, ev in enumerate(events, 1):
        frames = find_frames(dirs, ev["event_id"])
        if not frames:
            continue
        verdict = gate.detect_frames(frames)
        for fv, path in zip(verdict.frames, frames):
            box = fv.best.box if fv.best else None
            img = cv2.imread(str(path))
            if img is None:
                continue
            q = frame_quality(img, box)
            cands.append({**ev, "path": str(path), "frame": path.name,
                          "lighting": "ir" if is_infrared(img) else "day",
                          "distance": distance_bucket(q["box_area"]) if box else "unlocalised",
                          "localised": box is not None, "box": list(box) if box else None,
                          "holdout": is_holdout(ev["event_id"]), **q})
        if verbose and n % 25 == 0:
            print(f"  scanned {n}/{len(events)} events, {len(cands)} usable frames",
                  file=sys.stderr)
    return cands


def select(cands: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    """Round-robin over (camera, lighting) buckets, best first, one frame per event."""
    # A gallery image must have a localised animal: a full-frame crop mostly
    # encodes the deck, not the dog. The held-out set keeps the unlocalised
    # frames, so recall is still measured on the hard ones.
    pool = [c for c in cands if not c["holdout"] and c["localised"]]
    buckets: dict[tuple, list] = defaultdict(list)
    for c in pool:
        buckets[(c["camera_id"], c["lighting"])].append(c)
    for b in buckets.values():
        b.sort(key=lambda c: -(0.6 * c["score"] + 0.4 * c["probability"]))
    chosen: list[dict[str, Any]] = []
    # The reviewer's most confident frame on each camera is included outright.
    for cam in {c["camera_id"] for c in pool}:
        best = max((c for c in pool if c["camera_id"] == cam), key=lambda c: c["probability"])
        chosen.append(best)
        for b in buckets.values():
            if best in b:
                b.remove(best)
    used_events: set[str] = {c["event_id"] for c in chosen}
    order = sorted(buckets)
    i = 0
    while len(chosen) < target and any(buckets[k] for k in order):
        k = order[i % len(order)]
        i += 1
        while buckets[k]:
            c = buckets[k].pop(0)
            if c["event_id"] in used_events:
                continue            # one frame per event: 4 frames of one moment is not variety
            used_events.add(c["event_id"])
            chosen.append(c)
            break
    return chosen


def describe(rows: list[dict[str, Any]]) -> str:
    by_cam: dict[str, int] = defaultdict(int)
    by_light: dict[str, int] = defaultdict(int)
    by_dist: dict[str, int] = defaultdict(int)
    for r in rows:
        by_cam[r["camera_id"]] += 1
        by_light[r["lighting"]] += 1
        by_dist[r["distance"]] += 1
    return (f"  cameras : {dict(sorted(by_cam.items(), key=lambda kv: -kv[1]))}\n"
            f"  lighting: {dict(by_light)}\n"
            f"  distance: {dict(by_dist)}")


def cmd_plan(args, settings) -> int:
    db = open_db(settings)
    thr = float(settings.get("tracker", {}).get("confidence_threshold", 0.70))
    cands = scan(db, settings, args.threshold or thr, verbose=True)
    if not cands:
        print("no eligible frames found")
        return 1
    chosen = select(cands, args.target)
    held = [c for c in cands if c["holdout"]]
    print(f"\n{len(cands)} usable frames from session/owner verdicts "
          f"({len({c['event_id'] for c in cands})} events)")
    print(f"  {len(held)} frames held out for evaluation (never enter the gallery)")
    print(f"\nselected {len(chosen)}:")
    print(describe(chosen))
    print()
    for c in sorted(chosen, key=lambda c: (c["camera_id"], c["lighting"])):
        print(f"  {c['camera_id']:16} {c['lighting']:3} {c['distance']:4} "
              f"p={c['probability']:.2f} q={c['score']:.2f} {c['frame']}  {c['path']}")
    if args.json:
        Path(args.json).write_text(json.dumps(chosen, indent=2))
        print(f"\nwrote {args.json}")
    return 0


def cmd_apply(args, settings) -> int:
    db = open_db(settings)
    thr = float(settings.get("tracker", {}).get("confidence_threshold", 0.70))
    chosen = select(scan(db, settings, args.threshold or thr, verbose=True), args.target)
    # Flat, alongside the original photos: the gallery loader uses a
    # non-recursive iterdir() (which is how reserve/ stays out), so a
    # subdirectory would be silently ignored. The cam_ prefix keeps the
    # harvested set identifiable and removable.
    harvested = reference_dir(settings)
    existing = sorted(harvested.glob("cam_*.jpg"))
    if existing and not args.force:
        sys.exit(f"{len(existing)} harvested reference(s) already present; rerun with --force")
    for old in existing:
        old.unlink()
    harvested.mkdir(parents=True, exist_ok=True)
    from src.local_detector import LocalDetector

    manifest = []
    for c in chosen:
        name = (f"cam_{c['camera_id']}_{c['lighting']}_{c['distance']}_"
                f"p{c['probability']:.2f}_{c['event_id'][-6:]}.jpg")
        img = cv2.imread(c["path"])
        # A gallery entry is embedded WHOLE, so it must contain the dog and as
        # little else as possible. The six phone photos work because Winston
        # fills them; a full camera frame is 85-95% deck, and embedding that
        # would teach the gallery this property's backgrounds -- every empty
        # frame would then score high. Crop to the gate box, with padding for
        # context the detector also sees at inference.
        crop, cropped = LocalDetector.crop(img, tuple(c["box"]), padding=0.15)
        if not cropped:
            print(f"  skipped {name}: crop failed", file=sys.stderr)
            continue
        cv2.imwrite(str(harvested / name), crop)
        h, w = crop.shape[:2]
        manifest.append({**c, "reference_name": name, "crop_size": [int(w), int(h)]})
    (harvested / "cam_MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    print(f"\ncopied {len(manifest)} frames to {harvested}")
    print(describe(chosen))
    print("\nProvenance is in MANIFEST.json. Filenames carry what was measured "
          "(camera, lighting, distance, confidence) — not pose, which nothing here can see.")
    print("Next: delete backend/staging/gallery.pt so the gallery rebuilds, then `evaluate`.")
    return 0


def cmd_evaluate(args, settings) -> int:
    """Score the held-out events with the gallery as it stands. Run before and after."""
    from src.local_detector import LocalDetector, LocalSettings

    db = open_db(settings)
    thr = float(settings.get("tracker", {}).get("confidence_threshold", 0.70))
    cands = scan(db, settings, args.threshold or thr)
    held = [c for c in cands if c["holdout"]]
    if not held:
        print("no held-out frames")
        return 1
    dirs = staging_dirs(settings)
    det = LocalDetector(LocalSettings.from_settings(settings), reference_dir(settings),
                        dirs.root / "gallery.pt")
    if not det.load():
        sys.exit(f"local detector unavailable: {det._load_error}")
    by_event: dict[str, dict[str, Any]] = {}
    for c in held:
        v = det.evaluate([Path(c["path"])], [None])
        prev = by_event.get(c["event_id"])
        if prev is None or v.score > prev["score"]:
            by_event[c["event_id"]] = {"score": v.score, "camera_id": c["camera_id"],
                                       "lighting": c["lighting"]}
    # Negatives: held-out events a reviewer said had NO animal. Without these,
    # "similarity went up" cannot distinguish a better gallery from one that has
    # memorised the backgrounds every frame shares.
    neg_scores: list[float] = []
    for row in db._conn.execute(
            """SELECT p.event_id FROM processed_events p
                 LEFT JOIN observations o ON o.id = p.observation_id
                WHERE p.status = 'skipped' OR (o.id IS NOT NULL AND o.animal_present = 0)"""):
        eid = row["event_id"]
        if not is_holdout(eid):
            continue
        frames = find_frames(dirs, eid)
        if not frames:
            continue
        neg_scores.append(det.evaluate([frames[0]], [None]).score)
        if len(neg_scores) >= args.max_negatives:
            break

    scores = [e["score"] for e in by_event.values()]
    cfg = settings.get("detector", {}).get("pipeline", {})
    accept = float(cfg.get("accept_threshold", 0.30))
    out = {
        "gallery_size": det.gallery_size,
        "held_out_events": len(by_event),
        "mean_score": round(float(np.mean(scores)), 4),
        "median_score": round(float(np.median(scores)), 4),
        "min_score": round(float(np.min(scores)), 4),
        "at_or_above_accept": sum(s >= accept for s in scores),
        "recall_at_accept": round(sum(s >= accept for s in scores) / len(scores), 4),
        "accept_threshold": accept,
        "unlocalised_events": len({c["event_id"] for c in held if not c["localised"]}),
        "by_lighting": {},
        "negatives": {
            "events": len(neg_scores),
            "mean_score": round(float(np.mean(neg_scores)), 4) if neg_scores else None,
            "at_or_above_accept": sum(s >= accept for s in neg_scores),
            "false_positive_rate": (round(sum(s >= accept for s in neg_scores) / len(neg_scores), 4)
                                    if neg_scores else None),
        },
    }
    for light in ("day", "ir"):
        s = [e["score"] for e in by_event.values() if e["lighting"] == light]
        if s:
            out["by_lighting"][light] = {
                "events": len(s), "mean": round(float(np.mean(s)), 4),
                "recall_at_accept": round(sum(x >= accept for x in s) / len(s), 4)}
    print(json.dumps(out, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2))
    return 0


def _score_sets(settings, db, dirs, cands, max_neg):
    """Raw held-out scores for positives and negatives under the current gallery."""
    from src.local_detector import LocalDetector, LocalSettings

    det = LocalDetector(LocalSettings.from_settings(settings), reference_dir(settings),
                        dirs.root / "gallery.pt")
    if not det.load():
        sys.exit(f"local detector unavailable: {det._load_error}")
    pos: dict[str, float] = {}
    for c in cands:
        if not c["holdout"]:
            continue
        v = det.evaluate([Path(c["path"])], [None]).score
        pos[c["event_id"]] = max(pos.get(c["event_id"], 0.0), v)
    neg: list[float] = []
    for row in db._conn.execute(
            """SELECT p.event_id FROM processed_events p
                 LEFT JOIN observations o ON o.id = p.observation_id
                WHERE p.status = 'skipped' OR (o.id IS NOT NULL AND o.animal_present = 0)"""):
        if not is_holdout(row["event_id"]):
            continue
        frames = find_frames(dirs, row["event_id"])
        if not frames:
            continue
        neg.append(det.evaluate([frames[0]], [None]).score)
        if len(neg) >= max_neg:
            break
    return det.gallery_size, list(pos.values()), neg


def _separation(pos, neg) -> dict[str, Any]:
    """AUC and the best achievable operating point — the only fair comparison.

    Recall at a *fixed* threshold cannot tell a better gallery from one that
    simply shifted every score upward. AUC is threshold-free, and the sweep says
    what the threshold would have to become.
    """
    pos_a, neg_a = np.array(pos), np.array(neg)
    auc = float(np.mean([(pos_a > n).mean() + 0.5 * (pos_a == n).mean() for n in neg_a]))
    rows = []
    for t in np.arange(0.20, 0.61, 0.01):
        recall = float((pos_a >= t).mean())
        fpr = float((neg_a >= t).mean())
        f1 = 0.0 if recall + (1 - fpr) == 0 else 2 * recall * (1 - fpr) / (recall + (1 - fpr))
        rows.append({"threshold": round(float(t), 2), "recall": round(recall, 4),
                     "fpr": round(fpr, 4), "f1": round(f1, 4)})
    best = max(rows, key=lambda r: r["f1"])
    at10 = min((r for r in rows if r["fpr"] <= 0.10), key=lambda r: r["threshold"], default=None)
    return {"auc": round(auc, 4), "best_f1": best,
            "best_threshold_at_fpr<=0.10": at10,
            "pos_mean": round(float(pos_a.mean()), 4),
            "neg_mean": round(float(neg_a.mean()), 4),
            "sweep": rows}


def cmd_compare(args, settings) -> int:
    """Score the same held-out data under the photo-only and expanded galleries."""
    db, dirs = open_db(settings), staging_dirs(settings)
    thr = float(settings.get("tracker", {}).get("confidence_threshold", 0.70))
    cands = scan(db, settings, args.threshold or thr)
    ref = reference_dir(settings)
    gallery_pt = dirs.root / "gallery.pt"
    out: dict[str, Any] = {}

    gallery_pt.unlink(missing_ok=True)
    n, pos, neg = _score_sets(settings, db, dirs, cands, args.max_negatives)
    out["expanded"] = {"gallery_size": n, "positives": len(pos), "negatives": len(neg),
                       **_separation(pos, neg)}

    stash = Path(args.stash)
    stash.mkdir(parents=True, exist_ok=True)
    moved = list(ref.glob("cam_*.jpg"))
    for f in moved:
        shutil.move(str(f), str(stash / f.name))
    try:
        gallery_pt.unlink(missing_ok=True)
        n, pos, neg = _score_sets(settings, db, dirs, cands, args.max_negatives)
        out["photos_only"] = {"gallery_size": n, "positives": len(pos), "negatives": len(neg),
                              **_separation(pos, neg)}
    finally:
        for f in moved:
            shutil.move(str(stash / f.name), str(ref / f.name))
        gallery_pt.unlink(missing_ok=True)

    for k in ("photos_only", "expanded"):
        b = out[k]
        print(f"\n{k}: gallery={b['gallery_size']}  AUC={b['auc']}  "
              f"pos_mean={b['pos_mean']}  neg_mean={b['neg_mean']}")
        print(f"   best F1      : t={b['best_f1']['threshold']}  "
              f"recall={b['best_f1']['recall']}  fpr={b['best_f1']['fpr']}")
        a = b["best_threshold_at_fpr<=0.10"]
        print(f"   fpr<=0.10 at : t={a['threshold']}  recall={a['recall']}" if a
              else "   fpr<=0.10    : unreachable in 0.20-0.60")
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2))
        print(f"\nwrote {args.out}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("plan", cmd_plan), ("apply", cmd_apply), ("evaluate", cmd_evaluate),
                     ("compare", cmd_compare)):
        p = sub.add_parser(name)
        p.add_argument("--target", type=int, default=36)
        p.add_argument("--threshold", type=float,
                       help="minimum winston_probability to treat as confirmed")
        if name == "plan":
            p.add_argument("--json")
        if name == "apply":
            p.add_argument("--force", action="store_true")
        if name in ("evaluate", "compare"):
            p.add_argument("--out")
            p.add_argument("--max-negatives", type=int, default=120)
        if name == "compare":
            p.add_argument("--stash", default="/tmp/refset-stash")
        p.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    return args.fn(args, load_settings(args.settings))


if __name__ == "__main__":
    raise SystemExit(main())
