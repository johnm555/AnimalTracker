#!/usr/bin/env python3
"""Improve the local models from the verdicts your AI sessions have recorded.

    scripts/run.sh train status              # where the local models stand + what to do next
    scripts/run.sh train harvest [--apply]   # grow the reference gallery from confirmed sightings
    scripts/run.sh train evaluate            # held-out: does the gallery separate your animal from empty scenes?
    scripts/run.sh train calibrate           # replay the archive, print threshold bands (never writes them)
    scripts/run.sh train eval-set            # freeze labelled frames for offline evaluation
    scripts/run.sh train full [--apply]      # status → harvest → evaluate → calibrate
    scripts/run.sh train export [--out DIR]  # labelled frames as Training/ + Testing/ class folders
                                             # (Create ML image classifier, or any fine-tuning tool)

The loop: local models decide what they can; everything else goes to a review
queue that a Claude/Codex session drains (`run.sh detect list --sheets`). Those
verdicts — never the local models' own output — become new reference crops and
the ground truth the thresholds are fitted to. Thresholds are printed for a
human to copy into settings.yaml; this tool never changes them.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from src import paths  # noqa: E402
from src.api import load_settings  # noqa: E402

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp"}


def _run(script: str, *args: str) -> int:
    cmd = [sys.executable, str(ROOT / "scripts" / script), *args]
    print(f"\n$ {' '.join(cmd[1:])}", flush=True)
    return subprocess.run(cmd, cwd=BACKEND).returncode


def _q(con: sqlite3.Connection, sql: str, *args) -> int:
    return con.execute(sql, args).fetchone()[0] or 0


def status(settings: dict) -> dict:
    db = Path(paths.db_path(settings))
    if not db.exists():
        return {"error": f"no database at {db} yet — run the tracker first (scripts/run.sh api)"}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    s: dict = {}
    s["verdicts"] = {
        "session": _q(con, "SELECT COUNT(*) FROM observations WHERE json_extract(extra,'$.detector')='session'"),
        "local": _q(con, "SELECT COUNT(*) FROM observations WHERE json_extract(extra,'$.detector')='local'"),
        "owner_confirmed": _q(con, "SELECT COUNT(*) FROM observations WHERE extra LIKE '%owner_confirmation%'"),
    }
    week = {st: _q(con, "SELECT COUNT(*) FROM processed_events WHERE status=? AND timestamp>=?", st, since)
            for st in ("analyzed", "skipped", "staged", "failed")}
    local_obs = _q(con, """SELECT COUNT(*) FROM processed_events p JOIN observations o ON o.id=p.observation_id
                           WHERE p.timestamp>=? AND json_extract(o.extra,'$.detector')='local'""", since)
    local_skip = _q(con, "SELECT COUNT(*) FROM processed_events WHERE status='skipped' AND error LIKE 'local %' AND timestamp>=?", since)
    total = sum(week.values())
    s["last_7_days"] = {**week, "events": total, "decided_locally": local_obs + local_skip,
                        "auto_rate": round((local_obs + local_skip) / total, 3) if total else None}
    s["review_queue"] = _q(con, "SELECT COUNT(*) FROM processed_events WHERE status='staged'")
    audited = _q(con, "SELECT COUNT(*) FROM local_audits")
    agreed = _q(con, "SELECT COUNT(*) FROM local_audits WHERE agrees=1")
    s["audits"] = {"n": audited, "agreement": round(agreed / audited, 3) if audited else None}
    con.close()

    ref = paths.reference_images_dir(settings)
    s["gallery"] = {"images": len([p for p in ref.glob("*") if p.suffix.lower() in IMAGE_EXT]) if ref.is_dir() else 0,
                    "dir": str(ref)}
    cache = paths.staging_dir(settings) / "gallery.pt"
    s["gallery"]["embedded_at"] = (datetime.fromtimestamp(cache.stat().st_mtime).isoformat(timespec="minutes")
                                   if cache.exists() else None)
    pipe = ((settings.get("detector") or {}).get("pipeline") or {})
    s["thresholds"] = {k: pipe.get(k) for k in ("accept_threshold", "rescue_threshold", "not_winston_threshold")}
    s["recommendations"] = recommend(s)
    return s


def recommend(s: dict) -> list[str]:
    out = []
    v, wk, a, g = s["verdicts"], s["last_7_days"], s["audits"], s["gallery"]
    if s["review_queue"]:
        out.append(f"{s['review_queue']} events await review — have a Claude/Codex session run "
                   "`scripts/run.sh detect list --sheets` (see .claude/skills/review-frames).")
    if g["images"] < 6:
        out.append("Gallery has fewer than 6 images: add owner photos to the reference folder.")
    if v["session"] + v["owner_confirmed"] >= 50 and g["images"] < 30:
        out.append("Enough reviewed sightings to harvest camera-view references: `run.sh train harvest`, "
                   "then `train evaluate` before `train harvest --apply`.")
    if a["n"] < 20:
        out.append(f"Only {a['n']} audits of local decisions — run `run.sh detect audit --sheets` in a session "
                   "so drift is caught (aim for 20+/week).")
    elif a["agreement"] is not None and a["agreement"] < 0.9:
        out.append(f"Audit agreement {a['agreement']:.0%} is low — `run.sh detect audit-summary` shows which band "
                   "is wrong; recalibrate with `run.sh train calibrate`.")
    if wk["auto_rate"] is not None and wk["auto_rate"] < 0.6 and v["session"] >= 100:
        out.append(f"Local models decided {wk['auto_rate']:.0%} of events this week. More diverse references "
                   "(night/IR, lying down, far away) usually raise this most.")
    if not out:
        out.append("Nothing urgent. Keep sessions auditing; re-run `train full` after big gallery or camera changes.")
    return out


def print_status(s: dict) -> None:
    if "error" in s:
        print(s["error"])
        return
    v, wk, a, g, t = s["verdicts"], s["last_7_days"], s["audits"], s["gallery"], s["thresholds"]
    rate = f"{wk['auto_rate']:.0%}" if wk["auto_rate"] is not None else "n/a"
    print("Local model training status\n")
    print(f"  Verdicts          session {v['session']} · local {v['local']} · owner-confirmed {v['owner_confirmed']}")
    print(f"  Last 7 days       {wk['events']} events · decided locally {wk['decided_locally']} ({rate}) · "
          f"failed {wk['failed']}")
    print(f"  Review queue      {s['review_queue']} staged")
    agr = f"{a['agreement']:.0%}" if a["agreement"] is not None else "n/a"
    print(f"  Audits            {a['n']} · agreement {agr}")
    print(f"  Gallery           {g['images']} images · embedded {g['embedded_at'] or 'never'}")
    print(f"  Thresholds        accept {t['accept_threshold']} · rescue {t['rescue_threshold']} · "
          f"not-animal {t['not_winston_threshold']}")
    print("\nNext steps")
    for r in s["recommendations"]:
        print(f"  • {r}")


EXPORT_LABELS = {"winston": "target", "not_winston": "other_animal", "no_animal": "no_animal"}


def export(evalset_dir: Path, out: Path) -> dict:
    """Frozen eval-set frames → out/{Training,Testing}/<label>/ (hard links, copies across volumes).

    Only reviewer labels are exported (owner, audit, session — never a local-model
    decision); `uncertain` and gallery-overlap (`excluded`) events are left out.
    dev → Training, test → Testing, so a classifier is never scored on what it saw.
    """
    import os
    import shutil
    from src import evalset
    rows = evalset.load_manifest(evalset_dir)
    if not rows:
        raise SystemExit(f"no eval set at {evalset_dir} — run `scripts/run.sh train eval-set` first")
    counts: dict = {}
    for r in rows.values():
        label = EXPORT_LABELS.get(r.get("label"))
        split = {"dev": "Training", "test": "Testing"}.get(r.get("split"))
        if not (label and split):
            continue
        key = r.get("staging_key") or f"{r['device_id']}__{r['event_id']}"
        dest = out / split / label
        dest.mkdir(parents=True, exist_ok=True)
        for f in r.get("frames", []):
            src = evalset_dir / "frames" / key / f["name"]
            tgt = dest / f"{key}__{f['name']}"
            if tgt.exists() or not src.is_file():
                continue
            try:
                os.link(src, tgt)
            except OSError:
                shutil.copy2(src, tgt)
            counts[(split, label)] = counts.get((split, label), 0) + 1
    return {f"{s}/{lab}": n for (s, lab), n in sorted(counts.items())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    h = sub.add_parser("harvest")
    h.add_argument("--apply", action="store_true", help="write the new gallery (default: plan only)")
    h.add_argument("--target", type=int, default=36)
    sub.add_parser("evaluate")
    sub.add_parser("calibrate")
    sub.add_parser("eval-set")
    e = sub.add_parser("export")
    e.add_argument("--out", help="default: <data dir>/exports/classifier")
    e.add_argument("--evalset", help="default: <data dir>/evalset/v1")
    f = sub.add_parser("full")
    f.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    settings = load_settings()

    if args.cmd == "status":
        s = status(settings)
        if args.json:
            import json
            print(json.dumps(s, indent=2))
        else:
            print_status(s)
        return 0
    if args.cmd == "harvest":
        return _run("build_reference_set.py", "apply" if args.apply else "plan", "--target", str(args.target))
    if args.cmd == "evaluate":
        return _run("build_reference_set.py", "evaluate")
    if args.cmd == "calibrate":
        return _run("calibrate_local.py")
    if args.cmd == "eval-set":
        return _run("eval_set.py", "build") or _run("eval_set.py", "verify")
    if args.cmd == "export":
        out = Path(args.out) if args.out else paths.data_dir() / "exports" / "classifier"
        src = Path(args.evalset) if args.evalset else paths.data_dir() / "evalset" / "v1"
        counts = export(src, out)
        for k, n in counts.items():
            print(f"  {k:<28} {n} frames")
        print(f"\nWrote {out}\nCreate ML: New Project → Image Classifier → Training Data: {out / 'Training'},"
              f" Testing Data: {out / 'Testing'}. Train, then export the .mlmodel.")
        return 0
    if args.cmd == "full":
        print_status(status(settings))
        rc = _run("build_reference_set.py", "plan")
        rc = rc or _run("build_reference_set.py", "evaluate")
        if args.apply and not rc:
            rc = _run("build_reference_set.py", "apply")
        return rc or _run("calibrate_local.py")
    return 2


if __name__ == "__main__":
    sys.exit(main())
