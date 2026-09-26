import Foundation

/// The `winston` section of a push payload. Contract: docs/Notification_Payload.md.
public struct AnimalTrackerNotification: Codable, Equatable, Sendable {
    public enum Kind: String, Codable, Sendable {
        case normal
        case highPriority = "high_priority"
        case silent
    }

    public var type: Kind
    public var zone: String
    public var fromZone: String?
    public var arrivedAt: Date
    public var departedAt: Date?
    public var confidence: Double
    public var transitionId: Int?

    enum CodingKeys: String, CodingKey {
        case type, zone, confidence
        case fromZone = "from_zone"
        case arrivedAt = "arrived_at"
        case departedAt = "departed_at"
        case transitionId = "transition_id"
    }

    public var isSilent: Bool { type == .silent }
    public var isHighPriority: Bool { type == .highPriority }

    /// Apply this notification to a cached snapshot so the UI updates instantly,
    /// before the next `/tracker/location` fetch.
    public func applied(to snapshot: LocationSnapshot, now: Date = Date()) -> LocationSnapshot {
        var s = snapshot
        s.state = .seen
        s.zone = zone
        s.zoneLabel = ZoneLabel.label(for: zone)
        s.confidence = confidence
        s.lastSeenAt = arrivedAt
        s.minutesAgo = max(0, now.timeIntervalSince(arrivedAt) / 60)
        s.fromZone = nil
        s.toZone = nil
        s.asOf = now
        return s
    }
}

/// Full APNs payload as delivered to `userNotificationCenter(_:didReceive:)` /
/// `didReceiveRemoteNotification`. Only the parts we read are modeled.
public struct NotificationPayload: Codable, Equatable, Sendable {
    public struct APS: Codable, Equatable, Sendable {
        public struct Alert: Codable, Equatable, Sendable {
            public var title: String?
            public var body: String?
        }
        public var alert: Alert?
        public var category: String?
        public var threadId: String?
        public var interruptionLevel: String?
        public var contentAvailable: Int?

        enum CodingKeys: String, CodingKey {
            case alert, category
            case threadId = "thread-id"
            case interruptionLevel = "interruption-level"
            case contentAvailable = "content-available"
        }
    }

    public var aps: APS
    public var winston: AnimalTrackerNotification?

    public static let category = "WINSTON_MOVED"
    public static let threadId = "winston-location"

    /// Decode from the `userInfo` dictionary APNs hands the app.
    public static func decode(userInfo: [AnyHashable: Any]) throws -> NotificationPayload {
        let data = try JSONSerialization.data(withJSONObject: userInfo, options: [])
        return try AnimalTrackerJSON.decoder.decode(NotificationPayload.self, from: data)
    }

    public static func decode(data: Data) throws -> NotificationPayload {
        try AnimalTrackerJSON.decoder.decode(NotificationPayload.self, from: data)
    }
}
