//
//  Download.swift
//  virtualOS
//
//  Created by Jahn Bertsch.
//  Licensed under the Apache License, see LICENSE file.
//

import Virtualization
import Combine
import OSLog

#if arch(arm64)

protocol ProgressDelegate: AnyObject {
    func progress(_ progress: Double, progressString: String)
    func done(error: Error?)
}

final class RestoreImageDownload {
    weak var delegate: ProgressDelegate?
    fileprivate var observation: NSKeyValueObservation?
    fileprivate var downloadTask: URLSessionDownloadTask?
    fileprivate var downloading = true

    deinit {
        observation?.invalidate()
    }
    
    func fetch() {
        VZMacOSRestoreImage.fetchLatestSupported { [self](result: Result<VZMacOSRestoreImage, Error>) in
            switch result {
            case let .success(restoreImage):
                download(restoreImage: restoreImage)
            case let .failure(error):
                delegate?.done(error: error)
            }
        }
    }
    
    func cancel() {
        downloadTask?.cancel()
    }
    
    // MARK: - Private
    
    func download(url: URL) {
        Logger.shared.log(level: .default, "downloading from \(url)")
                
        let downloadTask = URLSession.shared.downloadTask(with: url) { localUrl, response, error in
            self.downloading = false
            self.downloadFinished(localURL: localUrl, error: error)
        }
        observation = downloadTask.progress.observe(\.fractionCompleted) { _, _ in }
        downloadTask.resume()
        self.downloadTask = downloadTask
        
        Logger.shared.log(level: .default, "downloading")
        
        func updateDownloadProgress() {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
                guard let self = self else { return }
                let progressString: String
                
                if let downloadTask = self.downloadTask,
                   let byteCompletedCount = downloadTask.progress.userInfo[ProgressUserInfoKey("NSProgressByteCompletedCountKey")] as? Int,
                   let byteTotalCount = downloadTask.progress.userInfo[ProgressUserInfoKey("NSProgressByteTotalCountKey")] as? Int
                {
                    let mbCompleted = byteCompletedCount / (1024 * 1024)
                    let mbTotal     = byteTotalCount / (1024 * 1024)
                    progressString = "Restore Image\nDownloading \(Int(downloadTask.progress.fractionCompleted * 100))% (\(mbCompleted) of \(mbTotal) MB)"
                    
                    self.delegate?.progress(downloadTask.progress.fractionCompleted, progressString: progressString)
                } else if let downloadTask = self.downloadTask {
                    progressString = "Restore Image\nDownloading \(Int(downloadTask.progress.fractionCompleted * 100))%"
                    
                    self.delegate?.progress(downloadTask.progress.fractionCompleted, progressString: progressString)
                }
                
                if self.downloading {
                    updateDownloadProgress()
                }
            }
        }
        
        updateDownloadProgress()
    }

    // MARK: - Private
    
    fileprivate func download(restoreImage: VZMacOSRestoreImage) {
        Logger.shared.log(level: .default, "fetched, macOS \(restoreImage.operatingSystemVersionString)")
        download(url: restoreImage.url)
    }
    
    fileprivate func downloadFinished(localURL: URL?, error: Error?) {
        Logger.shared.log(level: .default, "download finished")
        delegate?.progress(100, progressString: "Done")
        
        if let error = error {
            Logger.shared.log(level: .default, "\(error.localizedDescription)")
            delegate?.done(error: error)
            return
        }
        
        guard let localURL = localURL else {
            Logger.shared.log(level: .default, "failed to move downloaded restore image to \(URL.restoreImageURL)")
            delegate?.done(error: NSError(domain: "virtualOS", code: 1, userInfo: [NSLocalizedDescriptionKey: "Downloaded restore image file is missing."]))
            return
        }
        
        do {
            if FileManager.default.fileExists(atPath: URL.restoreImageURL.path) {
                try FileManager.default.removeItem(at: URL.restoreImageURL)
            }
            try FileManager.default.moveItem(at: localURL, to: URL.restoreImageURL)
            Logger.shared.log(level: .default, "moved restore image to \(URL.restoreImageURL)")
            delegate?.done(error: nil)
        } catch {
            Logger.shared.log(level: .default, "failed to move downloaded restore image to \(URL.restoreImageURL): \(error.localizedDescription)")
            delegate?.done(error: error)
        }
    }
}

#endif
