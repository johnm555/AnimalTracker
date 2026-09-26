#!/usr/bin/env bash
# One-time setup for the WinstonTracker backend on the Mac Mini.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null; then
  echo "python3 not found. Install it with: brew install python" >&2
  exit 1
fi

echo "==> Creating virtualenv at .venv"
[ -d .venv ] || "$PYTHON" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r backend/requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env from .env.example — fill in ANTHROPIC_API_KEY and Ring credentials."
fi

mkdir -p backend/reference_images backend/ring_downloads

if command -v ffmpeg >/dev/null; then
  echo "==> ffmpeg found (fallback frame extractor available)"
else
  echo "==> ffmpeg not found; OpenCV will be used for frame extraction (brew install ffmpeg for the fallback)"
fi

echo "==> Running tests"
(cd backend && python -m pytest -q -p no:warnings)

N_REF=$(find backend/reference_images -type f ! -name '.gitkeep' | wc -l | tr -d ' ')
echo
echo "Setup complete."
echo "  Reference photos of Winston in backend/reference_images: $N_REF (aim for 4-6 clear shots)"
echo "  Edit backend/config/cameras.yaml so camera IDs match your Ring device names."
echo "  Then: scripts/run.sh api        # start the query API"
echo "        scripts/run.sh pipeline   # start the Ring poller (prompts for Ring 2FA the first time)"
