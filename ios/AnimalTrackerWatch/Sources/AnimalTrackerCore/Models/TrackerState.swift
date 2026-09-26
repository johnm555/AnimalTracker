import Foundation

/// Mirrors `state_machine.StateKind` on the backend.
public enum TrackerState: String, Codable, Sendable, CaseIterable {
    case unknown
    case seen
    case transitioning
    case lastSeen = "last_seen"

    public var title: String {
        switch self {
        case .unknown: return "Unknown"
        case .seen: return "Seen"
        case .transitioning: return "Unconfirmed move"
        case .lastSeen: return "Last seen"
        }
    }

    /// SF Symbol name for the state.
    public var symbolName: String {
        switch self {
        case .unknown: return "questionmark.circle"
        case .seen: return "eye.fill"
        case .transitioning: return "arrow.right.circle.fill"
        case .lastSeen: return "clock.fill"
        }
    }
}
