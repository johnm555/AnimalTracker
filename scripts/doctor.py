#!/usr/bin/env python3
"""Check an Animal Tracker installation and say how to fix what's wrong.

    scripts/run.sh doctor            # human-readable report, exit 1 on any failure
    scripts/run.sh doctor --json     # machine-readable (for agents and CI)

Read-only: it never writes configs, never contacts Ring, never sends a message.
Every check prints PASS / WARN / FAIL and, for anything but PASS, the command or
edit that fixes it.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import yaml  # noqa: E402

from src import paths  # noqa: E402

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
MIN_REFERENCES = 6


class Report:
    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []

    def add(self, status: str, check: str, detail: str = "", fix: str = "") -> None:
        self.rows.append({"status": status, "check": check, "detail": detail, "fix": fix})

    @property
    def failed(self) -> bool:
        return any(r["status"] == FAIL for r in self.rows)


def _load_yaml(path: Path) -> tuple[dict | None, str | None]:
    try:
        return (yaml.safe_load(path.read_text()) or {}), None
    except FileNotFoundError:
        return None, "missing"
    except yaml.YAMLError as e:
        return None, f"invalid YAML: {e}"


def check_data_dir(r: Report) -> Path:
    d = paths.data_dir()
    r.add(PASS, "data dir", str(d))
    env = paths.env_path()
    if env.is_file():
        keys = {line.split("=", 1)[0].strip() for line in env.read_text().splitlines()
                if "=" in line and not line.lstrip().startswith("#")}
        if "WINSTON_API_TOKEN" in keys:
            r.add(PASS, ".env", "API write token set")
        else:
            r.add(WARN, ".env", "WINSTON_API_TOKEN not set — API writes are unauthenticated",
                  f"echo WINSTON_API_TOKEN=$(python3 -c 'import secrets;print(secrets.token_hex(24))') >> \"{env}\"")
    else:
        r.add(WARN, ".env", f"{env} missing", "scripts/run.sh setup   (or copy .env.example there)")
    return d


def check_configs(r: Report) -> tuple[dict | None, object | None]:
    settings, err = _load_yaml(paths.settings_path())
    if settings is None:
        r.add(FAIL, "settings.yaml", f"{paths.settings_path()}: {err}", "scripts/run.sh setup")
    else:
        r.add(PASS, "settings.yaml", str(paths.settings_path()))

    cams, err = _load_yaml(paths.cameras_path())
    topology = None
    if cams is None:
        r.add(FAIL, "cameras.yaml", f"{paths.cameras_path()}: {err}", "scripts/run.sh setup")
    else:
        from src.state_machine import Topology
        try:
            topology = Topology.from_dict(cams)
        except (ValueError, KeyError, TypeError) as e:
            r.add(FAIL, "cameras.yaml", f"topology invalid: {e}", f"edit {paths.cameras_path()}")
        else:
            n_cams = sum(len(z.cameras) for z in topology.zones.values())
            r.add(PASS, "cameras.yaml", f"{len(topology.zones)} zones, {n_cams} cameras")
            isolated = [z for z in topology.zones.values() if not z.neighbors and len(topology.zones) > 1]
            if isolated:
                r.add(WARN, "topology", "zones with no neighbors (every move into them is 'via skipped cameras'): "
                      + ", ".join(z.id for z in isolated), "add neighbors with travel windows in cameras.yaml")
            empty = [z.id for z in topology.zones.values() if not z.cameras]
            if empty:
                r.add(WARN, "topology", f"zones with no camera can never be observed: {', '.join(empty)}")

    if settings is not None and topology is not None:
        zones = set(topology.zones)
        refs = {
            "notifications.high_priority_zones": (settings.get("notifications") or {}).get("high_priority_zones") or [],
            "stats.inside_zones": (settings.get("stats") or {}).get("inside_zones") or [],
            "stats.ambiguous_zones": (settings.get("stats") or {}).get("ambiguous_zones") or [],
        }
        for key, listed in refs.items():
            unknown = [z for z in listed if z not in zones]
            if unknown:
                r.add(FAIL, key, f"names zones not in cameras.yaml: {', '.join(unknown)}",
                      f"fix {key} in {paths.settings_path()}")
        if not refs["notifications.high_priority_zones"]:
            r.add(WARN, "high_priority_zones", "none set — no zone alerts through quiet hours",
                  "set notifications.high_priority_zones (e.g. the zone at your exit door)")
        ids = (settings.get("ring") or {}).get("device_ids")
        if not ids:
            r.add(WARN, "ring.device_ids", "empty — the poller watches NO cameras (or all, if the key is absent)",
                  "scripts/run.sh setup  (lists your Ring cameras and fills this in)")
    return settings, topology


def check_references(r: Report, settings: dict | None) -> None:
    d = paths.reference_images_dir(settings)
    imgs = [p for p in d.glob("*") if p.suffix.lower() in IMAGE_EXT] if d.is_dir() else []
    if len(imgs) >= MIN_REFERENCES:
        r.add(PASS, "reference photos", f"{len(imgs)} in {d}")
    elif imgs:
        r.add(WARN, "reference photos", f"only {len(imgs)} in {d}",
              f"add at least {MIN_REFERENCES}: side, front, lying down, outdoors, night; then scripts/run.sh train harvest once you have verdicts")
    else:
        r.add(FAIL, "reference photos", f"none in {d}",
              f"copy {MIN_REFERENCES}+ clear photos of your animal into {d}")


def check_ring(r: Report, settings: dict | None) -> None:
    tok = paths.ring_token_path(settings)
    if tok.is_file():
        r.add(PASS, "ring token", str(tok))
    else:
        r.add(FAIL, "ring token", f"{tok} missing", "scripts/run.sh ring-login   (one-time 2FA)")


def check_db(r: Report, settings: dict | None) -> None:
    db = Path(paths.db_path(settings))
    if not db.exists():
        r.add(WARN, "database", f"{db} not created yet (normal before first run)")
        return
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        ok = con.execute("PRAGMA quick_check").fetchone()[0]
        n = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        con.close()
    except sqlite3.Error as e:
        r.add(FAIL, "database", f"{db}: {e}", "restore from a backup; see docs/Setup_Guide.md#troubleshooting")
        return
    if ok != "ok":
        r.add(FAIL, "database", f"integrity: {ok}", "stop the service and restore from a backup")
    else:
        r.add(PASS, "database", f"{n} observations")
    # Stale copies left in the repo by older versions are a split-brain risk.
    stale = [p.name for p in (BACKEND / "winston.db", BACKEND / "tracker.db") if p.exists()]
    if stale:
        r.add(WARN, "stale data in repo", f"backend/{', backend/'.join(stale)}: no longer read (data dir is used)",
              "delete them once you have confirmed the data dir copy is current")


def check_notifications(r: Report, settings: dict | None) -> None:
    n = (settings or {}).get("notifications") or {}
    backend = n.get("backend", "log")
    if backend == "log":
        r.add(WARN, "notifications", "backend: log — nothing reaches your phone",
              "set notifications.backend to imessage (macOS), pushover, or apns")
    elif backend == "imessage":
        im = n.get("imessage") or {}
        who = os.environ.get(im.get("recipient_env", "IMESSAGE_RECIPIENT")) or im.get("recipient")
        if not who:
            r.add(FAIL, "imessage", "no recipient", "set notifications.imessage.recipient (phone or Apple ID email)")
        else:
            r.add(PASS, "imessage", "recipient set")
        chat = Path.home() / "Library" / "Messages" / "chat.db"
        try:
            sqlite3.connect(f"file:{chat}?mode=ro", uri=True).execute("SELECT 1 FROM message LIMIT 1")
            r.add(PASS, "imessage replies", "chat.db readable (mute/status replies work)")
        except sqlite3.Error:
            r.add(WARN, "imessage replies", "chat.db not readable — reply commands disabled",
                  "System Settings → Privacy & Security → Full Disk Access → add your terminal / python")
    elif backend == "pushover":
        p = n.get("pushover") or {}
        missing = [k for k in (p.get("user_key_env", "PUSHOVER_USER_KEY"), p.get("api_token_env", "PUSHOVER_API_TOKEN"))
                   if not os.environ.get(k)]
        r.add(FAIL if missing else PASS, "pushover", f"missing {', '.join(missing)}" if missing else "keys set",
              f"add {', '.join(missing)} to {paths.env_path()}" if missing else "")
    elif backend == "apns":
        a = n.get("apns") or {}
        missing = [a.get(k, d) for k, d in (("team_id_env", "APNS_TEAM_ID"), ("key_id_env", "APNS_KEY_ID"),
                                            ("private_key_path_env", "APNS_PRIVATE_KEY_PATH")) if not os.environ.get(a.get(k, d))]
        r.add(FAIL if missing else PASS, "apns", f"missing {', '.join(missing)}" if missing else "keys set",
              "APNs needs a paid Apple Developer account (.p8 key)" if missing else "")


def check_service(r: Report, settings: dict | None) -> None:
    if platform.system() == "Darwin":
        out = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.winstontracker.api"],
                             capture_output=True, text=True)
        if out.returncode == 0:
            r.add(PASS, "launch agent", "loaded (starts at login, restarts on crash)")
        else:
            r.add(WARN, "launch agent", "not installed — the tracker stops when you close the terminal",
                  "scripts/install-launchd.sh install")
    api = (settings or {}).get("api") or {}
    try:
        import httpx
        h = httpx.get(f"http://127.0.0.1:{api.get('port', 8420)}/healthz", timeout=3).json()
    except Exception:
        r.add(WARN, "api", "not answering on 127.0.0.1", "scripts/run.sh api")
        return
    status = h.get("status")
    pipe = (h.get("pipeline") or {})
    detail = f"status {status}, pipeline {pipe.get('state')}"
    if (h.get("storage") or {}).get("over_limit"):
        r.add(WARN, "storage", f"{h['storage'].get('total_gb')} GB > {h['storage'].get('max_storage_gb')} GB cap",
              "scripts/run.sh cleanup --apply   (or scripts/install-launchd.sh cleanup for a nightly job)")
    if pipe.get("last_error"):
        r.add(WARN, "pipeline", pipe["last_error"], "see ~/Library/Logs/WinstonTracker/api.err.log")
    r.add(PASS if status == "ok" else WARN, "api", detail)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip-service", action="store_true", help="don't query launchd or the running API")
    args = ap.parse_args(argv)

    r = Report()
    check_data_dir(r)
    settings, _ = check_configs(r)
    check_references(r, settings)
    check_ring(r, settings)
    check_db(r, settings)
    check_notifications(r, settings)
    if not args.skip_service:
        check_service(r, settings)

    if args.json:
        print(json.dumps({"ok": not r.failed, "checks": r.rows}, indent=2))
    else:
        mark = {PASS: "✓", WARN: "!", FAIL: "✗"}
        for row in r.rows:
            print(f" {mark[row['status']]} {row['check']:<22} {row['detail']}")
            if row["fix"] and row["status"] != PASS:
                print(f"   {'':<22} → {row['fix']}")
        n = {s: sum(x["status"] == s for x in r.rows) for s in (PASS, WARN, FAIL)}
        print(f"\n{n[PASS]} ok, {n[WARN]} warnings, {n[FAIL]} failures")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
