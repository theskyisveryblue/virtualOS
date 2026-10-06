# MDM Scaffold: EnableRemoteDesktop

This folder contains a minimal scaffold for Apple's MDM `EnableRemoteDesktop` command.

## What it does

- Generates the XML plist command payload:
  - `RequestType = EnableRemoteDesktop`
  - `CommandUUID = ...`
- Optionally sends it via `PUT` to an MDM endpoint.

## What it does NOT do

- Device enrollment
- Device supervision
- APNs push / MDM check-in orchestration
- Any guarantee that the target device will accept the command

Those are still required in your actual MDM infrastructure.

## Usage

Generate payload:

```bash
cd /Users/theone/Documents/GitHub/virtualOS
python3 tools/mdm_scaffold/enable_remote_desktop.py payload --uuid 0001_EnableRemoteDesktop
```

Dry-run send:

```bash
python3 tools/mdm_scaffold/enable_remote_desktop.py send \
  --url https://yourmdmhost.example.com/mdm \
  --uuid 0001_EnableRemoteDesktop \
  --header "Authorization: Bearer YOUR_TOKEN" \
  --dry-run
```

Actual send:

```bash
python3 tools/mdm_scaffold/enable_remote_desktop.py send \
  --url https://yourmdmhost.example.com/mdm \
  --uuid 0001_EnableRemoteDesktop \
  --header "Authorization: Bearer YOUR_TOKEN"
```

## Tests

```bash
cd /Users/theone/Documents/GitHub/virtualOS
python3 -m unittest tools.mdm_scaffold.test_enable_remote_desktop
```

