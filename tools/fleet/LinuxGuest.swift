//
//  LinuxGuest.swift
//  virtualOS experimental Linux-on-Virtualization.framework helper
//
//  Standalone proof-of-concept for the "Linux, not only macOS" manifesto theme.
//  The shipping virtualOS.app only boots macOS guests (VZMacOSBootLoader). This
//  helper is the additive counterpart: it builds a VZVirtualMachineConfiguration
//  around a VZLinuxBootLoader so a generic Linux kernel + initrd + rootfs image
//  can be booted headlessly with a serial console on stdout.
//
//  This is EXPERIMENTAL and was authored without a real kernel/rootfs to boot
//  against. `--check` validates config/paths and prints what *would* boot; only
//  `run` actually starts a VM, and it blocks until the guest exits.
//
//  Build:
//    swiftc -framework Virtualization -framework Foundation \
//      tools/fleet/LinuxGuest.swift -o tools/fleet/linux-guest
//
//  Check (no boot):
//    tools/fleet/linux-guest --check --kernel /path/vmlinuz \
//      [--initrd /path/initrd.img] [--disk /path/rootfs.img] \
//      [--cmdline "console=hvc0 root=/dev/vda rw"] [--cpu 2] [--memory 2]
//
//  Run (boots and blocks):
//    tools/fleet/linux-guest --kernel ... --disk ... [--initrd ...]
//

import Foundation
import Virtualization

// MARK: - Config

/// Everything VZLinuxBootLoader needs, plus the generic platform's CPU/RAM.
struct LinuxGuestConfig {
    var kernelPath: String
    var initrdPath: String?
    var cmdLine: String
    var diskPath: String?
    var cpuCount: Int
    var memoryGB: Int

    init(
        kernelPath: String,
        initrdPath: String? = nil,
        cmdLine: String = "console=hvc0 root=/dev/vda rw",
        diskPath: String? = nil,
        cpuCount: Int = 2,
        memoryGB: Int = 2
    ) {
        self.kernelPath = kernelPath
        self.initrdPath = initrdPath
        self.cmdLine = cmdLine
        self.diskPath = diskPath
        self.cpuCount = cpuCount
        self.memoryGB = memoryGB
    }

    var kernelURL: URL { URL(fileURLWithPath: (kernelPath as NSString).expandingTildeInPath) }
    var initrdURL: URL? {
        initrdPath.map { URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath) }
    }
    var diskURL: URL? {
        diskPath.map { URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath) }
    }
}

// MARK: - Validation

enum LinuxGuestValidationError: Error, CustomStringConvertible {
    case missingKernel(String)
    case missingInitrd(String)
    case missingDisk(String)
    case badCPU(String)
    case badMemory(String)

    var description: String {
        switch self {
        case .missingKernel(let p): return "kernel not found: \(p)"
        case .missingInitrd(let p): return "initrd not found: \(p)"
        case .missingDisk(let p): return "disk image not found: \(p)"
        case .badCPU(let s): return "invalid cpu count: \(s)"
        case .badMemory(let s): return "invalid memory size (GB): \(s)"
        }
    }
}

// MARK: - Guest

/// A single headless Linux guest. Owns its serial queue so `VZVirtualMachine`
/// is only ever touched from one thread, mirroring FleetInstance's discipline.
final class LinuxGuest: NSObject {
    let config: LinuxGuestConfig
    private let queue = DispatchQueue(label: "virtualos.linuxguest")
    private var vm: VZVirtualMachine?

    init(config: LinuxGuestConfig) {
        self.config = config
        super.init()
    }

    private func log(_ line: String) {
        FileHandle.standardError.write(Data(("[linux-guest] " + line + "\n").utf8))
    }

    /// Validate that every referenced path exists and sizes are sane. Throws a
    /// clear error instead of failing deep inside Virtualization.framework.
    func validate() throws {
        guard FileManager.default.fileExists(atPath: config.kernelURL.path) else {
            throw LinuxGuestValidationError.missingKernel(config.kernelURL.path)
        }
        if let initrd = config.initrdURL,
           !FileManager.default.fileExists(atPath: initrd.path) {
            throw LinuxGuestValidationError.missingInitrd(initrd.path)
        }
        if let disk = config.diskURL,
           !FileManager.default.fileExists(atPath: disk.path) {
            throw LinuxGuestValidationError.missingDisk(disk.path)
        }
        guard config.cpuCount >= 1 else {
            throw LinuxGuestValidationError.badCPU(String(config.cpuCount))
        }
        guard config.memoryGB >= 1 else {
            throw LinuxGuestValidationError.badMemory(String(config.memoryGB))
        }
    }

    /// Describe, without booting, exactly what Virtualization.framework would be
    /// asked to do. Used by `--check`.
    func describe() -> String {
        var lines: [String] = []
        lines.append("LinuxGuest (EXPERIMENTAL, no boot performed)")
        lines.append("  platform   : VZGenericPlatformConfiguration")
        lines.append("  boot loader: VZLinuxBootLoader")
        lines.append("  kernel     : \(config.kernelURL.path)")
        lines.append("  initrd     : \(config.initrdURL?.path ?? "(none)")")
        lines.append("  cmdline    : \(config.cmdLine)")
        lines.append("  rootfs disk: \(config.diskURL?.path ?? "(none — guest will need another root)")")
        lines.append("  cpuCount   : \(config.cpuCount)")
        lines.append("  memory     : \(config.memoryGB) GB")
        lines.append("  storage    : VZVirtioBlockDeviceConfiguration (VZDiskImageStorageDeviceAttachment)")
        lines.append("  network    : VZVirtioNetworkDeviceConfiguration + VZNATNetworkDeviceAttachment")
        lines.append("  console    : VZVirtioConsoleDeviceConfiguration port 0 -> stdout")
        lines.append("  entropy    : VZVirtioEntropyDeviceConfiguration")
        lines.append("  rosetta    : not needed for bare Linux")
        return lines.joined(separator: "\n")
    }

    /// Build the configuration. Shared by `run()` and any future introspection.
    func buildConfiguration() throws -> VZVirtualMachineConfiguration {
        try validate()

        let bootLoader = VZLinuxBootLoader(kernelURL: config.kernelURL)
        bootLoader.commandLine = config.cmdLine
        if let initrd = config.initrdURL {
            bootLoader.initialRamdiskURL = initrd
        }

        let platform = VZGenericPlatformConfiguration()

        let vmConfig = VZVirtualMachineConfiguration()
        vmConfig.platform = platform
        vmConfig.bootLoader = bootLoader
        vmConfig.cpuCount = min(
            max(1, config.cpuCount),
            VZVirtualMachineConfiguration.maximumAllowedCPUCount
        )
        vmConfig.memorySize = UInt64(max(1, config.memoryGB)) * 1024 * 1024 * 1024

        // Rootfs image as a virtio block device.
        if let disk = config.diskURL {
            let attachment = try VZDiskImageStorageDeviceAttachment(url: disk, readOnly: false)
            vmConfig.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: attachment)]
        }

        // NAT networking.
        let network = VZVirtioNetworkDeviceConfiguration()
        network.attachment = VZNATNetworkDeviceAttachment()
        vmConfig.networkDevices = [network]

        // Serial console on stdout: guest -> our stdout, our stdin -> guest.
        let console = VZVirtioConsoleDeviceConfiguration()
        let port = VZVirtioConsolePortConfiguration()
        port.isConsole = true
        port.attachment = VZFileHandleSerialPortAttachment(
            fileHandleForReading: FileHandle.standardInput,
            fileHandleForWriting: FileHandle.standardOutput
        )
        console.ports[0] = port
        vmConfig.consoleDevices = [console]

        vmConfig.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]

        try vmConfig.validate()
        return vmConfig
    }

    /// Boot and block until the guest stops or errors. Console output streams to
    /// stdout via the serial attachment while this call is in flight.
    func run() throws {
        let vmConfig = try buildConfiguration()
        let machine = VZVirtualMachine(configuration: vmConfig, queue: queue)
        self.vm = machine

        let semaphore = DispatchSemaphore(value: 0)
        log("booting Linux guest (kernel=\(config.kernelURL.lastPathComponent))...")

        machine.start { result in
            switch result {
            case .success:
                self.log("guest started; streaming console (Ctrl-C to stop)")
            case .failure(let error):
                self.log("start failed: \(error.localizedDescription)")
                semaphore.signal()
            }
        }

        signal(SIGINT) { _ in exit(0) }
        signal(SIGTERM) { _ in exit(0) }

        semaphore.wait()
    }
}

// MARK: - CLI

struct LinuxGuestArgs {
    var check = false
    var config = LinuxGuestConfig(kernelPath: "")
    var parseError: String?

    static func parse(_ argv: [String]) -> LinuxGuestArgs {
        var args = LinuxGuestArgs()
        var i = 1
        while i < argv.count {
            let arg = argv[i]
            func next() -> String? { i += 1; return i < argv.count ? argv[i] : nil }
            switch arg {
            case "--check": args.check = true
            case "--kernel": args.config.kernelPath = next() ?? ""
            case "--initrd": args.config.initrdPath = next()
            case "--disk": args.config.diskPath = next()
            case "--cmdline": args.config.cmdLine = next() ?? args.config.cmdLine
            case "--cpu":
                if let v = next(), let n = Int(v) { args.config.cpuCount = n }
                else { args.parseError = "invalid --cpu value" }
            case "--memory":
                if let v = next(), let n = Int(v) { args.config.memoryGB = n }
                else { args.parseError = "invalid --memory value" }
            case "-h", "--help":
                args.parseError = usage
            default:
                args.parseError = "unknown argument: \(arg)"
            }
            i += 1
        }
        return args
    }
}

let usage = """
usage: linux-guest (--check | run) --kernel PATH [--initrd PATH] [--disk PATH]
                   [--cmdline "..."] [--cpu N] [--memory GB]

  --check   validate config/paths and print what WOULD boot (no VM starts)
  (default) --check when no boot is requested; use --kernel without --check to boot
"""

// `--check` is the safe default: `run` requires paths to exist and will emit a
// loud warning because no bundled Linux artifacts ship with this repo.
let args = LinuxGuestArgs.parse(CommandLine.arguments)

if let err = args.parseError {
    FileHandle.standardError.write(Data((err + "\n").utf8))
    exit(2)
}

guard !args.config.kernelPath.isEmpty else {
    FileHandle.standardError.write(Data(("missing required --kernel PATH\n" + usage + "\n").utf8))
    exit(2)
}

let guest = LinuxGuest(config: args.config)

if args.check {
    do {
        try guest.validate()
    } catch {
        FileHandle.standardError.write(Data(("check failed: \(error)\n").utf8))
        exit(1)
    }
    print(guest.describe())
    exit(0)
}

// Boot path. Warn loudly; validate first so missing artifacts fail cleanly.
FileHandle.standardError.write(Data("""
WARNING: this will attempt a REAL Linux boot.
  You need an actual Linux kernel, an optional initrd, and a rootfs image.
  None are bundled with virtualOS. See tools/LINUX_GUEST.md.
  This path is experimental and untested without those artifacts.

""".utf8))

do {
    try guest.run()
} catch {
    FileHandle.standardError.write(Data(("boot failed: \(error)\n").utf8))
    exit(1)
}
