import Foundation

/// App-Group-backed store shared by the watch app and the complication.
///
/// The app writes the latest `LocationSnapshot` after every fetch or push;
/// the WidgetKit timeline provider reads it so complications render without
/// touching the network.
public struct SharedStore: Sendable {
    public static let appGroup = "group.com.johnmarshall.winstontracker"
    public static let `default` = SharedStore(suiteName: appGroup)

    private enum Key {
        static let snapshot = "winston.snapshot.v1"
        static let baseURL = "winston.baseURL"
        static let deviceToken = "winston.apns.deviceToken"
        static let apiToken = "winston.apiToken"
        static let deviceTokenRegistered = "winston.apns.deviceTokenRegistered"
    }

    private let suiteName: String?

    public init(suiteName: String?) {
        self.suiteName = suiteName
    }

    private var defaults: UserDefaults {
        (suiteName.flatMap(UserDefaults.init(suiteName:))) ?? .standard
    }

    public var snapshot: LocationSnapshot? {
        get {
            guard let data = defaults.data(forKey: Key.snapshot) else { return nil }
            return try? WinstonJSON.decoder.decode(LocationSnapshot.self, from: data)
        }
        nonmutating set {
            if let newValue, let data = try? WinstonJSON.encoder.encode(newValue) {
                defaults.set(data, forKey: Key.snapshot)
            } else {
                defaults.removeObject(forKey: Key.snapshot)
            }
        }
    }

    /// Last action result, not a claim of the server's current mute state.
    public var muteActionStatus: String? {
        get { defaults.string(forKey: "winston.muteActionStatus") }
        nonmutating set { defaults.set(newValue, forKey: "winston.muteActionStatus") }
    }

    /// Backend base URL, e.g. `http://mac-mini.local:8420`.
    public var baseURL: URL? {
        get { defaults.string(forKey: Key.baseURL).flatMap(URL.init(string:)) }
        nonmutating set { defaults.set(newValue?.absoluteString, forKey: Key.baseURL) }
    }

    public var deviceToken: String? {
        get { defaults.string(forKey: Key.deviceToken) }
        nonmutating set { defaults.set(newValue, forKey: Key.deviceToken) }
    }

    /// Bearer token for backend writes (`WINSTON_API_TOKEN` on the server). Optional.
    public var apiToken: String? {
        get { defaults.string(forKey: Key.apiToken).flatMap { $0.isEmpty ? nil : $0 } }
        nonmutating set { defaults.set(newValue, forKey: Key.apiToken) }
    }

    /// The device token the backend last confirmed, so registration is retried only when it changes.
    public var registeredDeviceToken: String? {
        get { defaults.string(forKey: Key.deviceTokenRegistered) }
        nonmutating set { defaults.set(newValue, forKey: Key.deviceTokenRegistered) }
    }
}
