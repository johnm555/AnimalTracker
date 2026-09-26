import Foundation
import Testing
@testable import AnimalTrackerCore

// Exactly what backend/src/notification.py::build_payload emits (docs/Notification_Payload.md).
private let highPriority = """
{
  "aps": {
    "alert": {"title": "Winston is in the driveway", "body": "Moved from the side yard to the driveway. Street-adjacent."},
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
"""

private let silent = """
{
  "aps": {"content-available": 1, "category": "WINSTON_MOVED", "thread-id": "winston-location"},
  "winston": {"type": "silent", "zone": "backyard", "from_zone": "house",
              "arrived_at": "2026-09-20T15:00:00+00:00", "departed_at": null,
              "confidence": 0.8, "transition_id": null}
}
"""

@Suite("Notification payload")
struct NotificationPayloadTests {
    @Test func decodesHighPriorityPayload() throws {
        let p = try NotificationPayload.decode(data: Data(highPriority.utf8))
        #expect(p.aps.category == NotificationPayload.category)
        #expect(p.aps.threadId == NotificationPayload.threadId)
        #expect(p.aps.interruptionLevel == "time-sensitive")
        #expect(p.aps.alert?.title == "Winston is in the driveway")
        let w = try #require(p.winston)
        #expect(w.type == .highPriority)
        #expect(w.isHighPriority)
        #expect(w.zone == "driveway")
        #expect(w.fromZone == "side-yard")
        #expect(abs(w.confidence - 0.93) < 0.0001)
        #expect(w.transitionId == 42)
        let departed = try #require(w.departedAt)
        #expect(abs(w.arrivedAt.timeIntervalSince(departed) - 11) < 0.001)
    }

    @Test func decodesSilentPayloadWithNulls() throws {
        let p = try NotificationPayload.decode(data: Data(silent.utf8))
        #expect(p.aps.contentAvailable == 1)
        #expect(p.aps.alert == nil)
        let w = try #require(p.winston)
        #expect(w.isSilent)
        #expect(w.departedAt == nil)
        #expect(w.transitionId == nil)
    }

    @Test func decodesFromUserInfoDictionary() throws {
        let userInfo: [AnyHashable: Any] = [
            "aps": ["alert": ["title": "t", "body": "b"], "category": "WINSTON_MOVED"],
            "winston": ["type": "normal", "zone": "backyard", "arrived_at": "2026-09-20T15:00:00+00:00",
                        "confidence": 0.75],
        ]
        let p = try NotificationPayload.decode(userInfo: userInfo)
        #expect(p.winston?.type == .normal)
        #expect(p.winston?.zone == "backyard")
        #expect(p.winston?.fromZone == nil)
    }

    @Test func payloadWithoutWinstonSectionStillDecodes() throws {
        let p = try NotificationPayload.decode(data: Data(#"{"aps":{"alert":{"title":"hi"}}}"#.utf8))
        #expect(p.winston == nil)
        #expect(p.aps.alert?.title == "hi")
    }

    @Test func appliedToSnapshotUpdatesZoneAndTimestamp() throws {
        let p = try NotificationPayload.decode(data: Data(highPriority.utf8))
        let w = try #require(p.winston)
        let now = w.arrivedAt.addingTimeInterval(120)
        let updated = w.applied(to: .unknown, now: now)
        #expect(updated.state == .seen)
        #expect(updated.zone == "driveway")
        #expect(updated.displayZone == "driveway")
        #expect(updated.minutesSinceSeen(at: now) == 2)
        #expect(abs(updated.confidence - 0.93) < 0.0001)
    }
}
