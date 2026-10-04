import XCTest
@testable import DesktopCore
final class SetupTests: XCTestCase {
    func testSightingRequiresEvidenceAndNeverClaimsCurrentPresence() {
        XCTAssertNil(SightingSummary(["state": "unknown", "zone": "Kitchen"]).timestamp)
        XCTAssertNil(SightingSummary(["state": "seen", "zone": "Kitchen"]).timestamp)
        let seen = SightingSummary(["state": "seen", "zone": "Kitchen", "last_seen_at": "2026-01-01T00:00:00Z"])
        XCTAssertEqual(seen.title, "Last confirmed sighting")
        let pending = SightingSummary(["state": "transitioning", "zone": "Kitchen", "to_zone": "Yard", "last_seen_at": "2026-01-01T00:00:00Z"])
        XCTAssertEqual(pending.zone, "Kitchen")
        XCTAssertTrue(pending.title.contains("unconfirmed"))
    }
    func testRecoveryIsBoundedAndRespectsExplicitStop() {
        var policy = RecoveryPolicy()
        XCTAssertNil(policy.nextDelay(enabled: true, requestedStop: true))
        XCTAssertEqual(policy.attempts, 0)
        XCTAssertNil(policy.nextDelay(enabled: false, requestedStop: false))
        XCTAssertEqual(policy.nextDelay(enabled: true, requestedStop: false), 5)
        XCTAssertEqual(policy.nextDelay(enabled: true, requestedStop: false), 10)
        XCTAssertEqual(policy.nextDelay(enabled: true, requestedStop: false), 15)
        XCTAssertNil(policy.nextDelay(enabled: true, requestedStop: false))
        policy.reset()
        XCTAssertEqual(policy.nextDelay(enabled: true, requestedStop: false), 5)
    }
    func testOnlySelectedCamerasAndZeroMinimum() {
        var a = CameraChoice(id: "1", name: "Door"); a.selected = true; a.zone = "Kitchen"
        let b = CameraChoice(id: "2", name: "Other property")
        let result = Setup.answers(name: "Max", description: "", cameras: [a,b], links: [ZoneLink(from: "Kitchen", to: "Yard")], recipient: "", messages: false)
        XCTAssertEqual((result["cameras"] as? [[String:String]])?.count, 1)
        XCTAssertEqual((result["neighbors"] as? [[String:Any]])?.first?["min_seconds"] as? Int, 0)
        XCTAssertEqual((result["notifications"] as? [String:String])?["backend"], "log")
    }
    func testInvalidRoutesAndMissingRecipient() {
        var a = CameraChoice(id: "1", name: "Door"); a.selected = true; a.zone = "Kitchen"
        XCTAssertNotNil(Setup.validate(name: "Max", cameras: [a], links: [ZoneLink(from: "Kitchen", to: "Elsewhere")], messages: false, recipient: ""))
        XCTAssertNotNil(Setup.validate(name: "Max", cameras: [a], links: [], messages: true, recipient: ""))
        XCTAssertNil(Setup.validate(name: "Max", cameras: [a], links: [], messages: false, recipient: ""))
    }
}
