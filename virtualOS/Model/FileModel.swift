//
//  FileModel.swift
//  virtualOS
//
//  Created by Jahn Bertsch.
//  Licensed under the Apache License, see LICENSE file.
//

import Foundation
import OSLog

struct FileModel {
    var bundleExists: Bool {
        return FileManager.default.fileExists(atPath: URL.vmBundleURL.path())
    }
    
    var restoreImageExists: Bool {
        return FileManager.default.fileExists(atPath: URL.restoreImageURL.path)
    }

    func getVMBundles() -> [VMBundle] {
        if ProcessInfo.processInfo.arguments.contains("--nbd-r2-lab") {
            guard let configuredBundle = configuredBundleURL(),
                  configuredBundle.isR2LabMetadataBundle else {
                Logger.shared.error("NBD R2 lab refused: metadata-only bundle required")
                return []
            }
            return [VMBundle(url: configuredBundle)]
        }
        if let configuredBundle = configuredBundleURL() {
            return [VMBundle(url: configuredBundle)]
        }

        var result: [VMBundle] = []
        let candidateDirectories = vmDirectoryCandidates()

        var selectedDirectory: URL? = nil
        for directory in candidateDirectories {
            if let urls = try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil, options: []) {
                let bundles = urls.filter { $0.lastPathComponent.hasSuffix("bundle") }
                if !bundles.isEmpty {
                    selectedDirectory = directory
                    result = bundles.map { VMBundle(url: $0) }
                    break
                }
            }
        }

        if let selectedDirectory {
            let selectedPath = selectedDirectory.path
            if UserDefaults.standard.vmFilesDirectory != selectedPath {
                // Persist the working directory so future launches resolve consistently.
                UserDefaults.standard.vmFilesDirectory = selectedPath
                // Only keep bookmark for user-selected dirs. Container/default paths do not need it.
                if selectedPath == URL.baseURL.path || selectedPath.hasPrefix(containerDirectoryBasePath()) {
                    UserDefaults.standard.vmFilesDirectoryBookmarkData = nil
                }
            }
        }

        return result
    }

    fileprivate func configuredBundleURL() -> URL? {
        let arguments = ProcessInfo.processInfo.arguments
        var configuredPath = ProcessInfo.processInfo.environment["VOS_VM_BUNDLE"]

        if let flagIndex = arguments.firstIndex(of: "--vm-bundle"),
           arguments.indices.contains(flagIndex + 1) {
            configuredPath = arguments[flagIndex + 1]
        }

        guard let configuredPath,
              !configuredPath.isEmpty else {
            return nil
        }

        let url = URL(fileURLWithPath: configuredPath).standardizedFileURL
        guard url.pathExtension == "bundle",
              FileManager.default.fileExists(atPath: url.path) else {
            Logger.shared.error("Configured VM bundle is missing or invalid: \(configuredPath, privacy: .public)")
            return nil
        }

        Logger.shared.log(level: .default, "Using configured VM bundle: \(url.path, privacy: .public)")
        return url
    }
    
    func getRestoreImages() -> [String] {
        var result: [String] = []
        var seen = Set<String>()

        for directory in vmDirectoryCandidates() {
            if let urls = try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil, options: []) {
                for url in urls where url.lastPathComponent.hasSuffix("ipsw") {
                    let name = url.lastPathComponent
                    if !seen.contains(name) {
                        seen.insert(name)
                        result.append(name)
                    }
                }
            }
        }

        return result
    }

    // Supports both sandbox container Documents and non-sandbox ~/Documents layouts.
    fileprivate func vmDirectoryCandidates() -> [URL] {
        var candidates: [URL] = []

        if let hardDiskDirectoryPath = UserDefaults.standard.vmFilesDirectory, !hardDiskDirectoryPath.isEmpty {
            candidates.append(URL(fileURLWithPath: hardDiskDirectoryPath))
        }

        candidates.append(URL.baseURL)

        let userHome = FileManager.default.homeDirectoryForCurrentUser.path
        let explicitContainerDocs = URL(fileURLWithPath: "\(userHome)/Library/Containers/com.github.yep.ios.virtualOS/Data/Documents")
        candidates.append(explicitContainerDocs)

        // De-duplicate while preserving order.
        var deduped: [URL] = []
        var seen = Set<String>()
        for candidate in candidates {
            let path = candidate.path
            if !seen.contains(path) {
                seen.insert(path)
                deduped.append(candidate)
            }
        }
        return deduped
    }

    fileprivate func containerDirectoryBasePath() -> String {
        let userHome = FileManager.default.homeDirectoryForCurrentUser.path
        return "\(userHome)/Library/Containers/com.github.yep.ios.virtualOS"
    }

}
