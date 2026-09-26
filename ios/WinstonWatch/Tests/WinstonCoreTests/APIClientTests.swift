import Foundation
import Testing
@testable import WinstonCore

/// Intercepts URLSession so the client can be tested without a server.
final class StubURLProtocol: URLProtocol {
    nonisolated(unsafe) static var handler: (@Sendable (URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard let handler = Self.handler else { return }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }
    override func stopLoading() {}
}

private func makeClient() -> WinstonAPIClient {
    let config = URLSessionConfiguration.ephemeral
    config.protocolClasses = [StubURLProtocol.self]
    return WinstonAPIClient(baseURL: URL(string: "http://mac-mini.local:8420")!,
                            session: URLSession(configuration: config), bearerToken: "secret")
}

private func ok(_ request: URLRequest, _ body: String) -> (HTTPURLResponse, Data) {
    (HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!, Data(body.utf8))
}

// The stub handler is process-global, so these must not run in parallel.
@Suite("API client", .serialized)
struct APIClientTests {
    @Test func locationRequestAndDecode() async throws {
        StubURLProtocol.handler = { request in
            #expect(request.url?.path == "/winston/location")
            #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer secret")
            return ok(request, #"{"state":"seen","zone":"backyard","zone_label":"backyard","confidence":0.9,"last_seen_at":"2026-09-20T15:00:00+00:00","minutes_ago":0.2,"as_of":"2026-09-20T15:00:12+00:00"}"#)
        }
        let snapshot = try await makeClient().location()
        #expect(snapshot.state == .seen)
        #expect(snapshot.zone == "backyard")
    }

    @Test func historyPassesHoursQuery() async throws {
        StubURLProtocol.handler = { request in
            #expect(request.url?.path == "/winston/history")
            #expect(request.url?.query == "hours=6.0")
            return ok(request, #"{"since":"2026-09-20T09:00:00+00:00","hours":6,"count":0,"transitions":[]}"#)
        }
        let items = try await makeClient().history(hours: 6)
        #expect(items.isEmpty)
    }

    @Test func registerDevicePostsTokenWithBearer() async throws {
        StubURLProtocol.handler = { request in
            #expect(request.httpMethod == "POST")
            #expect(request.url?.path == "/winston/devices")
            #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer secret")
            #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
            // URLProtocol sees the body as a stream; read it back.
            var body = Data()
            if let stream = request.httpBodyStream {
                stream.open(); defer { stream.close() }
                var buf = [UInt8](repeating: 0, count: 1024)
                while stream.hasBytesAvailable { let n = stream.read(&buf, maxLength: buf.count); if n <= 0 { break }; body.append(buf, count: n) }
            } else if let b = request.httpBody { body = b }
            let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            #expect(json?["token"] as? String == "abcdef0123456789abcdef0123456789")
            #expect(json?["platform"] as? String == "watchos")
            #expect(json?["name"] as? String == "John's Watch")
            return (HTTPURLResponse(url: request.url!, statusCode: 201, httpVersion: nil, headerFields: nil)!,
                    Data(#"{"registered":true,"device_tokens":1}"#.utf8))
        }
        try await makeClient().registerDevice(token: "abcdef0123456789abcdef0123456789", name: "John's Watch")
    }

    @Test func mutePostsDurationAndBearer() async throws {
        StubURLProtocol.handler = { request in
            #expect(request.httpMethod == "POST")
            #expect(request.url?.path == "/winston/mute")
            #expect(request.url?.query == "minutes=60")
            #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer secret")
            return ok(request, #"{"muted":true,"muted_until":"2026-09-21T22:00:00+00:00","scope":"all_devices","as_of":"2026-09-21T21:00:00+00:00"}"#)
        }
        let status = try await makeClient().mute()
        #expect(status.muted)
        #expect(status.scope == "all_devices")
        #expect(status.mutedUntil?.timeIntervalSince(status.asOf) == 3600)
    }

    @Test func unmuteDecodesNullExpiry() async throws {
        StubURLProtocol.handler = { request in
            #expect(request.url?.query == "minutes=0")
            return ok(request, #"{"muted":false,"muted_until":null,"scope":"all_devices","as_of":"2026-09-21T21:00:00+00:00"}"#)
        }
        let status = try await makeClient().mute(minutes: 0)
        #expect(!status.muted)
        #expect(status.mutedUntil == nil)
    }

    @Test func failedMuteDoesNotReturnSuccess() async {
        StubURLProtocol.handler = { request in
            (HTTPURLResponse(url: request.url!, statusCode: 401, httpVersion: nil, headerFields: nil)!, Data())
        }
        await #expect(throws: WinstonAPIClient.APIError.badStatus(401)) {
            _ = try await makeClient().mute()
        }
    }

    @Test func badStatusThrows() async {
        StubURLProtocol.handler = { request in
            (HTTPURLResponse(url: request.url!, statusCode: 503, httpVersion: nil, headerFields: nil)!, Data())
        }
        await #expect(throws: WinstonAPIClient.APIError.badStatus(503)) {
            _ = try await makeClient().stats()
        }
    }
}
