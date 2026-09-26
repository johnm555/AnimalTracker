#!/usr/bin/env python
"""Record self-reported session metrics or inspect recent review activity."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from src.api import load_settings  # noqa: E402
from src.paths import env  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url")
    sub = p.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record", help="Post one completed, partial, or failed run report")
    record.add_argument("file", type=Path)
    report = sub.add_parser("report")
    report.add_argument("--hours", type=float, default=24)
    args = p.parse_args()
    base = args.url or f"http://127.0.0.1:{load_settings().get('api', {}).get('port', 8420)}"
    token = env("API_TOKEN")
    try:
        body = json.loads(args.file.read_text()) if args.command == "record" else None
        with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=15) as c:
            response = (c.post("/tracker/detection-runs", json=body) if args.command == "record"
                        else c.get("/tracker/detection-runs", params={"hours": args.hours}))
            response.raise_for_status()
            print(json.dumps(response.json(), indent=2))
    except httpx.HTTPStatusError as error:
        print(f"Backend returned HTTP {error.response.status_code}; report not confirmed.", file=sys.stderr)
        return 1
    except (httpx.HTTPError, OSError, ValueError):
        print("Unable to read file or backend response; report not confirmed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
