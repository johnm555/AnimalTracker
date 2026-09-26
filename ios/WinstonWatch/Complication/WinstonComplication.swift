import SwiftUI
import WidgetKit
import WinstonCore

struct LocationEntry: TimelineEntry {
    let date: Date
    let snapshot: LocationSnapshot
}

/// Reads the last snapshot the app stored in the App Group; never hits the network.
/// Emits one entry per minute for the next 15 minutes so "N min ago" advances
/// between reloads, then asks WidgetKit to refresh.
struct LocationTimelineProvider: TimelineProvider {
    private let store = SharedStore.default

    func placeholder(in context: Context) -> LocationEntry {
        LocationEntry(date: Date(), snapshot: .unknown)
    }

    func getSnapshot(in context: Context, completion: @escaping (LocationEntry) -> Void) {
        completion(LocationEntry(date: Date(), snapshot: store.snapshot ?? .unknown))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<LocationEntry>) -> Void) {
        let snapshot = store.snapshot ?? .unknown
        let now = Date()
        let entries = (0..<16).map { minute in
            LocationEntry(date: now.addingTimeInterval(Double(minute) * 60), snapshot: snapshot)
        }
        completion(Timeline(entries: entries, policy: .after(now.addingTimeInterval(15 * 60))))
    }
}

struct WinstonComplication: Widget {
    let kind = "WinstonComplication"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: LocationTimelineProvider()) { entry in
            ComplicationView(entry: entry)
                .containerBackground(for: .widget) { Color.clear }
        }
        .configurationDisplayName("Animal Tracker")
        .description("Where Winston was last seen.")
        .supportedFamilies([.accessoryCircular, .accessoryCorner, .accessoryRectangular, .accessoryInline])
    }
}

#Preview(as: .accessoryRectangular) {
    WinstonComplication()
} timeline: {
    LocationEntry(date: .now, snapshot: LocationSnapshot(state: .seen, zone: "driveway", confidence: 0.95,
                                                          lastSeenAt: .now.addingTimeInterval(-60)))
    LocationEntry(date: .now, snapshot: .unknown)
}
