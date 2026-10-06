# virtualOS Standards

Purpose: this repo exists to deliver hidden, reliable remote control of a guest macOS VM without taking over the host Mac.

These standards are normative. New work should either meet them or be clearly marked experimental.

## Product Standard

1. Every change must improve or protect the hidden-remote-control workflow.
2. The host Mac must remain usable while the guest VM is being controlled.
3. Remote work should leave the guest in a cleaner state than it found it unless the operator explicitly asks otherwise.
4. Docs must describe shipped behavior, not wishful behavior.

## Approved Paths

- Hidden/background VM launch from `start.sh` and `tools/vosctl`
- Guest unlock through `/vm/unlock`
- Guest command execution through `/vm/terminal/exec` and `/vm/exec`
- Cloudflare relay queue/result/trace logging with secret redaction
- Password loading from `VOS_VM_PASSWORD` or `~/.config/virtualos/start.env`

## Provisional Paths

These paths are allowed for development, but they are not yet considered background-safe:

- Generic keyboard shortcuts
- Spotlight-driven app launch
- Any action that cannot prove it avoids surfacing `virtualOS`

If a path briefly fronts `virtualOS`, it stays provisional until fixed and re-tested.

## Banned Paths

- Hardcoded credentials in source, scripts, docs, or test payloads
- Blind auto-login without detecting the guest password prompt
- Logging raw passwords, tokens, cookies, or session values to Cloudflare or local traces
- Relying on visible guest windows as the control contract
- Claiming a capability in docs before the code actually supports it

## Security Standard

1. Secrets live in environment variables or local config files outside the repo.
2. Cloudflare relay logs must redact secret-looking fields before persistence.
3. Result payloads must be safe to inspect after the fact.
4. Auth stays on for any real remote deployment.

## Remote Control Standard

1. Background launch may unlock the guest only after detecting its password prompt and only when `VOS_VM_PASSWORD` is configured.
2. Unlock should use direct guest input, not host pasteboard tricks.
3. Approved remote actions must be validated against the hidden VM path, not only against a visible debug window.
4. State-changing actions should produce a trace with enough evidence to debug what happened.

## Guest Cleanliness Standard

1. Transient Finder, System Settings, and helper Terminal windows should be closed after remote tasks.
2. The guest desktop should return to a neutral baseline after setup and testing.
3. If a cleanup command opens a helper window, the cleanup is not done until that window is closed.

## Verification Standard

Minimum bar for work on the remote-control path:

1. `xcodebuild -project virtualOS.xcodeproj -scheme virtualOS -configuration Debug build CODE_SIGNING_ALLOWED=NO`
2. `bash -n start.sh`
3. `bash -n tools/vosctl`
4. `bash -n tools/cloudflare/poller.sh`
5. Hidden launch verified through the VM command server health endpoint
6. One real remote control action verified through the relay
7. For any path called background-safe, frontmost-app sampling must show the host remains usable

## Definition Of Done

Work on the remote-control stack is done only when all of the following are true:

- The VM starts hidden
- The VM can be unlocked remotely
- At least one meaningful guest command runs remotely
- Secrets are not leaked in logs or traces
- The guest is left in a clean state
- The host user can keep working during the flow

## Current Standard Recommendation

Use `terminal_exec` or `exec` for meaningful remote work.

Treat generic shortcut-style control as experimental until repeated frontmost-app sampling shows it does not surface `virtualOS`.
