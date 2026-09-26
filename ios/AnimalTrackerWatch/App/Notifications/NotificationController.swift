import Foundation
import UserNotifications
import WatchKit
import WidgetKit
import AnimalTrackerCore

/// WKApplicationDelegate: APNs registration, category setup, silent-push handling.
final class NotificationController: NSObject, WKApplicationDelegate, UNUserNotificationCenterDelegate {
    enum Action {
        static let mute1h = "MUTE_1H"
        static let open = "OPEN"
    }

    func applicationDidFinishLaunching() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self

        let mute = UNNotificationAction(identifier: Action.mute1h, title: "Mute all 1 h", options: [.foreground])
        let open = UNNotificationAction(identifier: Action.open, title: "Open", options: [.foreground])
        let category = UNNotificationCategory(identifier: NotificationPayload.category,
                                              actions: [open, mute],
                                              intentIdentifiers: [],
                                              options: [])
        center.setNotificationCategories([category])

        Task {
            // Time-sensitive delivery comes from the entitlement (Config/Watch.entitlements), not an option.
            let granted = (try? await center.requestAuthorization(options: [.alert, .sound, .badge])) ?? false
            if granted {
                await MainActor.run { WKApplication.shared().registerForRemoteNotifications() }
            }
        }
    }

    // MARK: APNs registration

    func didRegisterForRemoteNotifications(withDeviceToken deviceToken: Data) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        let store = SharedStore.default
        if store.deviceToken != token {
            store.deviceToken = token
            store.registeredDeviceToken = nil   // new token: the backend must learn it
        }
        // LocationStore posts it to `POST /winston/devices` on its next refresh.
        NotificationCenter.default.post(name: .winstonDeviceTokenChanged, object: token)
    }

    func didFailToRegisterForRemoteNotificationsWithError(_ error: Error) {
        print("APNs registration failed: \(error.localizedDescription)")
    }

    // MARK: Silent (content-available) pushes

    func didReceiveRemoteNotification(_ userInfo: [AnyHashable: Any]) async -> WKBackgroundFetchResult {
        guard let payload = try? NotificationPayload.decode(userInfo: userInfo),
              let winston = payload.winston else { return .noData }
        var snapshot = SharedStore.default.snapshot ?? .unknown
        snapshot = winston.applied(to: snapshot)
        SharedStore.default.snapshot = snapshot
        WidgetCenter.shared.reloadAllTimelines()
        NotificationCenter.default.post(name: .winstonNotificationReceived, object: winston)
        return .newData
    }

    // MARK: UNUserNotificationCenterDelegate

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        forward(notification.request.content.userInfo)
        return [.banner, .sound]
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse) async {
        forward(response.notification.request.content.userInfo)
        switch response.actionIdentifier {
        case Action.mute1h:
            let store = SharedStore.default
            guard let url = store.baseURL else {
                store.muteActionStatus = "Mute failed: configure the server in Settings."
                return
            }
            do {
                let status = try await AnimalTrackerAPIClient(baseURL: url, bearerToken: store.apiToken).mute()
                if status.muted, let until = status.mutedUntil {
                    store.muteActionStatus = "Server confirmed all alerts muted until " + until.formatted(date: .omitted, time: .shortened)
                } else {
                    store.muteActionStatus = "Mute was not confirmed by the server."
                }
            } catch {
                store.muteActionStatus = "Mute not confirmed: " + error.localizedDescription
            }
        default:
            break
        }
    }

    private func forward(_ userInfo: [AnyHashable: Any]) {
        guard let payload = try? NotificationPayload.decode(userInfo: userInfo),
              let winston = payload.winston else { return }
        NotificationCenter.default.post(name: .winstonNotificationReceived, object: winston)
    }
}
