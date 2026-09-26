import Foundation

/// Evidence-only speech shared by Siri and Shortcuts; never infers a location.
public enum WinstonCheckIn {
    public static func response(snapshot: LocationSnapshot?, reachable: Bool,
                                now: Date = Date()) -> String {
        let prefix = reachable ? "" : "I couldn't reach Winston's tracker. "
        guard let s = snapshot, s.state != .unknown,
              let zone = s.zone, !zone.isEmpty,
              let seen = s.lastSeenAt, seen <= now else {
            return prefix + "I don't have a confirmed sighting of Winston."
        }
        let seconds = now.timeIntervalSince(seen)
        let age: String
        if seconds < 60 { age = "less than a minute ago" }
        else if seconds < 3600 {
            let n = Int(seconds / 60)
            age = "\(n) minute\(n == 1 ? "" : "s") ago"
        } else if seconds < 86400 {
            let n = Int(seconds / 3600)
            age = "\(n) hour\(n == 1 ? "" : "s") ago"
        } else {
            let n = Int(seconds / 86400)
            age = "\(n) day\(n == 1 ? "" : "s") ago"
        }
        let source = reachable ? "Winston was last seen" : "The saved sighting places Winston"
        var text = prefix + "\(source) at \(s.displayZone), \(age)."
        if !reachable || s.state == .lastSeen || seconds >= 120 {
            text += " His current location is unconfirmed."
        } else if s.state == .transitioning {
            text += " A possible move has not been confirmed."
        }
        return text
    }
}
