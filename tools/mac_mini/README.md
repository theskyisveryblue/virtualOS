# Mac Mini Host Setup

This folder packages the Mac mini bootstrap and startup flow into the repo so it does not depend on email snippets or chat history.

## Host security review — October 19, 2026

Read-only checks on macOS 26.2 (25C56) found FileVault off, the application
firewall disabled and stealth mode off. SIP and Gatekeeper are enabled.
Stored firewall rules permit both `sshd` and `sshd-keygen-wrapper`; this has
not been verified with the firewall enabled. Project native control listeners
were limited to `127.0.0.1:8845` and `127.0.0.1:9225` in the scoped check.
See the [measured receipt](../experiments/mini-host-security-review-20261019.json).
Apple's [security release list](https://support.apple.com/en-us/100100), checked
on October 19, lists the newer Tahoe 26.7.1 security update dated September 28.
The host update catalog was not queried and no payload was downloaded. OS
maintenance needs a space budget and an approved restart window after recovery
verification.

Effective SSH authentication settings remain unverified: unprivileged `sshd -T`
cannot read host keys, and noninteractive admin access is unavailable. Commented
configuration examples do not establish effective password or root-login policy.
An owner admin review must preserve the pinned SSH route and verify a fresh
key-authenticated connection before restricting authentication.

Enabling the firewall needs a host maintenance change that preserves SSH rules,
a prearranged rollback and a fresh connection without SSH multiplexing. FileVault
needs owner authentication, private recovery-key handling and a verified restart
unlock plan. These settings were not changed by the review. Apple silicon
storage remains hardware-encrypted with FileVault off; FileVault adds protection
requiring user credentials, as described in
[Apple's FileVault guide](https://support.apple.com/guide/deployment/intro-to-filevault-dep82064ec40/web).

## Emergency access and recovery plan

Run the bounded, read-only check from the operator Mac:

```sh
python3 tools/mac_mini/check_remote_access.py
```

It first checks the existing credential-file emergency SSH configuration in
`~/Documents/GitHub/realestate-machine/.local/mini-remote-20260915/ssh_config`,
when present, then falls back to the two default SSH aliases. All probes use
strict host-key verification. It reports guest-relay health separately.
Exit status 1 means no checked SSH route worked, even if the VM relay is healthy.
It never starts a service, changes
network settings, or queues commands.

### Recovered off-site access on October 13

The working emergency route is **`mac-mini.oto.house`**, configured in the
separate file above. A fresh off-site SSH session passed using the existing
dedicated Access credential, the existing operator SSH key and the recorded
Mini host identity under `192.168.1.123`. The hostname returned
`ais-Mac-mini.local`; production PID 94084 and fleet supervisor PID 2301 remained
running. The default `mac-mini-cloudflare` alias still points at obsolete DNS.

Use the existing protected configuration directly for host work:

```sh
ssh -F "$HOME/Documents/GitHub/realestate-machine/.local/mini-remote-20260915/ssh_config" \
  -o BatchMode=yes mac-mini-cloudflare 'hostname; df -h /System/Volumes/Data'
```

This host tunnel belongs to the existing Harris account. No resources in that
account were changed. A read-only inventory through CloudScope's active Ai
credential found no tunnels in the Ai account. All virtualOS cloud storage and
relay resources remain in Ai; moving host access into Ai is still planned.

The cache startup-budget fix was atomically installed in
`/Users/ai/vos-shared-fleet/tools/cloud_disk.py` after checking the previous
source checksum. The previous source was retained, and the Mini's full storage
self-test passed. This updates the cold-test tools; it does not migrate either
running VM disk or restart a VM.
Unused project `build/DerivedData` was removed after checking open files and
active builds, increasing free space by 175,337,472 bytes. The temporary
OpenEditor checkouts, booted simulator and open browser cache were preserved.
Evidence is in `tools/experiments/mac-mini-emergency-recovery-20261013.json`.

### Evidence on October 13, 2026

- LAN SSH to `192.168.1.123` worked briefly, allowing a measured 1.47 GB cache
  cleanup, then became unreachable after the operator Mac changed networks.
- `mac-mini-cloudflare` uses `mac-mini.mergerealestate.ca`; that name does not
  resolve. The existing tunnel therefore cannot carry SSH from this network.
- The `ai-share/scripts/controller-mini.sh` emergency shell queue exists, but
  its local controller on port 8897 is stopped. Both recorded temporary
  controller tunnel names fail DNS resolution. Its handoff is over three
  months old, and the Mini reads a separate local copy of that handoff.
  Starting a new operator tunnel alone cannot update the unreachable Mini.
- The older rendezvous Worker source in `terraform-ai` supports bounded jobs,
  not an arbitrary host shell. Its checked-in account ID is different from the
  authorized Ai account, and referenced controller/install scripts are missing
  in that checkout. It is reference material, not verified working access.
- The virtualOS relay has a fresh fleet heartbeat, but its browser credential
  controls guest operations only. It cannot repair host networking or deploy
  host tools.

### Recovery sequence

1. Recover one authenticated host path: an existing working remote SSH/VPN
   address, a restored named tunnel with an already-running connector, or the
   next successful LAN SSH session. Confirm the Mini's recorded SSH host key
   and hostname before changing anything. No physical visit is required if an
   existing host route becomes reachable; otherwise installing the first host
   recovery agent requires a reachable host session.
2. Inspect the Mini's actual `cloudflared` process, LaunchAgent, tunnel UUID,
   ingress and connector status. Confirm tunnel ownership in
   `ai@damidina.com` (`c24a3fb4bedba669785c18c834f51921`) before restoring DNS.
   A DNS record alone is insufficient without a live SSH ingress and Access
   policy. Choose a stable hostname in a zone owned by that account; the
   historical hostname's zone ownership has not been verified.
3. Install the named-tunnel connector on the Mini as a supervised login
   service with restart/backoff and an SSH ingress to `localhost:22`.
   Keep the host SSH key and use Cloudflare Access for the operator identity.
   Check tunnel, SSH and guest relay independently.
4. Add an independent outbound recovery agent on the Mini, using a stable
   HTTPS Worker in the same Ai account. Separate device and operator tokens.
   Start with named operations: host status, disk inventory, connector status,
   restart the connector and deploy a checksum-pinned reviewed tool update.
   Keep it separate from guest browser permissions. Jobs need unique IDs,
   expiry, bounded execution/output, atomic claims, retry leases, idempotent
   handlers and durable completion receipts. An expired queued maintenance
   job must not execute when a long-offline host returns.
5. Prove access from a different network with LAN SSH unavailable. Read
   hostname and free space over remote SSH; then stop only the tunnel connector
   and recover it through the independent agent. Restart the recovery agent
   and confirm supervised restart. Ensure both running VMs remain up.
6. Deploy the tested cloud-cache startup-budget fix, confirm its checksum and
   run the storage self-test in a small disposable directory. Inventory the
   temporary checkouts and simulator data with open-file/process checks before
   any deletion. Record actual free-space gains, not summed APFS bundle sizes.

The existing host tunnel is now verified from off-site. An Ai-owned host tunnel
and independent outbound host recovery agent have not been installed by this
work.
The SSH tunnel configuration should follow
[Cloudflare's client-side cloudflared SSH instructions](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/use-cases/ssh/ssh-cloudflared-authentication/).

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
