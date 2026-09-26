import AppIntents
import Foundation
import WidgetKit
import WinstonCore

struct CheckOnWinstonIntent: AppIntent {
    static var title: LocalizedStringResource = "Check on Winston"
    static var description = IntentDescription("Get Winston's last confirmed camera sighting and its age.")
    static var openAppWhenRun = false

    func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
        let store = SharedStore.default
        guard let url = store.baseURL else {
            let text = "Open Animal Tracker and set the tracker server address in Settings first."
            return .result(value: text, dialog: "\(text)")
        }
        let text: String
        do {
            let snapshot = try await WinstonAPIClient(baseURL: url).location()
            store.snapshot = snapshot
            WidgetCenter.shared.reloadAllTimelines()
            text = WinstonCheckIn.response(snapshot: snapshot, reachable: true)
        } catch {
            text = WinstonCheckIn.response(snapshot: store.snapshot, reachable: false)
        }
        return .result(value: text, dialog: "\(text)")
    }
}

struct WinstonShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: CheckOnWinstonIntent(), phrases: [
            "Check on Winston with \(.applicationName)",
            "Where is Winston with \(.applicationName)"
        ], shortTitle: "Check on Winston", systemImageName: "pawprint")
    }
}
