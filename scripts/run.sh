#!/usr/bin/env bash
# Run the backend services.
#   scripts/run.sh api                 The whole system: API server + in-process Ring poller
#   scripts/run.sh ring-login          One-time interactive Ring login (2FA) to create the token cache
#   scripts/run.sh detect <cmd>        Session-mode detection: list | record | record-batch | skip | status
#   scripts/run.sh animals <cmd>       Visiting animals: report | candidates | label | record
#   scripts/run.sh cleanup [--apply]   Retention cleanup of frames/clips/thumbnails (dry run by default)
#   scripts/run.sh calibrate-local     Calibrate the DINOv2 thresholds against archived verdicts
#   scripts/run.sh pipeline [args]     Standalone poller (debugging; --once, --post URL, --since ISO)
#   scripts/run.sh replay <dir>        Replay fixture clips offline (no Ring account needed)
#   scripts/run.sh ring-settings ...   Audit / tune Ring camera motion settings (see docs/Camera_Settings_Audit.md)
#   scripts/run.sh findmy-login        One-time interactive FindMy.py iCloud login (2FA) for AirTag polling
#   scripts/run.sh findmy-test         Verify AirTag location fetch works with saved session
#   scripts/run.sh setup [--answers f] Configure your property: cameras, zones, notifications (writes the data dir)
#   scripts/run.sh doctor [--json]     Check the installation; prints how to fix each problem
#   scripts/run.sh train <cmd>         Improve the local models: status | harvest | evaluate | calibrate | eval-set | full
#   scripts/run.sh test                Run the backend test suite
#   scripts/run.sh watch-test          Run the Swift AnimalTrackerCore tests (works without Xcode)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -x .venv/bin/python ]; then
  echo "No virtualenv found. Run scripts/setup.sh first." >&2
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Site data lives in ~/Library/Application Support/AnimalTracker (macOS)
DATA_DIR="${ANIMAL_TRACKER_DATA:-$HOME/Library/Application Support/AnimalTracker}"
export ANIMAL_TRACKER_DATA="$DATA_DIR"

# Load .env from data dir first, then repo root as fallback
if [ -f "$DATA_DIR/.env" ]; then
  set -a; # shellcheck disable=SC1091
  source "$DATA_DIR/.env"; set +a
elif [ -f .env ]; then
  set -a; # shellcheck disable=SC1091
  source .env; set +a
fi

cd backend

case "${1:-api}" in
  api)
    # Read host/port only here: `setup` and `doctor` must work before settings.yaml exists.
    HOST=$(python -c "from src.paths import settings_path; import yaml; print(yaml.safe_load(open(settings_path()))['api']['host'])")
    PORT=$(python -c "from src.paths import settings_path; import yaml; print(yaml.safe_load(open(settings_path()))['api']['port'])")
    exec uvicorn src.api:app --host "$HOST" --port "$PORT" --log-level info ;;
  ring-login)
    exec python -c "
from src.ring_client import RingClient
from src.api import load_settings
c = RingClient.from_settings(load_settings().get('ring')); c.authenticate()
print('Ring login OK; token cached at', c.settings.token_cache.resolve())
for cam in c.get_cameras(): print('  ', cam['device_id'], cam['camera_id'])" ;;
  check-camera)
    shift; exec python -m src.capture "$@" ;;
  pipeline)
    shift; exec python -m src.pipeline "$@" ;;
  ring-settings)
    shift; exec python "$ROOT/scripts/ring_camera_settings.py" "$@" ;;
  detect)
    shift; exec python "$ROOT/scripts/detect_pending.py" "$@" ;;
  animals)
    shift; exec python "$ROOT/scripts/animals.py" "$@" ;;
  detection-runs)
    shift; exec python "$ROOT/scripts/detection_runs.py" "$@" ;;
  quality)
    shift; exec python "$ROOT/scripts/quality.py" "$@" ;;
  cleanup)
    shift; exec python "$ROOT/scripts/cleanup.py" "$@" ;;
  eval-set)
    shift; exec python "$ROOT/scripts/eval_set.py" "$@" ;;
  setup)
    shift; exec python "$ROOT/scripts/setup_wizard.py" "$@" ;;
  doctor)
    shift; exec python "$ROOT/scripts/doctor.py" "$@" ;;
  train)
    shift; exec python "$ROOT/scripts/train.py" "$@" ;;
  calibrate-local)
    shift; exec python "$ROOT/scripts/calibrate_local.py" "$@" ;;
  findmy-login)
    exec python "$ROOT/backend/scripts/findmy_login.py" ;;
  findmy-test)
    exec python -c "
from findmy.reports import AppleAccount
from findmy.accessory import FindMyAccessory
acc = AppleAccount.from_json('secrets/findmy_account.json')
dev = FindMyAccessory.from_json('secrets/winston_airtag.json')
print(f'Device: {dev.name} (serial: {dev.serial_number})')
r = acc.fetch_location(dev)
if r is None: print('No location report available')
else: print(f'  {r.timestamp} — ({r.latitude:.6f}, {r.longitude:.6f})')
dev.to_json('secrets/winston_airtag.json')
acc.to_json('secrets/findmy_account.json')
print('Session updated.')
" ;;
  replay)
    shift; exec python -m src.pipeline --fixtures "${1:?usage: run.sh replay <dir>}" ;;
  test)
    exec python -m pytest -q -p no:warnings ;;
  watch-test)
    cd "$ROOT/ios/AnimalTrackerWatch"
    if xcode-select -p 2>/dev/null | grep -q "Xcode.app"; then
      exec swift test
    fi
    # Command Line Tools only: point swiftpm at its bundled Swift Testing framework.
    CLT=$(xcode-select -p)
    FW="$CLT/Library/Developer/Frameworks"; LIB="$CLT/Library/Developer/usr/lib"
    exec swift test -Xswiftc -F -Xswiftc "$FW" -Xlinker -F -Xlinker "$FW" \
         -Xlinker -rpath -Xlinker "$FW" -Xlinker -rpath -Xlinker "$LIB" ;;
  *)
    echo "unknown command: $1" >&2; sed -n '2,20p' "$0"; exit 2 ;;
esac
