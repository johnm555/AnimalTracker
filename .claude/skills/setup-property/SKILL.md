---
name: setup-property
description: Configure Animal Tracker for a new property — Ring cameras, zones, travel windows between zones, high-priority zones, indoor/outdoor zones, notifications. Use when the user wants to set up, reconfigure, add a camera, rename or add zones, or says their locations look wrong because of the layout.
---

# Set up a property

The goal is two files in the data dir (`~/Library/Application Support/AnimalTracker/config/`):
`cameras.yaml` (zones, cameras, neighbors) and `settings.yaml` (device ids, notification
policy, stats zones). Never write them by hand when the wizard can: it validates the
topology before writing and backs up what it replaces.

## Steps

1. **Check state.** `scripts/run.sh doctor --skip-service`. If configs already exist and the
   user wants a change, not a redo, edit the YAML directly and skip to step 6.
2. **List the cameras.** `scripts/run.sh setup --list-cameras`. If it says there is no Ring
   token, the user must run `scripts/run.sh ring-login` themselves (interactive 2FA) —
   tell them to type `! scripts/run.sh ring-login`.
3. **Interview the user**, one topic at a time. Don't guess any of these:
   - The animal's name and a short description.
   - Which cameras can see the animal. Leave out cameras at other properties.
   - The zone each camera covers. Cameras that see the same place share a zone.
     Zones should be places the user would say out loud ("kitchen", "back deck"), not
     camera names.
   - Neighbors: which zones the animal can walk between **without passing another
     camera**. If there's a blind spot between two cameras, those zones are still
     neighbors — the blind spot is just travel time.
   - Travel time for each neighbor pair: a typical *slow* walk in seconds → `max_seconds`.
     **Always set `min_seconds: 0`** unless the user has measured it; a guessed minimum
     makes the tracker reject real sightings as impossibly fast.
   - High-priority zones: where a sighting should wake them at 3am (an exit door, the
     street side, a pool). Keep this list short.
   - Which zones are indoors (`inside_zones`) and which straddle a doorway
     (`ambiguous_zones`) — these only affect statistics.
   - Notifications: `imessage` (macOS, needs a phone number or Apple ID), `pushover`,
     `apns` (paid Apple developer account), or `log`. Quiet hours.
4. **Write the answers** to a scratch `answers.json` (format in
   `scripts/setup_wizard.py`'s docstring) and run
   `scripts/run.sh setup --answers answers.json --dry-run`. Show the user the zone list
   and neighbor pairs in plain words ("kitchen ↔ back yard, up to 45 s") and confirm.
5. **Apply:** `scripts/run.sh setup --answers answers.json` (add `--force` only if configs
   exist and the user agreed to replace them).
6. **Verify:** `scripts/run.sh doctor`. Fix every FAIL before finishing. Then tell the user
   to add 6+ reference photos (the path doctor prints) and to install the service:
   `scripts/install-launchd.sh install`. If the API is already running, restart it so it
   reads the new topology: `launchctl kickstart -k gui/$(id -u)/com.winstontracker.api`.

## Rules

- A zone is only as real as its cameras. Don't create zones with no camera.
- Never invent travel times, device ids or zone names; ask.
- Configs live in the data dir. Never commit them to the repo.
