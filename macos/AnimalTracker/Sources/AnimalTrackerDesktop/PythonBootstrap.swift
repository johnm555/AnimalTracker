import Foundation
import CryptoKit

/// A pinned, checksum-verified interpreter; never installs into system Python.
final class PythonBootstrap {
    enum Failure: LocalizedError {
        case download, checksum, extraction
        var errorDescription: String? {
            switch self {
            case .download: return "Python could not be downloaded. Check your internet connection and retry."
            case .checksum: return "The Python download did not match its expected checksum. Nothing was installed."
            case .extraction: return "Python could not be unpacked. Check available disk space and retry."
            }
        }
    }
    private var task: Task<Void, Never>?
    func cancel() { task?.cancel() }
    func prepare(in data: URL, completion: @escaping (Result<URL, Error>) -> Void) {
        #if arch(arm64)
        let architecture = "aarch64"
        let digest = "ad8d0c637c0a36b967b310e2c07254f4d2ca8cabaa7699e55ed6290aceb481a2"
        #else
        let architecture = "x86_64"
        let digest = "562c30864ece2cb1d3e0ad66a1acd498611a47e5a10ce81b99158bef1ccbd355"
        #endif
        let filename = "cpython-3.12.15%2B20261003-\(architecture)-apple-darwin-install_only_stripped.tar.gz"
        let url = URL(string: "https://github.com/astral-sh/python-build-standalone/releases/download/20261003/\(filename)")!
        let destination = data.appendingPathComponent("python-3.12.15-20261003-\(architecture)")
        task = Task.detached {
            do {
                let executable = destination.appendingPathComponent("python/bin/python3")
                let marker = destination.appendingPathComponent(".verified")
                if (try? String(contentsOf: marker, encoding: .utf8)) == digest,
                   FileManager.default.isExecutableFile(atPath: executable.path) {
                    await MainActor.run { completion(.success(executable)) }; return
                }
                var request = URLRequest(url: url); request.timeoutInterval = 120
                let (archive, response) = try await URLSession.shared.download(for: request)
                defer { try? FileManager.default.removeItem(at: archive) }
                guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw Failure.download }
                try Task.checkCancellation()
                let bytes = try Data(contentsOf: archive, options: .mappedIfSafe)
                let actual = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
                guard actual == digest else { throw Failure.checksum }
                try FileManager.default.createDirectory(at: data, withIntermediateDirectories: true)
                let staging = data.appendingPathComponent(".python-install-" + UUID().uuidString)
                try FileManager.default.createDirectory(at: staging, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
                defer { try? FileManager.default.removeItem(at: staging) }
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/usr/bin/tar")
                process.arguments = ["-xzf", archive.path, "-C", staging.path]
                process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
                try process.run(); process.waitUntilExit()
                try Task.checkCancellation()
                guard process.terminationStatus == 0,
                      FileManager.default.isExecutableFile(atPath: staging.appendingPathComponent("python/bin/python3").path) else { throw Failure.extraction }
                // Never remove or overwrite an existing interpreter while an engine may use it.
                guard !FileManager.default.fileExists(atPath: destination.path) else { throw Failure.extraction }
                try digest.write(to: staging.appendingPathComponent(".verified"), atomically: true, encoding: .utf8)
                try FileManager.default.moveItem(at: staging, to: destination)
                await MainActor.run { completion(.success(executable)) }
            } catch {
                await MainActor.run { completion(.failure(error)) }
            }
        }
    }
}
