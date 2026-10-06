//
//  VMAutoLogin.swift
//  virtualOS
//
//  Watches the VM screen for the login prompt and automatically types the password.
//  Runs entirely inside the app — no external scripts, no focus stealing.
//  Reads the guest password prompt from the VZVirtualMachineView.
//

#if arch(arm64)

import AppKit
import Virtualization
import OSLog
import Vision

final class VMAutoLogin {
    static let shared = VMAutoLogin()

    private weak var vmView: VZVirtualMachineView?
    private var timer: Timer?
    private var password: String = ""
    private var isAttempting = false
    private var loginCount = 0
    private let maxAttempts = 1
    private var lastAttemptTime: Date = .distantPast
    private let cooldown: TimeInterval = 120 // seconds between attempts

    private init() {}

    /// Start watching for login screen. Call after VM starts.
    func start(view: VZVirtualMachineView, password: String) {
        self.vmView = view
        self.password = password
        self.loginCount = 0

        // Check every 5 seconds
        DispatchQueue.main.async { [weak self] in
            self?.timer?.invalidate()
            self?.timer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
                self?.checkScreen()
            }
            Logger.shared.log(level: .default, "VMAutoLogin: watching for login screen")
        }
    }

    func stop() {
        DispatchQueue.main.async { [weak self] in
            self?.timer?.invalidate()
            self?.timer = nil
        }
    }

    // MARK: - Screen Detection

    private func checkScreen() {
        guard !isAttempting else { return }
        guard loginCount < maxAttempts else { return }
        guard Date().timeIntervalSince(lastAttemptTime) > cooldown else { return }
        guard let view = vmView else { return }

        // Capture the view to a bitmap
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)

        if isLoginScreen(rep) {
            Logger.shared.log(level: .default, "VMAutoLogin: login screen detected, attempting unlock (#\(self.loginCount + 1))")
            attemptLogin(view: view)
        }
    }

    private func isLoginScreen(_ rep: NSBitmapImageRep) -> Bool {
        guard let image = rep.cgImage else { return false }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        do {
            try VNImageRequestHandler(cgImage: image).perform([request])
        } catch {
            Logger.shared.log(level: .error, "VMAutoLogin: could not inspect guest frame")
            return false
        }
        let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string.lowercased() }
        return lines.contains { $0.contains("enter password") } ||
            (lines.contains { $0.contains("password") } && lines.contains { $0.contains("log in") })
    }

    // MARK: - Login Action

    private func attemptLogin(view: VZVirtualMachineView) {
        isAttempting = true
        loginCount += 1
        lastAttemptTime = Date()

        guard let window = view.window else {
            isAttempting = false
            return
        }

        let wn = window.windowNumber

        // Make view first responder (no need to activate app / steal focus)
        window.makeFirstResponder(view)

        // Click on password field area (center-bottom of screen)
        let viewSize = view.bounds.size
        let clickPoint = CGPoint(x: viewSize.width / 2, y: viewSize.height * 0.1)
        let windowPoint = view.convert(clickPoint, to: nil)

        if let mouseMove = NSEvent.mouseEvent(with: .mouseMoved, location: windowPoint, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, eventNumber: 0, clickCount: 0, pressure: 0.0) {
            view.mouseMoved(with: mouseMove)
        }
        if let mouseDown = NSEvent.mouseEvent(with: .leftMouseDown, location: windowPoint, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, eventNumber: 0, clickCount: 1, pressure: 1.0) {
            view.mouseDown(with: mouseDown)
        }
        if let mouseUp = NSEvent.mouseEvent(with: .leftMouseUp, location: windowPoint, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, eventNumber: 0, clickCount: 1, pressure: 0.0) {
            view.mouseUp(with: mouseUp)
        }

        // Type password after short delay
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            guard let self, let view = self.vmView, let window = view.window else {
                self?.isAttempting = false
                return
            }
            let wn = window.windowNumber

            // Type each character with correct keycode
            for char in self.password {
                let s = String(char)
                let code = Self.keyCodes[s.lowercased()] ?? 0
                let flags: NSEvent.ModifierFlags = char.isUppercase ? .shift : []

                if let down = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags, timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, characters: s, charactersIgnoringModifiers: s.lowercased(), isARepeat: false, keyCode: code) {
                    view.keyDown(with: down)
                }
                if let up = NSEvent.keyEvent(with: .keyUp, location: .zero, modifierFlags: flags, timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, characters: s, charactersIgnoringModifiers: s.lowercased(), isARepeat: false, keyCode: code) {
                    view.keyUp(with: up)
                }
                Thread.sleep(forTimeInterval: 0.1)
            }

            // Press Return
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
                if let down = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: 36) {
                    view.keyDown(with: down)
                }
                if let up = NSEvent.keyEvent(with: .keyUp, location: .zero, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: wn, context: nil, characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: 36) {
                    view.keyUp(with: up)
                }

                Logger.shared.log(level: .default, "VMAutoLogin: password typed + enter pressed")

                // Check result after a delay
                DispatchQueue.main.asyncAfter(deadline: .now() + 5.0) { [weak self] in
                    self?.isAttempting = false
                    // Next checkScreen() cycle will detect if still on login screen
                }
            }
        }
    }

    private static let keyCodes: [String: UInt16] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
        "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
        "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
        "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    ]
}

#endif
