import Foundation

/// One row of `transitions` as returned by `/winston/history` and `/winston/transitions`.
public struct Transition: Codable, Identifiable, Equatable, Sendable {
    public var id: Int
    public var fromZone: String?
    public var toZone: String
    public var departedAt: Date?
    public var arrivedAt: Date
    public var confidence: Double
    public var observationIds: [Int]

    public init(id: Int, fromZone: String? = nil, toZone: String, departedAt: Date? = nil,
                arrivedAt: Date, confidence: Double, observationIds: [Int] = []) {
        self.id = id
        self.fromZone = fromZone
        self.toZone = toZone
        self.departedAt = departedAt
        self.arrivedAt = arrivedAt
        self.confidence = confidence
        self.observationIds = observationIds
    }

    enum CodingKeys: String, CodingKey {
        case id, confidence
        case fromZone = "from_zone"
        case toZone = "to_zone"
        case departedAt = "departed_at"
        case arrivedAt = "arrived_at"
        case observationIds = "observation_ids"
    }

    public var isInitialSighting: Bool { fromZone == nil }

    public var summary: String {
        let to = ZoneLabel.label(for: toZone)
        guard let fromZone else { return "Spotted in the \(to)" }
        return "\(ZoneLabel.label(for: fromZone)) → \(to)"
    }
}

public struct TransitionList: Codable, Sendable {
    public var count: Int
    public var transitions: [Transition]
}
