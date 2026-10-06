import Combine
import Foundation
import SwiftUI
import UIKit

enum CaptureSource: String, CaseIterable, Identifiable {
    case window
    case screen

    var id: String { rawValue }
}

enum ConnectionMode: String, CaseIterable, Identifiable {
    case auto
    case direct
    case remote

    var id: String { rawValue }

    var title: String {
        switch self {
        case .auto: return "Auto"
        case .direct: return "Direct"
        case .remote: return "Remote"
        }
    }
}

enum RemoteBackendKind: String {
    case virtualOSHTTP
    case aiRemoteWS
}

struct DiscoveredEndpoint: Identifiable, Hashable {
    let id: String
    let name: String
    let host: String
    let port: Int
    let isLocal: Bool

    var label: String {
        "\(name)  \(host):\(port)"
    }
}

final class BonjourDiscovery: NSObject, ObservableObject, NetServiceBrowserDelegate, NetServiceDelegate {
    @Published private(set) var endpoints: [DiscoveredEndpoint] = []
    @Published private(set) var status: String = "Idle"

    private let browser = NetServiceBrowser()
    private var servicesByName: [String: NetService] = [:]
    private var endpointsByName: [String: DiscoveredEndpoint] = [:]
    private let localIPs: Set<String>

    override init() {
        self.localIPs = BonjourDiscovery.currentLocalIPs()
        super.init()
    }

    func start() {
        stop()
        status = "Searching LAN..."
        browser.delegate = self
        browser.searchForServices(ofType: "_virtualosremote._tcp.", inDomain: "local.")
    }

    func refresh() {
        start()
    }

    func stop() {
        browser.stop()
        for service in servicesByName.values {
            service.stop()
        }
        servicesByName.removeAll()
        endpointsByName.removeAll()
        publish()
    }

    func netServiceBrowserWillSearch(_ browser: NetServiceBrowser) {
        status = "Searching LAN..."
    }

    func netServiceBrowser(_ browser: NetServiceBrowser, didNotSearch errorDict: [String: NSNumber]) {
        status = "Discovery failed"
    }

    func netServiceBrowserDidStopSearch(_ browser: NetServiceBrowser) {
        if endpoints.isEmpty {
            status = "No servers found"
        }
    }

    func netServiceBrowser(_ browser: NetServiceBrowser, didFind service: NetService, moreComing: Bool) {
        servicesByName[service.name] = service
        service.delegate = self
        service.resolve(withTimeout: 3)
        if !moreComing {
            publish()
        }
    }

    func netServiceBrowser(_ browser: NetServiceBrowser, didRemove service: NetService, moreComing: Bool) {
        servicesByName[service.name] = nil
        endpointsByName[service.name] = nil
        if !moreComing {
            publish()
        }
    }

    func netServiceDidResolveAddress(_ sender: NetService) {
        let allHosts = BonjourDiscovery.ipAddresses(from: sender.addresses ?? [])
        guard let preferred = BonjourDiscovery.preferredHost(from: allHosts) else {
            return
        }
        let endpoint = DiscoveredEndpoint(
            id: "\(sender.name)|\(preferred)|\(sender.port)",
            name: sender.name,
            host: preferred,
            port: sender.port,
            isLocal: localIPs.contains(preferred) || preferred == "127.0.0.1" || preferred == "::1"
        )
        endpointsByName[sender.name] = endpoint
        publish()
    }

    func netService(_ sender: NetService, didNotResolve errorDict: [String: NSNumber]) {
        endpointsByName[sender.name] = nil
        publish()
    }

    private func publish() {
        endpoints = endpointsByName.values.sorted {
            if $0.isLocal != $1.isLocal {
                return !$0.isLocal
            }
            if $0.name != $1.name {
                return $0.name < $1.name
            }
            return $0.host < $1.host
        }
        if endpoints.isEmpty {
            status = "No servers found"
        } else {
            status = "Found \(endpoints.count) server\(endpoints.count == 1 ? "" : "s")"
        }
    }

    private static func preferredHost(from hosts: [String]) -> String? {
        if let ipv4 = hosts.first(where: { $0.contains(".") && !$0.hasPrefix("169.254.") }) {
            return ipv4
        }
        if let nonLinkLocalV6 = hosts.first(where: { $0.contains(":") && !$0.lowercased().hasPrefix("fe80:") }) {
            return nonLinkLocalV6
        }
        return hosts.first
    }

    private static func ipAddresses(from addressData: [Data]) -> [String] {
        addressData.compactMap { ipAddress(from: $0) }
    }

    private static func ipAddress(from data: Data) -> String? {
        data.withUnsafeBytes { rawBuffer in
            guard let base = rawBuffer.baseAddress else { return nil }
            let sockaddrPointer = base.assumingMemoryBound(to: sockaddr.self)
            let family = Int32(sockaddrPointer.pointee.sa_family)
            guard family == AF_INET || family == AF_INET6 else { return nil }

            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            let result = getnameinfo(
                sockaddrPointer,
                socklen_t(data.count),
                &host,
                socklen_t(host.count),
                nil,
                0,
                NI_NUMERICHOST
            )
            guard result == 0 else { return nil }
            return String(cString: host)
        }
    }

    private static func currentLocalIPs() -> Set<String> {
        var result: Set<String> = ["127.0.0.1", "::1"]
        var ifaddrPointer: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ifaddrPointer) == 0, let firstAddr = ifaddrPointer else {
            return result
        }
        defer { freeifaddrs(ifaddrPointer) }

        var cursor: UnsafeMutablePointer<ifaddrs>? = firstAddr
        while let ifaddr = cursor?.pointee {
            guard let addrPointer = ifaddr.ifa_addr else {
                cursor = ifaddr.ifa_next
                continue
            }
            let family = Int32(addrPointer.pointee.sa_family)
            if family == AF_INET || family == AF_INET6 {
                var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
                let length = socklen_t(
                    family == AF_INET
                        ? MemoryLayout<sockaddr_in>.size
                        : MemoryLayout<sockaddr_in6>.size
                )
                if getnameinfo(addrPointer, length, &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 {
                    result.insert(String(cString: host))
                }
            }
            cursor = ifaddr.ifa_next
        }
        return result
    }
}

@MainActor
final class StreamViewModel: ObservableObject {
    @Published var connectionMode: ConnectionMode
    @Published var host: String
    @Published var port: String
    @Published var remoteURL: String
    @Published var authToken: String
    @Published var cfAccessClientID: String
    @Published var cfAccessClientSecret: String
    @Published var targetApp: String
    @Published var captureSource: CaptureSource
    @Published var image: UIImage?
    @Published var status: String = "Idle"
    @Published var detail: String = ""
    @Published var isStreaming = false
    @Published var discoveredEndpoints: [DiscoveredEndpoint] = []
    @Published var discoveryStatus: String = "Idle"
    @Published var lastCaptureMode: String = ""
    @Published var lastHTTPStatus: Int = 0
    @Published var lastLatencyMs: Int = 0
    @Published var recentLogs: [String] = []

    private let discovery = BonjourDiscovery()
    private var cancellables: Set<AnyCancellable> = []
    private var timerTask: Task<Void, Never>?
    private var remoteBackend: RemoteBackendKind = .virtualOSHTTP
    private var wsTask: URLSessionWebSocketTask?
    private var wsReceiveTask: Task<Void, Never>?
    private var wsAuthed = false
    private let wsMaximumMessageSize = 64 * 1024 * 1024

    init() {
        self.connectionMode =
            ConnectionMode(rawValue: UserDefaults.standard.string(forKey: "stream.connectionMode") ?? "remote")
            ?? .remote
        self.host = UserDefaults.standard.string(forKey: "stream.host") ?? "127.0.0.1"
        self.port = UserDefaults.standard.string(forKey: "stream.port") ?? "8899"
        self.remoteURL = UserDefaults.standard.string(forKey: "stream.remoteURL") ?? ""
        self.authToken = UserDefaults.standard.string(forKey: "stream.authToken") ?? ""
        self.cfAccessClientID = UserDefaults.standard.string(forKey: "stream.cfAccessClientID") ?? ""
        self.cfAccessClientSecret = UserDefaults.standard.string(forKey: "stream.cfAccessClientSecret") ?? ""
        let savedTargetApp = UserDefaults.standard.string(forKey: "stream.targetApp")?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if savedTargetApp.isEmpty || savedTargetApp.caseInsensitiveCompare("codex") == .orderedSame {
            self.targetApp = "virtualOS"
        } else {
            self.targetApp = savedTargetApp
        }
        let savedSource = UserDefaults.standard.string(forKey: "stream.captureSource") ?? CaptureSource.window.rawValue
        self.captureSource = CaptureSource(rawValue: savedSource) ?? .window

        let trimmedRemoteURL = remoteURL.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedRemoteURL.isEmpty, connectionMode != .remote {
            connectionMode = .remote
        } else if trimmedRemoteURL.isEmpty, connectionMode == .remote {
            // Avoid a dead state where Remote mode is selected but no tunnel URL exists.
            connectionMode = .auto
        }

        discovery.$endpoints
            .receive(on: DispatchQueue.main)
            .sink { [weak self] endpoints in
                self?.discoveredEndpoints = endpoints
                self?.autoApplyBestEndpointIfNeeded()
            }
            .store(in: &cancellables)

        discovery.$status
            .receive(on: DispatchQueue.main)
            .sink { [weak self] newStatus in
                self?.discoveryStatus = newStatus
                self?.addLog("discovery status: \(newStatus)")
            }
            .store(in: &cancellables)

        if connectionMode == .remote {
            discoveryStatus = "Remote mode (LAN discovery disabled)"
        } else {
            discovery.start()
        }
    }

    func start() {
        guard !isStreaming else { return }

        if connectionMode == .remote, remoteURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            status = "Missing URL"
            detail = "Enter Remote URL"
            addLog("start blocked: missing remote url")
            return
        }

        autoApplyBestEndpointIfNeeded()
        isStreaming = true
        status = "Connecting..."
        detail = ""
        addLog("stream start mode=\(connectionMode.rawValue)")
        persist()

        timerTask = Task { [weak self] in
            guard let self else { return }

            if self.connectionMode == .remote {
                self.remoteBackend = await self.detectRemoteBackend()
                if self.remoteBackend == .aiRemoteWS {
                    await self.runAIRemoteStreamLoop()
                    return
                }
            } else {
                self.remoteBackend = .virtualOSHTTP
            }

            while !Task.isCancelled {
                await self.fetchFrame()
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }

    func stop() {
        isStreaming = false
        timerTask?.cancel()
        timerTask = nil
        teardownWebSocket()
        status = "Stopped"
        detail = ""
        addLog("stream stopped")
    }

    func pullSingleFrame() async {
        if connectionMode == .remote, remoteBackend == .aiRemoteWS {
            if wsTask == nil {
                let connected = await connectAIRemoteWebSocket()
                guard connected else { return }
            }
            await sendAIMessage(["type": "screenshot"])
            return
        }
        await fetchFrame()
    }

    func saveSettings() {
        persist()
    }

    func refreshDiscovery() {
        guard connectionMode != .remote else {
            discoveryStatus = "Remote mode (LAN discovery disabled)"
            addLog("discovery skipped in remote mode")
            return
        }
        discovery.refresh()
        addLog("discovery refresh requested")
    }

    func onConnectionModeChanged() {
        teardownWebSocket()
        remoteBackend = .virtualOSHTTP
        if connectionMode == .remote {
            discovery.stop()
            discoveryStatus = "Remote mode (LAN discovery disabled)"
        } else {
            discovery.refresh()
        }
        persist()
    }

    func importTunnelFromClipboard() {
        guard let text = UIPasteboard.general.string, !text.isEmpty else {
            detail = "Clipboard is empty"
            addLog("paste tunnel: clipboard empty")
            return
        }

        var foundURL: String?
        if let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue) {
            let nsRange = NSRange(text.startIndex..<text.endIndex, in: text)
            let matches = detector.matches(in: text, options: [], range: nsRange)
            if
                let tryCloudflare = matches.compactMap({ $0.url?.absoluteString })
                    .first(where: { $0.contains("trycloudflare.com") || $0.starts(with: "https://") })
            {
                foundURL = tryCloudflare
            }
        }

        var foundToken: String?
        if let regex = try? NSRegularExpression(pattern: "\\b[a-fA-F0-9]{64}\\b") {
            let nsRange = NSRange(text.startIndex..<text.endIndex, in: text)
            if let match = regex.firstMatch(in: text, options: [], range: nsRange),
               let range = Range(match.range, in: text)
            {
                foundToken = String(text[range])
            }
        }

        guard let url = foundURL else {
            detail = "No tunnel URL found in clipboard"
            addLog("paste tunnel: no url")
            return
        }

        connectionMode = .remote
        remoteURL = url
        if let token = foundToken {
            authToken = token
        }
        persist()

        if foundToken != nil {
            detail = "Applied tunnel URL + secret"
            addLog("paste tunnel: url+token applied")
        } else {
            detail = "Applied tunnel URL (no token found)"
            addLog("paste tunnel: url applied")
        }
    }

    func useEndpoint(_ endpoint: DiscoveredEndpoint) {
        connectionMode = .direct
        host = endpoint.host
        port = String(endpoint.port)
        detail = "Using \(endpoint.label)"
        addLog("selected server \(endpoint.label)")
        persist()
    }

    func testConnection() async {
        if connectionMode == .remote {
            if let statusURL = makeURL(path: "/api/status") {
                var request = URLRequest(url: statusURL)
                request.cachePolicy = .reloadIgnoringLocalCacheData
                request.timeoutInterval = 3
                applyAuthHeaders(to: &request)
                do {
                    let (data, response) = try await URLSession.shared.data(for: request)
                    if let http = response as? HTTPURLResponse {
                        lastHTTPStatus = http.statusCode
                        if http.statusCode == 200,
                           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                           obj["provider"] != nil
                        {
                            status = "Health OK"
                            detail = "AI Remote reachable"
                            remoteBackend = .aiRemoteWS
                            addLog("health ok ai-remote \(statusURL.absoluteString)")
                            return
                        }
                    }
                } catch {
                    addLog("api/status probe failed \(error.localizedDescription)")
                }
            }
        }

        guard let url = makeURL(path: "/health") else {
            status = "Bad URL"
            detail = "Invalid connection settings"
            return
        }

        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 3
        applyAuthHeaders(to: &request)

        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                status = "Health failed"
                detail = "No HTTP response"
                addLog("health failed no-http")
                return
            }
            lastHTTPStatus = http.statusCode
            if http.statusCode == 200 {
                status = "Health OK"
                detail = "Server reachable"
                addLog("health ok \(url.absoluteString)")
            } else {
                status = "Health failed"
                detail = "HTTP \(http.statusCode)"
                addLog("health failed status=\(http.statusCode) url=\(url.absoluteString)")
            }
        } catch {
            status = "Health failed"
            detail = error.localizedDescription
            addLog("health error \(error.localizedDescription)")
        }
    }

    func clickNormalized(nx: CGFloat, ny: CGFloat) async {
        guard let url = makeURL(path: "/control/click") else {
            status = "Bad URL"
            detail = "Invalid connection settings"
            return
        }

        let payload: [String: Any] = [
            "nx": Double(nx),
            "ny": Double(ny),
            "source": captureSource == .screen ? "screen" : "window",
            "app": targetApp,
        ]
        guard let body = try? JSONSerialization.data(withJSONObject: payload) else {
            status = "Control error"
            detail = "Could not encode click payload"
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        request.timeoutInterval = 3
        applyAuthHeaders(to: &request)

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                status = "Control error"
                detail = "No HTTP response"
                addLog("click failed no-http")
                return
            }
            lastHTTPStatus = http.statusCode
            guard (200..<300).contains(http.statusCode) else {
                status = "Control failed"
                detail = "HTTP \(http.statusCode)"
                addLog("click failed status=\(http.statusCode)")
                return
            }
            if
                let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                let ok = obj["ok"] as? Bool,
                ok == true
            {
                status = "Live"
                detail = "Tapped (\(Int(nx * 100))%, \(Int(ny * 100))%)"
                addLog("click ok nx=\(String(format: "%.2f", nx)) ny=\(String(format: "%.2f", ny))")
            } else {
                status = "Control failed"
                detail = "Bad control response"
                addLog("click bad-response")
            }
        } catch {
            status = "Control error"
            detail = error.localizedDescription
            addLog("click error \(error.localizedDescription)")
        }
    }

    func focusTargetApp() async {
        let app = targetApp.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !app.isEmpty else {
            detail = "Target app is empty"
            return
        }
        guard let url = makeURL(path: "/control/focus") else { return }
        let payload: [String: Any] = ["app": app]
        guard let body = try? JSONSerialization.data(withJSONObject: payload) else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        request.timeoutInterval = 3
        applyAuthHeaders(to: &request)
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            if let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) {
                detail = "Focused \(app)"
                addLog("focus ok app=\(app)")
            } else if let http = response as? HTTPURLResponse {
                addLog("focus failed status=\(http.statusCode) app=\(app)")
            }
        } catch {
            detail = "Focus failed: \(error.localizedDescription)"
            addLog("focus error \(error.localizedDescription)")
        }
    }

    func sendShortcut(key: String, modifiers: [String] = []) async {
        guard let url = makeURL(path: "/control/shortcut") else {
            status = "Bad URL"
            detail = "Invalid connection settings"
            return
        }

        let payload: [String: Any] = [
            "key": key,
            "modifiers": modifiers,
        ]
        guard let body = try? JSONSerialization.data(withJSONObject: payload) else {
            status = "Control error"
            detail = "Could not encode shortcut payload"
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        request.timeoutInterval = 3
        applyAuthHeaders(to: &request)

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                status = "Control error"
                detail = "No HTTP response"
                addLog("shortcut failed no-http key=\(key)")
                return
            }
            lastHTTPStatus = http.statusCode
            guard (200..<300).contains(http.statusCode) else {
                status = "Shortcut failed"
                detail = "HTTP \(http.statusCode)"
                addLog("shortcut failed status=\(http.statusCode) key=\(key)")
                return
            }
            if
                let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                let ok = obj["ok"] as? Bool,
                ok == true
            {
                status = "Live"
                if modifiers.isEmpty {
                    detail = "Shortcut: \(key)"
                } else {
                    detail = "Shortcut: \(modifiers.joined(separator: "+"))+\(key)"
                }
                addLog("shortcut ok \(detail)")
            } else {
                status = "Shortcut failed"
                detail = "Bad shortcut response"
                addLog("shortcut bad-response key=\(key)")
            }
        } catch {
            status = "Control error"
            detail = error.localizedDescription
            addLog("shortcut error \(error.localizedDescription)")
        }
    }

    func quickActions() -> [(title: String, key: String, modifiers: [String])] {
        let app = targetApp.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if app.contains("codex") || app.contains("chatgpt") {
            return [
                ("Enter", "enter", []),
                ("Esc", "escape", []),
                ("Cmd+C", "c", ["command"]),
                ("Cmd+V", "v", ["command"]),
                ("Cmd+Tab", "tab", ["command"]),
                ("Cmd+N", "n", ["command"]),
                ("Cmd+F", "f", ["command"]),
            ]
        }
        return [
            ("Enter", "enter", []),
            ("Esc", "escape", []),
            ("Cmd+C", "c", ["command"]),
            ("Cmd+V", "v", ["command"]),
            ("Cmd+Tab", "tab", ["command"]),
        ]
    }

    private func autoApplyBestEndpointIfNeeded() {
        guard connectionMode == .auto else { return }
        guard let best = discoveredEndpoints.first(where: { !$0.isLocal }) ?? discoveredEndpoints.first else { return }
        let desiredHost = best.host
        let desiredPort = String(best.port)
        if host != desiredHost || port != desiredPort {
            host = desiredHost
            port = desiredPort
            detail = "Auto connected: \(best.label)"
            addLog("auto-selected server \(best.label)")
            persist()
        }
    }

    private func fetchFrame() async {
        autoApplyBestEndpointIfNeeded()

        var queryItems = [
            URLQueryItem(name: "ts", value: String(Date().timeIntervalSince1970)),
            URLQueryItem(name: "source", value: captureSource == .screen ? "screen" : "window"),
            URLQueryItem(name: "max", value: "1400"),
        ]
        let trimmedApp = targetApp.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedApp.isEmpty {
            queryItems.append(URLQueryItem(name: "app", value: trimmedApp))
        }

        guard let url = makeURL(path: "/frame.jpg", queryItems: queryItems) else {
            status = "Bad URL"
            detail = "Invalid connection settings"
            addLog("frame bad-url")
            return
        }

        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 3
        applyAuthHeaders(to: &request)

        let started = CFAbsoluteTimeGetCurrent()
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            lastLatencyMs = Int((CFAbsoluteTimeGetCurrent() - started) * 1000)
            guard let http = response as? HTTPURLResponse else {
                status = "HTTP error"
                detail = "No HTTP response"
                addLog("frame failed no-http")
                return
            }
            lastHTTPStatus = http.statusCode
            lastCaptureMode = http.value(forHTTPHeaderField: "X-Capture-Mode") ?? ""
            guard http.statusCode == 200 else {
                status = "HTTP error"
                detail = "HTTP \(http.statusCode)"
                addLog("frame failed status=\(http.statusCode)")
                return
            }
            guard let img = UIImage(data: data) else {
                status = "Decode failed"
                detail = "Could not decode image"
                addLog("frame decode failed bytes=\(data.count)")
                return
            }
            image = img
            status = "Live"
            detail = ""
            addLog("frame ok status=\(http.statusCode) mode=\(lastCaptureMode) latency=\(lastLatencyMs)ms")
        } catch {
            status = "Network error"
            detail = error.localizedDescription
            lastLatencyMs = Int((CFAbsoluteTimeGetCurrent() - started) * 1000)
            addLog("frame network error \(error.localizedDescription)")
        }
    }

    private func detectRemoteBackend() async -> RemoteBackendKind {
        guard connectionMode == .remote else { return .virtualOSHTTP }
        let remoteHostHint = remoteURL.lowercased()
        if remoteHostHint.contains("trycloudflare.com") {
            addLog("detected backend ai-remote-ws (host hint)")
            return .aiRemoteWS
        }
        guard let statusURL = makeURL(path: "/api/status") else { return .virtualOSHTTP }

        var request = URLRequest(url: statusURL)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 3
        applyAuthHeaders(to: &request)

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                return .virtualOSHTTP
            }
            if
                let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                obj["provider"] != nil,
                obj["tunnel"] != nil
            {
                addLog("detected backend ai-remote-ws")
                return .aiRemoteWS
            }
        } catch {
            addLog("backend detect failed \(error.localizedDescription)")
        }
        return .virtualOSHTTP
    }

    private func makeWebSocketURLForRemote() -> URL? {
        var raw = remoteURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty else { return nil }
        if !raw.contains("://") {
            raw = "https://\(raw)"
        }
        guard var components = URLComponents(string: raw) else { return nil }
        components.scheme = (components.scheme == "https") ? "wss" : "ws"
        components.path = "/ws"
        components.query = nil
        components.fragment = nil
        return components.url
    }

    private func runAIRemoteStreamLoop() async {
        let connected = await connectAIRemoteWebSocket()
        guard connected else {
            isStreaming = false
            return
        }

        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(1))
        }
    }

    private func connectAIRemoteWebSocket() async -> Bool {
        teardownWebSocket()

        guard let wsURL = makeWebSocketURLForRemote() else {
            status = "Bad URL"
            detail = "Invalid Remote URL"
            addLog("ws connect failed bad-url")
            return false
        }

        let token = authToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else {
            status = "Missing key"
            detail = "Enter secret key in Auth token"
            addLog("ws connect failed missing key")
            return false
        }

        let task = URLSession.shared.webSocketTask(with: wsURL)
        task.maximumMessageSize = wsMaximumMessageSize
        wsTask = task
        wsAuthed = false
        task.resume()
        status = "Connecting..."
        detail = "AI Remote WebSocket"
        addLog("ws opening \(wsURL.absoluteString)")

        wsReceiveTask = Task { [weak self] in
            await self?.receiveAIRemoteMessages()
        }

        await sendAIMessage([
            "type": "auth",
            "key": token,
        ])

        for _ in 0..<30 {
            if wsAuthed { return true }
            if Task.isCancelled || !isStreaming { return false }
            try? await Task.sleep(for: .milliseconds(100))
        }

        status = "Auth failed"
        detail = "AI Remote auth timeout"
        addLog("ws auth timeout")
        return false
    }

    private func receiveAIRemoteMessages() async {
        guard let wsTask else { return }

        while !Task.isCancelled {
            do {
                let message = try await wsTask.receive()
                switch message {
        case .string(let text):
            await handleAIMessageText(text)
        case .data(let data):
            if let text = String(data: data, encoding: .utf8) {
                await handleAIMessageText(text)
            } else if let img = UIImage(data: data) {
                image = img
                status = "Live"
                detail = ""
                lastCaptureMode = "ai-remote-ws-binary"
                addLog("ws binary screenshot bytes=\(data.count)")
            }
        @unknown default:
            break
                }
            } catch {
                if isStreaming {
                    status = "Network error"
                    detail = error.localizedDescription
                    addLog("ws receive error \(error.localizedDescription)")
                }
                break
            }
        }
    }

    private func handleAIMessageText(_ text: String) async {
        guard let data = text.data(using: .utf8) else { return }
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        guard let type = obj["type"] as? String else { return }

        switch type {
        case "connected":
            wsAuthed = true
            status = "Live"
            detail = "AI Remote connected"
            lastCaptureMode = "ai-remote-ws"
            addLog("ws connected+authed")
            await sendAIMessage(["type": "live_start"])

        case "screenshot":
            if let b64 = obj["data"] as? String,
               let pngData = Data(base64Encoded: b64),
               let img = UIImage(data: pngData)
            {
                image = img
                status = "Live"
                detail = ""
                lastCaptureMode = "ai-remote-ws"
            }

        case "live_started":
            addLog("ws live stream started")

        case "live_stopped":
            addLog("ws live stream stopped")

        case "error":
            let message = (obj["message"] as? String) ?? "Unknown error"
            if !wsAuthed {
                status = "Auth failed"
            } else {
                status = "Remote error"
            }
            detail = message
            addLog("ws error \(message)")

        default:
            break
        }
    }

    private func sendAIMessage(_ payload: [String: Any]) async {
        guard let wsTask else { return }
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: data, encoding: .utf8)
        else { return }
        do {
            try await wsTask.send(.string(text))
        } catch {
            addLog("ws send error \(error.localizedDescription)")
        }
    }

    private func teardownWebSocket() {
        wsReceiveTask?.cancel()
        wsReceiveTask = nil
        wsAuthed = false
        if let wsTask {
            wsTask.cancel(with: .goingAway, reason: nil)
        }
        wsTask = nil
    }

    private func makeURL(path: String, queryItems: [URLQueryItem] = []) -> URL? {
        switch connectionMode {
        case .auto, .direct:
            let cleanHost = host.trimmingCharacters(in: .whitespacesAndNewlines)
            let cleanPort = port.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !cleanHost.isEmpty, let portInt = Int(cleanPort) else { return nil }
            var components = URLComponents()
            components.scheme = "http"
            components.host = cleanHost
            components.port = portInt
            components.path = path
            components.queryItems = queryItems.isEmpty ? nil : queryItems
            return components.url

        case .remote:
            var raw = remoteURL.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !raw.isEmpty else { return nil }
            if !raw.contains("://") {
                raw = "https://\(raw)"
            }
            guard var components = URLComponents(string: raw) else { return nil }
            let suffix = path.hasPrefix("/") ? path : "/\(path)"
            if components.path.isEmpty || components.path == "/" {
                components.path = suffix
            } else if components.path.hasSuffix("/") {
                components.path = String(components.path.dropLast()) + suffix
            } else {
                components.path = components.path + suffix
            }
            components.queryItems = queryItems.isEmpty ? nil : queryItems
            return components.url
        }
    }

    private func applyAuthHeaders(to request: inout URLRequest) {
        let trimmedToken = authToken.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedToken.isEmpty {
            request.setValue("Bearer \(trimmedToken)", forHTTPHeaderField: "Authorization")
        }

        let cfID = cfAccessClientID.trimmingCharacters(in: .whitespacesAndNewlines)
        let cfSecret = cfAccessClientSecret.trimmingCharacters(in: .whitespacesAndNewlines)
        if !cfID.isEmpty && !cfSecret.isEmpty {
            request.setValue(cfID, forHTTPHeaderField: "CF-Access-Client-Id")
            request.setValue(cfSecret, forHTTPHeaderField: "CF-Access-Client-Secret")
        }
    }

    private func addLog(_ text: String) {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        let line = "\(formatter.string(from: Date()))  \(text)"
        recentLogs.insert(line, at: 0)
        if recentLogs.count > 50 {
            recentLogs = Array(recentLogs.prefix(50))
        }
    }

    private func persist() {
        UserDefaults.standard.set(connectionMode.rawValue, forKey: "stream.connectionMode")
        UserDefaults.standard.set(host, forKey: "stream.host")
        UserDefaults.standard.set(port, forKey: "stream.port")
        UserDefaults.standard.set(remoteURL, forKey: "stream.remoteURL")
        UserDefaults.standard.set(authToken, forKey: "stream.authToken")
        UserDefaults.standard.set(cfAccessClientID, forKey: "stream.cfAccessClientID")
        UserDefaults.standard.set(cfAccessClientSecret, forKey: "stream.cfAccessClientSecret")
        UserDefaults.standard.set(targetApp, forKey: "stream.targetApp")
        UserDefaults.standard.set(captureSource.rawValue, forKey: "stream.captureSource")
    }
}

struct ContentView: View {
    @StateObject private var vm = StreamViewModel()
    @State private var didAutoStart = false

    var body: some View {
        VStack(spacing: 12) {
            HStack(spacing: 8) {
                Picker("Connection", selection: $vm.connectionMode) {
                    ForEach(ConnectionMode.allCases) { mode in
                        Text(mode.title).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .frame(width: 210)

                if vm.connectionMode == .remote {
                    TextField("Remote URL (https://...)", text: $vm.remoteURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .textFieldStyle(.roundedBorder)
                    Button("Paste Tunnel") {
                        vm.importTunnelFromClipboard()
                    }
                    .buttonStyle(.bordered)
                } else {
                    TextField("Host", text: $vm.host)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .textFieldStyle(.roundedBorder)
                    TextField("Port", text: $vm.port)
                        .keyboardType(.numberPad)
                        .frame(width: 85)
                        .textFieldStyle(.roundedBorder)
                }
            }

            HStack(spacing: 8) {
                SecureField("Secret key / Auth token", text: $vm.authToken)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .textFieldStyle(.roundedBorder)
            }

            HStack(spacing: 8) {
                TextField("CF Access Client ID", text: $vm.cfAccessClientID)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .textFieldStyle(.roundedBorder)
                SecureField("CF Access Client Secret", text: $vm.cfAccessClientSecret)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .textFieldStyle(.roundedBorder)
            }

            HStack(spacing: 8) {
                TextField("Target app (e.g. virtualOS)", text: $vm.targetApp)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .textFieldStyle(.roundedBorder)
                Button("Focus") {
                    Task { await vm.focusTargetApp() }
                }
                .buttonStyle(.bordered)
            }

            if vm.connectionMode != .remote {
                HStack(spacing: 8) {
                    Menu("Servers") {
                        if vm.discoveredEndpoints.isEmpty {
                            Text("No discovered servers")
                        } else {
                            ForEach(vm.discoveredEndpoints) { endpoint in
                                Button(endpoint.label) {
                                    vm.useEndpoint(endpoint)
                                }
                            }
                        }
                    }
                    .menuStyle(.button)

                    Button("Refresh") {
                        vm.refreshDiscovery()
                    }
                    .buttonStyle(.bordered)

                    Text(vm.discoveryStatus)
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    Spacer()
                }
            } else {
                HStack(spacing: 8) {
                    Text(vm.discoveryStatus)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                }
            }

            Picker("Source", selection: $vm.captureSource) {
                Text("Window").tag(CaptureSource.window)
                Text("Screen").tag(CaptureSource.screen)
            }
            .pickerStyle(.segmented)

            HStack(spacing: 10) {
                Button(vm.isStreaming ? "Stop" : "Start") {
                    if vm.isStreaming {
                        vm.stop()
                    } else {
                        vm.start()
                    }
                }
                .buttonStyle(.borderedProminent)

                Button("Test") {
                    Task { await vm.testConnection() }
                }
                .buttonStyle(.bordered)

                Button("Single") {
                    Task { await vm.pullSingleFrame() }
                }
                .buttonStyle(.bordered)

                Text(vm.status)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(vm.quickActions(), id: \.title) { action in
                        Button(action.title) {
                            Task { await vm.sendShortcut(key: action.key, modifiers: action.modifiers) }
                        }
                        .buttonStyle(.bordered)
                    }
                }
            }

            if !vm.detail.isEmpty {
                Text(vm.detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }

            ZStack {
                RoundedRectangle(cornerRadius: 12)
                    .fill(Color.black.opacity(0.9))

                if let image = vm.image {
                    GeometryReader { proxy in
                        let container = proxy.size
                        let imageSize = image.size
                        let scale = min(
                            container.width / max(imageSize.width, 1),
                            container.height / max(imageSize.height, 1)
                        )
                        let drawnWidth = imageSize.width * scale
                        let drawnHeight = imageSize.height * scale
                        let drawnMinX = (container.width - drawnWidth) / 2
                        let drawnMinY = (container.height - drawnHeight) / 2

                        Image(uiImage: image)
                            .resizable()
                            .interpolation(.none)
                            .scaledToFit()
                            .frame(width: container.width, height: container.height)
                            .contentShape(Rectangle())
                            .gesture(
                                DragGesture(minimumDistance: 0)
                                    .onEnded { gesture in
                                        guard vm.isStreaming else { return }
                                        let p = gesture.location
                                        guard p.x >= drawnMinX, p.x <= drawnMinX + drawnWidth else { return }
                                        guard p.y >= drawnMinY, p.y <= drawnMinY + drawnHeight else { return }
                                        let nx = (p.x - drawnMinX) / max(drawnWidth, 1)
                                        let ny = (p.y - drawnMinY) / max(drawnHeight, 1)
                                        Task { await vm.clickNormalized(nx: nx, ny: ny) }
                                    }
                            )
                    }
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                } else {
                    Text("No frame yet")
                        .foregroundStyle(.white.opacity(0.8))
                }
            }
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .stroke(Color.white.opacity(0.1), lineWidth: 1)
            )

            if let log = vm.recentLogs.first {
                Text(log)
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding()
        .navigationTitle("virtualOS Remote")
        .task {
            guard !didAutoStart else { return }
            didAutoStart = true
            if vm.connectionMode == .remote {
                if !vm.remoteURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    vm.start()
                }
            } else {
                vm.start()
            }
        }
        .onChange(of: vm.connectionMode) { _, _ in
            vm.onConnectionModeChanged()
        }
        .onChange(of: vm.host) { _, _ in
            vm.saveSettings()
        }
        .onChange(of: vm.port) { _, _ in
            vm.saveSettings()
        }
        .onChange(of: vm.remoteURL) { _, _ in
            vm.saveSettings()
        }
        .onChange(of: vm.authToken) { _, _ in
            vm.saveSettings()
        }
        .onChange(of: vm.cfAccessClientID) { _, _ in
            vm.saveSettings()
        }
        .onChange(of: vm.cfAccessClientSecret) { _, _ in
            vm.saveSettings()
        }
        .onChange(of: vm.captureSource) { _, _ in
            vm.saveSettings()
        }
        .onChange(of: vm.targetApp) { _, _ in
            vm.saveSettings()
        }
    }
}
