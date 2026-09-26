# AnimalTrackerWatch

watchOS app + WidgetKit complication for WinstonTracker. Shows where Winston
was last seen, today's moves, and 24 h stats; receives APNs alerts from the
backend.

```
AnimalTrackerWatch/
├── Package.swift               SwiftPM: builds + tests AnimalTrackerCore only
├── Sources/AnimalTrackerCore/        shared library (also used by the widget)
│   ├── Models/                 TrackerState, LocationSnapshot, Transition, Stats,
│   │                           NotificationPayload (the APNs contract), ZoneLabel
│   ├── API/AnimalTrackerAPIClient    async client for the FastAPI backend
│   ├── Storage/SharedStore     App Group UserDefaults shared with the complication
│   └── Formatting/             AnimalTrackerJSON (date handling), RelativeTime
├── Tests/AnimalTrackerCoreTests/     Swift Testing suites (payload, models, client)
├── App/                        watchOS app target sources (SwiftUI)
│   ├── AnimalTrackerWatchApp.swift   @main, tabs, notification scene
│   ├── LocationStore.swift     ObservableObject: fetch, cache, apply pushes
│   ├── Views/                  LocationView, HistoryView, StatsView, SettingsView, …
│   └── Notifications/          NotificationController (APNs, categories, silent push),
│                               NotificationView (+ hosting controller)
└── Complication/               WidgetKit extension target sources
    ├── AnimalTrackerComplicationBundle.swift
    ├── AnimalTrackerComplication.swift   TimelineProvider (reads SharedStore, 15-min timeline)
    └── ComplicationViews.swift     circular / corner / inline / rectangular
```

## Test the shared code (no Xcode needed)

```bash
cd ios/AnimalTrackerWatch
swift build
swift test            # with Xcode installed
../../scripts/run.sh watch-test   # on a Command-Line-Tools-only Mac (adds framework paths)
```

## Current Xcode project

`AnimalTrackerWatch.xcodeproj` now builds the app and complication from the existing
sources. It is generated from `project.yml` with `xcodegen generate`.
Both targets successfully built for the watchOS Simulator on 2026-09-20 using
Xcode-beta. This is a compile check, not a paired-device or APNs delivery test.
Open the project, choose your signing team, and select a Watch destination.
Signing: team `KBGYVG6F36` (John Marshall), automatic signing, bundle ids
`com.johnmarshall.winstontracker` / `.complication`, App Group
`group.com.johnmarshall.winstontracker` — all set in `project.yml`; regenerate
with `xcodegen generate` after editing it. Xcode must be signed in to that
AppleID for automatic provisioning to register the App Group and APNs. The backend listens on `0.0.0.0:8420`;
enter `http://<mac-mini-lan-ip>:8420` (and the `WINSTON_API_TOKEN`, if set) in
the app's Settings. On first refresh after APNs issues a token the app POSTs it
to `/winston/devices`; Settings → Push shows "Backend: registered" once the
server has it. To build from the command line on a Mac whose `xcode-select`
points at the Command Line Tools:

```bash
DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer \
xcodebuild -project AnimalTrackerWatch.xcodeproj -scheme AnimalTrackerWatch \
  -destination 'generic/platform=watchOS Simulator' CODE_SIGNING_ALLOWED=NO build
```

## Original manual project setup reference

`App/` and `Complication/` are not SwiftPM targets — watchOS apps and widget
extensions need an Xcode project. When ready:

1. Xcode → File → New → Project → **watchOS → App**. Product name
   `AnimalTrackerWatch`, bundle id `com.johnmarshall.winstontracker`
   (match `notifications.apns.bundle_id` in `backend/config/settings.yaml`),
   interface SwiftUI, **Include Notification Scene** ✓. Save it next to this
   directory (e.g. `ios/AnimalTrackerWatch.xcodeproj`), or use XcodeGen with a
   `project.yml` if you prefer generated projects.
2. Delete the template `ContentView.swift`/`*App.swift`; add every file under
   `App/` to the app target.
3. File → Add Package Dependencies → Add Local → select this folder; link
   `AnimalTrackerCore` to the app target.
4. File → New → Target → **watchOS → Widget Extension**, name
   `AnimalTrackerComplication`, uncheck configuration intent. Delete its template
   files, add everything under `Complication/`, link `AnimalTrackerCore`.
5. Capabilities:
   - App target: **Push Notifications**, **Time Sensitive Notifications**,
     **Background Modes → Remote notifications**, **App Groups** →
     `group.com.johnmarshall.winstontracker`.
   - Widget target: **App Groups** → same group.
   If you change the group id, update `SharedStore.appGroup`.
6. Run on a paired watch or simulator. Set the server URL in the Settings
   tab (e.g. `http://mac-mini.local:8420`). Copy the device token shown there
   into `APNS_DEVICE_TOKENS` in the backend `.env` and set
   `notifications.backend: apns`.

## Contracts with the backend

- Push payload: `docs/Notification_Payload.md` ↔ `Models/NotificationPayload.swift`
  (tests decode the exact JSON the backend emits).
- REST shapes: `backend/src/api.py` ↔ `Models/*.swift`.
- Zone ids come from `backend/config/cameras.yaml`; display names live in
  `ZoneLabel.overrides`. Add your zones there.

## Not done

- Device-token registration endpoint (token is shown in Settings for manual copy).
- "Mute 1 h" action is wired in the UI but has no backend call yet.
- iPhone companion app (only needed for App Store distribution).

## Siri check-in

The Watch app includes `App/Intents/CheckOnAnimalIntent.swift` and an
AppShortcutsProvider. It queries the configured backend, speaks the last confirmed
area and sighting age, and returns the same text to Shortcuts. Network failures
explicitly identify saved sightings; unconfirmed moves are never spoken as confirmed
locations. `AnimalTrackerCore/Formatting/AnimalTrackerCheckIn.swift` owns the tested wording.
Say “Check on Winston with Winston” or create a personal Shortcut named
“Where's Winston” using the Check on Winston action. On-device Siri discovery and
invocation still need verification after installation; compilation alone is not proof.

Cached Watch and complication state now decays conservatively to Last seen after
120 seconds. Unknown/missing data does not show a fabricated placeholder location;
unconfirmed transitions are labeled as possible moves. This display safeguard does
not infer or change backend locations. Keep this conservative display timeout in
mind when changing backend freshness settings.
