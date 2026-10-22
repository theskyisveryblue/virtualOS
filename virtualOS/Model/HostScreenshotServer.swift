//
//  HostScreenshotServer.swift
//  virtualOS
//
//  Screenshot server using streaming SCK capture.
//  Starts SCStream at launch, serves latest frame via HTTP.
//  GET /frame.jpg returns JPEG bytes.
//

#if arch(arm64)

import ScreenCaptureKit
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers
import OSLog

final class HostScreenshotServer: NSObject, SCStreamOutput {
    static let shared = HostScreenshotServer()
    static let port: UInt16 = 8896

    private var stream: SCStream?
    private var latestFrame: CGImage?
    private let frameLock = NSLock()
    private var serverSocket: Int32 = -1
    private var started = false
    private let port = HostScreenshotServer.port
    private let clientSlots = DispatchSemaphore(value: 8)
    private var captureStarted = false
    private let streamSem = DispatchSemaphore(value: 0)

    func start() {
        guard !started else { return }
        started = true
        DispatchQueue.main.async {
            self.startCapture()
        }
        startHTTPServer()
        Logger.shared.log(level: .default, "HostScreenshotServer starting on port \(HostScreenshotServer.port)")
    }

    private func startCapture() {
        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: true) { [weak self] content, error in
            guard let self, let display = content?.displays.first else {
                Logger.shared.error("HostScreenshotServer no display: \(error?.localizedDescription ?? "?")")
                return
            }

            let config = SCStreamConfiguration()
            config.width = Int(display.width)
            config.height = Int(display.height)
            config.minimumFrameInterval = CMTime(value: 1, timescale: 30)
            config.pixelFormat = kCVPixelFormatType_32BGRA
            config.showsCursor = false
            config.queueDepth = 2

            let unwrapped = content!
            let filter = SCContentFilter(display: display, including: unwrapped.windows)
            self.stream = SCStream(filter: filter, configuration: config, delegate: nil)
            do {
                try self.stream?.addStreamOutput(self, type: .screen, sampleHandlerQueue: .main)
                self.stream?.startCapture()
                self.captureStarted = true
                self.streamSem.signal()
                Logger.shared.log(level: .default, "HostScreenshotServer capturing \(display.width)x\(display.height)")
            } catch {
                Logger.shared.error("HostScreenshotServer capture start failed: \(error.localizedDescription)")
            }
        }
    }

    func stream(_: SCStream, didOutput sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen else { return }
        guard let buf = CMSampleBufferGetImageBuffer(sb) else { return }
        let ci = CIImage(cvPixelBuffer: buf)
        guard let cg = CIContext(options: [.workingColorSpace: NSNull()]).createCGImage(ci, from: ci.extent) else { return }
        frameLock.lock()
        latestFrame = cg
        frameLock.unlock()
    }

    private func captureJPEG(maxSize: Int = 1400) -> Data? {
        frameLock.lock()
        guard let img = latestFrame else { frameLock.unlock(); return nil }
        frameLock.unlock()

        let scale = min(1.0, CGFloat(maxSize) / CGFloat(img.width), CGFloat(maxSize) / CGFloat(img.height))
        let finalImage: CGImage
        if scale < 1.0 {
            let w = Int(CGFloat(img.width) * scale)
            let h = Int(CGFloat(img.height) * scale)
            guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue) else { return nil }
            ctx.interpolationQuality = .high
            ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
            guard let scaled = ctx.makeImage() else { return nil }
            finalImage = scaled
        } else { finalImage = img }

        let d = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(d as CFMutableData, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(dest, finalImage, [kCGImageDestinationLossyCompressionQuality: 0.85] as CFDictionary)
        CGImageDestinationFinalize(dest)
        return d as Data
    }

    private func startHTTPServer() {
        DispatchQueue.global(qos: .background).async { [weak self] in
            guard let self = self else { return }
            // Wait for capture to start
            _ = self.streamSem.wait(timeout: .now() + 10)
            self.runServer()
        }
    }

    private func runServer() {
        let fd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return }
        serverSocket = fd
        var v: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &v, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        // This endpoint has no authentication. Remote clients must use the
        // authenticated stream service instead of connecting to it directly.
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        guard withUnsafePointer(to: &addr, { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } }) == 0 else { Logger.shared.error("HostScreenshotServer bind failed"); return }
        guard Darwin.listen(fd, 5) == 0 else { return }
        Logger.shared.log(level: .default, "HostScreenshotServer listening on 127.0.0.1:\(HostScreenshotServer.port)")
        while true {
            let c = Darwin.accept(fd, nil, nil)
            guard c >= 0 else {
                if errno != EINTR { Thread.sleep(forTimeInterval: 0.1) }
                continue
            }
            guard clientSlots.wait(timeout: .now()) == .success else {
                Darwin.close(c)
                continue
            }
            DispatchQueue.global(qos: .background).async { [self] in
                defer { clientSlots.signal() }
                handleClient(c)
            }
        }
    }

    private func handleClient(_ fd: Int32) {
        defer { Darwin.close(fd) }
        var noSignal: Int32 = 1
        var timeout = timeval(tv_sec: 5, tv_usec: 0)
        guard Darwin.setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal,
                                socklen_t(MemoryLayout<Int32>.size)) == 0 else { return }
        Darwin.setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        Darwin.setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        let received = LocalHTTPPolicy.readRequest(fd, port: port)
        guard let request = received.data else {
            _ = "HTTP/1.1 \(received.status) Rejected\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".withCString { Darwin.write(fd, $0, strlen($0)) }
            return
        }
        let raw = String(decoding: request, as: UTF8.self)
        guard raw.hasPrefix("GET ") else {
            _ = "HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".withCString { Darwin.write(fd, $0, strlen($0)) }
            return
        }
        let p = (raw.components(separatedBy: "\r\n").first ?? "").components(separatedBy: " ").dropFirst().first ?? "/"
        let path = p.components(separatedBy: "?").first ?? p
        if path == "/frame.jpg" || path == "/screenshot" {
            if let jpeg = captureJPEG(maxSize: 1400) {
                let h = "HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: \(jpeg.count)\r\nConnection: close\r\nCache-Control: no-cache\r\n\r\n"
                _ = h.withCString { Darwin.write(fd, $0, strlen($0)) }
                _ = jpeg.withUnsafeBytes { Darwin.write(fd, $0.baseAddress!, $0.count) }
            } else {
                _ = "HTTP/1.1 500\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{\"ok\":false,\"error\":\"no frame yet\"}".withCString { Darwin.write(fd, $0, strlen($0)) }
            }
        } else {
            let r = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{\"ok\":true,\"port\":\(Int(port))}"
            _ = r.withCString { Darwin.write(fd, $0, strlen($0)) }
        }
    }
}

#endif
