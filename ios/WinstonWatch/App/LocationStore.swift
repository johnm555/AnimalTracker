import Foundation
import SwiftUI
import WatchKit
import WidgetKit
import WinstonCore

/// Single source of truth for the app's UI. Reads/writes the App Group store
/// so the complication stays in sync, and talks to the backend.
@MainActor
final class LocationStore: ObservableObject {
    @Published private(set) var snapshot: LocationSnapshot
    @Published private(set) var today: [WinstonCore.Transition] = []
    @Published private(set) var stats: Stats?
    @Published private(set) var isRefreshing = false
    @Published var lastError: String?
    @Published var baseURLString: String {
        didSet { shared.baseURL = URL(string: baseURLString) }
    }
    @Published var apiToken: String {
        didSet { shared.apiToken = apiToken }
    }
    @Published private(set) var registrationStatus: String?

    private let shared: SharedStore
    private var client: WinstonAPIClient? {
        shared.baseURL.map { WinstonAPIClient(baseURL: $0, bearerToken: shared.apiToken) }
    }

    init(shared: SharedStore = .default) {
        self.shared = shared
        self.snapshot = shared.snapshot ?? .unknown
        self.baseURLString = shared.baseURL?.absoluteString ?? "http://mac-mini.local:8420"
        self.apiToken = shared.apiToken ?? ""
    }

    /// Send the APNs device token to the backend (`POST /winston/devices`). Retried on
    /// every refresh until the backend confirms it, and again whenever the token changes.
    func registerDeviceIfNeeded(force: Bool = false) async {
        guard let token = shared.deviceToken, let client else { return }
        if !force, shared.registeredDeviceToken == token { return }
        do {
            try await client.registerDevice(token: token, name: WKInterfaceDevice.current().name)
            shared.registeredDeviceToken = token
            registrationStatus = "registered"
        } catch {
            registrationStatus = "not registered: \(error.localizedDescription)"
        }
    }

    /// Fetch location, today's transitions and stats. Errors are surfaced, never fatal —
    /// the last known snapshot stays on screen (and is labelled with its age).
    func refresh() async {
        guard let client else {
            lastError = "Set the server address in Settings."
            return
        }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            let location = try await client.location()
            snapshot = location
            shared.snapshot = location
            lastError = nil
            WidgetCenter.shared.reloadAllTimelines()
            await registerDeviceIfNeeded()
        } catch {
            lastError = error.localizedDescription
        }
        async let transitions = client.transitions(on: Date())
        async let stats = client.stats(hours: 24)
        if let t = try? await transitions { today = t.reversed() }   // newest first
        if let s = try? await stats { self.stats = s }
    }

    /// Instant UI update from a push, before the next fetch confirms it.
    func apply(_ notification: WinstonNotification) {
        let updated = notification.applied(to: snapshot)
        snapshot = updated
        shared.snapshot = updated
        WidgetCenter.shared.reloadAllTimelines()
        Task { await refresh() }
    }
}
