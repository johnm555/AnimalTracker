# Setup Guide

From a fresh Mac to notifications about your animal in about 30 minutes, most of
it waiting for photos and a Ring 2FA code.

If you use Claude Code, open the repo and say **"set up my property"** — the
`setup-property` skill walks you through all of this conversationally. Codex
users: point it at `.claude/skills/setup-property/SKILL.md`.

## What you need

- A Mac that stays on (a Mac mini is ideal). macOS 14 or later; Apple Vision
  and Messages are part of the stack.
- Ring cameras with recordings enabled (a Ring Protect plan) — the tracker works
  from recorded clips.
- Python 3.11+ (`brew install python`).
- Optional: Claude or Codex, to review the events the local models can't decide.
  No API key needed; it uses your subscription.

## 1. Install

```bash
git clone https://github.com/johnm555/AnimalTracker.git
cd AnimalTracker
scripts/setup.sh          # virtualenv, dependencies, test suite
```

## 2. Configure your property

```bash
scripts/run.sh setup
```

The wizard logs in to Ring (one 2FA code; only the refresh token is stored),
lists your cameras, and asks:

| Question | What to answer |
|---|---|
| Which cameras | Only cameras that can see your animal. Leave out other properties. |
| Zone for each camera | A place name you'd say out loud: `kitchen`, `back deck`. Cameras that see the same place get the same zone. |
| Neighbors | Zones your animal can walk between directly. A blind spot between two cameras is fine — it's just travel time. |
| Travel time | A typical *slow* walk, in seconds. The minimum stays 0 until you've measured it; a guessed minimum makes the tracker reject real moves as impossibly fast. |
| High-priority zones | Where a sighting should reach you even at 3am — an exit door, the street side. Keep it to one or two. |
| Indoor / doorway zones | Only used for inside/outside statistics. |
| Notifications | `imessage` on a Mac (phone number or Apple ID), `pushover`, `apns` (paid developer account), or `log`. |

Everything is written to your **data dir**, never the repo:

```
~/Library/Application Support/AnimalTracker/
  config/settings.yaml   config/cameras.yaml   .env
  reference_images/      tracker.db            staging/   ring_downloads/
```

Set `ANIMAL_TRACKER_DATA` to put it elsewhere. Re-run with `--force` to start
over (old files are kept as `.bak-*`), or edit the YAML directly.

**Example topology** — a two-storey house with a yard:

```yaml
zones:
  kitchen:
    cameras: [kitchen-cam]
    neighbors:
      back-deck: {min_seconds: 0, max_seconds: 30}
      living-room: {min_seconds: 0, max_seconds: 20}
  living-room:
    cameras: [living-room-cam]
    neighbors:
      front-door: {min_seconds: 0, max_seconds: 30}
  back-deck:
    cameras: [deck-cam]
    neighbors:
      yard: {min_seconds: 0, max_seconds: 60}
  yard:
    cameras: [yard-cam, garden-cam]      # two cameras, one place
  front-door:
    cameras: [doorbell]                  # high-priority: the way out
```

Neighbors only need declaring once; the reverse direction is filled in.

## 3. Reference photos

Put **6 or more** clear photos of your animal in
`~/Library/Application Support/AnimalTracker/reference_images/`. Variety beats
quantity: side, front, lying down, outdoors, and at night under the cameras' IR
if you can. Later, `scripts/run.sh train harvest` adds crops from the cameras
themselves, which match what the cameras see far better than phone photos.

## 4. Check and start

```bash
scripts/run.sh doctor                   # every problem, with its fix
scripts/install-launchd.sh install      # run at login, restart on crash
scripts/install-launchd.sh cleanup      # nightly disk retention (recommended)
```

`doctor` checks configs, zone references, photos, the Ring token, the database,
notification requirements and the running service. Fix every ✗ before relying
on it.

## 5. Let your AI subscription review what the local models can't

The local models decide most events on their own. The rest wait for review:

```bash
scripts/install-launchd.sh review claude    # or: review codex
```

Every 30 minutes, if there's anything queued, a headless session views the
frames and records verdicts (it exits immediately when the queue is empty).
Those verdicts are also what the local models learn from — see
[Training_Guide.md](Training_Guide.md).

## Notifications

- **iMessage:** moves are summarised when your animal settles (default 5 min);
  high-priority zones alert immediately. Reply `status`, `mute`, `mute 30`,
  `unmute`, `off`, `on`. Replies need Full Disk Access for the process
  (System Settings → Privacy & Security → Full Disk Access).
- **Quiet hours** silence everything except high-priority zones.
- Details: `.claude/skills/configure-notifications/SKILL.md`.

## Optional: AirTag

An AirTag on the collar adds an on/off-property signal. It needs the tag's key
exported from a Mac that the tag is paired to (on macOS 15+ the OpenTagViewer
exporter works) and an iCloud session: `scripts/run.sh findmy-login`, then
`scripts/run.sh findmy-test`. Files go in the data dir's `secrets/`.

## Troubleshooting

Start with `scripts/run.sh doctor`, then `.claude/skills/troubleshoot/SKILL.md`.
The most common surprise: `unknown` is a correct answer. The tracker never
guesses where your animal is — if no camera has seen them, it says so.
