import SwiftUI

struct ConfidenceBar: View {
    let value: Double   // 0…1

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text("Confidence").font(.caption2).foregroundStyle(.secondary)
                Spacer()
                Text(value, format: .percent.precision(.fractionLength(0))).font(.caption2.monospacedDigit())
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(.quaternary)
                    Capsule().fill(value >= 0.9 ? Color.green : value >= 0.7 ? Color.yellow : Color.orange)
                        .frame(width: max(4, geo.size.width * value))
                }
            }
            .frame(height: 5)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Confidence \(Int(value * 100)) percent")
    }
}
