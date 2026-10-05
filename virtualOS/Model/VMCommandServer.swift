//
//  VMCommandServer.swift
//  virtualOS
//
//  Embedded HTTP server for reliable VM control.
//  Sends input directly to VZVirtualMachineView via NSEvent.
//  No CGEvents, no clipboard, no screenshots needed.
//

#if arch(arm64)

import Foundation
import AppKit
import Virtualization
import OSLog

final class VMCommandServer {
    static let shared = VMCommandServer()
    let port: UInt16 = 8845

    private var listener: (any NSObjectProtocol)?
    private var serverSocket: Int32 = -1
    private weak var vmView: VZVirtualMachineView?
    private let stateQueue = DispatchQueue(label: "com.github.yep.virtualOS.vmcommand.state")
    private let inputQueue = DispatchQueue(label: "com.github.yep.virtualOS.vmcommand.input")
    private let clientSlots = DispatchSemaphore(value: 8)
    private var serverStarted = false

    private init() {}

    func start(view: VZVirtualMachineView) {
        self.vmView = view
        let shouldStart = stateQueue.sync { () -> Bool in
            if serverStarted {
                return false
            }
            serverStarted = true
            return true
        }
        guard shouldStart else {
            Logger.shared.log(level: .default, "VMCommandServer updated vmView")
            return
        }
        DispatchQueue.global(qos: .utility).async { [weak self] in
            self?.runServer()
        }
        Logger.shared.log(level: .default, "VMCommandServer starting on port \(self.port)")
    }

    // MARK: - Server

    private func runServer() {
        let fd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else {
            stateQueue.sync { serverStarted = false }
            Logger.shared.log(level: .default, "VMCommandServer socket() failed errno=\(errno)")
            return
        }
        serverSocket = fd

        var yes: Int32 = 1
        Darwin.setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
        Darwin.setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &yes, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        // The node agent is the authenticated remote boundary. Keep the raw VM
        // control socket local so unauthenticated input is never exposed on LAN.
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")

        let bindResult = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0 else {
            Darwin.close(fd)
            serverSocket = -1
            stateQueue.sync { serverStarted = false }
            Logger.shared.log(level: .default, "VMCommandServer bind failed errno=\(errno)")
            return
        }
        guard Darwin.listen(fd, 5) == 0 else {
            Darwin.close(fd)
            serverSocket = -1
            stateQueue.sync { serverStarted = false }
            Logger.shared.log(level: .default, "VMCommandServer listen failed errno=\(errno)")
            return
        }

        Logger.shared.log(level: .default, "VMCommandServer listening on 127.0.0.1:\(self.port)")

        while true {
            let client = Darwin.accept(fd, nil, nil)
            guard client >= 0 else {
                // A resource failure can otherwise turn this listener into a
                // tight loop while the host is already under pressure.
                if errno != EINTR { Thread.sleep(forTimeInterval: 0.1) }
                continue
            }
            guard clientSlots.wait(timeout: .now()) == .success else {
                Darwin.close(client)
                continue
            }
            DispatchQueue.global(qos: .utility).async {
                defer { self.clientSlots.signal() }
                self.handleClient(client)
            }
        }
    }

    private func handleClient(_ fd: Int32) {
        // TCP is a byte stream: a POST body can arrive in several reads.
        // Cap input so command transport cannot grow an unbounded buffer.
        var timeout = timeval(tv_sec: 5, tv_usec: 0)
        Darwin.setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        Darwin.setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        var request = Data()
        var headerEnd: Int?
        var expectedLength: Int?
        let maxBody = 1_048_576
        while request.count <= maxBody + 8192 {
            var buffer = [UInt8](repeating: 0, count: 8192)
            let n = Darwin.read(fd, &buffer, buffer.count)
            if n <= 0 { break }
            request.append(contentsOf: buffer[0..<n])
            if headerEnd == nil, let end = request.range(of: Data("\r\n\r\n".utf8))?.upperBound {
                headerEnd = end
                let header = String(decoding: request.prefix(end), as: UTF8.self)
                let lengthLine = header.components(separatedBy: "\r\n").first {
                    $0.lowercased().hasPrefix("content-length:")
                }
                expectedLength = lengthLine.flatMap {
                    Int($0.dropFirst("content-length:".count).trimmingCharacters(in: .whitespaces))
                } ?? 0
                if expectedLength! < 0 || expectedLength! > maxBody { break }
            }
            if let end = headerEnd, let length = expectedLength, request.count >= end + length { break }
        }
        guard let end = headerEnd, let length = expectedLength,
              length >= 0, length <= maxBody, request.count >= end + length else {
            _ = writeAll(fd, data: Data("HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".utf8))
            Darwin.close(fd)
            return
        }
        let raw = String(decoding: request.prefix(end + length), as: UTF8.self)
        let (method, path, body) = parseHTTP(raw)

        // Handle screen capture separately (binary response)
        if method == "GET" && path == "/vm/screen" {
            handleScreenCapture(fd)
            return
        }

        let (status, responseBody) = route(method: method, path: path, body: body)
        let response = "HTTP/1.1 \(status)\r\nContent-Type: application/json\r\nConnection: close\r\nAccess-Control-Allow-Origin: *\r\n\r\n\(responseBody)"
        _ = writeAll(fd, data: Data(response.utf8))
        Darwin.close(fd)
    }

    @discardableResult
    private func writeAll(_ fd: Int32, data: Data) -> Bool {
        data.withUnsafeBytes { bytes in
            guard let base = bytes.baseAddress else { return data.isEmpty }
            var written = 0
            while written < bytes.count {
                let n = Darwin.write(fd, base.advanced(by: written), bytes.count - written)
                if n > 0 {
                    written += n
                } else if n < 0 && errno == EINTR {
                    continue
                } else {
                    return false
                }
            }
            return true
        }
    }

    private static let maxScreenshotEdge: CGFloat = 1568

    private func handleScreenCapture(_ fd: Int32) {
        guard let view = vmView else {
            let err = "HTTP/1.1 500\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{\"ok\":false}"
            _ = writeAll(fd, data: Data(err.utf8))
            Darwin.close(fd)
            return
        }
        let result: (Data, String, Int, Int, Int, Int)? = DispatchQueue.main.sync {
            guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return nil }
            view.cacheDisplay(in: view.bounds, to: rep)

            let captureW = Int(view.bounds.width)
            let captureH = Int(view.bounds.height)

            // Scale so longest edge <= maxScreenshotEdge (like usecomputer)
            let scale = min(1.0, Self.maxScreenshotEdge / max(view.bounds.width, view.bounds.height))
            let imageW = Int(view.bounds.width * scale)
            let imageH = Int(view.bounds.height * scale)

            let finalRep: NSBitmapImageRep
            if scale < 1.0 {
                let scaled = NSImage(size: NSSize(width: imageW, height: imageH))
                scaled.lockFocus()
                NSGraphicsContext.current?.imageInterpolation = .high
                rep.draw(in: NSRect(x: 0, y: 0, width: imageW, height: imageH))
                scaled.unlockFocus()
                guard let tiffData = scaled.tiffRepresentation,
                      let scaledRep = NSBitmapImageRep(data: tiffData) else { return nil }
                finalRep = scaledRep
            } else {
                finalRep = rep
            }

            guard let jpegData = finalRep.representation(using: .jpeg, properties: [.compressionFactor: 0.75]) else { return nil }
            let coordMap = "0,0,\(captureW),\(captureH),\(imageW),\(imageH)"
            return (jpegData, coordMap, captureW, captureH, imageW, imageH)
        }
        guard let (data, coordMap, captureW, captureH, imageW, imageH) = result else {
            let err = "HTTP/1.1 500\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{\"ok\":false}"
            _ = writeAll(fd, data: Data(err.utf8))
            Darwin.close(fd)
            return
        }
        let header = "HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: \(data.count)\r\nConnection: close\r\nCache-Control: no-cache\r\nX-Coord-Map: \(coordMap)\r\nX-Capture-Width: \(captureW)\r\nX-Capture-Height: \(captureH)\r\nX-Image-Width: \(imageW)\r\nX-Image-Height: \(imageH)\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Expose-Headers: X-Coord-Map,X-Capture-Width,X-Capture-Height,X-Image-Width,X-Image-Height\r\n\r\n"
        guard writeAll(fd, data: Data(header.utf8)) else { Darwin.close(fd); return }
        _ = writeAll(fd, data: data)
        Darwin.close(fd)
    }

    // MARK: - Router

    private func route(method: String, path: String, body: String) -> (String, String) {
        switch (method, path) {
        case ("GET", "/vm/health"):
            let state = VMHostController.shared.getVMStatus()
            let ready = state == "Running"
            return (
                ready ? "200 OK" : "503 Service Unavailable",
                jsonResponse([
                    "ok": ready,
                    "ready": ready,
                    "state": state.lowercased(),
                    "port": Int(port),
                    "apiVersion": 1,
                ])
            )

        case ("POST", "/vm/stop"):
            let accepted = VMHostController.shared.requestStop()
            return (
                accepted ? "202 Accepted" : "503 Service Unavailable",
                jsonResponse(["ok": accepted, "accepted": accepted])
            )

        case ("POST", "/vm/type"):
            let text = jsonValue(body, key: "text") ?? ""
            inputQueue.sync {
                sendText(text)
            }
            return ("200 OK", jsonResponse(["ok": true, "typed": text.count]))

        case ("POST", "/vm/paste"):
            let text = jsonValue(body, key: "text") ?? ""
            inputQueue.sync {
                ensureFocus(steal: true)
                pasteText(text)
            }
            return ("200 OK", jsonResponse(["ok": true, "pasted": text.count]))

        case ("POST", "/vm/key"):
            let key = jsonValue(body, key: "key") ?? ""
            inputQueue.sync {
                sendKey(key)
            }
            return ("200 OK", jsonResponse(["ok": true, "key": key]))

        case ("POST", "/vm/shortcut"):
            let key = jsonValue(body, key: "key") ?? ""
            let mods = jsonArrayValue(body, key: "modifiers")
            inputQueue.sync {
                sendShortcut(key: key, modifiers: mods)
            }
            return ("200 OK", jsonResponse(["ok": true]))

        case ("POST", "/vm/click"):
            let imageX = Double(jsonValue(body, key: "imageX") ?? "")
            let imageY = Double(jsonValue(body, key: "imageY") ?? "")
            let imageWidth = Double(jsonValue(body, key: "imageWidth") ?? "")
            let imageHeight = Double(jsonValue(body, key: "imageHeight") ?? "")
            let mappedPoint = mapImagePointToViewPoint(
                imageX: imageX,
                imageY: imageY,
                imageWidth: imageWidth,
                imageHeight: imageHeight
            )
            let x = mappedPoint?.x ?? Double(jsonValue(body, key: "x") ?? "0.5") ?? 0.5
            let y = mappedPoint?.y ?? Double(jsonValue(body, key: "y") ?? "0.5") ?? 0.5
            let count = Int(jsonValue(body, key: "count") ?? "1") ?? 1
            let button = jsonValue(body, key: "button") ?? "left"
            inputQueue.sync {
                sendClick(x: x, y: y, count: count, button: button)
            }
            return ("200 OK", jsonResponse(["ok": true, "x": x, "y": y]))

        case ("POST", "/vm/scroll"):
            let x = Double(jsonValue(body, key: "x") ?? "0.5") ?? 0.5
            let y = Double(jsonValue(body, key: "y") ?? "0.5") ?? 0.5
            let dx = Int32(jsonValue(body, key: "dx") ?? "0") ?? 0
            let dy = Int32(jsonValue(body, key: "dy") ?? "-3") ?? -3
            inputQueue.sync {
                sendScroll(x: x, y: y, dx: dx, dy: dy)
            }
            return ("200 OK", jsonResponse(["ok": true]))

        case ("POST", "/vm/mouse/move"):
            let x = Double(jsonValue(body, key: "x") ?? "0") ?? 0
            let y = Double(jsonValue(body, key: "y") ?? "0") ?? 0
            inputQueue.sync {
                sendMouseMove(x: x, y: y)
            }
            return ("200 OK", jsonResponse(["ok": true]))

        case ("POST", "/vm/drag"):
            let fromX = Double(jsonValue(body, key: "fromX") ?? "0") ?? 0
            let fromY = Double(jsonValue(body, key: "fromY") ?? "0") ?? 0
            let toX = Double(jsonValue(body, key: "toX") ?? "0") ?? 0
            let toY = Double(jsonValue(body, key: "toY") ?? "0") ?? 0
            let steps = Int(jsonValue(body, key: "steps") ?? "20") ?? 20
            inputQueue.sync {
                sendDrag(fromX: fromX, fromY: fromY, toX: toX, toY: toY, steps: steps)
            }
            return ("200 OK", jsonResponse(["ok": true]))

        case ("POST", "/vm/exec"):
            let cmd = jsonValue(body, key: "cmd") ?? ""
            let usePaste = jsonBoolValue(body, key: "paste") ?? true
            inputQueue.sync {
                ensureFocus(steal: true)
                if usePaste {
                    pasteText(cmd)
                } else {
                    sendText(cmd)
                }
                Thread.sleep(forTimeInterval: 0.05)
                sendKey("return")
            }
            return ("200 OK", jsonResponse(["ok": true, "typed": cmd.count, "paste": usePaste]))

        case ("POST", "/vm/terminal/open"):
            inputQueue.sync {
                openGuestApp("Terminal")
            }
            return ("200 OK", jsonResponse(["ok": true, "app": "Terminal"]))

        case ("POST", "/vm/terminal/exec"):
            let cmd = jsonValue(body, key: "cmd") ?? ""
            let newWindow = jsonBoolValue(body, key: "newWindow") ?? true
            inputQueue.sync {
                executeInTerminal(command: cmd, newWindow: newWindow)
            }
            return ("200 OK", jsonResponse(["ok": true, "typed": cmd.count, "newWindow": newWindow]))

        case ("POST", "/vm/unlock"):
            let pw = jsonValue(body, key: "password") ?? ""
            guard let view = vmView else {
                return ("500 Internal Server Error", jsonResponse(["ok": false, "error": "no vm view"]))
            }
            inputQueue.sync {
                ensureFocus(steal: true)
                let size = DispatchQueue.main.sync { view.frame.size }
                sendClick(x: size.width / 2, y: size.height * 0.1)
                Thread.sleep(forTimeInterval: 0.2)
                sendText(pw)
                Thread.sleep(forTimeInterval: 0.1)
                sendKey("return")
            }
            return ("200 OK", jsonResponse(["ok": true]))

        case ("POST", "/vm/spotlight"):
            let query = jsonValue(body, key: "query") ?? ""
            inputQueue.sync {
                openSpotlight()
                pasteText(query)
                Thread.sleep(forTimeInterval: 0.25)
                sendKey("return")
            }
            return ("200 OK", jsonResponse(["ok": true, "query": query]))

        case ("POST", "/vm/open"):
            let app = jsonValue(body, key: "app") ?? ""
            inputQueue.sync {
                openGuestApp(app)
            }
            return ("200 OK", jsonResponse(["ok": true, "app": app]))

        case ("POST", "/vm/batch"):
            return handleBatch(body)

        default:
            return ("404 Not Found", jsonResponse([
                "ok": false,
                "error": "not found",
                "endpoints": [
                    "/vm/health",
                    "/vm/stop",
                    "/vm/screen",
                    "/vm/type",
                    "/vm/paste",
                    "/vm/key",
                    "/vm/shortcut",
                    "/vm/click",
                    "/vm/scroll",
                    "/vm/mouse/move",
                    "/vm/drag",
                    "/vm/exec",
                    "/vm/unlock",
                    "/vm/open",
                    "/vm/spotlight",
                    "/vm/terminal/open",
                    "/vm/terminal/exec",
                    "/vm/batch",
                ],
            ]))
        }
    }

    // MARK: - Batch execution

    private func handleBatch(_ body: String) -> (String, String) {
        guard let data = body.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let actions = obj["actions"] as? [[String: Any]] else {
            return ("400 Bad Request", jsonResponse(["ok": false, "error": "expected {\"actions\": [{\"action\":..., ...}, ...]}"]))
        }

        var results: [[String: Any]] = []
        for action in actions {
            guard let actionType = action["action"] as? String else {
                results.append(["ok": false, "error": "missing action field"])
                continue
            }
            // Optional delay before this action (ms)
            if let delayMs = action["delayMs"] as? Int, delayMs > 0 {
                Thread.sleep(forTimeInterval: Double(delayMs) / 1000.0)
            }
            let actionBody: String
            if let bodyData = try? JSONSerialization.data(withJSONObject: action),
               let bodyStr = String(data: bodyData, encoding: .utf8) {
                actionBody = bodyStr
            } else {
                actionBody = "{}"
            }
            let (_, resultBody) = route(method: "POST", path: "/vm/\(actionType)", body: actionBody)
            if let resultData = resultBody.data(using: .utf8),
               let resultObj = try? JSONSerialization.jsonObject(with: resultData) as? [String: Any] {
                var r = resultObj
                r["action"] = actionType
                results.append(r)
            } else {
                results.append(["ok": false, "action": actionType])
            }
        }
        return ("200 OK", jsonResponse(["ok": true, "results": results]))
    }

    // MARK: - Input via NSEvent → VZVirtualMachineView

    private var runsInBackgroundMode: Bool {
        ProcessInfo.processInfo.arguments.contains("--background-vm") ||
        ProcessInfo.processInfo.environment["VOS_BACKGROUND_VM"] == "1"
    }

    private func ensureFocus(steal: Bool = false) {
        DispatchQueue.main.sync {
            guard let view = vmView, let window = view.window else { return }
            // Synthetic NSEvent key events are only delivered while the hosting
            // window is key and the app can become active. In background mode
            // AppDelegate orders the window out, so it is never key and typing
            // was silently dropped. Make it key without showing it: keep the
            // window ordered in but park it off the visible screen.
            if !window.isKeyWindow {
                if runsInBackgroundMode {
                    if NSApp.activationPolicy() == .prohibited {
                        NSApp.setActivationPolicy(.accessory)
                    }
                    NSApp.activate(ignoringOtherApps: true)
                    let savedOrigin = window.frame.origin
                    window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
                    window.orderFrontRegardless()
                    window.makeKey()
                    window.setFrameOrigin(savedOrigin)
                } else if steal {
                    NSApp.activate(ignoringOtherApps: true)
                    window.makeKeyAndOrderFront(nil)
                }
            }
            window.makeFirstResponder(view)
        }
    }

    private func openSpotlight() {
        ensureFocus(steal: true)
        sendShortcut(key: " ", modifiers: ["command"])
        Thread.sleep(forTimeInterval: 0.3)
    }

    private func openGuestApp(_ app: String) {
        openSpotlight()
        pasteText(app)
        Thread.sleep(forTimeInterval: 0.3)
        sendKey("return")
        Thread.sleep(forTimeInterval: 0.5)
    }

    private func executeInTerminal(command: String, newWindow: Bool) {
        openGuestApp("Terminal")
        if newWindow {
            sendShortcut(key: "n", modifiers: ["command"])
            Thread.sleep(forTimeInterval: 0.2)
        }
        pasteText(command)
        Thread.sleep(forTimeInterval: 0.05)
        sendKey("return")
    }

    private func sendText(_ text: String) {
        ensureFocus()
        for char in text {
            let s = String(char)
            var code: UInt16
            var needsShift = false

            if let (shiftCode, _) = Self.shiftMap[char] {
                code = shiftCode
                needsShift = true
            } else if let kc = Self.keyCodes[s.lowercased()] {
                code = kc
                needsShift = char.isUppercase
            } else {
                code = 0
            }

            if needsShift {
                DispatchQueue.main.sync {
                    guard let view = vmView, let window = view.window,
                          let shiftDown = NSEvent.keyEvent(with: .flagsChanged, location: .zero, modifierFlags: [.shift], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber, context: nil, characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: Self.shiftKeyCode) else { return }
                    view.flagsChanged(with: shiftDown)
                }
                // VZ forwards modifier changes asynchronously. Let the main
                // run loop process that event before sending the virtual key.
                Thread.sleep(forTimeInterval: 0.025)
            }
            DispatchQueue.main.sync {
                guard let view = vmView, let window = view.window else { return }
                let wn = window.windowNumber
                let ts = ProcessInfo.processInfo.systemUptime
                let flags: NSEvent.ModifierFlags = needsShift ? .shift : []
                let unshifted = needsShift ? String(Self.unshiftedChar(for: code)) : s
                if let down = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags, timestamp: ts, windowNumber: wn, context: nil, characters: s, charactersIgnoringModifiers: unshifted, isARepeat: false, keyCode: code) {
                    view.keyDown(with: down)
                }
                if let up = NSEvent.keyEvent(with: .keyUp, location: .zero, modifierFlags: flags, timestamp: ts, windowNumber: wn, context: nil, characters: s, charactersIgnoringModifiers: unshifted, isARepeat: false, keyCode: code) {
                    view.keyUp(with: up)
                }
            }
            if needsShift {
                Thread.sleep(forTimeInterval: 0.025)
                DispatchQueue.main.sync {
                    guard let view = vmView, let window = view.window,
                          let shiftUp = NSEvent.keyEvent(with: .flagsChanged, location: .zero, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber, context: nil, characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: Self.shiftKeyCode) else { return }
                    view.flagsChanged(with: shiftUp)
                }
            }
            Thread.sleep(forTimeInterval: 0.015)
        }
    }

    private static func unshiftedChar(for keyCode: UInt16) -> Character {
        let map: [UInt16: Character] = [
            0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z", 7: "x", 8: "c", 9: "v",
            11: "b", 12: "q", 13: "w", 14: "e", 15: "r", 16: "y", 17: "t", 18: "1", 19: "2",
            20: "3", 21: "4", 22: "6", 23: "5", 24: "=", 25: "9", 26: "7", 27: "-", 28: "8",
            29: "0", 30: "]", 31: "o", 32: "u", 33: "[", 34: "i", 35: "p", 37: "l", 38: "j",
            39: "'", 40: "k", 41: ";", 42: "\\", 43: ",", 44: "/", 45: "n", 46: "m", 47: ".",
            49: " ", 50: "`",
        ]
        return map[keyCode] ?? "a"
    }

    private static func keyEventText(for name: String) -> String {
        switch name {
        case "return", "enter":
            return "\r"
        case "tab":
            return "\t"
        case "space", " ":
            return " "
        case "delete":
            return String(UnicodeScalar(Int(NSBackspaceCharacter))!)
        case "escape", "esc":
            return String(UnicodeScalar(0x1b)!)
        default:
            return name.count == 1 ? name : ""
        }
    }

    private func sendKey(_ name: String) {
        ensureFocus()
        let normalized = name.lowercased()
        guard let code = Self.keyCodes[normalized] else { return }
        let text = Self.keyEventText(for: normalized)
        DispatchQueue.main.sync {
            guard let view = vmView, let window = view.window else { return }
            let wn = window.windowNumber
            if let down = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, characters: text, charactersIgnoringModifiers: text, isARepeat: false, keyCode: code) {
                view.keyDown(with: down)
            }
            if let up = NSEvent.keyEvent(with: .keyUp, location: .zero, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, characters: text, charactersIgnoringModifiers: text, isARepeat: false, keyCode: code) {
                view.keyUp(with: up)
            }
        }
    }

    private func sendShortcut(key: String, modifiers: [String]) {
        ensureFocus()
        let normalizedKey = key.lowercased()
        var activeFlags: NSEvent.ModifierFlags = []
        var modifierSteps: [(NSEvent.ModifierFlags, UInt16)] = []
        for modifier in modifiers {
            guard let descriptor = Self.modifierDescriptor(for: modifier),
                  !activeFlags.contains(descriptor.flag) else { continue }
            activeFlags.insert(descriptor.flag)
            modifierSteps.append((descriptor.flag, descriptor.keyCode))
        }

        var keyCode = Self.keyCodes[normalizedKey] ?? 0
        var keyText = Self.keyEventText(for: normalizedKey)
        var charactersIgnoringModifiers = keyText
        if key.count == 1 {
            keyText = key
            charactersIgnoringModifiers = normalizedKey
            if let (shiftedKeyCode, _) = Self.shiftMap[key.first!] {
                keyCode = shiftedKeyCode
                charactersIgnoringModifiers = String(Self.unshiftedChar(for: shiftedKeyCode))
                if !activeFlags.contains(.shift) {
                    activeFlags.insert(.shift)
                    modifierSteps.append((.shift, Self.shiftKeyCode))
                }
            } else if key.first?.isUppercase == true && !activeFlags.contains(.shift) {
                activeFlags.insert(.shift)
                modifierSteps.append((.shift, Self.shiftKeyCode))
            }
        }

        DispatchQueue.main.sync {
            guard let view = vmView, let window = view.window else { return }
            let wn = window.windowNumber
            var emittedFlags: NSEvent.ModifierFlags = []
            for (flag, modifierKeyCode) in modifierSteps {
                emittedFlags.insert(flag)
                emitModifierFlagsChanged(view: view, windowNumber: wn, modifierFlags: emittedFlags, keyCode: modifierKeyCode)
            }

            if let down = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: activeFlags, timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, characters: keyText, charactersIgnoringModifiers: charactersIgnoringModifiers, isARepeat: false, keyCode: keyCode) {
                view.keyDown(with: down)
            }
            if let up = NSEvent.keyEvent(with: .keyUp, location: .zero, modifierFlags: activeFlags, timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, characters: keyText, charactersIgnoringModifiers: charactersIgnoringModifiers, isARepeat: false, keyCode: keyCode) {
                view.keyUp(with: up)
            }

            for (flag, modifierKeyCode) in modifierSteps.reversed() {
                emittedFlags.remove(flag)
                emitModifierFlagsChanged(view: view, windowNumber: wn, modifierFlags: emittedFlags, keyCode: modifierKeyCode)
            }
        }
    }

    private func emitModifierFlagsChanged(view: VZVirtualMachineView, windowNumber: Int, modifierFlags: NSEvent.ModifierFlags, keyCode: UInt16) {
        let timestamp = ProcessInfo.processInfo.systemUptime
        guard let event = NSEvent.keyEvent(with: .flagsChanged, location: .zero, modifierFlags: modifierFlags, timestamp: timestamp, windowNumber: windowNumber, context: nil, characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: keyCode) else {
            return
        }
        view.flagsChanged(with: event)
    }

    private func sendClick(x: Double, y: Double, count: Int = 1, button: String = "left") {
        ensureFocus()
        DispatchQueue.main.sync {
            guard let view = vmView, let window = view.window else { return }
            let wn = window.windowNumber
            let viewPoint = CGPoint(x: x, y: y)
            let point = view.convert(viewPoint, to: nil)

            let isRight = button == "right"
            let downType: NSEvent.EventType = isRight ? .rightMouseDown : .leftMouseDown
            let upType: NSEvent.EventType = isRight ? .rightMouseUp : .leftMouseUp

            if let move = NSEvent.mouseEvent(with: .mouseMoved, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, eventNumber: 0, clickCount: 0, pressure: 0.0) {
                view.mouseMoved(with: move)
            }
            for clickNum in 1...count {
                if let down = NSEvent.mouseEvent(with: downType, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, eventNumber: 0, clickCount: clickNum, pressure: 1.0) {
                    if isRight { view.rightMouseDown(with: down) } else { view.mouseDown(with: down) }
                }
                if let up = NSEvent.mouseEvent(with: upType, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, eventNumber: 0, clickCount: clickNum, pressure: 0.0) {
                    if isRight { view.rightMouseUp(with: up) } else { view.mouseUp(with: up) }
                }
            }
        }
    }

    private func mapImagePointToViewPoint(imageX: Double?, imageY: Double?, imageWidth: Double?, imageHeight: Double?) -> CGPoint? {
        guard let imageX,
              let imageY,
              let imageWidth,
              let imageHeight,
              imageWidth > 0,
              imageHeight > 0 else {
            return nil
        }

        return DispatchQueue.main.sync {
            guard let view = vmView else { return nil }
            let x = imageX * Double(view.bounds.width) / imageWidth
            // JPEG coordinates start at the top; NSView coordinates start at the bottom.
            let y = (imageHeight - imageY) * Double(view.bounds.height) / imageHeight
            return CGPoint(x: x, y: y)
        }
    }

    private func sendScroll(x: Double, y: Double, dx: Int32, dy: Int32) {
        ensureFocus()
        DispatchQueue.main.sync {
            guard let view = vmView, let window = view.window else { return }
            let viewPoint = CGPoint(x: x, y: y)
            let point = view.convert(viewPoint, to: nil)
            // Move mouse to scroll position first
            if let move = NSEvent.mouseEvent(with: .mouseMoved, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: 0, pressure: 0.0) {
                view.mouseMoved(with: move)
            }
            if let cgEvent = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 2, wheel1: dy, wheel2: dx, wheel3: 0) {
                if let scrollEvent = NSEvent(cgEvent: cgEvent) {
                    view.scrollWheel(with: scrollEvent)
                }
            }
        }
    }

    private func sendMouseMove(x: Double, y: Double) {
        ensureFocus()
        DispatchQueue.main.sync {
            guard let view = vmView, let window = view.window else { return }
            let viewPoint = CGPoint(x: x, y: y)
            let point = view.convert(viewPoint, to: nil)
            if let move = NSEvent.mouseEvent(with: .mouseMoved, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: 0, pressure: 0.0) {
                view.mouseMoved(with: move)
            }
        }
    }

    private func sendDrag(fromX: Double, fromY: Double, toX: Double, toY: Double, steps: Int) {
        ensureFocus()
        DispatchQueue.main.sync {
            guard let view = vmView, let window = view.window else { return }
            let wn = window.windowNumber
            let fromPoint = view.convert(CGPoint(x: fromX, y: fromY), to: nil)
            let toPoint = view.convert(CGPoint(x: toX, y: toY), to: nil)

            // Mouse down at start
            if let down = NSEvent.mouseEvent(with: .leftMouseDown, location: fromPoint, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, eventNumber: 0, clickCount: 1, pressure: 1.0) {
                view.mouseDown(with: down)
            }
            // Interpolate drag steps
            let n = max(steps, 1)
            for i in 1...n {
                let t = Double(i) / Double(n)
                let x = fromPoint.x + (toPoint.x - fromPoint.x) * t
                let y = fromPoint.y + (toPoint.y - fromPoint.y) * t
                let loc = CGPoint(x: x, y: y)
                if let drag = NSEvent.mouseEvent(with: .leftMouseDragged, location: loc, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, eventNumber: 0, clickCount: 1, pressure: 1.0) {
                    view.mouseDragged(with: drag)
                }
            }
            // Mouse up at end
            if let up = NSEvent.mouseEvent(with: .leftMouseUp, location: toPoint, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, eventNumber: 0, clickCount: 1, pressure: 0.0) {
                view.mouseUp(with: up)
            }
        }
    }

    private func pasteText(_ text: String) {
        // The host pasteboard is not shared with a macOS guest. Command-V only
        // pasted the guest's old clipboard, so app search and Terminal commands
        // silently went to the wrong place. Emit guest keyboard events instead.
        sendText(text)
    }

    // MARK: - Helpers

    private static let keyCodes: [String: UInt16] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
        "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
        "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
        "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
        "-": 27, "=": 24, "[": 33, "]": 30, "\\": 42, ";": 41, "'": 39,
        ",": 43, ".": 47, "/": 44, "`": 50,
        "return": 36, "enter": 36, "tab": 48, "space": 49, " ": 49, "delete": 51,
        "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126,
    ]

    private static let shiftKeyCode: UInt16 = 56
    private static let commandKeyCode: UInt16 = 55
    private static let optionKeyCode: UInt16 = 58
    private static let controlKeyCode: UInt16 = 59

    // Characters that need shift held
    private static let shiftMap: [Character: (UInt16, Bool)] = {
        var m: [Character: (UInt16, Bool)] = [:]
        // Shifted punctuation
        let shifted: [(Character, UInt16)] = [
            ("!", 18), ("@", 19), ("#", 20), ("$", 21), ("%", 23), ("^", 22),
            ("&", 26), ("*", 28), ("(", 25), (")", 29), ("_", 27), ("+", 24),
            ("{", 33), ("}", 30), ("|", 42), (":", 41), ("\"", 39),
            ("<", 43), (">", 47), ("?", 44), ("~", 50), ("_", 27), ("\"", 39),
        ]
        for (c, code) in shifted { m[c] = (code, true) }
        return m
    }()

    private static func modifierDescriptor(for modifier: String) -> (flag: NSEvent.ModifierFlags, keyCode: UInt16)? {
        switch modifier.lowercased() {
        case "command", "cmd":
            return (.command, Self.commandKeyCode)
        case "shift":
            return (.shift, Self.shiftKeyCode)
        case "option", "alt":
            return (.option, Self.optionKeyCode)
        case "control", "ctrl":
            return (.control, Self.controlKeyCode)
        default:
            return nil
        }
    }

    private func parseHTTP(_ raw: String) -> (String, String, String) {
        let split = raw.range(of: "\r\n\r\n")
        let header = split.map { String(raw[..<$0.lowerBound]) } ?? raw
        let lines = header.components(separatedBy: "\r\n")
        let parts = (lines.first ?? "").components(separatedBy: " ")
        let method = parts.count > 0 ? parts[0] : "GET"
        let path = parts.count > 1 ? parts[1].components(separatedBy: "?").first ?? parts[1] : "/"
        let body = split.map { String(raw[$0.upperBound...]) } ?? ""
        return (method, path, body)
    }

    private func jsonValue(_ json: String, key: String) -> String? {
        guard let data = json.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let val = obj[key] else { return nil }
        if let s = val as? String { return s }
        return "\(val)"
    }

    private func jsonArrayValue(_ json: String, key: String) -> [String] {
        guard let data = json.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let arr = obj[key] as? [String] else { return [] }
        return arr
    }

    private func jsonBoolValue(_ json: String, key: String) -> Bool? {
        guard let data = json.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let val = obj[key] else { return nil }
        if let boolVal = val as? Bool {
            return boolVal
        }
        if let stringVal = val as? String {
            switch stringVal.lowercased() {
            case "true", "1", "yes":
                return true
            case "false", "0", "no":
                return false
            default:
                return nil
            }
        }
        return nil
    }

    private func jsonResponse(_ obj: [String: Any]) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: obj, options: []),
              let text = String(data: data, encoding: .utf8) else {
            return "{\"ok\":false}"
        }
        return text
    }

}

#endif
