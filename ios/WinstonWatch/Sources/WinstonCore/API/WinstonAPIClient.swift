import Foundation

/// Thin async client for the FastAPI backend (`backend/src/api.py`).
public actor WinstonAPIClient {
    public enum APIError: Error, LocalizedError, Equatable {
        case badStatus(Int)
        case invalidBaseURL

        public var errorDescription: String? {
            switch self {
            case .badStatus(let code): return "Server returned HTTP \(code)"
            case .invalidBaseURL: return "Invalid server URL"
            }
        }
    }

    public let baseURL: URL
    private let session: URLSession
    private let bearerToken: String?

    public init(baseURL: URL, session: URLSession = .shared, bearerToken: String? = nil) {
        self.baseURL = baseURL
        self.session = session
        self.bearerToken = bearerToken
    }

    public func location() async throws -> LocationSnapshot {
        try await get("winston/location")
    }

    public func history(hours: Double = 24) async throws -> [Transition] {
        let list: TransitionList = try await get("winston/history", query: ["hours": String(hours)])
        return list.transitions
    }

    public func transitions(on day: Date, calendar: Calendar = .current) async throws -> [Transition] {
        let f = DateFormatter()
        f.calendar = calendar
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        let list: TransitionList = try await get("winston/transitions", query: ["date": f.string(from: day)])
        return list.transitions
    }

    public func stats(hours: Double = 24) async throws -> Stats {
        try await get("winston/stats", query: ["hours": String(hours)])
    }

    /// Register this watch's APNs token so the backend can push to it
    /// (`POST /winston/devices`). Idempotent; safe to call on every launch.
    public func registerDevice(token: String, name: String? = nil, platform: String = "watchos") async throws {
        struct Body: Encodable { let token: String; let platform: String; let name: String? }
        let _: DeviceRegistration = try await send("winston/devices", method: "POST",
                                                   body: Body(token: token, platform: platform, name: name))
    }

    public struct DeviceRegistration: Decodable, Sendable {
        public let registered: Bool
        public let deviceTokens: Int

        enum CodingKeys: String, CodingKey {
            case registered
            case deviceTokens = "device_tokens"
        }
    }

    /// Global alert mute. Zero minutes clears it; tracking/background updates continue.
    public func mute(minutes: Int = 60) async throws -> MuteStatus {
        try await send("winston/mute", method: "POST", query: ["minutes": String(minutes)],
                       body: Optional<Data>.none)
    }

    public func muteStatus() async throws -> MuteStatus {
        try await get("winston/mute")
    }

    public struct MuteStatus: Decodable, Sendable {
        public let muted: Bool
        public let mutedUntil: Date?
        public let scope: String
        public let asOf: Date

        enum CodingKeys: String, CodingKey {
            case muted, scope
            case mutedUntil = "muted_until"
            case asOf = "as_of"
        }
    }

    // MARK: - Internals

    private func get<T: Decodable>(_ path: String, query: [String: String] = [:]) async throws -> T {
        try await send(path, method: "GET", query: query, body: Optional<Data>.none)
    }

    private func send<T: Decodable, B: Encodable>(_ path: String, method: String,
                                                   query: [String: String] = [:], body: B?) async throws -> T {
        guard var components = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)
        else { throw APIError.invalidBaseURL }
        if !query.isEmpty {
            components.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }.sorted { $0.name < $1.name }
        }
        guard let url = components.url else { throw APIError.invalidBaseURL }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 10
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let bearerToken {
            request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try WinstonJSON.encoder.encode(body)
        }
        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw APIError.badStatus(http.statusCode)
        }
        return try WinstonJSON.decoder.decode(T.self, from: data)
    }
}
