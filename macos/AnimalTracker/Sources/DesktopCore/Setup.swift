import Foundation

public struct CameraChoice: Identifiable {
    public var id: String
    public var name: String
    public var zone: String = ""
    public var selected = false
    public init(id: String, name: String) { self.id = id; self.name = name }
}
public struct ZoneLink: Identifiable {
    public let id = UUID()
    public var from = ""
    public var to = ""
    public var seconds = 120
    public init(from: String = "", to: String = "", seconds: Int = 120) {
        self.from = from; self.to = to; self.seconds = seconds
    }
}
public enum Setup {
    public static func answers(name: String, description: String, cameras: [CameraChoice], links: [ZoneLink], recipient: String, messages: Bool) -> [String: Any] {
        ["animal": ["name": name, "description": description],
         "cameras": cameras.filter(\.selected).map { ["device_id": $0.id, "name": $0.name, "zone": $0.zone] },
         "neighbors": links.map { ["a": $0.from, "b": $0.to, "min_seconds": 0, "max_seconds": $0.seconds] as [String: Any] },
         "notifications": ["backend": messages ? "imessage" : "log", "recipient": recipient],
         "inside_zones": [], "ambiguous_zones": [], "high_priority_zones": []]
    }
    public static func validate(name: String, cameras: [CameraChoice], links: [ZoneLink], messages: Bool, recipient: String) -> String? {
        if name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return "Add your animal’s name." }
        let selected = cameras.filter(\.selected)
        if selected.isEmpty { return "Choose at least one Ring camera." }
        if selected.contains(where: { $0.zone.trimmingCharacters(in: .whitespaces).isEmpty }) { return "Give every selected camera a zone." }
        let zones = Set(selected.map(\.zone))
        if links.contains(where: { !zones.contains($0.from) || !zones.contains($0.to) || $0.from == $0.to || $0.seconds <= 0 }) { return "Each route must connect two different selected zones with a positive travel window." }
        if messages && recipient.trimmingCharacters(in: .whitespaces).isEmpty { return "Add an iMessage recipient or choose local records only." }
        return nil
    }
}


/// A sighting is evidence with a timestamp, never an assertion of current presence.
public struct SightingSummary {
    public let title: String
    public let zone: String
    public let timestamp: String?
    public init(_ value: [String: Any]) {
        guard let state = value["state"] as? String, ["seen", "last_seen", "transitioning"].contains(state),
              let time = value["last_seen_at"] as? String, !time.isEmpty,
              let zone = value["zone_label"] as? String ?? value["zone"] as? String, !zone.isEmpty else {
            title = "Location unknown"; zone = "No confirmed sighting available"; timestamp = nil; return
        }
        self.title = state == "transitioning" ? "Transition unconfirmed • last sighting" : "Last confirmed sighting"
        self.zone = zone; self.timestamp = time
    }
}


/// Bounded recovery: avoid an endless crash loop or undoing an explicit stop.
public struct RecoveryPolicy {
    public private(set) var attempts = 0
    public init() {}
    public mutating func reset() { attempts = 0 }
    public mutating func nextDelay(enabled: Bool, requestedStop: Bool) -> TimeInterval? {
        guard enabled, !requestedStop, attempts < 3 else { return nil }
        attempts += 1
        return TimeInterval(attempts * 5)
    }
}
