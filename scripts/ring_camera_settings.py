#!/usr/bin/env python
"""Audit and (optionally) tune Ring camera settings for dog detection.

    scripts/run.sh ring-settings audit                 # read-only table of the 8 allow-listed cameras
    scripts/run.sh ring-settings optimize              # dry run: show the PATCH that would be sent
    scripts/run.sh ring-settings optimize --apply      # write it (backs up current settings first)
    scripts/run.sh ring-settings optimize --apply --frequency frequent
    scripts/run.sh ring-settings restore <backup.json> # put a camera's motion settings back

Reads credentials/token exactly like the pipeline (RingClient + settings.yaml).
Settings are written with `PATCH /devices/v1/devices/{id}/settings`, the same
endpoint `ring_doorbell.RingDoorBell.async_set_motion_detection` and
ring-client-api's `setDeviceSettings` use. Only the keys in the payload are
touched; everything else on the camera is left alone.

Background and per-camera findings: docs/Camera_Settings_Audit.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from src.ring_client import RingClient, RingSettings  # noqa: E402

SETTINGS_ENDPOINT = "/devices/v1/devices/{0}/settings"
BACKUP_DIR = BACKEND / "ring_settings_backup"  # gitignored

# Ring app "Motion Frequency" is `motion_settings.motion_snooze_profile`: the
# escalating post-event snooze in minutes. Verified live 2026-09-20: Frequent
# cameras report [0, 0, 0], Regular ones [1, 5, 15]; the legacy read-only
# `motion_snooze_preset_profile` string (none/low/...) is derived from it.
# The legacy PUT doorbots/{id} with motion_snooze_preset_profile returns 204
# and silently changes nothing. "light" is an educated guess, not verified.
FREQUENCY_PROFILES = {"frequent": [0, 0, 0], "regular": [1, 5, 15], "light": [5, 15, 30]}


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _client() -> RingClient:
    _load_env()
    cfg = yaml.safe_load((BACKEND / "config/settings.yaml").read_text())["ring"]
    os.chdir(BACKEND)  # token_cache / download_dir are relative to backend/
    client = RingClient(RingSettings.from_dict(cfg))

    def no_otp(prompt: str) -> str:
        raise SystemExit("Ring asked for a 2FA code; run scripts/run.sh pipeline once interactively.")

    client.authenticate(otp_callback=no_otp)
    return client


def fetch_settings(client: RingClient, device_id: str) -> dict[str, Any]:
    return client._ring.query(SETTINGS_ENDPOINT.format(device_id)).json()


def summarize(dev: Any, s: dict[str, Any]) -> dict[str, Any]:
    ms, am = s["motion_settings"], s["advanced_motion_settings"]
    vs, gs, ss = s["video_settings"], s["general_settings"], s["snapshot_settings"]
    pir = s.get("pir_settings", {})
    legacy = dev._attrs.get("settings", {})
    zones = []
    for key in ("zone_1", "zone_2", "zone_3"):
        z = am.get(key)
        if z and z.get("state"):
            verts = [(z[f"vertex{i}"]["x"], z[f"vertex{i}"]["y"]) for i in range(1, 9) if f"vertex{i}" in z]
            zones.append({"name": z["name"], "vertices": verts})
    return {
        "name": dev.name,
        "device_id": str(dev.id),
        "model": dev.model,
        "power_mode": gs.get("power_mode"),
        "battery": dev.battery_life,
        "motion_detection_enabled": ms.get("motion_detection_enabled"),
        "people_only_mode": ms.get("advanced_motion_detection_human_only_mode"),
        "detection_types": legacy.get("advanced_motion_detection_types"),
        "sensitivity": am.get("sensitivity"),
        "pir_sensitivity": pir.get("sensitivity_1"),
        "pir_validation": ms.get("enable_pir_validation"),
        "motion_frequency_profile": legacy.get("motion_snooze_preset_profile"),
        "end_detection_s": ms.get("end_detection"),
        "zones_enabled": ms.get("advanced_motion_zones_enabled"),
        "zones": zones,
        "snapshot_capture": gs.get("lite_24x7_enabled"),
        "snapshot_interval_s": ss.get("frequency_secs"),
        "clip_length_s": {"min": vs.get("clip_length_min"), "max": vs.get("clip_length_max"),
                          "auto": vs.get("auto_clip_length_enabled")},
        "night_vision": {"ir_led": ms.get("enable_ir_led"), "ir_brightness": vs.get("ir_led_brightness"),
                         "color_night_vision": vs.get("night_color_enable")},
    }


def cmd_audit(args: argparse.Namespace) -> int:
    client = _client()
    rows = [summarize(dev, fetch_settings(client, dev_id)) for dev_id, dev in client._devices_by_id.items()]
    rows.sort(key=lambda r: r["name"])
    cols = ["name", "device_id", "power_mode", "battery", "people_only_mode", "sensitivity",
            "pir_sensitivity", "motion_frequency_profile", "snapshot_interval_s"]
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join(str(r[c]) for c in cols))
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2))
        print(f"\nwrote {args.json}")
    return 0


def build_patch(frequency: str | None) -> dict[str, Any]:
    motion: dict[str, Any] = {
        # The one that matters: stop discarding motion events unless a person is in frame.
        "advanced_motion_detection_human_only_mode": False,
        # Belt and braces: don't let "record only when a person is present" sneak in.
        "advanced_motion_recording_human_mode": False,
        "motion_detection_enabled": True,
    }
    if frequency:
        motion["motion_snooze_profile"] = FREQUENCY_PROFILES[frequency]
    return {"motion_settings": motion}


def cmd_optimize(args: argparse.Namespace) -> int:
    client = _client()
    patch = build_patch(args.frequency)
    targets = {d: dev for d, dev in client._devices_by_id.items()
               if not args.only or d in args.only or dev.name in args.only}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"{'APPLYING' if args.apply else 'DRY RUN'} -> {len(targets)} camera(s)\n")
    print("PATCH", SETTINGS_ENDPOINT.format("{id}"), json.dumps(patch, indent=2))
    print()
    for dev_id, dev in targets.items():
        before = fetch_settings(client, dev_id)
        cur = before["motion_settings"].get("advanced_motion_detection_human_only_mode")
        prof = before["motion_settings"].get("motion_snooze_profile")
        print(f"{dev.name:18} {dev_id}  people_only={cur}  snooze_profile={prof}")
        if not args.apply:
            continue
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"{dev_id}-{stamp}.json"
        backup.write_text(json.dumps({"device_id": dev_id, "name": dev.name, "settings": before}, indent=2))
        client._ring.query(SETTINGS_ENDPOINT.format(dev_id), method="PATCH", json=patch)
        after = fetch_settings(client, dev_id)["motion_settings"]
        ok = all(after.get(k) == v for k, v in patch["motion_settings"].items())
        print(f"{'':18} -> people_only={after.get('advanced_motion_detection_human_only_mode')}"
              f"  snooze_profile={after.get('motion_snooze_profile')}"
              f"  {'OK' if ok else 'NOT APPLIED'}  backup={backup.relative_to(ROOT)}")
    if not args.apply:
        print("\nNothing written. Re-run with --apply to change the cameras.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    client = _client()
    data = json.loads(Path(args.backup).read_text())
    dev_id = data["device_id"]
    ms = data["settings"]["motion_settings"]
    patch = {"motion_settings": {k: ms[k] for k in (
        "advanced_motion_detection_human_only_mode", "advanced_motion_recording_human_mode",
        "motion_detection_enabled", "motion_snooze_profile") if k in ms}}
    print(f"restoring {data['name']} ({dev_id}):", json.dumps(patch))
    client._ring.query(SETTINGS_ENDPOINT.format(dev_id), method="PATCH", json=patch)
    after = fetch_settings(client, dev_id)["motion_settings"]
    print("now people_only =", after.get("advanced_motion_detection_human_only_mode"),
          "snooze_profile =", after.get("motion_snooze_profile"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit", help="print current settings for the allow-listed cameras")
    a.add_argument("--json", help="also write the full per-camera summary to this file")
    a.set_defaults(fn=cmd_audit)
    o = sub.add_parser("optimize", help="turn off People Only mode (dry run unless --apply)")
    o.add_argument("--apply", action="store_true", help="actually PATCH the cameras")
    o.add_argument("--frequency", choices=sorted(FREQUENCY_PROFILES), help="also set Motion Frequency")
    o.add_argument("--only", nargs="*", help="device ids or names to limit to")
    o.set_defaults(fn=cmd_optimize)
    r = sub.add_parser("restore", help="restore motion settings from a backup written by --apply")
    r.add_argument("backup")
    r.set_defaults(fn=cmd_restore)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
