import SwiftUI
import UserNotifications
import WinstonCore

/// Long-look UI for WINSTON_MOVED notifications.
struct NotificationView: View {
    let title: String
    let message: String
    let winston: WinstonNotification?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Image(systemName: ZoneLabel.symbolName(for: winston?.zone))
                    .foregroundStyle(winston?.isHighPriority == true ? .orange : .accentColor)
                Text(title).font(.headline).lineLimit(2)
            }
            Text(message).font(.footnote).foregroundStyle(.secondary)
            if let w = winston {
                HStack {
                    Text(w.arrivedAt, style: .time)
                    Spacer()
                    Text(w.confidence, format: .percent.precision(.fractionLength(0)))
                }
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 4)
    }
}

final class WinstonNotificationController: WKUserNotificationHostingController<NotificationView> {
    private var title = ""
    private var message = ""
    private var winston: WinstonNotification?

    override var body: NotificationView {
        NotificationView(title: title, message: message, winston: winston)
    }

    override func didReceive(_ notification: UNNotification) {
        let content = notification.request.content
        title = content.title
        message = content.body
        winston = (try? NotificationPayload.decode(userInfo: content.userInfo))?.winston
    }
}
