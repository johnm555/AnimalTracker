import Foundation

/// Response of `GET /tracker/stats`.
public struct Stats: Codable, Equatable, Sendable {
    public struct Window: Codable, Equatable, Sendable {
        public var since: Date
        public var until: Date
        public var hours: Double
    }

    public var window: Window
    public var transitions: Int
    public var transitionsPerHour: Double
    public var sightings: Int
    public var lowConfidenceObservations: Int
    public var outsideMinutes: Double
    public var insideMinutes: Double
    public var minutesByZone: [String: Double]
    public var visitsByZone: [String: Int]
    public var mostVisitedZone: String?
    public var streetAdjacentVisits: Int

    enum CodingKeys: String, CodingKey {
        case window, transitions, sightings
        case transitionsPerHour = "transitions_per_hour"
        case lowConfidenceObservations = "low_confidence_observations"
        case outsideMinutes = "outside_minutes"
        case insideMinutes = "inside_minutes"
        case minutesByZone = "minutes_by_zone"
        case visitsByZone = "visits_by_zone"
        case mostVisitedZone = "most_visited_zone"
        case streetAdjacentVisits = "street_adjacent_visits"
    }
}
