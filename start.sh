#!/usr/bin/env bash
# start.sh — build, launch virtualOS, and start the VM in the background.
# The app unlocks after detecting the guest password prompt when configured.

set -euo pipefail
cd "$(dirname "$0")"

bundle_override="${VOS_VM_BUNDLE:-}"
r2_lab_override="${VOS_NBD_R2_LAB:-}"
env_file="${HOME}/.config/virtualos/start.env"
if [[ -r "$env_file" ]]; then
  # shellcheck disable=SC1090
  source "$env_file"
fi
if [[ -n "$bundle_override" ]]; then
  VOS_VM_BUNDLE="$bundle_override"
fi

if [[ $# -ne 0 ]]; then
  echo "Usage: ./start.sh" >&2
  exit 2
fi

APP_ARGS=(--background-vm --auto-start-vm)
if [[ -n "$r2_lab_override" && "$r2_lab_override" != 1 ]]; then
  echo 'VOS_NBD_R2_LAB must be 1 when set.' >&2
  exit 2
fi
if [[ "$r2_lab_override" == 1 ]]; then
  expected_r2_bundle="$HOME/VirtualOSMachines/.virtualOS-r2-metadata.bundle"
  if [[ "$bundle_override" != "$expected_r2_bundle" ]]; then
    echo 'NBD R2 lab requires an explicit VOS_VM_BUNDLE pointing to its metadata-only bundle.' >&2
    exit 1
  fi
  ./tools/computer-space.sh --nbd-r2-metadata-check >/dev/null
  if [[ -n "${VOS_INSTALLED_APP:-}" ]]; then
    echo 'NBD R2 lab requires a build from this source; VOS_INSTALLED_APP is not allowed.' >&2
    exit 1
  fi
  APP_ARGS+=(--nbd-r2-lab)
fi
if [[ -n "${VOS_VM_BUNDLE:-}" ]]; then
  if [[ "$VOS_VM_BUNDLE" != *.bundle || ! -d "$VOS_VM_BUNDLE" ]]; then
    echo "VOS_VM_BUNDLE must point to an existing .bundle directory." >&2
    exit 1
  fi
  APP_ARGS+=(--vm-bundle "$VOS_VM_BUNDLE")
fi

VM_COMMAND_SERVER="http://127.0.0.1:8845"
if curl -sf --max-time 1 "$VM_COMMAND_SERVER/vm/health" 2>/dev/null | jq -e '.ready == true and .state == "running"' >/dev/null; then
  if [[ "$r2_lab_override" == 1 ]]; then
    echo 'NBD R2 lab refused: another VM is running.' >&2
    exit 1
  fi
  echo "==> VM is already running."
  exit 0
fi

if [[ -d /System/Volumes/Data ]]; then
  free_kb=$(df -Pk /System/Volumes/Data | awk 'NR == 2 { print $4 }')
  if [[ "$free_kb" =~ ^[0-9]+$ ]] && (( free_kb < 10 * 1024 * 1024 )); then
    echo "Cannot start VM: Data volume has $((free_kb / 1024 / 1024)) GiB free; at least 10 GiB is required." >&2
    exit 1
  fi
  if [[ "$r2_lab_override" == 1 && "$free_kb" =~ ^[0-9]+$ ]] && (( free_kb < 11 * 1024 * 1024 )); then
    echo 'Cannot start NBD R2 lab: at least 11 GiB host free is required.' >&2
    exit 1
  fi
fi

if [[ -n "${VOS_INSTALLED_APP:-}" ]]; then
  APP="$VOS_INSTALLED_APP"
  if [[ ! -d "$APP" ]]; then
    echo "Installed app is missing: $APP" >&2
    exit 1
  fi
else
  PROJECT="virtualOS.xcodeproj"
  SCHEME="virtualOS"
  BUILD_DIR="build/DerivedData"
  APP="$BUILD_DIR/Build/Products/Debug/virtualOS.app"

  echo "==> Building $SCHEME..."
  xcodebuild -quiet -project "$PROJECT" -scheme "$SCHEME" -configuration Debug \
    -destination 'platform=macOS,arch=arm64' -derivedDataPath "$BUILD_DIR" \
    CODE_SIGN_IDENTITY=- DEVELOPMENT_TEAM= CODE_SIGN_STYLE=Manual build
fi

echo "==> Quitting existing instance (if any)..."
osascript -e 'tell application "virtualOS" to quit' 2>/dev/null || true
sleep 1

wait_for_vm_running() {
  local attempts=30
  while (( attempts > 0 )); do
    if curl -sf --max-time 1 "$VM_COMMAND_SERVER/vm/health" 2>/dev/null | jq -e '.ready == true and .state == "running"' >/dev/null; then
      return 0
    fi
    sleep 1
    attempts=$((attempts - 1))
  done
  return 1
}

echo "==> Launching virtualOS..."
open -g "$APP" --args "${APP_ARGS[@]}"
sleep 2

echo "==> Waiting for background VM start..."
if ! wait_for_vm_running; then
  echo "VM did not reach running state in time." >&2
  exit 1
fi

echo "==> VM started in background."
