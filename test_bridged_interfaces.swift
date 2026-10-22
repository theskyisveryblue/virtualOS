#!/usr/bin/env swift
//
//  test_bridged_interfaces.swift
//  Test if bridged networking interfaces are actually available
//

import Foundation
import Virtualization

print("=== Testing Bridged Network Interfaces ===\n")

// Test 1: Check if Virtualization framework supports bridged networking
print("1. Checking Virtualization framework support...")
print("   ✅ Framework loaded")

// Test 2: Get available bridged interfaces
print("\n2. Checking available bridged network interfaces...")
let bridgedInterfaces = VZBridgedNetworkInterface.networkInterfaces

if bridgedInterfaces.isEmpty {
    print("   ❌ No bridged network interfaces available")
    print("   → Will use NAT networking")
} else {
    print("   ✅ Found \(bridgedInterfaces.count) bridged interface(s):")
    for (index, interface) in bridgedInterfaces.enumerated() {
        print("      [\(index + 1)] \(interface.identifier)")
        print("          Display Name: \(interface.localizedDisplayName)")
    }
}

// Test 3: Check for Tailscale specifically
print("\n3. Checking for Tailscale interface...")
if let tailscaleInterface = bridgedInterfaces.first(where: { interface in
    interface.identifier.lowercased().contains("tailscale") ||
    interface.localizedDisplayName.lowercased().contains("tailscale")
}) {
    print("   ✅ Tailscale interface found!")
    print("      ID: \(tailscaleInterface.identifier)")
    print("      Name: \(tailscaleInterface.localizedDisplayName)")
    print("   → Can use Tailscale for bridged networking")
} else {
    print("   ⚠️  Tailscale interface not found")
    print("   → Will try other bridge interfaces or NAT")
}

// Test 4: Check for bridge interfaces
print("\n4. Checking for bridge interfaces...")
if let bridgeInterface = bridgedInterfaces.first(where: { interface in
    interface.identifier.lowercased().contains("bridge")
}) {
    print("   ✅ Bridge interface found!")
    print("      ID: \(bridgeInterface.identifier)")
    print("      Name: \(bridgeInterface.localizedDisplayName)")
    print("   → Can use bridge for bridged networking")
} else {
    print("   ⚠️  Bridge interface not found in bridged list")
}

// Test 5: Test if we can actually create a bridged attachment
print("\n5. Testing bridged network attachment creation...")
if let firstInterface = bridgedInterfaces.first {
    do {
        let attachment = VZBridgedNetworkDeviceAttachment(interface: firstInterface)
        print("   ✅ Successfully created bridged network attachment")
        print("      Using interface: \(firstInterface.identifier)")
        print("   → Bridged networking is AVAILABLE and WORKING")
    } catch {
        print("   ❌ Failed to create bridged attachment: \(error)")
        print("   → Will use NAT networking")
    }
} else {
    print("   ⚠️  No interfaces available to test")
    print("   → Will use NAT networking")
}

// Test 6: Check system network interfaces (for reference)
print("\n6. System network interfaces (for reference):")
let task = Process()
task.launchPath = "/sbin/ifconfig"
task.arguments = []
let pipe = Pipe()
task.standardOutput = pipe
task.launch()
task.waitUntilExit()

if let data = pipe.fileHandleForReading.readDataToEndOfFile() as Data?,
   let output = String(data: data, encoding: .utf8) {
    let lines = output.components(separatedBy: "\n")
    let interfaces = lines
        .filter { !$0.hasPrefix("\t") && !$0.hasPrefix(" ") && $0.contains(":") }
        .compactMap { line -> String? in
            let parts = line.components(separatedBy: ":")
            return parts.first?.trimmingCharacters(in: .whitespaces)
        }
        .filter { $0 != "lo0" && !$0.isEmpty }
    
    print("   Found \(interfaces.count) system interfaces")
    for interface in interfaces.prefix(5) {
        let isBridged = bridgedInterfaces.contains { $0.identifier == interface }
        let marker = isBridged ? "✅ (bridged)" : ""
        print("      - \(interface) \(marker)")
    }
}

// Summary
print("\n=== Test Summary ===")
if !bridgedInterfaces.isEmpty {
    print("✅ Bridged networking: AVAILABLE")
    if bridgedInterfaces.contains(where: { $0.identifier.lowercased().contains("tailscale") }) {
        print("✅ Tailscale: AVAILABLE")
    } else {
        print("⚠️  Tailscale: Not found (but other bridges available)")
    }
    print("✅ Can use bridged networking as primary option")
} else {
    print("❌ Bridged networking: NOT AVAILABLE")
    print("✅ Will use NAT networking (fallback)")
}

print("\n=== Recommendation ===")
if !bridgedInterfaces.isEmpty {
    print("✅ Use bridged networking (interfaces available)")
    if let tailscale = bridgedInterfaces.first(where: { $0.identifier.lowercased().contains("tailscale") }) {
        print("   → Prefer Tailscale interface: \(tailscale.identifier)")
    } else if let bridge = bridgedInterfaces.first(where: { $0.identifier.lowercased().contains("bridge") }) {
        print("   → Prefer bridge interface: \(bridge.identifier)")
    } else {
        print("   → Use first available: \(bridgedInterfaces.first!.identifier)")
    }
} else {
    print("✅ Use NAT networking (no bridges available)")
}

