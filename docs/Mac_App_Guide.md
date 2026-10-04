# Animal Tracker for Mac (developer preview)

The native SwiftUI app guides a new installation through engine preparation,
an animal profile, Ring login and verification, camera selection, zone routes,
reference photos, Messages, optional Find My account setup, and a reviewed save.
It wraps the existing Python backend and setup validation. It does not change
the detector or deterministic tracker.

## Build and open

Building requires macOS 14+ and Xcode command-line tools with Swift 5.9+.
Users do not need to install Python separately; first-run preparation downloads it.
Build on the architecture you intend to use; this script does not produce a universal binary.

```sh
scripts/build-mac-app.sh /tmp/AnimalTracker-build
open '/tmp/AnimalTracker-build/Animal Tracker.app'
```

Move the app to your Applications folder. The build is locally ad-hoc signed;
it is **not notarized or ready for frictionless distribution to other Macs**.
A release needs a Developer ID signature, a notarization/stapling step, and a
clean-Mac acceptance test.
First-run setup downloads a pinned Python runtime from Astral’s official
python-build-standalone GitHub release, then dependencies from PyPI; it is not
an offline runtime or a Mac App Store build.

The bundle contains an allowlisted copy of framework Python sources, dependency
requirements, and example configs. It contains no site data. Build output goes
to ignored `dist/` by default; the script refuses to overwrite an existing app.

## First run

1. **Tracking engine:** click Download & prepare engine. The app downloads a
   standalone Python 3.12.15 interpreter (release 20261003, about 25 MB), verifies
   its architecture-specific SHA-256 digest, and unpacks it in the data directory.
   An advanced option accepts an existing Python 3.11+ interpreter.
   The app creates `desktop-runtime/` in the data directory and installs
   the requirements there. Internet, several GB of disk space, and several
   minutes may be needed. A failed installation can be retried. It never runs
   `sudo` or installs into system Python.
2. **Your animal:** enter the name and identifying appearance. This version
   tracks one enrolled animal; it does not provide independent multi-pet tracks.
   Backend alert titles and Messages journey summaries use this saved name;
   legacy payload identifiers remain unchanged for client compatibility.
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
app running; quitting asks before stopping its engine.
Open-at-login uses macOS Login Items and requires an explicit toggle. A separate
option resumes tracking when the app opens. Engine recovery retries at 5, 10, and
15 seconds, then stops; an explicit Stop or Quit cancels recovery. External services
are never managed by these controls. A stalled provider sign-in can be
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


## Notarized release

The release script deliberately requires a **Developer ID Application** certificate;
an Apple Development certificate alone cannot be used for this distribution step.
Store notarization credentials with Apple's `notarytool store-credentials` in the
Keychain, then supply the profile name (never a password in a shell argument).

```sh
export ANIMAL_TRACKER_SIGNING_IDENTITY='Developer ID Application: Your Name (TEAMID)'
export ANIMAL_TRACKER_NOTARY_PROFILE='animaltracker-release'
scripts/release-mac-app.sh /tmp/AnimalTracker-release
```

The script builds, signs with hardened runtime, submits to Apple, staples and
validates the ticket, checks Gatekeeper, and writes a zip plus checksum. It does
not publish a GitHub release. Run the real-account/device acceptance checks above
before distributing. Notarization still requires the owner's certificate and
credentials; the script's presence is not evidence that Apple approved a build.

The standalone Python archives are pinned in `PythonBootstrap.swift` for Apple
silicon and Intel. Update both URLs/digests together after verifying the official
release metadata; never replace checksum verification with a floating latest URL.
Automatic downloads and dependency setup require internet access. The Python
runtime's included license notices remain with the installed runtime.
