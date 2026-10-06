//
//  VMHostController.swift
//  virtualOS
//
//  Allows host computer to programmatically control the VM
//  Especially useful when VM freezes - host can send commands
//

import Foundation
import Virtualization
import OSLog
import AppKit

#if arch(arm64)

class VMHostController {
    static let shared = VMHostController()
    
    private var registeredVM: VZVirtualMachine?
    private var registeredView: VZVirtualMachineView?
    private var registeredVMQueue: DispatchQueue?
    private var serialPortHandle: FileHandle?
    
    private init() {}
    
    func registerVM(vm: VZVirtualMachine?, view: VZVirtualMachineView?, queue: DispatchQueue) {
        self.registeredVM = vm
        self.registeredView = view
        self.registeredVMQueue = queue
        Logger.shared.log(level: .default, "VM registered for host control")
    }

    @discardableResult
    func requestStop() -> Bool {
        guard let vm = registeredVM,
              let queue = registeredVMQueue else {
            return false
        }

        queue.async {
            guard vm.state != .stopped,
                  vm.state != .stopping,
                  vm.state != .error else {
                return
            }
            Logger.shared.log(level: .default, "VM stop requested through native control")
            vm.stop { error in
                if let error {
                    Logger.shared.error("native stop failed: \(error.localizedDescription, privacy: .public)")
                } else {
                    Logger.shared.log(level: .default, "VM stopped through native control")
                }
            }
        }
        return true
    }
    
    // Send keyboard input to VM programmatically
    func sendKeyboardInput(_ text: String) {
        guard registeredVM != nil else {
            Logger.shared.log(level: .default, "⚠️  No VM registered for keyboard input")
            return
        }
        
        // Use serial port or console to send input
        if let handle = serialPortHandle {
            if let data = text.data(using: .utf8) {
                handle.write(data)
                Logger.shared.log(level: .default, "✅ Sent keyboard input to VM: \(text)")
            }
        } else {
            Logger.shared.log(level: .default, "⚠️  Serial port not available for keyboard input")
        }
    }
    
    // Send mouse click to VM
    func sendMouseClick(at point: CGPoint, button: Int = 0) {
        guard registeredView != nil else {
            Logger.shared.log(level: .default, "⚠️  No VM view registered for mouse input")
            return
        }
        
        // Convert point to VM coordinates and send click
        // Note: This requires additional implementation with CGEvent
        let x = point.x
        let y = point.y
        Logger.shared.log(level: .default, "Mouse click at (\(x), \(y)) - requires CGEvent implementation")
    }
    
    // Take screenshot of VM
    func takeScreenshot() -> NSImage? {
        guard let view = registeredView else {
            Logger.shared.log(level: .default, "⚠️  No VM view registered for screenshot")
            return nil
        }
        
        // Capture the view's content
        let size = view.bounds.size
        let image = NSImage(size: size)
        image.lockFocus()
        if let context = NSGraphicsContext.current {
            view.layer?.render(in: context.cgContext)
        }
        image.unlockFocus()
        
        Logger.shared.log(level: .default, "✅ Screenshot captured")
        return image
    }
    
    // Send command to VM via serial port
    func sendCommand(_ command: String) {
        sendKeyboardInput(command + "\n")
    }
    
    // Restart VM
    func restartVM() {
        guard let vm = registeredVM else { return }
        
        vm.stop { error in
            if let error = error {
                Logger.shared.log(level: .default, "Error stopping VM: \(error.localizedDescription)")
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                    vm.start { result in
                        switch result {
                        case .success:
                            Logger.shared.log(level: .default, "✅ VM restarted")
                        case .failure(let error):
                            Logger.shared.log(level: .default, "Error restarting VM: \(error.localizedDescription)")
                        }
                    }
                }
            }
        }
    }
    
    // Get VM status
    func getVMStatus() -> String {
        guard let vm = registeredVM,
              let queue = registeredVMQueue else {
            return "No VM registered"
        }

        let state = queue.sync { vm.state }
        switch state {
        case .stopped:
            return "Stopped"
        case .starting:
            return "Starting"
        case .running:
            return "Running"
        case .paused:
            return "Paused"
        case .pausing:
            return "Pausing"
        case .resuming:
            return "Resuming"
        case .stopping:
            return "Stopping"
        case .saving:
            return "Saving"
        case .error:
            return "Error"
        case .restoring:
            return "Restoring"
        @unknown default:
            return "Unknown"
        }
    }
}

#endif
