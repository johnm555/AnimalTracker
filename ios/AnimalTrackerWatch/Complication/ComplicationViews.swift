import SwiftUI
import WidgetKit
import AnimalTrackerCore

struct ComplicationView: View {
    @Environment(\.widgetFamily) private var family
    let entry: LocationEntry

    private var s: LocationSnapshot { entry.snapshot.aged(at: entry.date) }
    private var symbol: String { ZoneLabel.symbolName(for: s.zone) }
    private var age: String { RelativeTime.compact(since: s.lastSeenAt, now: entry.date) }
    private var tint: Color {
        if s.state == .unknown { return .secondary }
        return ZoneLabel.isHighPriority(s.zone) ? .orange : .accentColor
    }

    var body: some View {
        switch family {
        case .accessoryCircular: circular
        case .accessoryCorner: corner
        case .accessoryInline: inline
        default: rectangular
        }
    }

    private var circular: some View {
        ZStack {
            AccessoryWidgetBackground()
            VStack(spacing: 0) {
                Image(systemName: symbol).font(.title3).foregroundStyle(tint)
                Text(age).font(.caption2.monospacedDigit())
            }
        }
        .widgetAccentable()
    }

    private var corner: some View {
        Image(systemName: symbol)
            .font(.title2)
            .foregroundStyle(tint)
            .widgetLabel {
                Text(s.state == .unknown ? "Winston: unknown" : "\(s.displayZone) · \(age)")
            }
            .widgetAccentable()
    }

    private var inline: some View {
        Label(s.state == .unknown ? "Winston: unknown" : "Winston: \(s.displayZone) \(age)",
              systemImage: symbol)
    }

    private var rectangular: some View {
        HStack(spacing: 8) {
            Image(systemName: symbol).font(.title2).foregroundStyle(tint).frame(width: 26)
            VStack(alignment: .leading, spacing: 1) {
                Text("Winston").font(.caption2).foregroundStyle(.secondary)
                Text(s.state == .unknown ? "Not seen yet" : s.displayZone.capitalized)
                    .font(.headline)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                Text(s.state == .unknown ? "—"
                     : s.state == .transitioning ? "unconfirmed move · \(age)"
                     : "\(s.state == .seen ? "seen" : "last seen") \(age)")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .widgetAccentable()
    }
}
