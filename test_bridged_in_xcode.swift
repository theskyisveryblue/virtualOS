//
//  test_bridged_in_xcode.swift
//  Run this in Xcode or as part of the app to test bridged interfaces
//

import Foundation
import Virtualization

func testBridgedInterfaces() {
    print("=== Testing Bridged Network Interfaces ===\n")
    
    let bridgedInterfaces = VZBridgedNetworkInterface.networkInterfaces
    
    print("Found \(bridgedInterfaces.count) bridged interface(s):\n")
    
    if bridgedInterfaces.isEmpty {
        print("❌ No bridged interfaces available")
        print("→ Will use NAT networking\n")
        return
    }
    
    for (index, interface) in bridgedInterfaces.enumerated() {
        print("[\(index + 1)] \(interface.identifier)")
        print("    Display: \(interface.localizedDisplayName)\n")
    }
    
    // Check for Tailscale
    if let tailscale = bridgedInterfaces.first(where: { 
        $0.identifier.lowercased().contains("tailscale") ||
        $0.localizedDisplayName.lowercased().contains("tailscale")
    }) {
        print("✅ Tailscale interface found: \(tailscale.identifier)\n")
    } else {
        print("⚠️  Tailscale not found\n")
    }
    
    // Test creating attachment
    if let firstInterface = bridgedInterfaces.first {
        let attachment = VZBridgedNetworkDeviceAttachment(interface: firstInterface)
        print("✅ Can create bridged attachment with: \(firstInterface.identifier)\n")
    }
    
    print("=== Result ===")
    print("Bridged networking: \(bridgedInterfaces.isEmpty ? "NOT AVAILABLE" : "AVAILABLE")")
}

// Uncomment to run:
// testBridgedInterfaces()

