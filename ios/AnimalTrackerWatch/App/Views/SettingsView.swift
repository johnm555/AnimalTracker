import SwiftUI
import AnimalTrackerCore

struct SettingsView: View {
    @EnvironmentObject private var store: LocationStore

    @AppStorage("winston.muteActionStatus", store: UserDefaults(suiteName: SharedStore.appGroup))
    private var muteActionStatus = "No mute requested."
    @State private var changingMute = false

    var body: some View {
        Form {
            Section("Server") {
                TextField("http://mac-mini.local:8420", text: $store.baseURLString)
                    .textContentType(.URL)
                    .autocorrectionDisabled()
                TextField("API token (optional)", text: $store.apiToken)
                    .autocorrectionDisabled()
                Button("Test connection") { Task { await store.refresh() } }
            }
            Section("Push") {
                LabeledContent("Device token",
                               value: SharedStore.default.deviceToken.map { String($0.prefix(8)) + "…" } ?? "none yet")
                    .font(.caption2)
                LabeledContent("Backend", value: store.registrationStatus
                               ?? (SharedStore.default.registeredDeviceToken != nil ? "registered" : "not registered"))
                    .font(.caption2)
                Button("Register with backend") { Task { await store.registerDeviceIfNeeded(force: true) } }
                    .disabled(SharedStore.default.deviceToken == nil)
            }
            Section("Alerts on all devices") {
                Button("Mute all for 1 hour") { Task { await setMute(60) } }
                    .disabled(changingMute)
                Button("Unmute all") { Task { await setMute(0) } }
                    .disabled(changingMute)
                Text("Last request: " + muteActionStatus).font(.caption2)
                Text("Includes priority alerts. Tracking and background updates continue.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            Section {
                Text("Reports only what cameras have observed. \"Unknown\" means no camera has seen Winston yet.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .navigationTitle("Settings")
    }

    @MainActor private func setMute(_ minutes: Int) async {
        guard let url = SharedStore.default.baseURL else {
            muteActionStatus = "Configure the server first."
            return
        }
        changingMute = true
        defer { changingMute = false }
        do {
            let status = try await AnimalTrackerAPIClient(baseURL: url, bearerToken: SharedStore.default.apiToken)
                .mute(minutes: minutes)
            if status.muted, let until = status.mutedUntil {
                muteActionStatus = "Server confirmed mute until " + until.formatted(date: .omitted, time: .shortened)
            } else {
                muteActionStatus = "Server confirmed alerts unmuted at " + status.asOf.formatted(date: .omitted, time: .shortened)
            }
        } catch {
            muteActionStatus = "Not confirmed: " + error.localizedDescription
        }
    }
}
