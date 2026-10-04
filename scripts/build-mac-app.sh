#!/bin/bash
# Build a local development .app. No site data is copied; no live service is changed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT="${1:-$ROOT/dist}"
APP="$OUTPUT/Animal Tracker.app"
mkdir -p "$OUTPUT"
if [ -e "$APP" ]; then
  echo "Refusing to overwrite $APP; choose an empty output directory." >&2
  exit 1
fi
swift build --package-path "$ROOT/macos/AnimalTracker" -c release
BIN="$(swift build --package-path "$ROOT/macos/AnimalTracker" -c release --show-bin-path)"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/Engine/backend/config" "$APP/Contents/Resources/Engine/scripts"
cp "$BIN/AnimalTracker" "$APP/Contents/MacOS/AnimalTracker"
cp "$ROOT/macos/AnimalTracker/Info.plist" "$APP/Contents/Info.plist"
# Allowlist framework resources, never recursively copy the repository or data directory.
cp -R "$ROOT/backend/src" "$APP/Contents/Resources/Engine/backend/"
find "$APP/Contents/Resources/Engine/backend/src" -name __pycache__ -type d -exec rm -r {} +
cp "$ROOT/backend/requirements.txt" "$APP/Contents/Resources/Engine/backend/"
cp "$ROOT/backend/config/"*.example.yaml "$APP/Contents/Resources/Engine/backend/config/"
cp "$ROOT/scripts/desktop_bridge.py" "$ROOT/scripts/prepare_runtime.py" "$ROOT/scripts/setup_wizard.py" "$ROOT/scripts/doctor.py" "$APP/Contents/Resources/Engine/scripts/"
if [ -f "$ROOT/macos/AnimalTracker/AppIcon.icns" ]; then cp "$ROOT/macos/AnimalTracker/AppIcon.icns" "$APP/Contents/Resources/"; fi
codesign --force --sign - --entitlements "$ROOT/macos/AnimalTracker/AnimalTracker.entitlements" "$APP"
codesign --verify --deep --strict "$APP"
printf 'Built local development app: %s\n' "$APP"
printf '%s\n' 'Distribution to other Macs requires Developer ID signing and notarization.'
