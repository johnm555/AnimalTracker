import Foundation

public enum RelativeTime {
    /// "just now", "3 min ago", "2 h ago", "yesterday" — short enough for a complication.
    public static func short(since date: Date?, now: Date = Date()) -> String {
        guard let date else { return "—" }
        let s = max(0, now.timeIntervalSince(date))
        switch s {
        case ..<60: return "just now"
        case ..<3600: return "\(Int(s / 60)) min ago"
        case ..<86_400: return "\(Int(s / 3600)) h ago"
        case ..<172_800: return "yesterday"
        default: return "\(Int(s / 86_400)) d ago"
        }
    }

    /// Even shorter, for inline/corner complications: "3m", "2h", "1d".
    public static func compact(since date: Date?, now: Date = Date()) -> String {
        guard let date else { return "—" }
        let s = max(0, now.timeIntervalSince(date))
        switch s {
        case ..<60: return "now"
        case ..<3600: return "\(Int(s / 60))m"
        case ..<86_400: return "\(Int(s / 3600))h"
        default: return "\(Int(s / 86_400))d"
        }
    }
}
