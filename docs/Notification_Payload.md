# Notification Payload Contract

Produced by `backend/src/notification.py::build_payload()`. Consumed by the
watchOS app (`ios/AnimalTrackerWatch/Sources/AnimalTrackerCore/NotificationPayload.swift`)
and mirrored, minus `aps`, by Pushover. **Change both sides together.**

## Shape

```json
{
  "aps": {
    "alert": { "title": "Max is in the driveway", "body": "Moved from the side yard to the driveway. Street-adjacent." },
    "sound": "default",
    "interruption-level": "time-sensitive",
    "relevance-score": 1.0,
    "category": "WINSTON_MOVED",
    "thread-id": "winston-location"
  },
  "winston": {
    "type": "high_priority",
    "zone": "driveway",
    "from_zone": "side-yard",
    "arrived_at": "2026-09-20T14:04:31+00:00",
    "departed_at": "2026-09-20T14:04:20+00:00",
    "confidence": 0.93,
    "transition_id": 42
  }
}
```

### `aps` (Apple-defined)

| type | alert | sound | interruption-level | content-available |
|---|---|---|---|---|
| `normal` | yes | default | `active` | — |
| `high_priority` | yes | default | `time-sensitive` | — |
| `silent` | no | — | — | `1` (background refresh of the complication) |
| `suppressed` | never sent; recorded in DB only | | | |

`category` is always `WINSTON_MOVED` (register a `UNNotificationCategory` with
that id for actions like "Show map" / "Mute 1h"). `thread-id` groups every
tracker alert into one stack. APNs headers: `apns-push-type` alert/background,
`apns-priority` 10/5, `apns-collapse-id: winston-location`.

### `winston` (ours)

| field | type | notes |
|---|---|---|
| `type` | `normal` \| `high_priority` \| `silent` | the policy decision |
| `zone` | string | zone id from `cameras.yaml` (hyphenated) |
| `from_zone` | string \| null | null on the first sighting after startup |
| `arrived_at` | ISO-8601 UTC | when the sighting that confirmed the move happened |
| `departed_at` | ISO-8601 UTC \| null | last sighting in `from_zone` |
| `confidence` | 0–1, 3 dp | transition confidence (already includes topology penalties) |
| `transition_id` | int \| null | row id in `transitions`; null only in dry runs |

Zone display names: the watch app maps ids → labels itself
(`ZoneLabel.swift`); the backend's `zone_labels` setting is only for alert text.

## Watch app expectations

- Decode `winston` first; if it's missing, treat as a plain alert.
- On `silent`: refresh the complication timeline from
  `GET /tracker/location`; don't show UI.
- Time-sensitive delivery requires the *Time Sensitive Notifications*
  capability on the app target.
- Payload size stays well under APNs' 4 KB limit.


## Manual mute (P2-09, 2026-09-21)

`POST /tracker/mute?minutes=60` uses the same bearer authentication as other
write endpoints. Duration is an integer from 0 to 1440 minutes (default 60);
zero immediately unmutes. Each request replaces the previous expiry relative
to server wall time. `GET /tracker/mute` reports the current setting:

```json
{"muted":true,"muted_until":"2026-09-21T22:00:00+00:00","scope":"all_devices","as_of":"2026-09-21T21:00:00+00:00"}
```

The setting is global across registered devices/backends and persists in the
SQLite `notification_preferences` singleton row. It expires automatically; an
expired response has `muted:false` and `muted_until:null`. A successful response
means persistence completed. Backend restarts preserve the requested expiry.

During an explicit mute, eligible transitions become **silent**, including
high-priority zones. Tracking, observation persistence, transition history,
and notification audit rows continue. APNs can deliver background updates;
Pushover skips silent sends. Expiry does not replay alerts from the muted period.
Mute decisions use delivery wall time, not a clip's historical timestamp, so
delayed session reviews cannot bypass the user's current mute. Confidence
suppression still takes precedence, and muted decisions do not start cooldowns.

The Watch notification action is **Mute all 1 h** and calls this endpoint using
the configured server/token. Settings offers mute and unmute controls and shows
the last server-confirmed action or error (not an offline claim of current mute
state). Missing connectivity or rejected requests do not report success. Real
Watch action execution still requires physical-device/APNs verification.
