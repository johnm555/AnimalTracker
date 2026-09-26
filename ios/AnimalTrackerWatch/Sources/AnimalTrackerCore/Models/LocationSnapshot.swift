import Foundation

/// Response of `GET /tracker/location`. Also the shape persisted in the App
/// Group so the complication can render without a network call.
public struct LocationSnapshot: Codable, Equatable, Sendable {
    public var state: TrackerState
    public var zone: String?
    public var zoneLabel: String?
    public var confidence: Double
    public var lastSeenAt: Date?
    public var minutesAgo: Double?
    public var fromZone: String?
    public var toZone: String?
    public var asOf: Date

    public init(state: TrackerState, zone: String? = nil, zoneLabel: String? = nil,
                confidence: Double = 0, lastSeenAt: Date? = nil, minutesAgo: Double? = nil,
                fromZone: String? = nil, toZone: String? = nil, asOf: Date = Date()) {
        self.state = state
        self.zone = zone
        self.zoneLabel = zoneLabel
        self.confidence = confidence
        self.lastSeenAt = lastSeenAt
        self.minutesAgo = minutesAgo
        self.fromZone = fromZone
        self.toZone = toZone
        self.asOf = asOf
    }

    enum CodingKeys: String, CodingKey {
        case state, zone, confidence
        case zoneLabel = "zone_label"
        case lastSeenAt = "last_seen_at"
        case minutesAgo = "minutes_ago"
        case fromZone = "from_zone"
        case toZone = "to_zone"
        case asOf = "as_of"
    }

    /// The zone to headline. For TRANSITIONING that's still the confirmed zone;
    /// the candidate is shown separately.
    public var displayZone: String {
        guard let zone else { return "—" }
        return zoneLabel ?? ZoneLabel.label(for: zone)
    }

    /// "N min ago" computed against `now` so timelines can advance offline.
    public func minutesSinceSeen(at now: Date = Date()) -> Int? {
        guard let lastSeenAt else { return nil }
        return max(0, Int(now.timeIntervalSince(lastSeenAt) / 60))
    }

    /// Conservative display decay for cached state; never promotes a sighting.
    public func aged(at now: Date = Date()) -> LocationSnapshot {
        guard state != .unknown, zone != nil, let seen = lastSeenAt, seen <= now else {
            return .unknown
        }
        var value = self
        if now.timeIntervalSince(seen) >= 120 {
            value.state = .lastSeen
            value.toZone = nil
            value.fromZone = nil
        }
        return value
    }

    public static let unknown = LocationSnapshot(state: .unknown)
}
