//
//  VM.swift
//  virtualOS
//
//  Created by Jahn Bertsch.
//  Licensed under the Apache License, see LICENSE file.
//

#if arch(arm64)

import Virtualization
import AVFoundation // for audio
import OSLog
import Foundation

private final class NBDLabConnectionObserver: NSObject, VZNetworkBlockDeviceStorageDeviceAttachmentDelegate {
    func attachmentWasConnected(_ attachment: VZNetworkBlockDeviceStorageDeviceAttachment) {
        Logger.shared.log(level: .default, "NBD lab connected")
    }

    func attachment(_ attachment: VZNetworkBlockDeviceStorageDeviceAttachment, didEncounterError error: Error) {
        let failure = error as NSError
        Logger.shared.error("NBD lab fatal error domain=\(failure.domain, privacy: .public) code=\(failure.code, privacy: .public)")
    }
}

final class VMConfiguration: VZVirtualMachineConfiguration {
    // The attachment's delegate is weak; the VM controller retains this configuration.
    private var nbdConnectionObserver: NBDLabConnectionObserver?

    /// Experimental: report whether this host can run a hypervisor *inside* a guest.
    ///
    /// Nested virtualization requires macOS 15+, Apple silicon (M3+/M4), and an
    /// entitlement/policy that permits it. Callers use this before building a
    /// fleet of VMs that themselves host VMs.
    static func nestedVirtualizationCapability() -> (supported: Bool, reason: String) {
        if #available(macOS 15.0, *) {
            let supported = VZGenericPlatformConfiguration.isNestedVirtualizationSupported
            let reason = supported
                ? "nested virtualization supported on this host"
                : "nested virtualization not supported (hardware/policy)"
            return (supported, reason)
        }
        return (false, "nested virtualization requires macOS 15 or newer")
    }

    func setup(parameters: VMParameters, macPlatformConfiguration: VZMacPlatformConfiguration, bundleURL: URL) {
        cpuCount        = parameters.cpuCount
        memorySize      = parameters.memorySizeInGB.gigabytesToBytes()
        pointingDevices = [VZUSBScreenCoordinatePointingDeviceConfiguration()]
        entropyDevices  = [VZVirtioEntropyDeviceConfiguration()]
        keyboards       = [VZUSBKeyboardConfiguration()]
        bootLoader      = VZMacOSBootLoader()

        configureAudioDevice(parameters: parameters)
        configureGraphicsDevice(parameters: parameters)
        configureStorageDevice(parameters: parameters, bundleURL: bundleURL)
        configureNetworkDevices(parameters: parameters)
        configureSharedFolder(parameters: parameters)
        configureClipboardSharing()
        configureVirtioSocket()
        configureUSB()

        platform = macPlatformConfiguration

        configureNestedVirtualization(parameters: parameters)
    }

    /// Experimental fleet support: when the host and platform allow it, expose a
    /// hypervisor to the guest so it can run its own VMs.
    fileprivate func configureNestedVirtualization(parameters: VMParameters) {
        guard parameters.nestedVirtualizationEnabled else { return }
        let capability = Self.nestedVirtualizationCapability()
        guard capability.supported else {
            Logger.shared.log(level: .default, "nested virtualization requested but unavailable: \(capability.reason)")
            return
        }
        if #available(macOS 15.0, *) {
            if let generic = platform as? VZGenericPlatformConfiguration {
                generic.isNestedVirtualizationEnabled = true
                Logger.shared.log(level: .default, "nested virtualization enabled (generic platform)")
            } else {
                Logger.shared.log(level: .default, "nested virtualization requested on a non-generic platform; ignored")
            }
        }
    }

    func setDefault(parameters: inout VMParameters) {
        let cpuCountMax = computeCPUCount()
        let bytesMax = VZVirtualMachineConfiguration.maximumAllowedMemorySize
        let hostMemoryGB = ProcessInfo.processInfo.physicalMemory.bytesToGigabytes()
        let minCPUCount = VZVirtualMachineConfiguration.minimumAllowedCPUCount
        let minMemoryGB = VZVirtualMachineConfiguration.minimumAllowedMemorySize.bytesToGigabytes()
        let maxMemoryGB = bytesMax.bytesToGigabytes()

        // Favor stability over peak throughput for defaults.
        let recommendedCPU = max(minCPUCount, min(cpuCountMax, max(2, min(6, cpuCountMax / 3))))
        let recommendedMemoryGB = max(minMemoryGB, min(maxMemoryGB, max(4, min(12, hostMemoryGB / 3))))

        cpuCount = recommendedCPU
        memorySize = recommendedMemoryGB.gigabytesToBytes()

        parameters.cpuCount = cpuCount
        parameters.cpuCountMax = cpuCountMax
        parameters.memorySizeInGB = memorySize.bytesToGigabytes()
        parameters.memorySizeInGBMax = bytesMax.bytesToGigabytes()
    }

    // MARK: - Private

    fileprivate func configureAudioDevice(parameters: VMParameters) {
        let audioDevice = VZVirtioSoundDeviceConfiguration()

        if parameters.microphoneEnabled {
            AVCaptureDevice.requestAccess(for: .audio) { (granted: Bool) in
                Logger.shared.log(level: .default, "microphone request granted: \(granted)")
            }

            let inputStreamConfiguration = VZVirtioSoundDeviceInputStreamConfiguration()
            inputStreamConfiguration.source = VZHostAudioInputStreamSource()
            audioDevice.streams.append(inputStreamConfiguration)
        }

        let outputStreamConfiguration = VZVirtioSoundDeviceOutputStreamConfiguration()
        outputStreamConfiguration.sink = VZHostAudioOutputStreamSink()
        audioDevice.streams.append(outputStreamConfiguration)

        audioDevices = [audioDevice]
    }

    fileprivate func configureGraphicsDevice(parameters: VMParameters) {
        let graphicsDevice = VZMacGraphicsDeviceConfiguration()
        // Always use explicit pixel display config.
        // The `for:mainScreen` mode can go black on some multi-monitor setups.
        let width = max(parameters.screenWidth, 1280)
        let height = max(parameters.screenHeight, 800)
        let ppi = max(72, min(parameters.pixelsPerInch, 220))

        graphicsDevice.displays = [VZMacGraphicsDisplayConfiguration(
            widthInPixels: width,
            heightInPixels: height,
            pixelsPerInch: ppi
        )]
        Logger.shared.log(level: .default, "graphics display config width=\(width) height=\(height) ppi=\(ppi)")
        graphicsDevices = [graphicsDevice]
    }

    fileprivate func configureStorageDevice(parameters: VMParameters, bundleURL: URL) {
        do {
            if ProcessInfo.processInfo.arguments.contains("--nbd-r2-lab") {
                guard !ProcessInfo.processInfo.arguments.contains("--nbd-clone-lab"),
                      bundleURL.isR2LabMetadataBundle else {
                    Logger.shared.error("NBD R2 lab refused: metadata-only bundle required")
                    return
                }
                let endpoint = URL(string: "nbd://127.0.0.1:40809/virtualos-test")!
                let attachment = try VZNetworkBlockDeviceStorageDeviceAttachment(
                    url: endpoint, timeout: 30, isForcedReadOnly: false,
                    synchronizationMode: .full)
                let observer = NBDLabConnectionObserver()
                attachment.delegate = observer
                nbdConnectionObserver = observer
                storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: attachment)]
                Logger.shared.log(level: .default, "NBD R2 lab attached metadata-only bundle")
                return
            }
            if ProcessInfo.processInfo.arguments.contains("--nbd-clone-lab") {
                let expected = FileManager.default.homeDirectoryForCurrentUser
                    .appendingPathComponent("VirtualOSMachines/.virtualOS-r2-staging.bundle")
                    .standardizedFileURL
                guard bundleURL.standardizedFileURL == expected,
                      FileManager.default.fileExists(atPath: expected.appendingPathComponent(".complete").path) else {
                    Logger.shared.error("NBD clone lab refused: isolated completed clone required")
                    return
                }
                let endpoint = URL(string: "nbd://127.0.0.1:40809/virtualos-test")!
                let attachment = try VZNetworkBlockDeviceStorageDeviceAttachment(
                    url: endpoint, timeout: 30, isForcedReadOnly: false,
                    synchronizationMode: .full)
                let observer = NBDLabConnectionObserver()
                attachment.delegate = observer
                nbdConnectionObserver = observer
                storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: attachment)]
                Logger.shared.log(level: .default, "NBD clone lab attached isolated disk")
                return
            }
            let diskImageStorageDeviceAttachment = try VZDiskImageStorageDeviceAttachment(url: bundleURL.bootDiskImageURL, readOnly: false)
            let blockDeviceConfiguration = VZVirtioBlockDeviceConfiguration(attachment: diskImageStorageDeviceAttachment)
            storageDevices = [blockDeviceConfiguration]
        } catch let error {
            Logger.shared.log(level: .default, "could not create storage device: \(error.localizedDescription)")
        }
    }

    fileprivate func configureNetworkDevices(parameters: VMParameters) {
        let networkDevice = VZVirtioNetworkDeviceConfiguration()

        // Use NAT networking - bridged networking requires the restricted
        // "com.apple.vm.networking" entitlement which is only available to
        // Apple-approved apps
        let networkAttachment = VZNATNetworkDeviceAttachment()
        Logger.shared.log(level: .default, "Using NAT networking")

        networkDevice.attachment = networkAttachment
        networkDevice.macAddress = VZMACAddress(string: parameters.macAddress) ?? .randomLocallyAdministered()
        networkDevices = [networkDevice]
    }

    fileprivate func configureSharedFolder(parameters: VMParameters) {
        guard let sharedFolderURL = parameters.sharedFolderURL,
              let sharedFolderBookmarkData = Bookmark.startAccess(bookmarkData: parameters.sharedFolderData, for: sharedFolderURL.absoluteString) else
        {
            return
        }

        let sharedDirectory = VZSharedDirectory(url: sharedFolderBookmarkData, readOnly: false)
        let singleDirectoryShare = VZSingleDirectoryShare(directory: sharedDirectory)
        let sharingConfiguration = VZVirtioFileSystemDeviceConfiguration(tag: VZVirtioFileSystemDeviceConfiguration.macOSGuestAutomountTag)
        sharingConfiguration.share = singleDirectoryShare

        directorySharingDevices = [sharingConfiguration]
    }

    fileprivate func configureClipboardSharing() {
        let consoleDevice = VZVirtioConsoleDeviceConfiguration()

        let spiceAgentPortConfiguration = VZVirtioConsolePortConfiguration()
        spiceAgentPortConfiguration.name = VZSpiceAgentPortAttachment.spiceAgentPortName
        spiceAgentPortConfiguration.attachment = VZSpiceAgentPortAttachment()
        consoleDevice.ports[0] = spiceAgentPortConfiguration

        consoleDevices.append(consoleDevice)
    }

    fileprivate func configureVirtioSocket() {
        let socketDevice = VZVirtioSocketDeviceConfiguration()
        socketDevices = [socketDevice]
        Logger.shared.log(level: .default, "VirtioSocket configured (host<->guest channel)")
    }

    fileprivate func configureUSB() {
        let usbControllerConfiguration = VZXHCIControllerConfiguration()
        usbControllers = [usbControllerConfiguration]
    }

    fileprivate func computeCPUCount() -> Int {
        let totalAvailableCPUs = ProcessInfo.processInfo.processorCount

        var virtualCPUCount = totalAvailableCPUs <= 1 ? 1 : totalAvailableCPUs
        virtualCPUCount = max(virtualCPUCount, VZVirtualMachineConfiguration.minimumAllowedCPUCount)
        virtualCPUCount = min(virtualCPUCount, VZVirtualMachineConfiguration.maximumAllowedCPUCount)

        return virtualCPUCount
    }
}

#endif
