import Foundation
import Testing
@testable import AnimalTrackerCore

@Suite("Cached sighting decay")
struct SnapshotAgeTests {
    @Test func seenAndPendingBecomeHistorical() {
        let now = Date()
        for state in [TrackerState.seen, .transitioning] {
            let snapshot = LocationSnapshot(state: state, zone: "kitchen", lastSeenAt: now.addingTimeInterval(-121), toZone: "garden")
            let aged = snapshot.aged(at: now)
            #expect(aged.state == .lastSeen)
            #expect(aged.zone == "kitchen")
            #expect(aged.toZone == nil)
        }
    }
    @Test func missingOrFutureSightingIsUnknown() {
        let now = Date()
        #expect(LocationSnapshot(state: .seen, zone: "kitchen").aged(at: now).state == .unknown)
        #expect(LocationSnapshot(state: .seen, zone: "kitchen", lastSeenAt: now.addingTimeInterval(30)).aged(at: now).state == .unknown)
    }
    @Test func historicalStateIsNeverPromoted() {
        let now = Date()
        #expect(LocationSnapshot(state: .lastSeen, zone: "kitchen", lastSeenAt: now).aged(at: now).state == .lastSeen)
    }
}
