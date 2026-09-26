import Foundation

/// Human-readable zone names. Zone ids come from `backend/config/cameras.yaml`;
/// anything not listed here falls back to de-hyphenating the id.
public enum ZoneLabel {
    public static var overrides: [String: String] = [
        "house": "house",
        "backyard": "backyard",
        "side-yard": "side yard",
        "driveway": "driveway",
        "front": "front yard",
    ]

    /// Zones considered street-adjacent by the UI (mirrors `notifications.high_priority_zones`).
    public static var highPriorityZones: Set<String> = ["front", "driveway"]

    public static func label(for zone: String) -> String {
        overrides[zone] ?? zone.replacingOccurrences(of: "-", with: " ")
    }

    public static func isHighPriority(_ zone: String?) -> Bool {
        guard let zone else { return false }
        return highPriorityZones.contains(zone)
    }

    /// SF Symbol per zone for complications.
    public static func symbolName(for zone: String?) -> String {
        switch zone {
        case "house": return "house.fill"
        case "backyard": return "tree.fill"
        case "side-yard": return "fence"
        case "driveway": return "car.fill"
        case "front": return "door.left.hand.open"
        case nil: return "questionmark"
        default: return "mappin.circle.fill"
        }
    }
}
