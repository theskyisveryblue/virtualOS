//
//  ViewController.swift
//  virtualOS
//
//  Created by Jahn Bertsch.
//  Licensed under the Apache License, see LICENSE file.
//

import Cocoa
import Virtualization
import OSLog

#if arch(arm64)

final class MainViewController: NSViewController {
    fileprivate struct HostSnapshot {
        let freeDiskGB: UInt64
        let totalDiskGB: UInt64
        let usedDiskPercent: Int
    }

    fileprivate struct StartSafetyResult {
        let allowed: Bool
        let didChange: Bool
        let messageText: String?
        let informativeText: String?
    }

    @IBOutlet weak var tableView: NSTableView!
    @IBOutlet weak var vmNameTextField: NSTextField!
    @IBOutlet weak var parameterOutlineView: NSOutlineView!
    @IBOutlet weak var installButton: NSButton!
    @IBOutlet weak var startButton: NSButton!
    @IBOutlet weak var sharedFolderButton: NSButton!
    @IBOutlet weak var deleteButton: NSButton!
    @IBOutlet weak var cpuCountLabel: NSTextField!
    @IBOutlet weak var cpuCountSlider: NSSlider!
    @IBOutlet weak var ramLabel: NSTextField!
    @IBOutlet weak var ramSlider: NSSlider!
    
    fileprivate let mainStoryBoard = NSStoryboard(name: "Main", bundle: nil)
    fileprivate let viewModel = MainViewModel()
    fileprivate var diskImageSize = 1
    fileprivate var didAttemptAutoStart = false
    fileprivate var activeVMWindows: [String: NSWindow] = [:]
    fileprivate var activeVMWindowObservers: [String: NSObjectProtocol] = [:]
    fileprivate var shouldStartInBackground: Bool {
        ProcessInfo.processInfo.arguments.contains("--background-vm")
    }
    fileprivate var shouldForceAutoStartOnLaunch: Bool {
        shouldStartInBackground || ProcessInfo.processInfo.arguments.contains("--auto-start-vm")
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        
        tableView.dataSource            = viewModel.tableViewDataSource
        tableView.delegate              = self
        parameterOutlineView.dataSource = viewModel.parametersViewDataSource
        parameterOutlineView.delegate   = viewModel.parametersViewDelegate
        vmNameTextField.delegate        = viewModel.textFieldDelegate

        NotificationCenter.default.addObserver(self, selector: #selector(updateUI), name: NSApplication.didBecomeActiveNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(updateUI), name: NSControl.textDidEndEditingNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(updateUI), name: Constants.didChangeVMLocationNotification, object: nil)
        
        NotificationCenter.default.addObserver(self, selector: #selector(restoreImageSelected), name: Constants.restoreImageNameSelectedNotification, object: nil)

        ramSlider.target = self
        ramSlider.action = #selector(memorySliderChanged(sender:))
        cpuCountSlider.target = self
        cpuCountSlider.action = #selector(cpuCountChanged(sender:))
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
        for (_, token) in activeVMWindowObservers {
            NotificationCenter.default.removeObserver(token)
        }
        activeVMWindowObservers.removeAll()
    }

    override func viewWillAppear() {
        super.viewWillAppear()
        view.window?.delegate = self
        vmNameTextField.resignFirstResponder()
        beginAccessToVMFilesDirectory()
        self.updateUI()
        attemptAutoStartIfNeeded()
    }
    
    // Opens the saved security-scoped access to the VM files directory. A
    // headless/background launch skips viewWillAppear(), so the sandbox scope
    // must be opened explicitly before reading Parameters.txt.
    fileprivate func beginAccessToVMFilesDirectory() {
        guard let bookmarkURL = UserDefaults.standard.vmFilesDirectory else {
            return
        }
        if let bookmarkData = UserDefaults.standard.vmFilesDirectoryBookmarkData {
            if Bookmark.startAccess(bookmarkData: bookmarkData, for: bookmarkURL) != nil {
                return
            }
        }
        // No usable bookmark yet: create one from the configured path so a
        // headless launch can read Parameters.txt and the disk image.
        if let fresh = Bookmark.createBookmarkData(fromUrl: URL(fileURLWithPath: bookmarkURL)) {
            UserDefaults.standard.vmFilesDirectoryBookmarkData = fresh
            _ = Bookmark.startAccess(bookmarkData: fresh, for: bookmarkURL)
        }
    }

    @IBAction func startButtonPressed(_ sender: NSButton) {
        guard let vmBundle = viewModel.vmBundle,
              var vmParameters = viewModel.vmParameters else {
            return
        }
        let bundlePath = vmBundle.url.path
        Logger.shared.log(level: .default, "Starting VM bundle: \(bundlePath, privacy: .public)")

        if focusExistingVMWindowIfAvailable(bundlePath: bundlePath, bringToFront: !shouldStartInBackground) {
            return
        }

        let safety = applyStartSafetyGuardrails(vmParameters: &vmParameters)
        if !safety.allowed {
            if let messageText = safety.messageText, let informativeText = safety.informativeText {
                Logger.shared.error("VM start blocked: \(messageText, privacy: .public). \(informativeText, privacy: .public)")
                if !shouldStartInBackground {
                    let alert = NSAlert.okCancelAlert(messageText: messageText, informativeText: informativeText, showCancelButton: false)
                    _ = alert.runModal()
                }
            }
            return
        }

        if safety.didChange {
            viewModel.vmParameters = vmParameters
            viewModel.storeParametersToDisk()
            updateCpuCount(vmParameters)
            updateRam(vmParameters)
            updateLabels(setZero: false)
            updateOutlineView()
            if let messageText = safety.messageText, let informativeText = safety.informativeText {
                Logger.shared.log(level: .default, "\(messageText): \(informativeText)")
            }
        }

        if let vmViewController = mainStoryBoard.instantiateController(withIdentifier: "VMViewController") as? VMViewController {
            vmViewController.vmBundle = vmBundle
            vmViewController.vmParameters = vmParameters
            vmViewController.onStartResult = { [weak self] result in
                guard let self else {
                    return
                }
                if case .failure = result {
                    self.unregisterVMWindow(bundlePath: bundlePath)
                }
            }
            vmViewController.onStopped = { [weak self] in
                self?.unregisterVMWindow(bundlePath: bundlePath)
            }

            // Create a dedicated VM display window instead of reusing the main window controller.
            // Reusing the shared "NSWindowController" can open another copy of the control window.
            let vmWindow = NSWindow(contentViewController: vmViewController)
            vmWindow.title = vmBundle.name
            vmWindow.styleMask = [.titled, .closable, .miniaturizable, .resizable]
            vmWindow.setContentSize(NSSize(width: 980, height: 700))
            vmWindow.minSize = NSSize(width: 640, height: 420)
            vmWindow.isReleasedWhenClosed = true
            if shouldStartInBackground {
                // Virtualization stops producing display pixels for a fully off-screen window.
                // Keep the window renderable and place it behind normal host applications.
                vmWindow.center()
                vmWindow.orderBack(self)
            } else {
                vmWindow.center()
                vmWindow.makeKeyAndOrderFront(self)
                NSApp.activate(ignoringOtherApps: true)
            }
            registerVMWindow(vmWindow, bundlePath: bundlePath)
            vmViewController.beginSessionIfNeeded()
        } else {
            Logger.shared.log(level: .default, "show vm window failed")
        }
    }
    
    @IBAction func installButtonPressed(_ sender: NSButton) {
        if let windowController = mainStoryBoard.instantiateController(withIdentifier: "NSWindowController") as? NSWindowController,
           let restoreImageViewController = mainStoryBoard.instantiateController(withIdentifier: "RestoreImageViewController") as? RestoreImageViewController
        {
            windowController.showWindow(self)
            windowController.contentViewController = restoreImageViewController
            if let parentFrame = view.window?.frame,
               let childWindow = restoreImageViewController.view.window
            {
                childWindow.setFrame(parentFrame.offsetBy(dx: 200, dy: 10), display: true)
            }
        } else {
            Logger.shared.log(level: .default, "show restore image window failed")
        }
    }
    
    @IBAction func deleteButtonPressed(_ sender: Any) {
        guard let vmBundle = viewModel.vmBundle else {
            return
        }
        
        let alert: NSAlert = NSAlert.okCancelAlert(messageText: "Delete VM '\(vmBundle.name)'?", informativeText: "This can not be undone.", alertStyle: .warning)
        let selection = alert.runModal()
        if selection == NSApplication.ModalResponse.alertFirstButtonReturn ||
           selection == NSApplication.ModalResponse.OK
        {
            viewModel.deleteVM(selection: selection, vmBundle: vmBundle)
            viewModel.vmBundle = nil
            viewModel.vmParameters = nil
        }

        self.updateUI()
    }
        
    @IBAction func sharedFolderButtonPressed(_ sender: Any) {
        let openPanel = NSOpenPanel()
        openPanel.allowsMultipleSelection = false
        openPanel.canChooseDirectories = true
        openPanel.canChooseFiles = false
        openPanel.prompt = "Select"
        let modalResponse = openPanel.runModal()
        var sharedFolderURL: URL?
        if modalResponse == .OK,
           let selectedURL = openPanel.url
        {
            sharedFolderURL = selectedURL
        } else if modalResponse == .cancel {
            sharedFolderURL = nil
        }
        
        viewModel.set(sharedFolderUrl: sharedFolderURL)
        updateOutlineView()
    }
    
    @objc func restoreImageSelected(notification: Notification) {
        if let userInfo = notification.userInfo,
           let restoreImageName = userInfo[Constants.selectedRestoreImage] as? String
        {
            var isDownload = false
            if let url = URL(string: restoreImageName), (url.scheme == "http" || url.scheme == "https") {
                isDownload = true
            }
            
            if restoreImageName != Constants.restoreImageNameLatest && !isDownload {
                let accessoryView = NSTextField(frame: NSRect(x: 0, y: 0, width: 200, height: 20))
                accessoryView.stringValue = "\(UserDefaults.standard.diskSize)"
                
                let alert = NSAlert.okCancelAlert(messageText: "Disk Image Size in GB", informativeText: "Disk size can not be changed after VM is created. Minimum disk size is 30 GB. During install, a lot of RAM is used, ignore the warning about low system memory.", accessoryView: accessoryView)
                let modalResponse = alert.runModal()
                accessoryView.becomeFirstResponder()

                if modalResponse == .OK || modalResponse == .alertFirstButtonReturn  {
                    diskImageSize = Int(accessoryView.intValue)
                } else {
                    return // cancel install
                }
                if diskImageSize < 30 {
                    self.diskImageSize = 30
                }
            }
            
            showSheet(mode: .install, restoreImageName: restoreImageName, diskImageSize: self.diskImageSize)
        }
    }
    
    @objc func cpuCountChanged(sender: NSSlider) {
        updateUIAndStoreParametersToDisk()
    }
    
    @objc func memorySliderChanged(sender: NSSlider) {
        updateUIAndStoreParametersToDisk()
    }
        
    @objc func updateUI() {
        self.tableView.reloadData()
        cpuCountSlider.isEnabled = false
        ramSlider.isEnabled = false
        
        if let selectedRow = viewModel.selectedRow,
           let vmBundle = viewModel.tableViewDataSource.vmBundle(forRow: selectedRow)
        {
            vmNameTextField.stringValue = vmBundle.name
            tableView.selectRowIndexes(IndexSet(integer: selectedRow), byExtendingSelection: false)
            viewModel.vmBundle = vmBundle
            if let vmParameters = VMParameters.readFrom(url: vmBundle.url) {
                viewModel.vmParameters = vmParameters
                viewModel.textFieldDelegate.vmBundle = vmBundle
                updateButtons(enabled: true)
                updateLabels(setZero: false)
                updateCpuCount(vmParameters)
                updateRam(vmParameters)
            }
        } else {
            vmNameTextField.stringValue = ""
            viewModel.selectedRow = 0
            updateButtons(enabled: false)
            updateLabels(setZero: true)
        }

        updateOutlineView()
    }
    
    func showErrorAlert(error: Error) {
        var messageText = "Error"
        var informativeText = "An unknown error occurred."
        
        if let vzError = error as? VZError,
           let reason = vzError.userInfo[NSLocalizedFailureErrorKey] as? String,
           let failureReason = vzError.userInfo[NSLocalizedFailureReasonErrorKey] as? String,
           let underlyingError = vzError.userInfo[NSUnderlyingErrorKey] as? NSError
        {
            messageText = failureReason
            informativeText = reason + " " + underlyingError.localizedDescription + "\n\n(Error Code: \(vzError.errorCode), Underlying Error Domain: \(underlyingError.domain), Underlying Error Code: \(underlyingError.code))"
            Logger.shared.log(level: .default, "vz error: \(messageText) \(informativeText)")
        } else {
            informativeText = error.localizedDescription
            Logger.shared.log(level: .default, "error: \(error.localizedDescription)")
        }

        let alert = NSAlert.okCancelAlert(messageText: messageText, informativeText: informativeText, showCancelButton: false)
        let _ = alert.runModal()
    }
    
    // MARK: - Private

    fileprivate func updateButtons(enabled: Bool) {
        installButton.isEnabled = true
        startButton.isEnabled = enabled
        sharedFolderButton.isEnabled = enabled
        deleteButton.isEnabled = enabled
        vmNameTextField.isEnabled = enabled
    }
    
    fileprivate func updateCpuCount(_ vmParameters: VMParameters) {
        cpuCountSlider.minValue = Double(vmParameters.cpuCountMin)
        cpuCountSlider.maxValue = Double(vmParameters.cpuCountMax)
        cpuCountSlider.numberOfTickMarks = Int(cpuCountSlider.maxValue - cpuCountSlider.minValue)
        cpuCountSlider.doubleValue = Double(vmParameters.cpuCount)
        cpuCountSlider.isEnabled = true
    }
    
    fileprivate func updateRam(_ vmParameters: VMParameters) {
        ramSlider.minValue = max(Double(vmParameters.memorySizeInGBMin), 2.0)
        ramSlider.maxValue = Double(vmParameters.memorySizeInGBMax)
        ramSlider.numberOfTickMarks = Int(ramSlider.maxValue - ramSlider.minValue)
        ramSlider.doubleValue = Double(vmParameters.memorySizeInGB)
        ramSlider.isEnabled = true
    }
    
    fileprivate func updateLabels(setZero: Bool) {
        let cpuCount = Int(round(cpuCountSlider.doubleValue))
        let memorySizeInGB = Int(round(ramSlider.doubleValue))
        viewModel.vmParameters?.cpuCount = cpuCount
        viewModel.vmParameters?.memorySizeInGB = UInt64(memorySizeInGB)
        
        if setZero {
            cpuCountLabel.stringValue = "CPU Count"
            ramLabel.stringValue = "RAM"
        } else {
            cpuCountLabel.stringValue = "CPU Count: \(cpuCount)"
            ramLabel.stringValue = "RAM: \(memorySizeInGB) GB"
        }
    }
    
    fileprivate func updateOutlineView() {
        parameterOutlineView.reloadData()
    }
    
    fileprivate func updateUIAndStoreParametersToDisk() {
        viewModel.storeParametersToDisk()
        updateLabels(setZero: false)
        updateOutlineView()
    }

    fileprivate func hostSnapshot() -> HostSnapshot? {
        let path = "/System/Volumes/Data"
        guard let attributes = try? FileManager.default.attributesOfFileSystem(forPath: path),
              let totalBytes = attributes[.systemSize] as? NSNumber,
              let freeBytes = attributes[.systemFreeSize] as? NSNumber
        else {
            return nil
        }

        let totalGB = UInt64(totalBytes.uint64Value).bytesToGigabytes()
        let freeGB = UInt64(freeBytes.uint64Value).bytesToGigabytes()
        guard totalGB > 0 else {
            return nil
        }

        let usedDiskPercent = Int(round((Double(totalGB - freeGB) / Double(totalGB)) * 100.0))
        return HostSnapshot(freeDiskGB: freeGB, totalDiskGB: totalGB, usedDiskPercent: usedDiskPercent)
    }

    fileprivate func applyStartSafetyGuardrails(vmParameters: inout VMParameters) -> StartSafetyResult {
        let hostCPUCount = max(1, ProcessInfo.processInfo.processorCount)
        let hostMemoryGB = max(UInt64(1), ProcessInfo.processInfo.physicalMemory.bytesToGigabytes())

        // Keep some host headroom while allowing meaningful tuning on capable machines.
        var cpuCap = max(
            vmParameters.cpuCountMin,
            min(vmParameters.cpuCountMax, max(2, min(8, hostCPUCount / 2)))
        )
        var memoryCap = max(
            vmParameters.memorySizeInGBMin,
            min(vmParameters.memorySizeInGBMax, max(UInt64(6), min(UInt64(12), hostMemoryGB / 2)))
        )

        if let snapshot = hostSnapshot() {
            if snapshot.freeDiskGB < 10 {
                return StartSafetyResult(
                    allowed: false,
                    didChange: false,
                    messageText: "Not enough disk space to safely run the VM",
                    informativeText: "The host only has \(snapshot.freeDiskGB) GB free (\(snapshot.usedDiskPercent)% used). Free at least 20 GB before starting, otherwise the VM can crash due to host pressure."
                )
            }
            if snapshot.freeDiskGB < 15 {
                cpuCap = min(cpuCap, 3)
                memoryCap = min(memoryCap, UInt64(6))
            } else if snapshot.freeDiskGB < 20 {
                cpuCap = min(cpuCap, 4)
                memoryCap = min(memoryCap, UInt64(8))
            }
        }

        let clampedCPU = max(vmParameters.cpuCountMin, min(vmParameters.cpuCount, cpuCap))
        let clampedMemory = max(vmParameters.memorySizeInGBMin, min(vmParameters.memorySizeInGB, memoryCap))
        let requestedCPU = vmParameters.cpuCount
        let requestedMemory = vmParameters.memorySizeInGB

        if let snapshot = hostSnapshot() {
            Logger.shared.log(level: .default, "start safety host free=\(snapshot.freeDiskGB)GB used=\(snapshot.usedDiskPercent)% total=\(snapshot.totalDiskGB)GB hostCPU=\(hostCPUCount) hostRAM=\(hostMemoryGB)GB capCPU=\(cpuCap) capRAM=\(memoryCap) reqCPU=\(requestedCPU) reqRAM=\(requestedMemory) finalCPU=\(clampedCPU) finalRAM=\(clampedMemory)")
        } else {
            Logger.shared.log(level: .default, "start safety hostCPU=\(hostCPUCount) hostRAM=\(hostMemoryGB)GB capCPU=\(cpuCap) capRAM=\(memoryCap) reqCPU=\(requestedCPU) reqRAM=\(requestedMemory) finalCPU=\(clampedCPU) finalRAM=\(clampedMemory)")
        }

        let didChange = clampedCPU != vmParameters.cpuCount || clampedMemory != vmParameters.memorySizeInGB
        vmParameters.cpuCount = clampedCPU
        vmParameters.memorySizeInGB = clampedMemory

        guard didChange else {
            return StartSafetyResult(allowed: true, didChange: false, messageText: nil, informativeText: nil)
        }

        return StartSafetyResult(
            allowed: true,
            didChange: true,
            messageText: "Applied a safer VM profile",
            informativeText: "Adjusted this VM to \(clampedCPU) CPU and \(clampedMemory) GB RAM for better stability on the current host."
        )
    }
    
    fileprivate func updateButtonEnabledState() {
        var enabled = false
        if viewModel.selectedRow != nil {
            enabled = true
        }
        vmNameTextField.isEnabled    = enabled
        installButton.isEnabled      = true
        startButton.isEnabled        = enabled
        sharedFolderButton.isEnabled = enabled
        deleteButton.isEnabled       = enabled
        ramSlider.isEnabled          = enabled
        cpuCountSlider.isEnabled     = enabled
    }

    fileprivate func showSheet(mode: ProgressViewController.Mode, restoreImageName: String?, diskImageSize: Int?)  {
        if let progressWindowController = mainStoryBoard.instantiateController(withIdentifier: "ProgressWindowController") as? NSWindowController,
           let progressWindow = progressWindowController.window
        {
            if let progressViewController = progressWindow.contentViewController as? ProgressViewController {
                progressViewController.mode = mode
                progressViewController.diskImageSize = diskImageSize
                progressViewController.restoreImageName = restoreImageName
                presentAsSheet(progressViewController)
            }
        } else {
            Logger.shared.log(level: .default, "show modal failed")
        }
    }

    fileprivate func focusExistingVMWindowIfAvailable(bundlePath: String, bringToFront: Bool = true) -> Bool {
        guard let existingWindow = activeVMWindows[bundlePath] else {
            unregisterVMWindow(bundlePath: bundlePath)
            return false
        }

        if let vmViewController = existingWindow.contentViewController as? VMViewController,
           !vmViewController.hasActiveSession
        {
            Logger.shared.log(level: .default, "Found stale VM window for \(bundlePath). Closing it and starting a fresh session.")
            existingWindow.close()
            unregisterVMWindow(bundlePath: bundlePath)
            return false
        }

        guard bringToFront else {
            Logger.shared.log(level: .default, "VM for \(bundlePath) is already open in background")
            return true
        }

        if existingWindow.isMiniaturized {
            existingWindow.deminiaturize(self)
        }
        existingWindow.makeKeyAndOrderFront(self)
        NSApp.activate(ignoringOtherApps: true)
        Logger.shared.log(level: .default, "VM for \(bundlePath) is already open, focusing existing window")
        return true
    }

    fileprivate func registerVMWindow(_ window: NSWindow, bundlePath: String) {
        unregisterVMWindow(bundlePath: bundlePath)
        activeVMWindows[bundlePath] = window
        let token = NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: window, queue: .main) { [weak self] _ in
            self?.unregisterVMWindow(bundlePath: bundlePath)
        }
        activeVMWindowObservers[bundlePath] = token
    }

    fileprivate func unregisterVMWindow(bundlePath: String) {
        activeVMWindows.removeValue(forKey: bundlePath)
        if let token = activeVMWindowObservers.removeValue(forKey: bundlePath) {
            NotificationCenter.default.removeObserver(token)
        }
    }

    fileprivate func attemptAutoStartIfNeeded() {
        guard !didAttemptAutoStart else {
            return
        }
        didAttemptAutoStart = true

        guard UserDefaults.standard.autoStartLastVMOnLaunch || shouldForceAutoStartOnLaunch else {
            return
        }

        guard viewModel.tableViewDataSource.rows() > 0 else {
            return
        }

        if viewModel.selectedRow == nil {
            viewModel.selectedRow = 0
        }

        // A headless/background launch may never present the window, so
        // viewWillAppear() cannot be relied on to open the sandbox scope or
        // populate the selected bundle and its parameters. Do both here.
        beginAccessToVMFilesDirectory()
        updateUI()

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            guard let self else {
                return
            }
            guard self.startButton.isEnabled else {
                return
            }
            Logger.shared.log(level: .default, "Auto-starting selected VM on app launch")
            self.startButtonPressed(self.startButton)
        }
    }

    func startSelectedVMFromLaunchIfNeeded() {
        guard !didAttemptAutoStart else {
            return
        }
        didAttemptAutoStart = true

        guard viewModel.tableViewDataSource.rows() > 0 else {
            return
        }

        if viewModel.selectedRow == nil {
            viewModel.selectedRow = 0
        }

        // A headless launch may instantiate the controller without ever presenting
        // the main window, so viewWillAppear() cannot be relied on to populate state.
        updateUI()

        guard startButton.isEnabled else {
            Logger.shared.error("Direct launch could not start the selected VM because its configuration is unavailable")
            return
        }

        Logger.shared.log(level: .default, "Direct launch auto-start requested")
        startButtonPressed(startButton)
    }
}

extension MainViewController: NSTableViewDelegate {
    func tableViewSelectionDidChange(_ notification: Notification) {
        var row: Int? = nil
        
        if let userInfo = notification.userInfo,
            let indexSet = userInfo["NSTableViewCurrentRowSelectionUserInfoKey"] as? NSIndexSet {
            if indexSet.count > 0 {
                row = indexSet.firstIndex
            }
        }
        
        if let row  = row {
            viewModel.selectedRow = row
            updateUI()
            updateButtonEnabledState()
        }
    }
}

extension MainViewController: NSWindowDelegate  {
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        let alert: NSAlert = NSAlert.okCancelAlert(messageText: "Quit", informativeText: "Quitting the app will stop all virtual machines.", alertStyle: .warning)
        let selection = alert.runModal()
        if selection == NSApplication.ModalResponse.alertFirstButtonReturn ||
           selection == NSApplication.ModalResponse.OK
        {
            NSApplication.shared.terminate(self)
            return true
        } else {
            return false
        }
    }
}

#else

// minimum implementation used for intel cpus

final class MainViewController: NSViewController {
    @IBOutlet weak var vmNameTextField: NSTextField!
    @IBOutlet weak var installButton: NSButton!
    @IBOutlet weak var startButton: NSButton!
    @IBOutlet weak var sharedFolderButton: NSButton!
    @IBOutlet weak var deleteButton: NSButton!
    @IBOutlet weak var cpuCountLabel: NSTextField!
    @IBOutlet weak var cpuCountSlider: NSSlider!
    @IBOutlet weak var ramLabel: NSTextField!
    @IBOutlet weak var ramSlider: NSSlider!

    override func viewWillAppear() {
        super.viewWillAppear()
        vmNameTextField.stringValue = "Virtualization requires an Apple Silicon machine"
        vmNameTextField.isEditable = false
        installButton.isEnabled = false
        startButton.isEnabled = false
        sharedFolderButton.isEnabled = false
        deleteButton.isEnabled = false
        cpuCountSlider.isEnabled = false
        ramSlider.isEnabled = false
        cpuCountLabel.stringValue = ""
        ramLabel.stringValue = ""
    }
}

#endif

// place all code before #else
