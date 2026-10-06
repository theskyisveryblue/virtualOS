# Mac Mini Host Setup

This folder packages the Mac mini bootstrap and startup flow into the repo so it does not depend on email snippets or chat history.

## What This Covers

- SSH key install for the host user
- Remote Login enablement
- Xcode CLI tools check
- Optional Homebrew, Cursor, and Tailscale install
- Power settings to keep the machine reachable
- LaunchAgent setup for running `start.sh` at login
- LaunchAgent setup for running the remote host service at login

## Assumptions

- The Mac mini is signed into a user account that will own the repo, for example `ai`
- The repo is cloned locally on the Mac mini
- `start.sh` exists at the repo root and is the entrypoint for launching `virtualOS`
- The Mac mini will be used as a GUI host, not only a headless SSH box

## Quick Start On The Mac Mini

From the repo root on the Mac mini:

```bash
chmod +x tools/mac_mini/bootstrap.sh
./tools/mac_mini/bootstrap.sh \
  --enable-remote-login \
  --disable-sleep \
  --install-homebrew \
  --install-cursor \
  --install-tailscale
```

If you prefer Finder double-click entrypoints instead of terminal commands:

- `tools/mac_mini/one_click_remote.command`
- `tools/mac_mini/setup_codex_access.command`
- `tools/mac_mini/start_remote_site.command`
- `tools/mac_mini/install_remote_host_agent.command`

If you want to trust a new SSH public key at the same time:

```bash
./tools/mac_mini/bootstrap.sh \
  --ssh-public-key-file /path/to/key.pub \
  --enable-remote-login \
  --disable-sleep
```

## Install Login Startup

If you want the existing shell startup flow to start the VM at login and check
its health every 30 seconds:

```bash
chmod +x tools/mac_mini/install_launch_agent.sh
./tools/mac_mini/install_launch_agent.sh
```

That installer creates:

- `~/Library/LaunchAgents/com.github.yep.virtualos.start.plist`
- `~/Library/Logs/virtualos-startup.log`

The LaunchAgent uses:

- `tools/mac_mini/start_virtualos_at_login.sh`
- `start.sh`
- optional env: `~/.config/virtualos/start.env`

The installer copies those existing scripts into
`~/Library/Application Support/virtualOS/launch-agent/` and runs the installed
`/Applications/virtualOS.app`. Re-run the installer after editing either script.
macOS does not allow this LaunchAgent to execute scripts directly from the
repo's Documents location.

Set `VOS_VM_BUNDLE=/absolute/path/to/your.bundle` in that env file when the VM
is outside the default Documents location. On the current Mac mini the VM lives
at `/Users/ai/VirtualOSMachines/virtualOS.bundle`.

The LaunchAgent starts the VM in the background. The app detects the guest
password prompt and unlocks it when a saved password is configured. Create:

```bash
mkdir -p ~/.config/virtualos
cat > ~/.config/virtualos/start.env <<'EOF'
VOS_VM_BUNDLE=/Users/ai/VirtualOSMachines/virtualOS.bundle
VOS_VM_PASSWORD=replace-me
EOF
chmod 600 ~/.config/virtualos/start.env
```

## Install Remote Host Startup

If you want the remote host server to start automatically at login and restart if it dies:

```bash
chmod +x tools/mac_mini/install_remote_host_agent.sh
./tools/mac_mini/install_remote_host_agent.sh
```

Finder click path:

- `tools/mac_mini/install_remote_host_agent.command`

That installer creates:

- `~/Library/LaunchAgents/com.github.yep.virtualos.remotehost.plist`
- `~/Library/Logs/virtualos-remote-host.log`

The LaunchAgent uses:

- `tools/mac_mini/start_remote_host_at_login.sh`
- optional env: `~/.config/virtualos/remote_host.env`

The remote host binds to loopback by default. To allow LAN connections, set
`VOS_REMOTE_HOST=0.0.0.0` and a strong `VOS_AUTH_TOKEN` in that env file.
`start_remote_site.command` also requires `VOS_AUTH_TOKEN` for its LAN bind.

Default watched repos:

- `~/Documents/GitHub/virtualOS`
- `~/Documents/GitHub/ai-share`

## Manual Checks

After bootstrap:

1. Open Xcode once and let it finish extra component installs.
2. Confirm `Remote Login` is enabled in `System Settings > General > Sharing`.
3. If using Tailscale, sign in from the app and verify the machine appears on your tailnet.
4. Run `./start.sh` once manually before relying on the LaunchAgent.
5. Grant Accessibility and Screen Recording permissions to any app that needs input automation or capture.

## Notes

- `brew install --cask cursor` installs Cursor, but you still need to sign in after install.
- The LaunchAgent runs only for a logged-in Aqua session, which is required for `open` and `osascript`.
- Keep secrets outside the repo. The startup password file lives in `~/.config/virtualos/start.env`.
- The remote host env file, if used, lives in `~/.config/virtualos/remote_host.env`.
