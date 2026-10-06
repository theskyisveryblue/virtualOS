//
//  AppDelegate.swift
//  virtualOS
//
//  Created by Jahn Bertsch.
//  Licensed under the Apache License, see LICENSE file.
//

import Cocoa
import OSLog

@main
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var mainWindowController: NSWindowController?

    func applicationDidFinishLaunching(_ aNotification: Notification) {
        let arguments = ProcessInfo.processInfo.arguments
        let shouldStartInBackground = arguments.contains("--background-vm")
        let shouldAutoStartVM = shouldStartInBackground || arguments.contains("--auto-start-vm")

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
            let existingWindow = NSApp.windows.first { window in
                window.contentViewController is MainViewController
            }
            let windowController: NSWindowController

            if let existingWindow {
                windowController = existingWindow.windowController ?? NSWindowController(window: existingWindow)
            } else {
                let storyboard = NSStoryboard(name: "Main", bundle: nil)
                guard let instantiatedController = storyboard.instantiateInitialController() as? NSWindowController else {
                    Logger.shared.error("Could not instantiate the main VM controller window")
                    return
                }
                windowController = instantiatedController
            }

            self.mainWindowController = windowController
            guard let window = windowController.window,
                  let controller = window.contentViewController as? MainViewController else {
                Logger.shared.error("Main VM controller window has no MainViewController")
                return
            }

            if shouldStartInBackground {
                window.orderOut(nil)
            } else {
                windowController.showWindow(nil)
                NSApp.activate(ignoringOtherApps: true)
            }

            if shouldAutoStartVM {
                controller.startSelectedVMFromLaunchIfNeeded()
            }
        }
    }

    func applicationWillTerminate(_ aNotification: Notification) {
        // Insert code here to tear down your application
    }

    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool {
        return true
    }


}
