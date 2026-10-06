//
//  VMViewController.swift
//  virtualOS
//
//  Created by Jahn Bertsch.
//  Licensed under the Apache License, see LICENSE file.
//

import Virtualization
import OSLog
import AppKit

#if arch(arm64)

final class VMViewController: NSViewController {
    @IBOutlet var containerView: NSView!
    @IBOutlet weak var statusLabel: NSTextField!
    
    var vmBundle: VMBundle?
    var vmParameters: VMParameters?
    var onStartResult: ((Result<Void, Error>) -> Void)?
    var onStopped: (() -> Void)?
    fileprivate var vmConfiguration: VMConfiguration?
    fileprivate var vm: VZVirtualMachine?
    fileprivate var didAttemptStart = false
    fileprivate let vmView = VZVirtualMachineView()
    fileprivate let queue = DispatchQueue(label: "com.github.yep.virtualOS.vmqueue", qos: .userInitiated)
    
    // Used by the main window to decide whether an existing VM window can be reused.
    var hasActiveSession: Bool {
        guard let vm else {
            return false
        }
        return vm.state != .stopped && vm.state != .error
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        
        createVM()
        setupConstraints()
        
    }

    override func viewDidAppear() {
        super.viewDidAppear()
        beginSessionIfNeeded()
        let vf = vmView.frame
        let cf = containerView?.frame ?? .zero
        let wf = view.window?.frame ?? .zero
        let hasSup = vmView.superview != nil
        let vmState = vm?.state.rawValue ?? -1
        Logger.shared.log(level: .default, "DEBUG vmView=\(vf.debugDescription, privacy: .public) container=\(cf.debugDescription, privacy: .public) window=\(wf.debugDescription, privacy: .public) hasSuperView=\(hasSup, privacy: .public) vmState=\(vmState, privacy: .public)")
    }
    
    override func viewWillDisappear() {
        super.viewWillDisappear()
        stopVMIfNeeded(trigger: "window closing")
    }
    
    deinit {
        stopVMIfNeeded(trigger: "controller deinit")
    }
    
    // MARK: - Private
    
    fileprivate func createVM() {
        guard let bundleURL = vmBundle?.url,
              let vmParameters = vmParameters,
              let macPlatformConfiguration = MacPlatformConfiguration.read(fromBundleURL: bundleURL) else
        {
            Logger.shared.log(level: .default, "could not create vm config")
            return
        }
        
        let vmConfiguration = VMConfiguration()
        vmConfiguration.setup(parameters: vmParameters, macPlatformConfiguration: macPlatformConfiguration, bundleURL: bundleURL)
        self.vmConfiguration = vmConfiguration
        
        do {
            try vmConfiguration.validate()
            Logger.shared.log(level: .default, "vm configuration is valid, using \(vmParameters.cpuCount) cpus and \(vmParameters.memorySizeInGB) gb ram")
        } catch let error {
            let nsError = error as NSError
            Logger.shared.log(level: .default, "failed to validate vm configuration: \(nsError.domain, privacy: .public) code=\(nsError.code, privacy: .public) desc=\(nsError.localizedDescription, privacy: .public)")
            if let underlying = nsError.userInfo[NSUnderlyingErrorKey] as? NSError {
                Logger.shared.log(level: .default, "underlying error: \(underlying.domain, privacy: .public) code=\(underlying.code, privacy: .public) desc=\(underlying.localizedDescription, privacy: .public)")
            }
            return
        }
        
        let vm = VZVirtualMachine(configuration: vmConfiguration, queue: queue)
        vm.delegate = self
        
        self.vm = vm
        
        // Enable programmatic control for host interaction
        setupHostControl()
    }

    func beginSessionIfNeeded() {
        startVMIfNeeded()
    }

    fileprivate func startVMIfNeeded() {
        guard !didAttemptStart else {
            return
        }
        didAttemptStart = true

        queue.async { [weak self] in
            self?.vm?.start { (result: Result<Void, Error>) in
                switch result {
                case .success:
                    Logger.shared.log(level: .default, "running")
                    self?.onStartResult?(.success(()))
                case .failure(let error):
                    let nsError = error as NSError
                    Logger.shared.error("running failed domain=\(nsError.domain, privacy: .public) code=\(nsError.code, privacy: .public) desc=\(nsError.localizedDescription, privacy: .public)")
                    self?.handleStartFailure(error)
                    self?.onStartResult?(.failure(error))
                }
            }
        }
    }
    
    fileprivate func setupHostControl() {
        VMHostController.shared.registerVM(vm: vm, view: vmView, queue: queue)
        VMCommandServer.shared.start(view: vmView)
        if let password = resolvedAutoLoginPassword() {
            VMAutoLogin.shared.start(view: vmView, password: password)
        } else {
            Logger.shared.log(level: .default, "Auto-login disabled: no VOS_VM_PASSWORD configured")
        }
    }
    
    fileprivate func stopVMIfNeeded(trigger: String) {
        guard let vm else { return }
        VMAutoLogin.shared.stop()
        
        queue.async {
            guard vm.state != .stopped && vm.state != .error else {
                return
            }
            
            Logger.shared.log(level: .default, "Stopping VM (\(trigger))")
            vm.stop { error in
                if let error {
                    let nsError = error as NSError
                    Logger.shared.error("stop failed domain=\(nsError.domain, privacy: .public) code=\(nsError.code, privacy: .public) desc=\(nsError.localizedDescription, privacy: .public)")
                } else {
                    Logger.shared.log(level: .default, "VM stopped")
                }
            }
        }
    }

    fileprivate func resolvedAutoLoginPassword() -> String? {
        if let value = ProcessInfo.processInfo.environment["VOS_VM_PASSWORD"]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !value.isEmpty
        {
            return value
        }

        let envURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config", isDirectory: true)
            .appendingPathComponent("virtualos", isDirectory: true)
            .appendingPathComponent("start.env", isDirectory: false)

        guard let contents = try? String(contentsOf: envURL, encoding: .utf8) else {
            return nil
        }

        for rawLine in contents.components(separatedBy: .newlines) {
            var line = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !line.isEmpty, !line.hasPrefix("#") else {
                continue
            }

            if line.hasPrefix("export ") {
                line.removeFirst("export ".count)
                line = line.trimmingCharacters(in: .whitespacesAndNewlines)
            }

            let parts = line.split(separator: "=", maxSplits: 1).map(String.init)
            guard parts.count == 2, parts[0] == "VOS_VM_PASSWORD" else {
                continue
            }

            let value = parts[1].trimmingCharacters(in: .whitespacesAndNewlines)
            return unquotedSecret(value)
        }

        return nil
    }

    fileprivate func unquotedSecret(_ value: String) -> String {
        guard value.count >= 2 else {
            return value
        }

        if (value.hasPrefix("\"") && value.hasSuffix("\"")) ||
            (value.hasPrefix("'") && value.hasSuffix("'"))
        {
            return String(value.dropFirst().dropLast())
        }

        return value
    }
    
    fileprivate func handleStartFailure(_ error: Error) {
        let nsError = error as NSError
        let description = nsError.localizedDescription

        var messageText = "Failed to start virtual machine"
        var informativeText = description

        if nsError.domain == "VZErrorDomain",
           description.localizedCaseInsensitiveContains("Failed to lock auxiliary storage") {
            messageText = "VM is already in use"
            informativeText = "This VM bundle is locked by another running instance. Close any other virtualOS windows or app instances using the same VM, then start again."
        }

        DispatchQueue.main.async { [weak self] in
            self?.statusLabel.stringValue = messageText + ": " + informativeText
            let alert = NSAlert.okCancelAlert(messageText: messageText, informativeText: informativeText, showCancelButton: false)
            _ = alert.runModal()
        }
    }

    fileprivate func setupConstraints() {
        if let containerView  {
            // Fill the VM view with autoresizing to avoid legacy nib/autolayout edge cases
            // where constraints do not get applied and the VM view stays at a small default size.
            vmView.frame = containerView.bounds
            vmView.autoresizingMask = [.width, .height]
            vmView.translatesAutoresizingMaskIntoConstraints = true
            containerView.addSubview(vmView)

            if let vm {
                vmView.virtualMachine = vm
            }
            vmView.automaticallyReconfiguresDisplay = true
            vmView.capturesSystemKeys = true
            
            let centerX = NSLayoutConstraint(item: containerView, attribute: .centerX, relatedBy: .equal, toItem: statusLabel, attribute: .centerX, multiplier: 1, constant: 1)
            let centerY = NSLayoutConstraint(item: containerView, attribute: .centerY, relatedBy: .equal, toItem: statusLabel, attribute: .centerY, multiplier: 1, constant: 0)

            statusLabel.stringValue = ""
            statusLabel.removeFromSuperview()
            containerView.addSubview(statusLabel)
            containerView.addConstraint(centerX)
            containerView.addConstraint(centerY)
        }
    }
}

extension VMViewController: VZVirtualMachineDelegate {
    func guestDidStop(_ virtualMachine: VZVirtualMachine) {
        let message = "Guest did stop"
        DispatchQueue.main.async { [weak self] in
            self?.statusLabel.stringValue = message
        }
        Logger.shared.log(level: .default, "\(message)")
        onStopped?()
    }
    
    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: any Error) {
        let nsError = error as NSError
        DispatchQueue.main.async { [weak self] in
            self?.statusLabel.stringValue = "Guest did stop with error: \(error.localizedDescription)"
        }
        Logger.shared.error("guest stop error domain=\(nsError.domain, privacy: .public) code=\(nsError.code, privacy: .public) desc=\(nsError.localizedDescription, privacy: .public)")
        onStopped?()
    }
}

#endif
