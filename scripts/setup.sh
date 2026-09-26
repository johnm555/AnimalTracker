#!/usr/bin/env bash
# One-time install: Python environment + dependencies + tests.
# Your property's configuration is a separate step: scripts/run.sh setup
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null; then
  echo "python3 not found. Install it with: brew install python" >&2
  exit 1
fi
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "Python 3.11+ required (found $("$PYTHON" --version))." >&2; exit 1; }

echo "==> Creating virtualenv at .venv"
[ -d .venv ] || "$PYTHON" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r backend/requirements.txt

DATA_DIR="${ANIMAL_TRACKER_DATA:-$HOME/Library/Application Support/AnimalTracker}"
mkdir -p "$DATA_DIR/config" "$DATA_DIR/reference_images"
echo "==> Site data directory: $DATA_DIR"

if command -v ffmpeg >/dev/null; then
  echo "==> ffmpeg found (fallback frame extractor available)"
else
  echo "==> ffmpeg not found; OpenCV will be used (brew install ffmpeg for the fallback)"
fi

echo "==> Running tests"
(cd backend && python -m pytest -q -p no:warnings)

echo
echo "Install complete. Next:"
if [ ! -f "$DATA_DIR/config/settings.yaml" ]; then
  echo "  scripts/run.sh setup                  # Ring login, pick cameras, define zones, notifications"
fi
echo "  scripts/run.sh doctor                 # check everything, with fixes"
echo "  scripts/install-launchd.sh install    # run at login, restart on crash"
