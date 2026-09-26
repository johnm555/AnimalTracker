import SwiftUI
import WatchKit
import AnimalTrackerCore

@main
struct AnimalTrackerWatchApp: App {
    @WKApplicationDelegateAdaptor(NotificationController.self) private var appDelegate
    @StateObject private var store = LocationStore()

    var body: some Scene {
        WindowGroup {
            TabView {
                LocationView()
                HistoryView()
                StatsView()
                SettingsView()
            }
            .tabViewStyle(.verticalPage)
            .environmentObject(store)
            .task { await store.refresh() }
            .onReceive(NotificationCenter.default.publisher(for: .winstonNotificationReceived)) { note in
                if let payload = note.object as? AnimalTrackerNotification {
                    store.apply(payload)
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: .winstonDeviceTokenChanged)) { _ in
                Task { await store.registerDeviceIfNeeded(force: true) }
            }
        }

        // Custom long-look UI for WINSTON_MOVED pushes.
        WKNotificationScene(controller: AnimalTrackerNotificationController.self,
                            category: NotificationPayload.category)
    }
}

extension Notification.Name {
    /// Posted by NotificationController with a `AnimalTrackerNotification` object.
    static let winstonNotificationReceived = Notification.Name("winstonNotificationReceived")
    /// Posted by NotificationController when APNs hands the app a (new) device token.
    static let winstonDeviceTokenChanged = Notification.Name("winstonDeviceTokenChanged")
}
