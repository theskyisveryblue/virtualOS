# Bridged Networking Setup (Backup Option)

## ✅ Tests Completed

All tests passed. Current setup verified:
- NAT networking working
- Shared folder working
- Network interfaces available for bridging

## 🔧 Changes Made

Added bridged networking as **backup option** in `VMConfiguration.swift`:

1. **Priority Order:**
   - First: Try Tailscale interface (if available)
   - Second: Try bridge interface
   - Third: Try any available bridged interface
   - Fallback: Use NAT (if bridged fails or not available)

2. **Automatic Fallback:**
   - If bridged networking fails, automatically falls back to NAT
   - No manual configuration needed
   - Works with or without Tailscale

## ⚠️ Important: Sandbox Removal Required

**Bridged networking requires removing the app sandbox:**

### Current Entitlements (blocks bridged):
```xml
<key>com.apple.security.app-sandbox</key>
<true/>
```

### To Enable Bridged Networking:

**Option 1: Remove sandbox entirely** (for development)
```xml
<!-- Remove or set to false -->
<key>com.apple.security.app-sandbox</key>
<false/>
```

**Option 2: Keep sandbox, use NAT only**
- Current setup will automatically use NAT
- Bridged networking will be skipped
- No changes needed

## 🧪 Testing

The code will:
1. Try to find Tailscale interface
2. If not found, try bridge interface  
3. If not found, try any bridged interface
4. If all fail, use NAT (current behavior)

**No breaking changes** - NAT still works as before.

## 📋 Usage

The bridged networking is **automatic**:
- If Tailscale/bridge is available → uses it
- If not available → uses NAT
- No user configuration needed

## 🔍 Verification

To verify which mode is being used, check the logs:
- "Using Tailscale bridged interface" → Bridged with Tailscale
- "Using bridge interface" → Bridged with bridge
- "Using NAT networking" → NAT (fallback)

