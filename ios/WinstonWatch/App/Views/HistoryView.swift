import SwiftUI
import WinstonCore

struct HistoryView: View {
    @EnvironmentObject private var store: LocationStore

    var body: some View {
        List {
            if store.today.isEmpty {
                ContentUnavailableView("No moves today", systemImage: "pawprint",
                                       description: Text("Transitions appear here as cameras confirm them."))
            } else {
                ForEach(store.today) { t in
                    TransitionRow(transition: t)
                }
            }
        }
        .navigationTitle("Today")
        .refreshable { await store.refresh() }
    }
}

struct TransitionRow: View {
    let transition: WinstonCore.Transition

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: ZoneLabel.symbolName(for: transition.toZone))
                .foregroundStyle(ZoneLabel.isHighPriority(transition.toZone) ? .orange : .accentColor)
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(transition.summary)
                    .font(.footnote.weight(.medium))
                    .lineLimit(2)
                HStack(spacing: 6) {
                    Text(transition.arrivedAt, style: .time)
                    Text("·")
                    Text(transition.confidence, format: .percent.precision(.fractionLength(0)))
                }
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }
}
