#!/bin/bash
# Notarized distribution build. Requires a Developer ID and a saved notarytool profile.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT="${1:?Usage: scripts/release-mac-app.sh NEW_OUTPUT_DIRECTORY}"
IDENTITY="${ANIMAL_TRACKER_SIGNING_IDENTITY:-}"
PROFILE="${ANIMAL_TRACKER_NOTARY_PROFILE:-}"
if [[ "$IDENTITY" != "Developer ID Application: "* ]] || [ -z "$PROFILE" ]; then
  echo 'Set ANIMAL_TRACKER_SIGNING_IDENTITY to a Developer ID Application certificate and ANIMAL_TRACKER_NOTARY_PROFILE to a saved notarytool keychain profile.' >&2
  exit 1
fi
"$ROOT/scripts/build-mac-app.sh" "$OUTPUT"
APP="$OUTPUT/Animal Tracker.app"
ZIP="$OUTPUT/AnimalTracker.zip"
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
  --entitlements "$ROOT/macos/AnimalTracker/AnimalTracker.entitlements" "$APP"
codesign --verify --deep --strict "$APP"
ditto -c -k --keepParent "$APP" "$ZIP"
# notarytool exits nonzero on a failed request; stapling/assessment also gate delivery.
xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
spctl --assess --type execute --verbose "$APP"
# Replace only this script's archive, now containing the stapled ticket.
rm "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
shasum -a 256 "$ZIP" > "$OUTPUT/AnimalTracker.sha256"
printf 'Notarized release ready: %s\n' "$ZIP"
