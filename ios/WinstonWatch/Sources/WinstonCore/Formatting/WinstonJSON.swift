import Foundation

/// JSON coding configured for the backend's date formats.
///
/// The backend emits ISO-8601 in two flavors: `2026-09-20T14:04:31+00:00`
/// (Python `isoformat()`) and `2026-09-20T19:07:17.822680Z` (pydantic).
public enum WinstonJSON {
    public static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            if let date = ISO8601.parse(raw) { return date }
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Unparseable date: \(raw)")
        }
        return d
    }()

    public static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .custom { date, encoder in
            var c = encoder.singleValueContainer()
            try c.encode(ISO8601.string(from: date))
        }
        e.outputFormatting = [.sortedKeys]
        return e
    }()
}

public enum ISO8601 {
    private static let withFraction: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let plain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    public static func parse(_ s: String) -> Date? {
        // Python may emit up to 6 fractional digits; ISO8601DateFormatter wants ≤3.
        let normalized = trimFraction(s)
        return withFraction.date(from: normalized) ?? plain.date(from: normalized)
    }

    public static func string(from date: Date) -> String {
        withFraction.string(from: date)
    }

    static func trimFraction(_ s: String) -> String {
        guard let dot = s.firstIndex(of: ".") else { return s }
        let afterDot = s.index(after: dot)
        var end = afterDot
        while end < s.endIndex, s[end].isNumber { end = s.index(after: end) }
        let digits = s[afterDot..<end]
        guard digits.count > 3 else { return s }
        return String(s[..<afterDot]) + digits.prefix(3) + String(s[end...])
    }
}
