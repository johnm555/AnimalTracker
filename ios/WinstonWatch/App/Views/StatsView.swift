import SwiftUI
import WinstonCore

struct StatsView: View {
    @EnvironmentObject private var store: LocationStore

    var body: some View {
        ScrollView {
            if let s = store.stats {
                VStack(alignment: .leading, spacing: 10) {
                    StatTile(title: "Outside", value: minutes(s.outsideMinutes), symbol: "sun.max.fill")
                    StatTile(title: "Moves", value: "\(s.transitions)", symbol: "arrow.left.arrow.right",
                             detail: String(format: "%.1f / h", s.transitionsPerHour))
                    StatTile(title: "Street side", value: "\(s.streetAdjacentVisits)", symbol: "car.fill",
                             detail: "front / driveway visits")
                    if let top = s.mostVisitedZone {
                        StatTile(title: "Favourite", value: ZoneLabel.label(for: top).capitalized,
                                 symbol: ZoneLabel.symbolName(for: top))
                    }
                    Text("Last 24 h").font(.caption2).foregroundStyle(.secondary)
                }
                .padding(.horizontal, 4)
            } else {
                ContentUnavailableView("No stats yet", systemImage: "chart.bar",
                                       description: Text("Pull to refresh once the backend has data."))
            }
        }
        .navigationTitle("Stats")
        .refreshable { await store.refresh() }
    }

    private func minutes(_ m: Double) -> String {
        m >= 60 ? String(format: "%.1f h", m / 60) : "\(Int(m)) min"
    }
}

struct StatTile: View {
    let title: String
    let value: String
    let symbol: String
    var detail: String? = nil

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: symbol).frame(width: 22).foregroundStyle(Color.accentColor)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.caption2).foregroundStyle(.secondary)
                Text(value).font(.headline)
                if let detail { Text(detail).font(.caption2).foregroundStyle(.secondary) }
            }
            Spacer(minLength: 0)
        }
        .padding(8)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
    }
}
