//
//  FleetSupervisor.swift
//  virtualOS experimental multi-VM box
//
//  Headless supervisor that boots N Virtualization.framework VMs concurrently
//  on one Mac and exposes them over a single local HTTP control surface.
//
//  This is deliberately separate from the shipping virtualOS.app UI. It reuses
//  the same VM bundle layout (Disk.img + HardwareModel + MachineIdentifier +
//  AuxiliaryStorage + Parameters.txt) so existing VM bundles can be added to a
//  fleet unchanged.
//
//  Experimental: nested virtualization is probed and, when the host allows it
//  AND the bundle requests it via Parameters.txt[.nestedVirtualizationEnabled],
//  enabled on a generic platform so a guest can host its own VMs.
//
//  Build:
//    swiftc -O -framework Virtualization -framework AppKit \
//      tools/fleet/FleetSupervisor.swift -o tools/fleet/fleet-supervisor
//
//  Run:
//    tools/fleet/fleet-supervisor --root ~/VirtualOSMachines \
//      --names vm-a,vm-b --base-port 9100 --http-port 9200
//

import Foundation
import Virtualization
import AppKit

// MARK: - Small JSON helpers

func jsonLine(_ object: [String: Any]) -> String {
    guard let data = try? JSONSerialization.data(withJSONObject: object),
          let text = String(data: data, encoding: .utf8) else {
        return "{}"
    }
    return text
}

/// Build a `VZMacPlatformConfiguration` from a virtualOS bundle directory.
///
/// Mirrors the layout used by the shipping app: `AuxiliaryStorage`,
/// `HardwareModel`, and `MachineIdentifier` side by side inside the `.bundle`.
func readMacPlatform(bundleURL: URL) -> VZMacPlatformConfiguration? {
    let hardwareModelURL = bundleURL.appendingPathComponent("HardwareModel")
    let machineIdentifierURL = bundleURL.appendingPathComponent("MachineIdentifier")
    let auxiliaryStorageURL = bundleURL.appendingPathComponent("AuxiliaryStorage")

    guard let hardwareModelData = try? Data(contentsOf: hardwareModelURL),
          let hardwareModel = VZMacHardwareModel(dataRepresentation: hardwareModelData),
          hardwareModel.isSupported else {
        return nil
    }
    guard let machineIdentifierData = try? Data(contentsOf: machineIdentifierURL),
          let machineIdentifier = VZMacMachineIdentifier(dataRepresentation: machineIdentifierData) else {
        return nil
    }
    guard FileManager.default.fileExists(atPath: auxiliaryStorageURL.path) else {
        return nil
    }

    let platform = VZMacPlatformConfiguration()
    platform.hardwareModel = hardwareModel
    platform.machineIdentifier = machineIdentifier
    platform.auxiliaryStorage = VZMacAuxiliaryStorage(contentsOf: auxiliaryStorageURL)
    return platform
}

// MARK: - Per-VM instance

/// One supervised virtual machine. A single serial queue owns all VM state
/// transitions so `VZVirtualMachine` is only touched from one thread at a time.
final class FleetInstance: NSObject {
    let name: String
    let bundleURL: URL
    let controlPort: UInt16
    let log: (String) -> Void

    private let queue: DispatchQueue
    private var vm: VZVirtualMachine?
    private var configuration: VZVirtualMachineConfiguration?
    private var state: String = "idle"
    private var lastError: String = ""
    private var headlessView: VZVirtualMachineView?
    private var headlessWindow: NSWindow?
    private var windowWidth: CGFloat = 1440
    private var windowHeight: CGFloat = 900

    init(name: String, bundleURL: URL, controlPort: UInt16, log: @escaping (String) -> Void) {
        self.name = name
        self.bundleURL = bundleURL
        self.controlPort = controlPort
        self.log = log
        self.queue = DispatchQueue(label: "virtualos.fleet.\(name)")
        super.init()
    }

    private var bootDiskURL: URL { bundleURL.appendingPathComponent("Disk.img") }
    private var hardwareModelURL: URL { bundleURL.appendingPathComponent("HardwareModel") }
    private var machineIdentifierURL: URL { bundleURL.appendingPathComponent("MachineIdentifier") }
    private var auxiliaryStorageURL: URL { bundleURL.appendingPathComponent("AuxiliaryStorage") }
    private var parametersURL: URL { bundleURL.appendingPathComponent("Parameters.txt") }

    private func readParameters() -> [String: Any] {
        guard let data = try? Data(contentsOf: parametersURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return [:]
        }
        return obj
    }

    /// Parse a Parameters.txt field tolerating both numbers and numeric strings.
    private func intParam(_ params: [String: Any], _ key: String, default def: Int) -> Int {
        if let n = params[key] as? Int { return n }
        if let n = params[key] as? NSNumber { return n.intValue }
        if let s = params[key] as? String, let n = Int(s) { return n }
        return def
    }

    private func boolParam(_ params: [String: Any], _ key: String, default def: Bool) -> Bool {
        if let b = params[key] as? Bool { return b }
        if let n = params[key] as? NSNumber { return n.boolValue }
        if let s = params[key] as? String { return (s as NSString).boolValue }
        return def
    }

    func status() -> [String: Any] {
        queue.sync {
            [
                "name": name,
                "bundle": bundleURL.path,
                "controlPort": Int(controlPort),
                "state": state,
                "ready": state == "running",
                "error": lastError,
            ]
        }
    }

    func start() {
        queue.async { [self] in
            guard state != "running" && state != "starting" else { return }
            state = "starting"
            lastError = ""

            guard FileManager.default.fileExists(atPath: bootDiskURL.path) else {
                state = "failed"; lastError = "missing Disk.img"; log("\(name): \(lastError)"); return
            }
            guard FileManager.default.fileExists(atPath: hardwareModelURL.path),
                  FileManager.default.fileExists(atPath: machineIdentifierURL.path) else {
                state = "failed"; lastError = "missing platform files"; log("\(name): \(lastError)"); return
            }

            let params = readParameters()
            let cpuCount = max(1, intParam(params, "cpuCount", default: 2))
            let memoryGB = max(1, intParam(params, "memorySizeInGB", default: 4))
            let nested = boolParam(params, "nestedVirtualizationEnabled", default: false)
            windowWidth = CGFloat(max(640, intParam(params, "screenWidth", default: 1440)))
            windowHeight = CGFloat(max(480, intParam(params, "screenHeight", default: 900)))

            // A macOS guest boots through VZMacPlatformConfiguration +
            // VZMacOSBootLoader; nested virtualization only exists on the
            // generic platform, so the two are mutually exclusive today.
            let useMacPlatform: Bool
            if nested {
                useMacPlatform = false
            } else {
                useMacPlatform = true
            }

            let config = VZVirtualMachineConfiguration()
            config.cpuCount = min(cpuCount, VZVirtualMachineConfiguration.maximumAllowedCPUCount)
            config.memorySize = UInt64(memoryGB) * 1024 * 1024 * 1024
            config.pointingDevices = [VZUSBScreenCoordinatePointingDeviceConfiguration()]
            config.keyboards = [VZUSBKeyboardConfiguration()]
            config.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]

            if useMacPlatform {
                guard let macPlatform = readMacPlatform(bundleURL: bundleURL) else {
                    state = "failed"; lastError = "could not read Mac platform from bundle"
                    log("\(name): \(lastError)"); return
                }
                config.platform = macPlatform
                config.bootLoader = VZMacOSBootLoader()
                log("\(name): macOS platform + VZMacOSBootLoader")
            } else {
                let platform = VZGenericPlatformConfiguration()
                if #available(macOS 15.0, *) {
                    if VZGenericPlatformConfiguration.isNestedVirtualizationSupported {
                        platform.isNestedVirtualizationEnabled = true
                        log("\(name): nested virtualization ENABLED")
                    } else {
                        log("\(name): nested virtualization requested but unsupported")
                    }
                }
                config.platform = platform
                config.bootLoader = VZEFIBootLoader()
                log("\(name): generic platform + VZEFIBootLoader (nested)")
            }

            do {
                let diskAttachment = try VZDiskImageStorageDeviceAttachment(url: bootDiskURL, readOnly: false)
                config.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: diskAttachment)]
            } catch {
                state = "failed"; lastError = "storage: \(error.localizedDescription)"
                log("\(name): \(lastError)"); return
            }

            let network = VZVirtioNetworkDeviceConfiguration()
            network.attachment = VZNATNetworkDeviceAttachment()
            config.networkDevices = [network]

            let graphics = VZMacGraphicsDeviceConfiguration()
            graphics.displays = [
                VZMacGraphicsDisplayConfiguration(
                    widthInPixels: Int(windowWidth),
                    heightInPixels: Int(windowHeight),
                    pixelsPerInch: 96
                )
            ]
            config.graphicsDevices = [graphics]

            let socketDevice = VZVirtioSocketDeviceConfiguration()
            config.socketDevices = [socketDevice]

            do {
                try config.validate()
            } catch {
                state = "failed"; lastError = "invalid config: \(error.localizedDescription)"
                log("\(name): \(lastError)"); return
            }

            let machine = VZVirtualMachine(configuration: config, queue: queue)
            self.configuration = config
            self.vm = machine

            machine.start { [weak self] result in
                guard let self else { return }
                switch result {
                case .success:
                    self.state = "running"
                    self.log("\(self.name): running (nested=\(nested))")
                case .failure(let error):
                    self.state = "failed"
                    self.lastError = error.localizedDescription
                    self.log("\(self.name): start failed: \(error.localizedDescription)")
                }
            }
        }
    }

    func stop() {
        queue.async { [self] in
            guard let vm else { state = "idle"; return }
            state = "stopping"
            vm.stop { [weak self] error in
                guard let self else { return }
                self.queue.async {
                    if let error { self.lastError = error.localizedDescription }
                    self.state = "idle"
                    self.vm = nil
                }
            }
        }
    }

    // MARK: - Frame capture

    /// Capture the current guest frame as JPEG data.
    ///
    /// A `VZVirtualMachineView` in a borderless window is attached to the machine
    /// so the guest's framebuffer renders. The view must be created and rendered
    /// on the main thread. The window is shown without stealing focus so the
    /// supervisor stays headless from the operator's point of view.
    func captureFrame() -> (jpeg: Data, width: Int, height: Int)? {
        let machine: VZVirtualMachine? = queue.sync { self.vm }
        guard let machine, queue.sync(execute: { state == "running" }) else { return nil }

        return DispatchQueue.main.sync { [self] in
            if headlessView == nil {
                let view = VZVirtualMachineView()
                view.virtualMachine = machine
                view.automaticallyReconfiguresDisplay = true
                let window = NSWindow(
                    contentRect: NSRect(x: 0, y: 0, width: windowWidth, height: windowHeight),
                    styleMask: [.borderless],
                    backing: .buffered,
                    defer: false
                )
                window.contentView = view
                window.isReleasedWhenClosed = false
                window.setFrameOrigin(NSPoint(x: -10000, y: -10000))
                window.orderFrontRegardless()
                headlessWindow = window
                headlessView = view
            }
            guard let view = headlessView, view.bounds.width > 0, view.bounds.height > 0 else {
                return nil
            }
            guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return nil }
            view.cacheDisplay(in: view.bounds, to: rep)

            let scale = min(1.0, 1568.0 / max(view.bounds.width, view.bounds.height))
            let imageW = Int(view.bounds.width * scale)
            let imageH = Int(view.bounds.height * scale)

            let finalRep: NSBitmapImageRep
            if scale < 1.0 {
                let scaled = NSImage(size: NSSize(width: imageW, height: imageH))
                scaled.lockFocus()
                NSGraphicsContext.current?.imageInterpolation = .high
                rep.draw(in: NSRect(x: 0, y: 0, width: imageW, height: imageH))
                scaled.unlockFocus()
                guard let tiff = scaled.tiffRepresentation,
                      let scaledRep = NSBitmapImageRep(data: tiff) else { return nil }
                finalRep = scaledRep
            } else {
                finalRep = rep
            }
            guard let jpeg = finalRep.representation(using: .jpeg, properties: [.compressionFactor: 0.75]) else { return nil }
            return (jpeg, imageW, imageH)
        }
    }
}

// MARK: - Supervisor

final class FleetSupervisor {
    private let root: URL
    private let names: [String]
    private let basePort: UInt16
    private var instances: [FleetInstance] = []

    init(root: URL, names: [String], basePort: UInt16) {
        self.root = root
        self.names = names
        self.basePort = basePort
    }

    private func log(_ line: String) {
        FileHandle.standardError.write(Data(("[fleet] " + line + "\n").utf8))
    }

    func buildInstances() {
        for (index, name) in names.enumerated() {
            let bundle = root.appendingPathComponent(name).appendingPathExtension("bundle")
            let instance = FleetInstance(
                name: name,
                bundleURL: bundle,
                controlPort: basePort + UInt16(index),
                log: log
            )
            instances.append(instance)
        }
    }

    func capability() -> [String: Any] {
        var result: [String: Any] = ["platform": "macOS \(ProcessInfo.processInfo.operatingSystemVersionString)"]
        if #available(macOS 15.0, *) {
            let supported = VZGenericPlatformConfiguration.isNestedVirtualizationSupported
            result["nestedVirtualizationSupported"] = supported
            result["nestedVirtualizationReason"] = supported
                ? "supported"
                : "not supported (hardware or policy)"
        } else {
            result["nestedVirtualizationSupported"] = false
            result["nestedVirtualizationReason"] = "requires macOS 15+"
        }
        return result
    }

    func status() -> [String: Any] {
        [
            "ok": true,
            "root": root.path,
            "basePort": Int(basePort),
            "capability": capability(),
            "count": instances.count,
            "instances": instances.map { $0.status() },
        ]
    }

    func startAll() -> [String: Any] {
        for instance in instances { instance.start() }
        return status()
    }

    func start(name: String) -> [String: Any] {
        guard let instance = instances.first(where: { $0.name == name }) else {
            return ["ok": false, "error": "unknown vm \(name)"]
        }
        instance.start()
        return ["ok": true, "instance": instance.status()]
    }

    func stop(name: String) -> [String: Any] {
        guard let instance = instances.first(where: { $0.name == name }) else {
            return ["ok": false, "error": "unknown vm \(name)"]
        }
        instance.stop()
        return ["ok": true, "instance": instance.status()]
    }

    func instance(named name: String) -> FleetInstance? {
        instances.first { $0.name == name }
    }
}

// MARK: - HTTP surface

final class FleetHTTPServer {
    private let supervisor: FleetSupervisor
    private let port: UInt16
    private var serverSocket: Int32 = -1

    init(supervisor: FleetSupervisor, port: UInt16) {
        self.supervisor = supervisor
        self.port = port
    }

    func run() {
        let fd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { fatalError("socket() failed errno=\(errno)") }
        serverSocket = fd
        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")

        let bindResult = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0, Darwin.listen(fd, 16) == 0 else {
            fatalError("bind/listen failed errno=\(errno)")
        }
        FileHandle.standardError.write(Data("[fleet] http listening on 127.0.0.1:\(port)\n".utf8))

        while true {
            let client = Darwin.accept(fd, nil, nil)
            guard client >= 0 else { continue }
            DispatchQueue.global(qos: .utility).async { [self] in
                handle(client)
            }
        }
    }

    private func handle(_ fd: Int32) {
        defer { Darwin.close(fd) }
        var request = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        let n = Darwin.read(fd, &buffer, buffer.count)
        if n > 0 { request.append(contentsOf: buffer[0..<n]) }
        let raw = String(decoding: request, as: UTF8.self)
        let requestLine = raw.components(separatedBy: "\r\n").first ?? ""
        let parts = requestLine.split(separator: " ")
        let method = parts.count > 0 ? String(parts[0]) : "GET"
        let path = parts.count > 1 ? String(parts[1]) : "/"

        // Binary frame endpoint: return raw JPEG with headers.
        if method == "GET" && path.hasPrefix("/fleet/frame/") {
            let name = String(path.dropFirst("/fleet/frame/".count))
            guard let instance = supervisor.instance(named: name),
                  let frame = instance.captureFrame() else {
                let body = jsonLine(["ok": false, "error": "no frame for \(name)"])
                let response = "HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nConnection: close\r\nAccess-Control-Allow-Origin: *\r\n\r\n\(body)"
                _ = response.withCString { Darwin.write(fd, $0, strlen($0)) }
                return
            }
            let header = "HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: \(frame.jpeg.count)\r\nConnection: close\r\nCache-Control: no-cache\r\nX-Image-Width: \(frame.width)\r\nX-Image-Height: \(frame.height)\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Expose-Headers: X-Image-Width,X-Image-Height\r\n\r\n"
            _ = header.withCString { Darwin.write(fd, $0, strlen($0)) }
            frame.jpeg.withUnsafeBytes { ptr in
                if let base = ptr.baseAddress {
                    _ = Darwin.write(fd, base, frame.jpeg.count)
                }
            }
            return
        }

        let (status, body) = route(method: method, path: path, raw: raw)
        let response = "HTTP/1.1 \(status)\r\nContent-Type: application/json\r\nConnection: close\r\nAccess-Control-Allow-Origin: *\r\n\r\n\(body)"
        _ = response.withCString { ptr in Darwin.write(fd, ptr, strlen(ptr)) }
    }

    private func route(method: String, path: String, raw: String) -> (String, String) {
        if method == "GET" && path == "/fleet/health" {
            return ("200 OK", jsonLine(["ok": true, "service": "virtualos-fleet"]))
        }
        if method == "GET" && path == "/fleet/status" {
            return ("200 OK", jsonLine(supervisor.status()))
        }
        if method == "GET" && path == "/fleet/capability" {
            return ("200 OK", jsonLine(["ok": true, "capability": supervisor.capability()]))
        }
        if method == "POST" && path == "/fleet/start" {
            return ("200 OK", jsonLine(supervisor.startAll()))
        }
        if path.hasPrefix("/fleet/start/") {
            let name = String(path.dropFirst("/fleet/start/".count))
            return ("200 OK", jsonLine(supervisor.start(name: name)))
        }
        if path.hasPrefix("/fleet/stop/") {
            let name = String(path.dropFirst("/fleet/stop/".count))
            return ("200 OK", jsonLine(supervisor.stop(name: name)))
        }
        return ("404 Not Found", jsonLine(["ok": false, "error": "not found", "path": path]))
    }
}

// MARK: - Entry point

struct FleetArgs {
    var root: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("VirtualOSMachines")
    var names: [String] = []
    var basePort: UInt16 = 9100
    var httpPort: UInt16 = 9200
    var startImmediately = false

    static func parse(_ argv: [String]) -> FleetArgs {
        var args = FleetArgs()
        var i = 1
        while i < argv.count {
            let arg = argv[i]
            func next() -> String? { i += 1; return i < argv.count ? argv[i] : nil }
            switch arg {
            case "--root": if let v = next() { args.root = URL(fileURLWithPath: (v as NSString).expandingTildeInPath) }
            case "--names": if let v = next() { args.names = v.split(separator: ",").map(String.init) }
            case "--base-port": if let v = next(), let p = UInt16(v) { args.basePort = p }
            case "--http-port": if let v = next(), let p = UInt16(v) { args.httpPort = p }
            case "--start": args.startImmediately = true
            default: break
            }
            i += 1
        }
        return args
    }
}

let args = FleetArgs.parse(CommandLine.arguments)
guard !args.names.isEmpty else {
    FileHandle.standardError.write(Data("usage: fleet-supervisor --names vm-a,vm-b [--root DIR] [--base-port N] [--http-port N] [--start]\n".utf8))
    exit(2)
}

let supervisor = FleetSupervisor(root: args.root, names: args.names, basePort: args.basePort)
supervisor.buildInstances()

if args.startImmediately {
    _ = supervisor.startAll()
}

let server = FleetHTTPServer(supervisor: supervisor, port: args.httpPort)
signal(SIGINT) { _ in exit(0) }
signal(SIGTERM) { _ in exit(0) }

// The HTTP server runs off the main thread; the main thread becomes the AppKit
// run loop so headless VZVirtualMachineView frame capture can render.
DispatchQueue.global(qos: .utility).async {
    server.run()
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.run()
