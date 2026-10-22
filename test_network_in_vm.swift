//
//  test_network_in_vm.swift
//  Quick test script to verify network configuration logic
//

import Foundation
import Virtualization

print("=== Testing Network Configuration Logic ===\n")

// Test 1: Get bridged interfaces
let bridgedInterfaces = VZBridgedNetworkInterface.networkInterfaces
print("1. Bridged interfaces found: \(bridgedInterfaces.count)")

for interface in bridgedInterfaces {
    print("   - \(interface.identifier): \(interface.localizedDisplayName ?? "unknown")")
}

// Test 2: Check for Tailscale
if let tailscale = bridgedInterfaces.first(where: { 
    $0.identifier.lowercased().contains("tailscale") ||
    ($0.localizedDisplayName?.lowercased().contains("tailscale") ?? false)
}) {
    print("\n2. ✅ Tailscale found: \(tailscale.identifier)")
} else {
    print("\n2. ⚠️  Tailscale not found")
}

// Test 3: Check for bridge
if let bridge = bridgedInterfaces.first(where: { 
    $0.identifier.lowercased().contains("bridge")
}) {
    print("3. ✅ Bridge found: \(bridge.identifier)")
} else {
    print("3. ⚠️  Bridge not found in bridged list")
}

// Test 4: What will be used?
print("\n4. Network mode selection:")
if !bridgedInterfaces.isEmpty {
    if let tailscale = bridgedInterfaces.first(where: { 
        $0.identifier.lowercased().contains("tailscale") ||
        ($0.localizedDisplayName?.lowercased().contains("tailscale") ?? false)
    }) {
        print("   ✅ Will use: Tailscale bridged (\(tailscale.identifier))")
    } else if let bridge = bridgedInterfaces.first(where: { 
        $0.identifier.lowercased().contains("bridge")
    }) {
        print("   ✅ Will use: Bridge interface (\(bridge.identifier))")
    } else if let first = bridgedInterfaces.first {
        print("   ✅ Will use: First available bridged (\(first.identifier))")
    }
} else {
    print("   ✅ Will use: NAT (no bridged interfaces)")
}

print("\n=== Test Complete ===")

