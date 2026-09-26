---
name: configure-notifications
description: Set up or change Animal Tracker notifications — iMessage, Pushover, APNs, quiet hours, high-priority zones, journey summaries, muting. Use when the user wants alerts on their phone or watch, gets too many or too few alerts, or asks why a notification didn't arrive.
---

# Configure notifications

Everything lives under `notifications:` in the data-dir `settings.yaml`. Restart the API
after editing: `launchctl kickstart -k gui/$(id -u)/com.winstontracker.api`.

## Backends

| backend | needs | notes |
|---|---|---|
| `imessage` | macOS, Messages signed in, `imessage.recipient` | Journey summaries: moves are buffered and one message goes out when the animal settles (`settle_seconds`, default 300). High-priority zones send immediately. Replies `mute`, `mute 30`, `unmute`, `status`, `on`, `off` work if the process has **Full Disk Access** (to read `~/Library/Messages/chat.db`). |
| `pushover` | `PUSHOVER_USER_KEY`, `PUSHOVER_API_TOKEN` in the data-dir `.env` | Cross-platform, one alert per move, no silent updates. |
| `apns` | paid Apple Developer account, `.p8` key, `APNS_*` in `.env`, matching `bundle_id` | Needed for the watch complication to refresh in the background. |
| `log` | nothing | Writes to the log only. Every decision is also stored in the `notifications` table. |

## Policy knobs

- `high_priority_zones` — alert immediately and through quiet hours. Keep it to the one or
  two places that matter at 3am.
- `quiet_hours: {start, end}` — local time; only high-priority alerts make noise.
- `cooldown_seconds` — the same from→to move repeated within this window becomes silent.
- `imessage.settle_seconds` — how long without a new move before a journey summary is sent.

## Diagnosing "no notification"

1. `scripts/run.sh doctor` — backend requirements, recipient, Full Disk Access.
2. Check the decision was made at all: `sqlite3 "<data dir>/tracker.db" "SELECT sent_at,type,backend,success,error FROM notifications ORDER BY id DESC LIMIT 10"`.
   `silent` (cooldown / quiet hours / mute) and `suppressed` are by design.
3. `grep -i notif ~/Library/Logs/WinstonTracker/api.err.log | tail`.

Notification text must only restate what the tracker reported. Never write copy that
infers a location the tracker didn't produce.
