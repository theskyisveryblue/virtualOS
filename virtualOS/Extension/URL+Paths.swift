//
//  URL+Paths.swift
//  virtualOS
//

import Foundation

extension URL {
    static let basePath             = NSHomeDirectory() + "/Documents"
    static let restoreImageURL      = URL(fileURLWithPath: basePath + "/RestoreImage.ipsw")
    static let bundleName           = "virtualOS.bundle/"
    static let defaultVMBundlePath  = basePath + "/\(bundleName)"
    
    static var baseURL: URL {
        return URL(fileURLWithPath: basePath)
    }
    static var vmBundleURL: URL {
        return URL(fileURLWithPath: defaultVMBundlePath)
    }
    var auxiliaryStorageURL: URL {
        return self.appending(path: "AuxiliaryStorage")
    }
    var hardwareModelURL: URL {
        return self.appending(path: "HardwareModel")
    }
    var diskImageURL: URL {
        return self.appending(path: "Disk.img")
    }
    var bootDiskImageURL: URL {
        // The URL attachment API documents a local RAW disk image. An ASIF
        // experiment must use an explicitly supported attachment initializer.
        return diskImageURL
    }
    var isR2LabMetadataBundle: Bool {
        let expected = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("VirtualOSMachines/.virtualOS-r2-metadata.bundle")
            .standardizedFileURL
        guard standardizedFileURL == expected,
              let bundleValues = try? resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]),
              bundleValues.isDirectory == true, bundleValues.isSymbolicLink != true else {
            return false
        }
        for name in ["HardwareModel", "MachineIdentifier", "AuxiliaryStorage", "Parameters.txt", ".r2-lab"] {
            let file = appendingPathComponent(name)
            guard let values = try? file.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey]),
                  values.isRegularFile == true, values.isSymbolicLink != true else {
                return false
            }
        }
        let disk = diskImageURL.path
        guard !FileManager.default.fileExists(atPath: disk),
              (try? FileManager.default.destinationOfSymbolicLink(atPath: disk)) == nil,
              let marker = try? String(contentsOf: appendingPathComponent(".r2-lab"), encoding: .utf8),
              marker == "virtualos-r2-metadata/v1\n" else {
            return false
        }
        return true
    }
    var machineIdentifierURL: URL {
        return self.appending(path: "MachineIdentifier")
    }
    var parametersURL: URL {
        return self.appending(path: "Parameters.txt")
    }
}
