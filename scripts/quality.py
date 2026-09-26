#!/usr/bin/env python
"""Terminal detection-quality dashboard and authenticated manual review entry."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from src.api import load_settings
from src.paths import env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Backend URL (default: localhost and configured API port)")
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="Read-only per-camera quality dashboard")
    report.add_argument("--hours", type=float, default=24)
    report.add_argument("--json", action="store_true")
    review = sub.add_parser("review", help="Append a review after inspecting evidence")
    review.add_argument("observation_id", type=int)
    review.add_argument("--label", choices=["winston", "not_winston", "uncertain"], required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--notes", required=True)
    args = parser.parse_args()
    port = load_settings().get("api", {}).get("port", 8420)
    base = (args.url or f"http://127.0.0.1:{port}").rstrip("/")
    token = env("API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(base_url=base, headers=headers, timeout=15) as client:
            if args.command == "review":
                response = client.post(f"/tracker/observations/{args.observation_id}/reviews",
                                       json={"label": args.label, "reviewer": args.reviewer, "notes": args.notes})
            else:
                response = client.get("/tracker/quality", params={"hours": args.hours})
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as error:
        print(f"Backend returned HTTP {error.response.status_code}; no successful action confirmed.", file=sys.stderr)
        return 1
    except (httpx.HTTPError, ValueError):
        print("Could not read a valid backend response; no successful action confirmed.", file=sys.stderr)
        return 1
    if args.command == "review" or args.json:
        print(json.dumps(data, indent=2))
        return 0
    print(f"Detection quality: {data['since']} to {data['until']}")
    print(f"Threshold: {data['threshold']}; explicit reviews only; uncertain labels excluded.")
    print(f"{'Camera / Ring device':48} {'TP':>5} {'FP':>5} {'TN':>5} {'FN':>5} {'Uncertain':>10} {'Unreviewed':>11}")
    for row in data["cameras"]:
        name = row["camera_id"] + " / " + (row["ring_device_id"] or "unknown")
        print(f"{name:48} {row['true_positive']:5} {row['false_positive']:5} {row['true_negative']:5} "
              f"{row['false_negative']:5} {row['uncertain']:10} {row['unreviewed']:11}")
    print("Totals: " + json.dumps(data["totals"], sort_keys=True))
    print("Review sample only; this does not measure events cameras never captured.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
