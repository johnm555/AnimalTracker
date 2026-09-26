import Foundation
import Testing
@testable import WinstonCore

@Suite("Model decoding")
struct ModelDecodingTests {
    @Test func locationSnapshotDecodesPydanticShape() throws {
        // pydantic emits microseconds + "Z"; python isoformat emits "+00:00". Both must parse.
        let json = """
        {"state":"last_seen","zone":"driveway","zone_label":"driveway","confidence":0.93,
         "last_seen_at":"2026-09-20T18:07:17.822680Z","minutes_ago":60.2,
         "from_zone":null,"to_zone":null,"as_of":"2026-09-20T19:07:17+00:00"}
        """
        let s = try WinstonJSON.decoder.decode(LocationSnapshot.self, from: Data(json.utf8))
        #expect(s.state == .lastSeen)
        #expect(s.zone == "driveway")
        #expect(s.minutesAgo == 60.2)
        let lastSeen = try #require(s.lastSeenAt)
        #expect(abs(s.asOf.timeIntervalSince(lastSeen) - (3600 - 0.822)) < 0.01)
        #expect(s.minutesSinceSeen(at: s.asOf) == 59)
    }

    @Test func unknownSnapshot() throws {
        let json = #"{"state":"unknown","zone":null,"zone_label":null,"confidence":0.0,"last_seen_at":null,"minutes_ago":null,"as_of":"2026-09-20T19:07:17Z"}"#
        let s = try WinstonJSON.decoder.decode(LocationSnapshot.self, from: Data(json.utf8))
        #expect(s.state == .unknown)
        #expect(s.displayZone == "—")
        #expect(s.minutesSinceSeen() == nil)
    }

    @Test func transitioningSnapshotCarriesCandidate() throws {
        let json = #"{"state":"transitioning","zone":"backyard","zone_label":"backyard","confidence":0.9,"last_seen_at":"2026-09-20T15:00:00+00:00","minutes_ago":0.5,"from_zone":"backyard","to_zone":"side-yard","as_of":"2026-09-20T15:00:30+00:00"}"#
        let s = try WinstonJSON.decoder.decode(LocationSnapshot.self, from: Data(json.utf8))
        #expect(s.state == .transitioning)
        #expect(s.toZone == "side-yard")
        #expect(ZoneLabel.label(for: try #require(s.toZone)) == "side yard")
    }

    @Test func transitionListDecodes() throws {
        let json = """
        {"since":"2026-09-19T15:00:00+00:00","hours":24,"count":2,"transitions":[
          {"id":1,"from_zone":null,"to_zone":"backyard","departed_at":null,"arrived_at":"2026-09-20T15:00:00+00:00","confidence":0.95,"observation_ids":[1]},
          {"id":2,"from_zone":"backyard","to_zone":"side-yard","departed_at":"2026-09-20T15:00:00+00:00","arrived_at":"2026-09-20T15:00:40+00:00","confidence":0.86,"observation_ids":[2,3]}
        ]}
        """
        let list = try WinstonJSON.decoder.decode(TransitionList.self, from: Data(json.utf8))
        #expect(list.count == 2)
        #expect(list.transitions[0].isInitialSighting)
        #expect(list.transitions[0].summary == "Spotted in the backyard")
        #expect(list.transitions[1].summary == "backyard → side yard")
        #expect(list.transitions[1].observationIds == [2, 3])
    }

    @Test func statsDecodes() throws {
        let json = """
        {"window":{"since":"2026-09-19T15:00:00+00:00","until":"2026-09-20T15:00:00+00:00","hours":24.0},
         "transitions":3,"transitions_per_hour":0.12,"sightings":9,"low_confidence_observations":2,
         "outside_minutes":42.5,"inside_minutes":1397.5,"minutes_by_zone":{"house":1397.5,"backyard":40.0,"driveway":2.5},
         "visits_by_zone":{"backyard":2,"driveway":1},"most_visited_zone":"backyard","street_adjacent_visits":1,
         "current":{"state":"seen","zone":"house"}}
        """
        let s = try WinstonJSON.decoder.decode(Stats.self, from: Data(json.utf8))
        #expect(s.transitions == 3)
        #expect(s.outsideMinutes == 42.5)
        #expect(s.minutesByZone["backyard"] == 40.0)
        #expect(s.mostVisitedZone == "backyard")
    }

    @Test func snapshotRoundTripsThroughSharedStoreEncoding() throws {
        let original = LocationSnapshot(state: .seen, zone: "front", zoneLabel: "front yard", confidence: 0.88,
                                        lastSeenAt: Date(timeIntervalSince1970: 1_790_000_000),
                                        minutesAgo: 1, asOf: Date(timeIntervalSince1970: 1_790_000_060))
        let data = try WinstonJSON.encoder.encode(original)
        let decoded = try WinstonJSON.decoder.decode(LocationSnapshot.self, from: data)
        #expect(decoded == original)
    }

    @Test func iso8601TrimsLongFractions() {
        #expect(ISO8601.trimFraction("2026-09-20T19:07:17.822680Z") == "2026-09-20T19:07:17.822Z")
        #expect(ISO8601.trimFraction("2026-09-20T19:07:17.82+00:00") == "2026-09-20T19:07:17.82+00:00")
        #expect(ISO8601.trimFraction("2026-09-20T19:07:17+00:00") == "2026-09-20T19:07:17+00:00")
        #expect(ISO8601.parse("2026-09-20T19:07:17.822680Z") != nil)
        #expect(ISO8601.parse("2026-09-20T19:07:17+00:00") != nil)
        #expect(ISO8601.parse("yesterday") == nil)
    }

    @Test func relativeTime() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        #expect(RelativeTime.short(since: now.addingTimeInterval(-30), now: now) == "just now")
        #expect(RelativeTime.short(since: now.addingTimeInterval(-180), now: now) == "3 min ago")
        #expect(RelativeTime.short(since: now.addingTimeInterval(-7200), now: now) == "2 h ago")
        #expect(RelativeTime.compact(since: now.addingTimeInterval(-180), now: now) == "3m")
        #expect(RelativeTime.compact(since: nil, now: now) == "—")
    }
}
