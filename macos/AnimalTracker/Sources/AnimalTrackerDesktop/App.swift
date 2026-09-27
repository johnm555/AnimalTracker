import SwiftUI
import AppKit
import DesktopCore

enum Page: String, CaseIterable, Identifiable {
    case welcome = "Welcome", engine = "Tracking engine", animal = "Your animal", ring = "Connect Ring", cameras = "Cameras & zones", photos = "Reference photos", providers = "Notifications & Find My", review = "Review setup", dashboard = "Overview"
    var id: String { rawValue }
    var icon: String {
        switch self {
        case .welcome: return "pawprint.fill"
        case .engine: return "cpu"
        case .animal: return "dog"
        case .ring: return "video"
        case .cameras: return "point.3.connected.trianglepath.dotted"
        case .photos: return "photo.on.rectangle.angled"
        case .providers: return "bell.badge"
        case .review: return "checkmark.shield"
        case .dashboard: return "square.grid.2x2"
        }
    }
}
private let forest = Color(nsColor: NSColor(name: nil) { appearance in
    appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        ? NSColor(calibratedRed: 0.52, green: 0.80, blue: 0.66, alpha: 1)
        : NSColor(calibratedRed: 0.12, green: 0.34, blue: 0.27, alpha: 1)
})

final class AppDelegate: NSObject, NSApplicationDelegate {
    var model: AppModel?
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if model?.ownedRunning == true {
            let alert = NSAlert(); alert.messageText = "Stop tracking and quit?"
            alert.informativeText = "The engine started by this app runs while the app is open. Closing the window keeps it running; quitting stops it."
            alert.addButton(withTitle: "Stop and Quit"); alert.addButton(withTitle: "Keep Running")
            if alert.runModal() != .alertFirstButtonReturn { return .terminateCancel }
        }
        model?.shutdown(); return .terminateNow
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
}
@main
struct AnimalTrackerApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel()
    var body: some Scene {
        WindowGroup("Animal Tracker") {
            ContentView().environmentObject(model).onAppear { delegate.model = model }
                .frame(minWidth: 980, minHeight: 710).tint(forest)
        }.defaultSize(width: 1120, height: 810)
    }
}

struct ContentView: View {
    @EnvironmentObject var m: AppModel
    @State private var email = ""
    @State private var password = ""
    @State private var appleEmail = ""
    @State private var applePassword = ""
    @State private var anisette = "https://ani.sidestore.io"
    @State private var consent = false
    @State private var code = ""
    @State private var confirmMessage = false
    var body: some View {
        NavigationSplitView {
            VStack(alignment: .leading, spacing: 22) {
                HStack(spacing: 12) {
                    Image(systemName: "pawprint.fill").font(.title).foregroundStyle(forest)
                    VStack(alignment: .leading) { Text("Animal Tracker").font(.headline); Text("A little more peace of mind.").font(.caption).foregroundStyle(.secondary) }
                }.padding(.top, 24)
                List(Page.allCases, selection: $m.page) { page in Label(page.rawValue, systemImage: page.icon).tag(page).padding(.vertical, 5) }.listStyle(.sidebar)
                VStack(alignment: .leading, spacing: 7) {
                    Label("Private by design", systemImage: "lock.shield").font(.caption.bold())
                    Text("Your photos, observations and account sessions stay in your Mac’s data folder.").font(.caption).foregroundStyle(.secondary)
                }.padding(.bottom, 20)
            }.padding(.horizontal, 12).navigationSplitViewColumnWidth(245)
        } detail: {
            VStack(spacing: 0) {
                HStack { Text(m.page == .dashboard ? "YOUR TRACKER" : "LET’S GET YOU SET UP").font(.caption.weight(.semibold)).tracking(2).foregroundStyle(forest); Spacer(); Text("macOS • Preview").font(.caption).foregroundStyle(.secondary) }.padding(28)
                ScrollView {
                    VStack(alignment: .leading, spacing: 22) {
                        Text(m.page.rawValue).font(.system(size: 34, weight: .semibold, design: .rounded))
                        pageContent
                    }.frame(maxWidth: 760, alignment: .leading).padding(.horizontal, 32).padding(.bottom, 30)
                    .disabled(m.busy)
                }
                if !m.message.isEmpty || m.busy {
                    HStack(spacing: 12) {
                        if m.busy { ProgressView().controlSize(.small) } else { Image(systemName: m.error ? "exclamationmark.circle" : "checkmark.circle").foregroundStyle(m.error ? Color.orange : forest) }
                        Text(m.message).font(.callout).textSelection(.enabled)
                        Spacer()
                        if m.busy { Button("Cancel") { m.bridge.cancel() } }
                    }.padding(18).background(.quaternary.opacity(0.4))
                }
            }.background(Color(nsColor: .windowBackgroundColor))
        }
        .sheet(isPresented: $m.otp) {
            VStack(alignment: .leading, spacing: 18) {
                Label("Verify your account", systemImage: "lock.shield").font(.title2.bold())
                Text("Enter the code from your trusted device or message. It is used only for this sign-in.").foregroundStyle(.secondary)
                SecureField("Verification code", text: $code).textFieldStyle(.roundedBorder).onSubmit { submitCode() }
                HStack { Button("Cancel sign-in") { m.bridge.cancel(); m.otp = false; code = "" }; Spacer(); Button("Verify") { submitCode() }.buttonStyle(.borderedProminent).disabled(code.isEmpty) }
            }.padding(30).frame(width: 410)
        }
        .alert("Send a setup test?", isPresented: $confirmMessage) {
            Button("Cancel", role: .cancel) {}
            Button("Send test") { m.run(["action": "imessage_test", "recipient": m.recipient, "confirmed": true]) }
        } message: { Text("A test message will be sent from this Mac’s Messages account to \(m.recipient). This does not create an animal sighting.") }
        .onChange(of: m.page) { _, _ in m.previewed = false }
    }
    func submitCode() { m.bridge.code(code); code = ""; m.otp = false }
    @ViewBuilder var pageContent: some View {
        switch m.page {
        case .welcome:
            Text("Know who was seen.\nKnow when. Stay connected.").font(.system(size: 40, weight: .medium, design: .rounded)).foregroundStyle(forest)
            Text("Turn the Ring cameras you already have into a record of your animal’s movements, with local vision and thoughtful notifications.").font(.title3).foregroundStyle(.secondary)
            HStack(alignment: .top, spacing: 14) {
                feature("video", "Connect", "Choose the cameras that cover one property.")
                feature("pawprint", "Recognize", "Teach the tracker what your animal looks like.")
                feature("applewatch", "Check in", "See observations and connect your watch.")
            }
            card("Start with one animal", icon: "heart") {
                Text("This version tracks one enrolled animal per installation. Other animals may be recorded as visitors; independent tracking for multiple pets is not available yet.")
            }
            Button("Set up my tracker", systemImage: "arrow.right") { m.page = .engine }.buttonStyle(.borderedProminent).controlSize(.large)
            if m.engineReady { Button("Open existing tracker") { m.page = .dashboard; m.refresh() } }
        case .engine:
            lead("Install the engine once. Your data stays separate from the app, so app updates won’t replace it.")
            card("Local vision, on your Mac", icon: "cpu") {
                Text("The app installs Python libraries into a private environment. Downloads can take several minutes and need internet access and several GB of free space. First tracking may also download model weights.")
                field("Python 3.11 or newer", text: $m.pythonPath)
                HStack {
                    Button("Choose Python…") { let panel = NSOpenPanel(); panel.canChooseDirectories = false; if panel.runModal() == .OK, let u = panel.url { m.pythonPath = u.path } }
                    Button("Get Python 3.12") { m.open("https://www.python.org/downloads/macos/") }
                    Spacer()
                    Button(m.engineReady ? "Repair engine" : "Prepare engine") { m.prepare() }.buttonStyle(.borderedProminent)
                }
                Text("Python is a prerequisite for this preview. Choose the installed python3 executable, not the installer file.").font(.caption).foregroundStyle(.secondary)
            }
            Text(m.engineReady ? "✓ Tracking engine is installed." : "The engine has not been prepared yet.").foregroundStyle(m.engineReady ? forest : .secondary)
            next(.animal, enabled: m.engineReady)
        case .animal:
            lead("Recognition starts with a specific animal, not a generic ‘dog’ label.")
            card("Meet your animal", icon: "pawprint") {
                field("Name", text: $m.name)
                field("Appearance • breed, coat, size and distinguishing marks", text: $m.animalDescription)
                Text("A collar can change. Include features that will still identify your animal without it.").font(.caption).foregroundStyle(.secondary)
            }
            next(.ring, enabled: !m.name.trimmingCharacters(in: .whitespaces).isEmpty && m.engineReady)
        case .ring:
            lead("Sign in to Ring to find your cameras. Only the account session is saved; the app does not save your password.")
            card("Ring account", icon: "video") {
                field("Email", text: $email)
                SecureField("Password", text: $password).textFieldStyle(.roundedBorder)
                Button("Connect Ring") { m.ring(email: email, password: password); password = "" }.buttonStyle(.borderedProminent).disabled(!m.engineReady || email.isEmpty || password.isEmpty)
                Text("If a session is already saved, Ring reuses that account. To change accounts, remove its token from the data folder first.").font(.caption).foregroundStyle(.secondary)
                Button("Use saved Ring session") { m.ring(email: "", password: "") }.disabled(!m.engineReady)
            }
            card("Before you continue", icon: "motion") {
                Text("In Ring, enable All Motion for the selected cameras. People Only can miss your animal. Recorded event clips must be available on your Ring plan. The tracker uses event clips, never continuous live streaming.")
            }
            next(.cameras, enabled: !m.cameras.isEmpty)
        case .cameras:
            lead("Select cameras at one property. Cameras looking at the same area can share a zone. Leave other properties unchecked.")
            if m.cameras.isEmpty { card("Connect Ring first", icon: "video.slash") { Button("Go to Ring sign-in") { m.page = .ring } } }
            ForEach($m.cameras) { $camera in
                HStack {
                    Toggle(camera.name, isOn: $camera.selected).frame(maxWidth: .infinity, alignment: .leading)
                    TextField("Zone, e.g. kitchen", text: $camera.zone).textFieldStyle(.roundedBorder).frame(width: 215).disabled(!camera.selected)
                }.padding(14).background(.background, in: RoundedRectangle(cornerRadius: 12))
            }
            card("Connect neighboring zones", icon: "point.3.connected.trianglepath.dotted") {
                Text("Add routes your animal can walk directly. The minimum stays at zero until measured; the slow-walk window is editable. Routes work in both directions.").font(.callout).foregroundStyle(.secondary)
                ForEach($m.links) { $link in
                    HStack {
                        Picker("From", selection: $link.from) { Text("Choose…").tag(""); ForEach(m.zones, id: \.self) { Text($0).tag($0) } }.labelsHidden()
                        Image(systemName: "arrow.left.arrow.right")
                        Picker("To", selection: $link.to) { Text("Choose…").tag(""); ForEach(m.zones, id: \.self) { Text($0).tag($0) } }.labelsHidden()
                        TextField("Seconds", value: $link.seconds, format: .number).frame(width: 65).textFieldStyle(.roundedBorder)
                        Text("sec").font(.caption)
                        Button { m.links.removeAll { $0.id == link.id } } label: { Image(systemName: "minus.circle") }.accessibilityLabel("Remove route")
                    }
                }
                Button("Add route", systemImage: "plus") { m.links.append(ZoneLink()) }.disabled(m.zones.count < 2)
            }
            next(.photos, enabled: !m.cameras.filter(\.selected).isEmpty)
        case .photos:
            lead("Add at least six clear photos of your animal: front, side, standing, resting, and different lighting. These help the model verify identity.")
            card("A gallery, just for your animal", icon: "photo.on.rectangle.angled") {
                Text("Choose JPEG, PNG or WebP images. Photos are copied to your private data folder, resized and stripped of location metadata. Originals stay unchanged.")
                Button("Choose reference photos…", systemImage: "plus") { m.importPhotos() }.buttonStyle(.borderedProminent).disabled(!m.engineReady)
                Text("Avoid images dominated by another pet. Detection quality needs evaluation on your own cameras; adding photos does not guarantee identification.").font(.caption).foregroundStyle(.secondary)
            }
            next(.providers)
        case .providers:
            lead("Choose how you want to hear from the tracker. Optional providers can be configured later.")
            card("iMessage", icon: "message.fill") {
                Toggle("Send meaningful transitions through Messages", isOn: $m.messages)
                if m.messages { field("Recipient • phone number or Apple ID email", text: $m.recipient) }
                Text("Uses Messages on this Mac. Sign in there with your Apple account; no Apple password is needed here. macOS may ask to allow Animal Tracker to control Messages when a notification is sent.").foregroundStyle(.secondary)
                Button("Open Messages") { NSWorkspace.shared.open(URL(fileURLWithPath: "/System/Applications/Messages.app")) }
                Button("Send test message…") { confirmMessage = true }.disabled(!m.engineReady || m.recipient.isEmpty || !m.messages)
                Text("Full Disk Access is needed only for reading replies, not for sending alerts. Without iMessage, events are still recorded locally.").font(.caption).foregroundStyle(.secondary)
            }
            card("Find My • experimental", icon: "location.circle") {
                Text("Optional account setup for future AirTag integration. Connecting an account does not enable AirTag tracking: tag keys and a poller are still required.").foregroundStyle(.secondary)
                DisclosureGroup("Advanced account setup") {
                    VStack(alignment: .leading, spacing: 12) {
                        field("Apple account email", text: $appleEmail)
                        SecureField("Apple account password", text: $applePassword).textFieldStyle(.roundedBorder)
                        field("Anisette service URL", text: $anisette)
                        Text("The unofficial Find My library uses this third-party service to obtain Apple authentication metadata. Your password is sent to Apple, not saved by this app. Session credentials are stored locally. Only proceed if you trust the service.").font(.caption)
                        Toggle("I understand this is experimental and trust this anisette service", isOn: $consent)
                        Button("Connect experimental account") {
                            m.run(["action": "findmy_login", "email": appleEmail, "password": applePassword, "anisette_url": anisette, "consent": consent]); applePassword = ""
                        }.disabled(!m.engineReady || !consent || appleEmail.isEmpty || applePassword.isEmpty)
                    }.padding(.top, 12)
                }
                Button("Open Find My") { NSWorkspace.shared.open(URL(fileURLWithPath: "/System/Applications/FindMy.app")) }
            }
            next(.review)
        case .review:
            lead("Review before saving. This changes camera mappings and notification settings; existing configuration files are backed up first.")
            card("Your setup", icon: "checklist") {
                LabeledContent("Animal", value: m.name.isEmpty ? "Not entered" : m.name)
                LabeledContent("Cameras", value: String(m.cameras.filter(\.selected).count))
                LabeledContent("Zones", value: m.zones.joined(separator: ", "))
                LabeledContent("Notifications", value: m.messages ? "iMessage to \(m.recipient)" : "Local records only")
                ForEach(m.cameras.filter(\.selected)) { c in Text("\(c.name) → \(c.zone)").font(.callout) }
                Text("Quiet hours default to 23:00–06:30 for a new installation. This setup does not mark any zone high priority or classify indoor/outdoor statistics.").font(.caption).foregroundStyle(.secondary)
                Toggle("Allow phone and watch access on my local network", isOn: $m.allowLAN)
                Text("An API token is generated when you save. LAN access is off by default; the Watch needs LAN access and the same token.").font(.caption).foregroundStyle(.secondary)
                Toggle("Allow replacing existing configuration, with backups", isOn: $m.replace)
                if let validation = m.validation { Label(validation, systemImage: "exclamationmark.circle").foregroundStyle(.orange) }
                HStack {
                    Button("Validate setup") { m.configure(preview: true) }.disabled(m.validation != nil || !m.engineReady)
                    Button("Save setup") { m.configure(preview: false) }.buttonStyle(.borderedProminent).disabled(!m.previewed || m.validation != nil)
                }
            }
            Text("The backend may queue uncertain frames for review. A Claude or Codex review schedule is a separate setup step; this app does not configure or promise subscription-based detection automatically.").font(.callout).foregroundStyle(.secondary)
        case .dashboard:
            sightingCard
            lead("A clear view of the engine, without guessing where your animal is.")
            HStack(spacing: 14) {
                feature("server.rack", m.externalRunning ? "API available" : (m.ownedRunning ? "Starting…" : "Not running"), m.externalRunning ? "Pipeline: \(m.state["pipeline_state"] as? String ?? "unknown")" : "Start the engine when setup is ready.")
                feature("photo.stack", "\(m.state["reference_count"] as? Int ?? 0) photos", "Stored reference images")
            }
            card("Tracking controls", icon: "power") {
                HStack {
                    Button("Refresh") { m.refresh() }.disabled(!m.engineReady)
                    Button("Start tracking", systemImage: "play.fill") { m.start() }.buttonStyle(.borderedProminent).disabled(!m.engineReady || !m.configured || m.ownedRunning || m.externalRunning)
                    Button("Stop tracking", systemImage: "stop.fill") { m.stop() }.disabled(!m.ownedRunning)
                }
                Text(m.externalRunning && !m.ownedRunning ? "An existing service is running. This app will not stop or replace it." : "Keep this app open while tracking. Closing the window is fine; quitting stops the engine started here.").foregroundStyle(.secondary)
                Text("If a camera cannot see your animal, its absence is not evidence of a new location. Check the Watch or API for timestamped observations.").font(.caption).foregroundStyle(.secondary)
            }
            card("Connections & diagnostics", icon: "stethoscope") {
                LabeledContent("Ring session", value: m.state["ring_saved"] as? Bool == true ? "Saved • not a live verification" : "Not saved")
                LabeledContent("Find My session", value: m.state["findmy_saved"] as? Bool == true ? "Saved • AirTag tracking not enabled" : "Optional • not connected")
                HStack {
                    Button("Check setup") { m.run(["action": "doctor"]) { m.checks = $0["checks"] as? [[String: Any]] ?? [] } }.disabled(!m.engineReady)
                    Button("Open data folder") { NSWorkspace.shared.open(m.data) }
                    Button("Copy Watch API token") { m.run(["action": "watch_token"]) { value in
                        if let token = value["token"] as? String { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(token, forType: .string); m.message = "API token copied. Paste it into Watch Settings; keep it private." }
                    } }.disabled(!m.engineReady)
                }
                Text("Watch connection: enable LAN access, then enter this Mac’s address, port \(m.state["port"] as? Int ?? 8420), and the token in Watch Settings. Installing the Watch app still requires Xcode signing and pairing.").font(.caption).foregroundStyle(.secondary)
                ForEach(Array(m.checks.enumerated()), id: \.offset) { _, check in
                    VStack(alignment: .leading, spacing: 4) {
                        Text("\(check["status"] as? String ?? "") · \(check["check"] as? String ?? "")").font(.headline)
                        Text(check["detail"] as? String ?? "")
                        if let fix = check["fix"] as? String, !fix.isEmpty { Text(fix).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
                    }.padding(.vertical, 5)
                }
            }
        }
    }
    var sightingCard: some View {
        let sighting = SightingSummary(m.state["location"] as? [String: Any] ?? [:])
        return card(sighting.title, icon: "pawprint.fill") {
            Text(sighting.zone).font(.system(size: 28, weight: .semibold, design: .rounded)).foregroundStyle(forest)
            if let time = sighting.timestamp {
                Text("Observed: \(displayTime(time))").font(.callout).foregroundStyle(.secondary)
                Text("This is the last confirmed observation, not a guarantee of current presence.").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
    func displayTime(_ value: String) -> String {
        let parser = ISO8601DateFormatter(); parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = parser.date(from: value) ?? ISO8601DateFormatter().date(from: value)
        return date?.formatted(date: .abbreviated, time: .standard) ?? value
    }
    func next(_ page: Page, enabled: Bool = true) -> some View { HStack { Spacer(); Button("Continue", systemImage: "arrow.right") { m.page = page }.buttonStyle(.borderedProminent).controlSize(.large).disabled(!enabled) } }
    func lead(_ text: String) -> some View { Text(text).font(.title3).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
    func field(_ label: String, text: Binding<String>) -> some View { VStack(alignment: .leading, spacing: 6) { Text(label).font(.callout.weight(.medium)); TextField(label, text: text).labelsHidden().textFieldStyle(.roundedBorder) } }
    func feature(_ icon: String, _ title: String, _ body: String) -> some View {
        VStack(alignment: .leading, spacing: 12) { Image(systemName: icon).font(.title2).foregroundStyle(forest); Text(title).font(.headline); Text(body).font(.callout).foregroundStyle(.secondary) }.frame(maxWidth: .infinity, alignment: .leading).padding(20).frame(maxHeight: .infinity, alignment: .top).background(forest.opacity(0.06), in: RoundedRectangle(cornerRadius: 18))
    }
    func card<Content: View>(_ title: String, icon: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 16) { Label(title, systemImage: icon).font(.headline); content() }.padding(22).frame(maxWidth: .infinity, alignment: .leading).background(.background, in: RoundedRectangle(cornerRadius: 18)).overlay(RoundedRectangle(cornerRadius: 18).stroke(.quaternary, lineWidth: 1))
    }
}
