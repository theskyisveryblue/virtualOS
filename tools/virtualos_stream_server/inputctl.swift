import Foundation
import ApplicationServices
import Cocoa
import Carbon.HIToolbox
import Vision
import ImageIO
import UniformTypeIdentifiers

func usage() {
    fputs("usage: inputctl <click x y | type text | key name | shortcut key [command] [shift] [control] [option] | ocr image.jpg | ocr-fast image.jpg | crop input.jpg output.jpg x y width height>\n", stderr)
}

func keyCode(for name: String) -> CGKeyCode? {
    let n = name.lowercased()
    let map: [String: Int] = [
        "a": kVK_ANSI_A,
        "b": kVK_ANSI_B,
        "c": kVK_ANSI_C,
        "d": kVK_ANSI_D,
        "e": kVK_ANSI_E,
        "f": kVK_ANSI_F,
        "g": kVK_ANSI_G,
        "h": kVK_ANSI_H,
        "i": kVK_ANSI_I,
        "j": kVK_ANSI_J,
        "k": kVK_ANSI_K,
        "l": kVK_ANSI_L,
        "m": kVK_ANSI_M,
        "n": kVK_ANSI_N,
        "o": kVK_ANSI_O,
        "p": kVK_ANSI_P,
        "q": kVK_ANSI_Q,
        "r": kVK_ANSI_R,
        "s": kVK_ANSI_S,
        "t": kVK_ANSI_T,
        "u": kVK_ANSI_U,
        "v": kVK_ANSI_V,
        "w": kVK_ANSI_W,
        "x": kVK_ANSI_X,
        "y": kVK_ANSI_Y,
        "z": kVK_ANSI_Z,
        "0": kVK_ANSI_0,
        "1": kVK_ANSI_1,
        "2": kVK_ANSI_2,
        "3": kVK_ANSI_3,
        "4": kVK_ANSI_4,
        "5": kVK_ANSI_5,
        "6": kVK_ANSI_6,
        "7": kVK_ANSI_7,
        "8": kVK_ANSI_8,
        "9": kVK_ANSI_9,
        "space": kVK_Space,
        "enter": kVK_Return,
        "return": kVK_Return,
        "tab": kVK_Tab,
        "escape": kVK_Escape,
        "esc": kVK_Escape,
        "left": kVK_LeftArrow,
        "right": kVK_RightArrow,
        "down": kVK_DownArrow,
        "up": kVK_UpArrow,
        "delete": kVK_Delete,
        "backspace": kVK_Delete,
    ]
    if let code = map[n] {
        return CGKeyCode(code)
    }
    return nil
}

func modifierFlags(for names: [String]) -> CGEventFlags {
    var flags: CGEventFlags = []
    for raw in names {
        let name = raw.lowercased()
        switch name {
        case "cmd", "command":
            flags.insert(.maskCommand)
        case "shift":
            flags.insert(.maskShift)
        case "ctrl", "control":
            flags.insert(.maskControl)
        case "alt", "option":
            flags.insert(.maskAlternate)
        default:
            continue
        }
    }
    return flags
}

func ensureAccessibilityTrusted() -> Bool {
    return AXIsProcessTrusted()
}

func postKey(code: CGKeyCode, flags: CGEventFlags = []) {
    guard let src = CGEventSource(stateID: .hidSystemState) else { return }
    let down = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: true)
    let up = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: false)
    down?.flags = flags
    up?.flags = flags
    down?.post(tap: .cghidEventTap)
    up?.post(tap: .cghidEventTap)
}

func postText(_ text: String) {
    guard let src = CGEventSource(stateID: .hidSystemState) else { return }
    for scalar in text.unicodeScalars {
        var chars = Array(String(scalar).utf16)
        let down = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: true)
        down?.keyboardSetUnicodeString(stringLength: chars.count, unicodeString: &chars)
        down?.post(tap: .cghidEventTap)

        let up = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: false)
        up?.keyboardSetUnicodeString(stringLength: chars.count, unicodeString: &chars)
        up?.post(tap: .cghidEventTap)
    }
}

func clickAt(x: Double, y: Double) {
    guard let src = CGEventSource(stateID: .hidSystemState) else { return }
    let point = CGPoint(x: x, y: y)

    let move = CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)
    move?.post(tap: .cghidEventTap)

    let down = CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)
    let up = CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)
    down?.post(tap: .cghidEventTap)
    up?.post(tap: .cghidEventTap)
}

let args = CommandLine.arguments
if args.count < 2 {
    usage()
    exit(2)
}

if args[1] == "ocr" || args[1] == "ocr-fast" {
    guard args.count == 3 else { usage(); exit(2) }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = args[1] == "ocr-fast" ? .fast : .accurate
    request.usesLanguageCorrection = false
    do {
        let handler = VNImageRequestHandler(url: URL(fileURLWithPath: args[2]), options: [:])
        try handler.perform([request])
        let entries: [[String: Any]] = (request.results ?? []).compactMap { item in
            guard let text = item.topCandidates(1).first?.string else { return nil }
            return ["text": text, "x": item.boundingBox.minX, "y": item.boundingBox.minY,
                    "width": item.boundingBox.width, "height": item.boundingBox.height]
        }
        let data = try JSONSerialization.data(withJSONObject: entries, options: [])
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([10]))
    } catch {
        fputs("error: OCR failed: \(error)\n", stderr)
        exit(1)
    }
    exit(0)
}

if args[1] == "crop" {
    guard args.count == 8, let x = Int(args[4]), let y = Int(args[5]),
          let width = Int(args[6]), let height = Int(args[7]),
          x >= 0, y >= 0, width > 0, height > 0 else { usage(); exit(2) }
    let sourceURL = URL(fileURLWithPath: args[2]) as CFURL
    let outputURL = URL(fileURLWithPath: args[3]) as CFURL
    guard let source = CGImageSourceCreateWithURL(sourceURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil),
          x + width <= image.width, y + height <= image.height,
          let cropped = image.cropping(to: CGRect(x: x, y: y, width: width, height: height)),
          let destination = CGImageDestinationCreateWithURL(outputURL, UTType.jpeg.identifier as CFString, 1, nil) else {
        fputs("error: crop source or bounds invalid\n", stderr)
        exit(1)
    }
    CGImageDestinationAddImage(destination, cropped, [kCGImageDestinationLossyCompressionQuality: 0.9] as CFDictionary)
    guard CGImageDestinationFinalize(destination) else {
        fputs("error: crop write failed\n", stderr)
        exit(1)
    }
    print("ok")
    exit(0)
}

if !ensureAccessibilityTrusted() {
    fputs("error: accessibility permission not granted for inputctl\n", stderr)
    exit(3)
}

switch args[1] {
case "click":
    guard args.count >= 4, let x = Double(args[2]), let y = Double(args[3]) else {
        usage(); exit(2)
    }
    clickAt(x: x, y: y)
case "type":
    guard args.count >= 3 else { usage(); exit(2) }
    postText(args[2])
case "key":
    guard args.count >= 3, let code = keyCode(for: args[2]) else {
        usage(); exit(2)
    }
    postKey(code: code)
case "shortcut":
    guard args.count >= 3, let code = keyCode(for: args[2]) else {
        usage(); exit(2)
    }
    let mods = args.count > 3 ? Array(args[3...]) : []
    let flags = modifierFlags(for: mods)
    postKey(code: code, flags: flags)
default:
    usage(); exit(2)
}

print("ok")
