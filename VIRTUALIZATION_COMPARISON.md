# Virtualization Framework: Swift vs Objective-C Comparison

This document compares implementations of macOS virtualization using Apple's Virtualization framework in both Swift and Objective-C.

## Overview

The Virtualization framework allows you to:
- Run macOS in a VM on Apple Silicon
- Run Linux in a VM
- Configure CPU, RAM, storage, network, and graphics
- Share directories between host and guest

## Key Differences: Swift vs Objective-C

### Swift (Current Implementation)
- Modern, type-safe language
- Automatic memory management
- Cleaner syntax for framework APIs
- Better integration with SwiftUI

### Objective-C
- More verbose but explicit
- Manual memory management (ARC)
- Traditional Cocoa patterns
- Better for legacy codebases

## Implementation Comparison

### 1. Creating a VM Configuration

#### Swift
```swift
import Virtualization

func createVMConfiguration() -> VZVirtualMachineConfiguration {
    let configuration = VZVirtualMachineConfiguration()
    
    // CPU and Memory
    configuration.cpuCount = 4
    configuration.memorySize = 4 * 1024 * 1024 * 1024 // 4GB
    
    // Boot loader
    let bootLoader = VZMacOSBootLoader()
    configuration.bootLoader = bootLoader
    
    // Graphics
    let graphicsConfiguration = VZMacGraphicsDeviceConfiguration()
    graphicsConfiguration.displays = [
        VZMacGraphicsDeviceConfiguration.DisplayConfiguration(
            widthInPixels: 1920,
            heightInPixels: 1080,
            pixelsPerInch: 80
        )
    ]
    configuration.graphicsDevices = [graphicsConfiguration]
    
    // Storage
    let diskImageURL = URL(fileURLWithPath: "/path/to/disk.img")
    let diskImageAttachment = try! VZDiskImageStorageDeviceAttachment(
        url: diskImageURL,
        readOnly: false
    )
    let storageDevice = VZVirtioBlockDeviceConfiguration(attachment: diskImageAttachment)
    configuration.storageDevices = [storageDevice]
    
    // Network
    let networkDevice = VZVirtioNetworkDeviceConfiguration()
    networkDevice.attachment = VZNATNetworkDeviceAttachment()
    configuration.networkDevices = [networkDevice]
    
    // Keyboard and Mouse
    configuration.keyboards = [VZUSBKeyboardConfiguration()]
    configuration.pointingDevices = [VZUSBScreenCoordinatePointingDeviceConfiguration()]
    
    // Audio
    let audioConfiguration = VZVirtioSoundDeviceConfiguration()
    let audioStream = VZVirtioSoundDeviceOutputStreamConfiguration()
    audioStream.sink = VZHostAudioOutputStreamSink()
    audioConfiguration.streams = [audioStream]
    configuration.audioDevices = [audioConfiguration]
    
    // Platform (macOS specific)
    let platformConfiguration = VZMacPlatformConfiguration()
    let hardwareModel = VZMacHardwareModel(dataRepresentation: hardwareModelData)!
    platformConfiguration.hardwareModel = hardwareModel
    let auxiliaryStorage = VZMacAuxiliaryStorage(contentsOf: auxiliaryStorageURL)
    platformConfiguration.auxiliaryStorage = auxiliaryStorage
    configuration.platform = platformConfiguration
    
    try! configuration.validate()
    return configuration
}
```

#### Objective-C
```objc
#import <Virtualization/Virtualization.h>

- (VZVirtualMachineConfiguration *)createVMConfiguration {
    VZVirtualMachineConfiguration *configuration = [[VZVirtualMachineConfiguration alloc] init];
    
    // CPU and Memory
    configuration.cpuCount = 4;
    configuration.memorySize = 4ULL * 1024 * 1024 * 1024; // 4GB
    
    // Boot loader
    VZMacOSBootLoader *bootLoader = [[VZMacOSBootLoader alloc] init];
    configuration.bootLoader = bootLoader;
    
    // Graphics
    VZMacGraphicsDeviceConfiguration *graphicsConfiguration = 
        [[VZMacGraphicsDeviceConfiguration alloc] init];
    VZMacGraphicsDeviceConfigurationDisplayConfiguration *displayConfig = 
        [[VZMacGraphicsDeviceConfigurationDisplayConfiguration alloc] 
         initWithWidthInPixels:1920 
         heightInPixels:1080 
         pixelsPerInch:80];
    graphicsConfiguration.displays = @[displayConfig];
    configuration.graphicsDevices = @[graphicsConfiguration];
    
    // Storage
    NSURL *diskImageURL = [NSURL fileURLWithPath:@"/path/to/disk.img"];
    NSError *error = nil;
    VZDiskImageStorageDeviceAttachment *diskImageAttachment = 
        [[VZDiskImageStorageDeviceAttachment alloc] 
         initWithURL:diskImageURL 
         readOnly:NO 
         error:&error];
    if (error) {
        NSLog(@"Error creating disk attachment: %@", error);
        return nil;
    }
    VZVirtioBlockDeviceConfiguration *storageDevice = 
        [[VZVirtioBlockDeviceConfiguration alloc] initWithAttachment:diskImageAttachment];
    configuration.storageDevices = @[storageDevice];
    
    // Network
    VZVirtioNetworkDeviceConfiguration *networkDevice = 
        [[VZVirtioNetworkDeviceConfiguration alloc] init];
    networkDevice.attachment = [[VZNATNetworkDeviceAttachment alloc] init];
    configuration.networkDevices = @[networkDevice];
    
    // Keyboard and Mouse
    configuration.keyboards = @[[[VZUSBKeyboardConfiguration alloc] init]];
    configuration.pointingDevices = @[[[VZUSBScreenCoordinatePointingDeviceConfiguration alloc] init]];
    
    // Audio
    VZVirtioSoundDeviceConfiguration *audioConfiguration = 
        [[VZVirtioSoundDeviceConfiguration alloc] init];
    VZVirtioSoundDeviceOutputStreamConfiguration *audioStream = 
        [[VZVirtioSoundDeviceOutputStreamConfiguration alloc] init];
    audioStream.sink = [[VZHostAudioOutputStreamSink alloc] init];
    audioConfiguration.streams = @[audioStream];
    configuration.audioDevices = @[audioConfiguration];
    
    // Platform (macOS specific)
    VZMacPlatformConfiguration *platformConfiguration = 
        [[VZMacPlatformConfiguration alloc] init];
    VZMacHardwareModel *hardwareModel = 
        [[VZMacHardwareModel alloc] initWithDataRepresentation:hardwareModelData];
    platformConfiguration.hardwareModel = hardwareModel;
    VZMacAuxiliaryStorage *auxiliaryStorage = 
        [[VZMacAuxiliaryStorage alloc] initWithContentsOfURL:auxiliaryStorageURL];
    platformConfiguration.auxiliaryStorage = auxiliaryStorage;
    configuration.platform = platformConfiguration;
    
    // Validate
    NSError *validationError = nil;
    if (![configuration validateWithError:&validationError]) {
        NSLog(@"Configuration validation failed: %@", validationError);
        return nil;
    }
    
    return configuration;
}
```

### 2. Creating and Starting a VM

#### Swift
```swift
func createAndStartVM() {
    let configuration = createVMConfiguration()
    let virtualMachine = VZVirtualMachine(configuration: configuration)
    
    virtualMachine.delegate = self
    
    virtualMachine.start { result in
        switch result {
        case .success:
            print("VM started successfully")
        case .failure(let error):
            print("Failed to start VM: \(error)")
        }
    }
}

// Delegate
extension ViewController: VZVirtualMachineDelegate {
    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
        print("VM stopped with error: \(error)")
    }
    
    func guestDidStop(_ virtualMachine: VZVirtualMachine) {
        print("Guest OS stopped")
    }
}
```

#### Objective-C
```objc
- (void)createAndStartVM {
    VZVirtualMachineConfiguration *configuration = [self createVMConfiguration];
    VZVirtualMachine *virtualMachine = 
        [[VZVirtualMachine alloc] initWithConfiguration:configuration];
    
    virtualMachine.delegate = self;
    
    [virtualMachine startWithCompletionHandler:^(NSError * _Nullable error) {
        if (error) {
            NSLog(@"Failed to start VM: %@", error);
        } else {
            NSLog(@"VM started successfully");
        }
    }];
}

// Delegate
- (void)virtualMachine:(VZVirtualMachine *)virtualMachine 
    didStopWithError:(NSError *)error {
    NSLog(@"VM stopped with error: %@", error);
}

- (void)guestDidStopVirtualMachine:(VZVirtualMachine *)virtualMachine {
    NSLog(@"Guest OS stopped");
}
```

### 3. Creating VM View

#### Swift
```swift
import Virtualization

func createVMView() -> VZVirtualMachineView {
    let vmView = VZVirtualMachineView()
    vmView.virtualMachine = virtualMachine
    vmView.capturesSystemKeys = true
    return vmView
}
```

#### Objective-C
```objc
#import <Virtualization/Virtualization.h>

- (VZVirtualMachineView *)createVMView {
    VZVirtualMachineView *vmView = [[VZVirtualMachineView alloc] init];
    vmView.virtualMachine = self.virtualMachine;
    vmView.capturesSystemKeys = YES;
    return vmView;
}
```

## Key Differences Summary

| Feature | Swift | Objective-C |
|---------|-------|-------------|
| Memory Management | Automatic (ARC) | Automatic (ARC) |
| Error Handling | `try/catch`, `Result<T>` | `NSError **` pattern |
| Optionals | `?` and `!` | `nil` checks |
| Array Syntax | `[item]` | `@[item]` |
| Dictionary Syntax | `["key": value]` | `@{@"key": value}` |
| Method Calls | `.method()` | `[object method]` |
| Properties | `object.property` | `object.property` |
| Type Inference | Strong | Explicit types |

## When to Use Each

### Use Swift When:
- Starting a new project
- Building modern macOS/iOS apps
- Want cleaner, more readable code
- Need better type safety
- Working with SwiftUI

### Use Objective-C When:
- Maintaining legacy code
- Need to integrate with C/C++ libraries
- Team is more familiar with Objective-C
- Working with older frameworks

## Entitlements Required

Both implementations need the same entitlements in `Info.plist` or `.entitlements`:

```xml
<key>com.apple.security.virtualization</key>
<true/>
```

## Current virtualOS Implementation

The virtualOS project is written in **Swift** and includes:
- `VMConfiguration.swift` - VM setup
- `MacPlatformConfiguration.swift` - macOS-specific platform config
- `VMParameters.swift` - VM parameters management
- `RestoreImageDownload.swift` - Downloading macOS restore images
- `RestoreImageInstall.swift` - Installing macOS

## Next Steps

To add Objective-C support:
1. Create Objective-C wrapper classes
2. Use bridging headers if mixing languages
3. Or create a separate Objective-C target

