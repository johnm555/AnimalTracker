import AppKit
import Foundation

/// One JSON-lines transaction. Credentials travel through a pipe, never argv.
final class Bridge {
    private var process: Process?
    private var input: Pipe?
    func cancel() { input?.fileHandleForWriting.closeFile(); if process?.isRunning == true { process?.terminate() } }
    func code(_ value: String) { write(["code": value]) }
    private func write(_ value: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: value) else { return }
        try? input?.fileHandleForWriting.write(contentsOf: data + Data([10]))
    }
    func run(python: URL, script: URL, environment: [String: String], request: [String: Any]?, event: @escaping ([String: Any]) -> Void, done: @escaping (Int32) -> Void) {
        let p = Process(), out = Pipe(), stdin = Pipe()
        p.executableURL = python; p.arguments = ["-u", script.path]
        p.environment = environment; p.standardInput = stdin; p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        process = p; input = stdin
        do { try p.run() } catch {
            event(["event": "error", "message": "Could not launch Python. Choose a working interpreter on the Engine page."]); done(-1); return
        }
        if let request { write(request) }
        DispatchQueue.global(qos: .userInitiated).async {
            var buffer = Data()
            while true {
                let chunk = out.fileHandleForReading.availableData
                if chunk.isEmpty { break }
                buffer.append(chunk)
                while let newline = buffer.firstIndex(of: 10) {
                    let line = buffer[..<newline]; buffer.removeSubrange(...newline)
                    if let value = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
                        DispatchQueue.main.async { event(value) }
                    }
                }
            }
            p.waitUntilExit()
            DispatchQueue.main.async { done(p.terminationStatus) }
        }
    }
}

final class AppModel: ObservableObject {
    @Published var page = Page.welcome
    @Published var busy = false
    @Published var message = ""
    @Published var error = false
    @Published var otp = false
    @Published var state: [String: Any] = [:]
    @Published var checks: [[String: Any]] = []
    @Published var engineReady = false
    @Published var ownedRunning = false
    @Published var pythonPath = "/opt/homebrew/bin/python3.12"
    @Published var name = ""
    @Published var animalDescription = ""
    @Published var cameras: [CameraChoice] = []
    @Published var links: [ZoneLink] = []
    @Published var recipient = ""
    @Published var messages = false
    @Published var allowLAN = false
    @Published var replace = false
    @Published var previewed = false
    let bridge = Bridge()
    private var server: Process?
    private var timer: Timer?
    var root: URL { Bundle.main.resourceURL!.appendingPathComponent("Engine") }
    var data: URL { URL(fileURLWithPath: ProcessInfo.processInfo.environment["ANIMAL_TRACKER_DATA"] ?? NSHomeDirectory() + "/Library/Application Support/AnimalTracker") }
    var python: URL { data.appendingPathComponent("desktop-runtime/bin/python") }
    var environment: [String: String] {
        var e = ProcessInfo.processInfo.environment
        e["ANIMAL_TRACKER_DATA"] = data.path; e["PYTHONUNBUFFERED"] = "1"
        e["PYTHONDONTWRITEBYTECODE"] = "1" // Keep the signed app bundle immutable.
        return e
    }
    var configured: Bool { state["configured"] as? Bool ?? false }
    var externalRunning: Bool { state["service_running"] as? Bool ?? false }
    var zones: [String] { Array(Set(cameras.filter(\.selected).map(\.zone).filter { !$0.isEmpty })).sorted() }
    var answers: [String: Any] { Setup.answers(name: name, description: animalDescription, cameras: cameras, links: links, recipient: recipient, messages: messages) }
    var validation: String? { Setup.validate(name: name, cameras: cameras, links: links, messages: messages, recipient: recipient) }
    init() {
        engineReady = FileManager.default.fileExists(atPath: data.appendingPathComponent("desktop-runtime/.ready").path)
        for p in ["/opt/homebrew/bin/python3.12", "/usr/local/bin/python3.12", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"] where FileManager.default.isExecutableFile(atPath: p) { pythonPath = p; break }
        if engineReady { refresh() }
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            guard let self, self.engineReady, !self.busy else { return }; self.refresh(silent: true)
        }
    }
    func run(_ request: [String: Any], completion: @escaping ([String: Any]) -> Void = { _ in }) {
        guard !busy else { return }
        busy = true; message = "Working…"; error = false
        var gotResult = false
        bridge.run(python: python, script: root.appendingPathComponent("scripts/desktop_bridge.py"), environment: environment, request: request, event: { [weak self] event in
            guard let self else { return }
            switch event["event"] as? String {
            case "otp_required": self.otp = true; self.message = "Waiting for verification code…"
            case "result":
                gotResult = true
                let result = event["data"] as? [String: Any] ?? [:]
                self.message = result["message"] as? String ?? "Done."
                completion(result)
            case "error": self.error = true; self.message = event["message"] as? String ?? "The operation failed."
            default: break
            }
        }, done: { [weak self] _ in
            guard let self else { return }; self.busy = false; self.otp = false
            if !gotResult && !self.error { self.error = true; self.message = "Operation stopped. You can retry." }
        })
    }
    func prepare() {
        guard !busy else { return }; busy = true; error = false
        bridge.run(python: URL(fileURLWithPath: pythonPath), script: root.appendingPathComponent("scripts/prepare_runtime.py"), environment: environment, request: nil, event: { [weak self] event in
            guard let self else { return }
            if event["event"] as? String == "result" {
                self.engineReady = true
                try? Data().write(to: self.data.appendingPathComponent("desktop-runtime/.ready"))
                self.message = "Engine ready. Continue to your animal’s profile."
            } else { self.message = event["message"] as? String ?? "Preparing…"; self.error = event["event"] as? String == "error" }
        }, done: { [weak self] code in
            self?.busy = false
            if code != 0 { self?.error = true }
        })
    }
    func refresh(silent: Bool = false) {
        guard !busy else { return }
        run(["action": "status"]) { [weak self] value in
            self?.state = value
            if self?.name.isEmpty == true { self?.name = value["animal"] as? String ?? "" }
            if silent { self?.message = "" }
        }
    }
    func ring(email: String, password: String) {
        run(["action": "ring_login", "email": email, "password": password]) { [weak self] value in
            self?.cameras = (value["cameras"] as? [[String: Any]] ?? []).compactMap { c in
                guard let name = c["name"] as? String, let id = c["device_id"] else { return nil }
                return CameraChoice(id: String(describing: id), name: name)
            }
            self?.page = .cameras
        }
    }
    func configure(preview: Bool) {
        if let validation { error = true; message = validation; return }
        run(["action": "configure", "answers": answers, "preview": preview, "replace_existing": replace, "allow_lan": allowLAN]) { [weak self] _ in
            self?.previewed = preview
            if !preview { self?.state["configured"] = true; self?.page = .dashboard }
        }
    }
    func start() {
        guard !ownedRunning && !busy else { return }
        // Check the port immediately before launching; never terminate an external service.
        run(["action": "status"]) { [weak self] result in
            guard let self else { return }
            self.state = result
            if result["service_running"] as? Bool == true { self.message = "A tracker is already running. This app will not start another copy."; return }
            let p = Process(), input = Pipe()
            p.executableURL = self.python; p.arguments = ["-u", self.root.appendingPathComponent("scripts/desktop_bridge.py").path]
            p.environment = self.environment; p.standardInput = input
            p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
            p.terminationHandler = { [weak self] process in DispatchQueue.main.async {
                self?.ownedRunning = false
                self?.message = process.terminationStatus == 0 || process.terminationReason == .uncaughtSignal ? "Tracking stopped." : "Engine exited. Run Check setup to diagnose configuration and credentials."
            } }
            do {
                try p.run(); try input.fileHandleForWriting.write(contentsOf: Data("{\"action\":\"serve\"}\n".utf8)); try input.fileHandleForWriting.close()
                self.server = p; self.ownedRunning = true; self.message = "Engine starting. First use may download local vision models."
            } catch { self.error = true; self.message = "Could not start the tracking engine." }
        }
    }
    func stop() { if server?.isRunning == true { server?.terminate() }; ownedRunning = false }
    func shutdown() { bridge.cancel(); stop() }
    func importPhotos() {
        let panel = NSOpenPanel(); panel.allowsMultipleSelection = true; panel.canChooseDirectories = false
        panel.allowedContentTypes = [.jpeg, .png, .webP]
        if panel.runModal() == .OK { run(["action": "import_photos", "files": panel.urls.map(\.path)]) }
    }
    func open(_ url: String) { if let u = URL(string: url) { NSWorkspace.shared.open(u) } }
}
import DesktopCore
