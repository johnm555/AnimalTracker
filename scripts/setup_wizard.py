#!/usr/bin/env python3
"""Configure Animal Tracker for your property.

    scripts/run.sh setup                          # interactive
    scripts/run.sh setup --list-cameras           # print your Ring cameras as JSON (for agents)
    scripts/run.sh setup --answers answers.json   # non-interactive (what the Claude/Codex skill uses)
    scripts/run.sh setup ... --dry-run            # print the YAML, write nothing
    scripts/run.sh setup ... --force              # replace existing configs (old ones are backed up)

Writes `config/settings.yaml`, `config/cameras.yaml` and `.env` in the data dir
(~/Library/Application Support/AnimalTracker, or $ANIMAL_TRACKER_DATA). The
topology is validated before anything is written.

answers.json:

    {
      "animal": {"name": "Max", "description": "golden retriever, red collar"},
      "cameras": [{"device_id": "123", "name": "Back Door", "zone": "kitchen"},
                  {"device_id": "456", "name": "Yard Cam",  "zone": "backyard"}],
      "neighbors": [{"a": "kitchen", "b": "backyard", "min_seconds": 0, "max_seconds": 60}],
      "high_priority_zones": ["backyard"],
      "inside_zones": ["kitchen"],
      "ambiguous_zones": [],
      "notifications": {"backend": "imessage", "recipient": "+15551234567",
                        "quiet_hours": {"start": "23:00", "end": "06:30"}}
    }

Travel windows: `min_seconds` is the fastest the animal could possibly make the
trip. Leave it at 0 until you have measured it — a guessed minimum makes the
tracker reject real sightings. `max_seconds` is a typical slow walk; slower
moves are still accepted, with lower confidence.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import yaml  # noqa: E402

from src import paths  # noqa: E402
from src.state_machine import Topology, normalize_camera_id  # noqa: E402

EXAMPLE_SETTINGS = BACKEND / "config" / "settings.example.yaml"
BACKENDS = ("imessage", "pushover", "apns", "log")


def zone_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


# --------------------------------------------------------------------------- #
# Pure: answers -> configs (tested; no I/O)
# --------------------------------------------------------------------------- #

def build_cameras(answers: dict[str, Any]) -> dict[str, Any]:
    zones: dict[str, dict[str, Any]] = {}
    for cam in answers["cameras"]:
        z = zone_id(cam["zone"])
        spec = zones.setdefault(z, {"cameras": [], "description": cam.get("zone_description", cam["zone"]),
                                    "neighbors": {}})
        spec["cameras"].append(normalize_camera_id(cam["name"]))
    for n in answers.get("neighbors", []):
        a, b = zone_id(n["a"]), zone_id(n["b"])
        if a not in zones or b not in zones:
            raise ValueError(f"neighbor {a} <-> {b}: both zones need at least one camera")
        zones[a]["neighbors"][b] = {"min_seconds": float(n.get("min_seconds", 0)),
                                    "max_seconds": float(n.get("max_seconds", 120))}
    for spec in zones.values():
        if not spec["neighbors"]:
            del spec["neighbors"]
    data = {"zones": zones}
    Topology.from_dict(data)  # raises on unknown neighbors, duplicate cameras, bad windows
    return data


def build_settings(answers: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    s = json.loads(json.dumps(base))  # deep copy
    zones = {zone_id(c["zone"]) for c in answers["cameras"]}
    animal = answers.get("animal") or {}
    s["animal"] = {"name": animal.get("name", "your animal"), "description": animal.get("description", "")}
    s.setdefault("ring", {})["device_ids"] = [str(c["device_id"]) for c in answers["cameras"]]
    n = s.setdefault("notifications", {})
    for key, target in (("high_priority_zones", n), ("inside_zones", s.setdefault("stats", {})),
                        ("ambiguous_zones", s["stats"])):
        listed = [zone_id(z) for z in answers.get(key, [])]
        unknown = [z for z in listed if z not in zones]
        if unknown:
            raise ValueError(f"{key} names unknown zones: {', '.join(unknown)}")
        target[key] = listed
    na = answers.get("notifications") or {}
    backend = na.get("backend", "imessage" if platform.system() == "Darwin" else "log")
    if backend not in BACKENDS:
        raise ValueError(f"notifications.backend must be one of {BACKENDS}")
    n["backend"] = backend
    if na.get("recipient"):
        n.setdefault("imessage", {})["recipient"] = na["recipient"]
    if na.get("quiet_hours"):
        n["quiet_hours"] = na["quiet_hours"]
    return s


# --------------------------------------------------------------------------- #
# Ring
# --------------------------------------------------------------------------- #

def ring_cameras(interactive: bool) -> list[dict[str, Any]]:
    from src.ring_client import RingClient, RingSettings
    client = RingClient(RingSettings.from_dict({}))  # no allow-list: list everything on the account
    if not client.settings.token_cache.is_file():
        if not interactive:
            raise SystemExit("No Ring token yet. Run `scripts/run.sh ring-login` once (it asks for 2FA).")
        print("\nRing login (one time; only the refresh token is stored, not your password).")
        os.environ.setdefault("RING_USERNAME", input("  Ring email: ").strip())
        os.environ.setdefault("RING_PASSWORD", getpass.getpass("  Ring password: "))
        client = RingClient(RingSettings.from_dict({}))
    client.authenticate()
    return client.get_cameras()


# --------------------------------------------------------------------------- #
# Interactive prompts
# --------------------------------------------------------------------------- #

def ask(prompt: str, default: str = "") -> str:
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def pick(prompt: str, options: list[str], allow_none: bool = True) -> list[str]:
    for i, o in enumerate(options, 1):
        print(f"    {i}. {o}")
    raw = ask(f"{prompt} (numbers, comma-separated{', blank for none' if allow_none else ''})")
    out = []
    for part in filter(None, (p.strip() for p in raw.split(","))):
        if part.isdigit() and 1 <= int(part) <= len(options):
            out.append(options[int(part) - 1])
    return out


def interview() -> dict[str, Any]:
    print("Animal Tracker setup — about 5 minutes. Blank answers take the [default].\n")
    a: dict[str, Any] = {"animal": {"name": ask("Your animal's name", "Max"),
                                    "description": ask("Short description (breed, colour, collar)", "")}}
    cams = ring_cameras(interactive=True)
    print(f"\nFound {len(cams)} Ring cameras:")
    chosen = pick("Which ones can see your animal", [f"{c['name']}  ({c['device_id']})" for c in cams],
                  allow_none=False)
    chosen_cams = [c for c in cams if f"{c['name']}  ({c['device_id']})" in chosen]
    if not chosen_cams:
        raise SystemExit("No cameras chosen.")
    print("\nZones are the places you want reported (\"kitchen\", \"backyard\"). Cameras that see the\n"
          "same place share a zone — type the same zone name.")
    a["cameras"] = []
    for c in chosen_cams:
        z = ask(f"  Zone for '{c['name']}'", c["camera_id"])
        a["cameras"].append({"device_id": c["device_id"], "name": c["name"], "zone": z})
    zones = sorted({zone_id(c["zone"]) for c in a["cameras"]})

    print("\nNeighbors: zones your animal can walk between directly, without passing another camera.")
    a["neighbors"] = []
    seen = set()
    for z in zones:
        others = [o for o in zones if o != z and (o, z) not in seen]
        if not others:
            continue
        for o in pick(f"  From '{z}', directly to", others):
            mx = ask(f"    Typical slow walk {z} → {o}, seconds", "120")
            a["neighbors"].append({"a": z, "b": o, "min_seconds": 0, "max_seconds": float(mx)})
            seen.add((z, o))

    print("\nHigh-priority zones alert immediately, even during quiet hours (e.g. an exit door, the street side).")
    a["high_priority_zones"] = pick("  High-priority", zones)
    a["inside_zones"] = pick("  Which zones are indoors (for inside/outside stats)", zones)
    a["ambiguous_zones"] = pick("  Which zones straddle inside and outside (doorways)", zones)

    default_backend = "imessage" if platform.system() == "Darwin" else "log"
    backend = ask(f"\nNotifications via ({' | '.join(BACKENDS)})", default_backend)
    notif: dict[str, Any] = {"backend": backend}
    if backend == "imessage":
        notif["recipient"] = ask("  Send iMessages to (phone number or Apple ID email)")
    qs = ask("  Quiet hours start (HH:MM, blank for none)", "23:00")
    if qs:
        notif["quiet_hours"] = {"start": qs, "end": ask("  Quiet hours end", "06:30")}
    a["notifications"] = notif
    return a


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def _backup(path: Path) -> None:
    if path.exists():
        dest = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        path.rename(dest)
        print(f"  backed up {path.name} → {dest.name}")


def write_env() -> None:
    env = paths.env_path()
    lines = env.read_text().splitlines() if env.exists() else (ROOT / ".env.example").read_text().splitlines()
    if not any(line.startswith("WINSTON_API_TOKEN=") and line.split("=", 1)[1].strip() for line in lines):
        lines = [line for line in lines if not line.startswith("WINSTON_API_TOKEN=")]
        lines.append(f"WINSTON_API_TOKEN={secrets.token_hex(24)}")
    env.write_text("\n".join(lines) + "\n")
    os.chmod(env, 0o600)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--answers", help="JSON answers file (non-interactive)")
    ap.add_argument("--list-cameras", action="store_true", help="print Ring cameras as JSON and exit")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="replace existing configs (backs them up first)")
    args = ap.parse_args(argv)

    if args.list_cameras:
        print(json.dumps(ring_cameras(interactive=sys.stdin.isatty()), indent=2))
        return 0

    sp, cp = paths.settings_path(), paths.cameras_path()
    if (sp.exists() or cp.exists()) and not (args.force or args.dry_run):
        print(f"Configs already exist in {sp.parent}. Re-run with --force to replace them "
              "(the old files are kept as .bak-*), or edit them directly.")
        return 1

    answers = json.loads(Path(args.answers).read_text()) if args.answers else interview()
    try:
        cameras = build_cameras(answers)
        settings = build_settings(answers, yaml.safe_load(EXAMPLE_SETTINGS.read_text()))
    except (ValueError, KeyError) as e:
        print(f"Setup answers are invalid: {e}", file=sys.stderr)
        return 2

    header = "# Written by scripts/run.sh setup on {:%Y-%m-%d}. Edit freely; run `scripts/run.sh doctor` after.\n"
    s_yaml = header.format(datetime.now()) + yaml.safe_dump(settings, sort_keys=False)
    c_yaml = header.format(datetime.now()) + yaml.safe_dump(cameras, sort_keys=False)
    if args.dry_run:
        print(f"--- {cp}\n{c_yaml}\n--- {sp}\n{s_yaml}")
        return 0

    sp.parent.mkdir(parents=True, exist_ok=True)
    for p in (sp, cp):
        _backup(p)
    sp.write_text(s_yaml)
    cp.write_text(c_yaml)
    write_env()
    ref = paths.reference_images_dir(settings)
    ref.mkdir(parents=True, exist_ok=True)
    print(f"\nWrote {cp}\n      {sp}\n      {paths.env_path()} (API token generated)\n")
    print("Next:")
    print(f"  1. Put 6+ clear photos of {settings['animal']['name']} in:\n       {ref}")
    print("     (side, front, lying down, outdoors, at night if you can)")
    print("  2. scripts/run.sh doctor                 # checks everything")
    print("  3. scripts/install-launchd.sh install    # run at login, restart on crash")
    return 0


if __name__ == "__main__":
    sys.exit(main())
