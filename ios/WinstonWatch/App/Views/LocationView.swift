import Combine
import SwiftUI
import WinstonCore

struct LocationView: View {
    @EnvironmentObject private var store: LocationStore
    // Re-render every 30 s so "N min ago" stays honest without a fetch.
    private let clock = Timer.publish(every: 30, on: .main, in: .common).autoconnect()
    @State private var now = Date()

    var body: some View {
        let s = store.snapshot.aged(at: now)
        ScrollView {
            VStack(spacing: 8) {
                Image(systemName: ZoneLabel.symbolName(for: s.zone))
                    .font(.system(size: 34))
                    .foregroundStyle(ZoneLabel.isHighPriority(s.zone) ? .orange : .accentColor)
                    .padding(.top, 4)

                Text(s.displayZone.capitalized)
                    .font(.title3.weight(.semibold))
                    .multilineTextAlignment(.center)
                    .minimumScaleFactor(0.7)

                StatePill(state: s.state)

                if s.state == .transitioning, let to = s.toZone {
                    Label("Possible move to \(ZoneLabel.label(for: to))", systemImage: "arrow.right")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Text(s.state == .unknown ? "No camera has seen Winston yet"
                                         : "Seen \(RelativeTime.short(since: s.lastSeenAt, now: now))")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)

                if s.state != .unknown {
                    ConfidenceBar(value: s.confidence)
                        .padding(.horizontal)
                }

                if let err = store.lastError {
                    Text(err)
                        .font(.caption2)
                        .foregroundStyle(.red)
                        .multilineTextAlignment(.center)
                        .padding(.top, 2)
                }

                Button {
                    Task { await store.refresh() }
                } label: {
                    if store.isRefreshing { ProgressView() } else { Label("Refresh", systemImage: "arrow.clockwise") }
                }
                .buttonStyle(.bordered)
                .disabled(store.isRefreshing)
                .padding(.top, 4)
            }
            .padding(.horizontal, 4)
        }
        .navigationTitle("Winston")
        .onReceive(clock) { now = $0 }
    }
}

#Preview {
    LocationView().environmentObject(LocationStore(shared: SharedStore(suiteName: nil)))
}
