# Animal Tracker for Mac (developer preview)

The native SwiftUI app guides a new installation through engine preparation,
an animal profile, Ring login and verification, camera selection, zone routes,
reference photos, Messages, optional Find My account setup, and a reviewed save.
It wraps the existing Python backend and setup validation. It does not change
the detector or deterministic tracker.

## Build and open

Requires macOS 14+, Xcode command-line tools with Swift 5.9+, and Python 3.11+
(Python 3.12 recommended for the runtime). Build on the architecture you intend
to use; this script does not produce a universal binary.

```sh
scripts/build-mac-app.sh /tmp/AnimalTracker-build
open '/tmp/AnimalTracker-build/Animal Tracker.app'
```

Move the app to your Applications folder. The build is locally ad-hoc signed;
it is **not notarized or ready for frictionless distribution to other Macs**.
A release needs a Developer ID signature, a notarization/stapling step, and a
clean-Mac acceptance test. The preview downloads dependencies from PyPI during
setup; it is not a self-contained offline runtime or a Mac App Store build.

The bundle contains an allowlisted copy of framework Python sources, dependency
requirements, and example configs. It contains no site data. Build output goes
to ignored `dist/` by default; the script refuses to overwrite an existing app.

## First run

1. **Tracking engine:** select an installed Python executable and click Prepare
   engine. The app creates `desktop-runtime/` in the data directory and installs
   the requirements there. Internet, several GB of disk space, and several
   minutes may be needed. A failed installation can be retried. It never runs
   `sudo` or installs into system Python.
2. **Your animal:** enter the name and identifying appearance. This version
   tracks one enrolled animal; it does not provide independent multi-pet tracks.
3. **Ring:** enter account credentials, then the verification code when prompted.
   A saved session can be reused. The password is discarded after submission;
   the existing Ring client saves its refresh token. Select cameras at one
   property and enable All Motion in Ring. Event recordings must be available.
4. **Cameras and zones:** name the areas each selected camera sees. Add direct
   neighbor routes and typical slow-walk windows. Minimum travel time remains
   zero until measured. No zones are automatically marked high priority or
   indoors; advanced policy remains configurable in the existing YAML tools.
5. **Photos:** import at least six varied JPEG, PNG or WebP reference images.
   Imports remove EXIF metadata and avoid duplicate copies. Existing reference
   photos remain in place; do not mix different enrolled animals in one gallery.
6. **Messages:** sign into the Mac’s Messages app, choose the recipient, and
   optionally confirm a setup test. The test does not create an observation.
   A successful AppleScript call means Messages accepted it, not that delivery
   to an iPhone or Watch has been verified. macOS Automation permission may be
   needed; Full Disk Access is only needed for the backend’s reply reader.
7. **Find My:** optional and explicitly experimental. An Apple account session
   can be saved with trusted-device verification using the unofficial library
   and a user-approved HTTPS anisette service. This is **not working AirTag
   tracking**: tag keys and the poller remain separate work. The anisette service
   supplies authentication metadata; review its operator before opting in.
8. **Review:** validate the plan, then save. Existing files require an explicit
   replacement checkbox and get private timestamped backups. This replaces the
   camera/route list and notification selection, not just the visible fields.
   Other settings are retained. Writes roll back on an ordinary I/O failure;
   simultaneous writers and power loss between files are not transaction-safe.

New installations bind the API to localhost unless LAN access is selected. A
random API token is generated if none exists. The Watch needs LAN access and
the matching token, copied explicitly from Overview. Installing the Watch app
still requires signing and pairing in Xcode. The desktop app does not install
or pair the Watch automatically.

## Existing installations and lifecycle

Data stays in `~/Library/Application Support/AnimalTracker/`, with
`ANIMAL_TRACKER_DATA` honored for isolated testing. The app does not switch a
checkout, install LaunchAgents, or restart an existing service. Overview detects
an API on the configured port and refuses to start a second instance. Stop only
controls a process started by this app. Saved sessions are labelled as saved,
not as proof of a currently healthy connection.

Keep the app running for its owned engine to run. Closing the window leaves the
app running; quitting asks before stopping its engine. Automatic launch at login
and crash recovery are not implemented. A stalled provider sign-in can be
cancelled and retried. Diagnostics reuse `doctor` and include remediation text.
The app does not automatically change model thresholds, schedule AI reviews,
or guarantee identification accuracy. Uncertain frames still need the existing
review workflow. Pushover and APNs settings remain available through the existing
setup tools; their credential screens are not in this preview.

## Security and validation

- App ↔ Python commands use a private JSON-lines stdin/stdout protocol. Account
  passwords and verification codes never appear in command-line arguments.
- Provider console output and exceptions are suppressed/redacted. Configs,
  account sessions and backups written by the bridge have owner-only permissions.
  They use existing backend file storage, **not Keychain encryption**.
- AppleScript source is constant; message recipients and text are arguments,
  preventing entered text from becoming executable AppleScript.
- Import and setup tests use temporary directories and mocked providers. No
  test signs into Ring/Apple or sends messages.
- The app requires macOS Automation permission for Messages. See Apple’s
  [Messages sign-in guide](https://support.apple.com/en-gb/guide/messages/ichte16154fb/mac)
  and [Apple Events usage description](https://developer.apple.com/documentation/bundleresources/information-property-list/nsappleeventsusagedescription).

```sh
cd macos/AnimalTracker && swift test
# From repo root, in the backend virtual environment:
cd backend && python -m pytest tests/ -v
```

Before a public binary release, verify fresh Ring login/2FA, expired-token
recovery, Apple trusted-device authentication, denied and approved Messages
permissions, an actual received alert, engine startup with local model downloads,
and Watch access on a second device. Those checks need account/device access
and are not substituted by mocked tests.
