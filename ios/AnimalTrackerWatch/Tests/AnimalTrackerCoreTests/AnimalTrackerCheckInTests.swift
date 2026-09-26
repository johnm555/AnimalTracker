import Foundation
import Testing
@testable import AnimalTrackerCore

@Suite("Siri evidence-only responses")
struct AnimalTrackerCheckInTests {
    let now = Date(timeIntervalSince1970: 100000)

    @Test func unknownDoesNotInventLocation() {
        let text = AnimalTrackerCheckIn.response(snapshot: .unknown, reachable: true)
        #expect(text == "I don't have a confirmed sighting of Winston.")
    }
    @Test func cachedSeenStateCannotSoundCurrent() {
        let s = LocationSnapshot(state: .seen, zone: "dog-room", lastSeenAt: now.addingTimeInterval(-600))
        let text = AnimalTrackerCheckIn.response(snapshot: s, reachable: false, now: now)
        #expect(text.contains("couldn't reach"))
        #expect(text.contains("10 minutes ago"))
        #expect(text.contains("current location is unconfirmed"))
    }
    @Test func pendingZoneIsNotReportedAsConfirmed() {
        let s = LocationSnapshot(state: .transitioning, zone: "kitchen", lastSeenAt: now.addingTimeInterval(-30), toZone: "garden")
        let text = AnimalTrackerCheckIn.response(snapshot: s, reachable: true, now: now)
        #expect(text.contains("kitchen"))
        #expect(!text.contains("garden"))
        #expect(text.contains("possible move has not been confirmed"))
    }
    @Test func futureTimestampIsNotEvidence() {
        let s = LocationSnapshot(state: .seen, zone: "kitchen", lastSeenAt: now.addingTimeInterval(60))
        #expect(AnimalTrackerCheckIn.response(snapshot: s, reachable: true, now: now).contains("don't have a confirmed sighting"))
    }
    @Test func staleOnlineResultIsStillHistorical() {
        let s = LocationSnapshot(state: .seen, zone: "kitchen", lastSeenAt: now.addingTimeInterval(-7200))
        let text = AnimalTrackerCheckIn.response(snapshot: s, reachable: true, now: now)
        #expect(text.contains("2 hours ago"))
        #expect(text.contains("current location is unconfirmed"))
    }
}
