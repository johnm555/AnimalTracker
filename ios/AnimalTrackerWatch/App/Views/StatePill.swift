import SwiftUI
import AnimalTrackerCore

struct StatePill: View {
    let state: TrackerState

    private var tint: Color {
        switch state {
        case .seen: return .green
        case .transitioning: return .yellow
        case .lastSeen: return .gray
        case .unknown: return .secondary
        }
    }

    var body: some View {
        Label(state.title, systemImage: state.symbolName)
            .font(.caption2.weight(.medium))
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(tint.opacity(0.25), in: Capsule())
            .foregroundStyle(tint)
    }
}
