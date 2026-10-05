#!/usr/bin/env bash
# VM storage probes and narrow regenerable-cache cleanup.
set -euo pipefail

if [[ "${1:-}" == --display-diagnostics ]]; then
  [[ $# -eq 1 ]] || { echo 'display_diagnostics=refused: no extra arguments' >&2; exit 2; }
  repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
  echo 'display_diagnostics=read_only'
  "$repo_dir/tools/vosctl" status | sed -n '1,4p'
  window_count="$(osascript -e 'tell application "System Events" to tell process "virtualOS" to count of windows' 2>/dev/null || echo unknown)"
  frontmost="$(osascript -e 'tell application "System Events" to tell process "virtualOS" to get frontmost' 2>/dev/null || echo unknown)"
  printf 'host_window_count=%s host_frontmost=%s\n' "$window_count" "$frontmost"
  for sample in 1 2 3; do
    "$repo_dir/tools/vosctl" computer doctor | python3 -c 'import json,sys; d=json.load(sys.stdin); print("guest_sample=%s usable=%s jpeg_bytes=%s active_app=%s" % (sys.argv[1],str(d.get("screenshot_usable")).lower(),d.get("jpeg_bytes"),"present" if d.get("active_app") else "absent"))' "$sample"
    [[ $sample -eq 3 ]] || sleep 2
  done
  sysctl vm.swapusage
  exit 0
fi

if [[ "${1:-}" == --guest-accessibility-authorize ]]; then
  [[ $# -eq 1 ]] || { echo 'guest_accessibility_authorize=refused: no extra arguments' >&2; exit 2; }
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  python3 "$script_dir/computer_use.py" guest-accessibility-authorize
  exit 0
fi

if [[ "${1:-}" == --guest-ax-verify ]]; then
  [[ $# -eq 2 && "$2" == finder_window_present ]] || {
    echo 'guest_ax_verify=refused: only finder_window_present is supported' >&2; exit 2;
  }
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  VOS_GUEST_AX_STATE="$2" python3 "$script_dir/computer_use.py" guest-tree
  exit 0
fi

if [[ "${1:-}" == --guest-ax-delayed-focus ]]; then
  [[ $# -eq 1 ]] || { echo 'guest_ax_delayed_focus=refused: no extra arguments' >&2; exit 2; }
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  python3 "$script_dir/computer_use.py" guest-ax-delayed-focus
  exit 0
fi

if [[ "${1:-}" == --ax-codec-measure ]]; then
  [[ $# -eq 1 ]] || { echo 'ax_codec_measure=refused: no extra arguments' >&2; exit 2; }
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  python3 "$script_dir/ax_codec.py" measure
  exit 0
fi

if [[ "${1:-}" == --reclaim-operator-2degrees-simulator ]]; then
  [[ $# -eq 1 && "$(id -un)" == theone ]] || {
    echo 'operator_simulator_reclaim=refused: operator account required' >&2; exit 2;
  }
  device_id=6DC78E02-5B70-4E95-B946-873A57EB927F
  state="$(xcrun simctl list devices --json | python3 -c '
import json,sys
devices=json.load(sys.stdin)["devices"]
matches=[d for values in devices.values() for d in values if d.get("udid")==sys.argv[1]]
print(matches[0].get("state") if len(matches)==1 and matches[0].get("name")=="2degrees Offline QA" else "invalid")
' "$device_id")"
  [[ "$state" == Shutdown ]] || {
    echo 'operator_simulator_reclaim=refused: named device is not shutdown' >&2; exit 1;
  }
  before_kib="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  xcrun simctl delete "$device_id"
  after_kib="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  printf 'operator_simulator_reclaim=complete device=%s gained_kib=%s free_kib=%s\n' "$device_id" "$((after_kib - before_kib))" "$after_kib"
  exit 0
fi

if [[ "${1:-}" == --reclaim-operator-morph-simulator ]]; then
  [[ $# -eq 1 && "$(id -un)" == theone ]] || {
    echo 'operator_morph_reclaim=refused: operator account required' >&2; exit 2;
  }
  device_id=B24AAAFF-429A-47BC-8D84-81C5FE09F750
  state="$(xcrun simctl list devices --json | python3 -c '
import json,sys
devices=json.load(sys.stdin)["devices"]
matches=[d for values in devices.values() for d in values if d.get("udid")==sys.argv[1]]
print(matches[0].get("state") if len(matches)==1 and matches[0].get("name")=="Morph Cam CI" else "invalid")
' "$device_id")"
  [[ "$state" == Shutdown ]] || {
    echo 'operator_morph_reclaim=refused: named device is not shutdown' >&2; exit 1;
  }
  if ps -axo comm | grep -Eq 'Simulator[.]app|CoreSimulator/Volumes/'; then
    echo 'operator_morph_reclaim=refused: simulator runtime may be active' >&2; exit 1;
  fi
  before_kib="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  xcrun simctl delete "$device_id"
  after_kib="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  printf 'operator_morph_reclaim=complete device=%s gained_kib=%s free_kib=%s\n' "$device_id" "$((after_kib - before_kib))" "$after_kib"
  exit 0
fi

if [[ "${1:-}" == --reclaim-operator-iphone16-simulator ]]; then
  [[ $# -eq 1 && "$(id -un)" == theone ]] || {
    echo 'operator_simulator_reclaim=refused: operator account required' >&2; exit 2;
  }
  device_id=673B6738-7FB8-49D0-AB38-2DC513794120
  state="$(xcrun simctl list devices --json | python3 -c '
import json,sys
devices=json.load(sys.stdin)["devices"]
matches=[d for values in devices.values() for d in values if d.get("udid")==sys.argv[1]]
print(matches[0].get("state") if len(matches)==1 and matches[0].get("name")=="iPhone 16 Pro" else "invalid")
' "$device_id")"
  [[ "$state" == Shutdown ]] || {
    echo 'operator_simulator_reclaim=refused: named device is not shutdown' >&2; exit 1;
  }
  before_kib="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  xcrun simctl delete "$device_id"
  after_kib="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  printf 'operator_simulator_reclaim=complete device=%s gained_kib=%s free_kib=%s\n' "$device_id" "$((after_kib - before_kib))" "$after_kib"
  exit 0
fi

if [[ "${1:-}" == --reclaim-mini-morph-simulator ]]; then
  [[ $# -eq 1 && "$(id -un)" == ai ]] || {
    echo 'mini_simulator_reclaim=refused: Mini ai account required' >&2; exit 2;
  }
  device_id=2F14B60C-0AA6-4FF4-89BA-C8A5DA114197
  state="$(xcrun simctl list devices --json | python3 -c '
import json,sys
devices=json.load(sys.stdin)["devices"]
matches=[d for values in devices.values() for d in values if d.get("udid")==sys.argv[1]]
print(matches[0].get("state") if len(matches)==1 and matches[0].get("name")=="Morph Cam CI" else "invalid")
' "$device_id")"
  [[ "$state" == Shutdown ]] || {
    echo 'mini_simulator_reclaim=refused: named device is not shutdown' >&2; exit 1;
  }
  before_kib="$(df -Pk /System/Volumes/Data | awk 'NR==2 {print $4}')"
  xcrun simctl delete "$device_id"
  after_kib="$(df -Pk /System/Volumes/Data | awk 'NR==2 {print $4}')"
  printf 'mini_simulator_reclaim=complete device=%s gained_kib=%s free_kib=%s\n' "$device_id" "$((after_kib - before_kib))" "$after_kib"
  exit 0
fi

if [[ "${1:-}" == --reclaim-operator-playwright-cache ||
      "${1:-}" == --reclaim-operator-puppeteer-cache ]]; then
  [[ $# -eq 1 && "$(id -un)" == theone ]] || {
    echo 'operator_browser_reclaim=refused: operator account required' >&2; exit 2;
  }
  if [[ "$1" == --reclaim-operator-playwright-cache ]]; then
    cache_dir="$HOME/Library/Caches/ms-playwright"
  else
    cache_dir="$HOME/.cache/puppeteer"
  fi
  [[ ! -e "$cache_dir" || ( -d "$cache_dir" && ! -L "$cache_dir" ) ]] || {
    echo 'operator_browser_reclaim=refused: unexpected cache path' >&2; exit 2;
  }
  process_list="$(ps -axo comm)" || {
    echo 'operator_browser_reclaim=refused: process list unavailable' >&2; exit 1;
  }
  if printf '%s\n' "$process_list" | awk 'tolower($0) ~ /playwright|headless.?shell|chromium|firefox|webkit/ {found=1} END {exit !found}'; then
    echo 'operator_browser_reclaim=refused: browser process may use cache' >&2; exit 1;
  fi
  if [[ -d "$cache_dir" ]]; then
    command -v lsof >/dev/null || {
      echo 'operator_browser_reclaim=refused: lsof unavailable' >&2; exit 1;
    }
    report="$(mktemp /private/tmp/vos-lsof.XXXXXX)"
    if lsof -nP +D "$cache_dir" > "$report" 2>&1 || [[ -s "$report" ]]; then
      rm -f "$report"
      echo 'operator_browser_reclaim=refused: cache files may be open' >&2; exit 1;
    fi
    rm -f "$report"
  fi
  before_kib="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  [[ ! -d "$cache_dir" ]] || rm -rf -- "$cache_dir"
  after_kib="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  printf 'operator_browser_reclaim=complete target=%s gained_kib=%s free_kib=%s\n' "$1" "$((after_kib - before_kib))" "$after_kib"
  exit 0
fi

if [[ "${1:-}" == --reclaim-operator-uv-cache ]]; then
  [[ $# -eq 1 && "$(id -un)" == theone ]] || {
    echo 'operator_uv_reclaim=refused: operator account required' >&2; exit 2;
  }
  cache_dir="$HOME/.cache/uv"
  [[ ! -e "$cache_dir" || ( -d "$cache_dir" && ! -L "$cache_dir" ) ]] || {
    echo 'operator_uv_reclaim=refused: unexpected cache path' >&2; exit 2;
  }
  process_list="$(ps -axo comm)" || {
    echo 'operator_uv_reclaim=refused: process list unavailable' >&2; exit 1;
  }
  if printf '%s\n' "$process_list" | awk '$1 ~ /(^|\/)(uv|uvx)$/ {found=1} END {exit !found}'; then
    echo 'operator_uv_reclaim=refused: uv is running' >&2; exit 1;
  fi
  command -v uv >/dev/null || { echo 'operator_uv_reclaim=refused: uv unavailable' >&2; exit 1; }
  before_kib="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  UV_CACHE_DIR="$cache_dir" uv cache clean >/dev/null
  after_kib="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  printf 'operator_uv_reclaim=complete gained_kib=%s free_kib=%s\n' "$((after_kib - before_kib))" "$after_kib"
  exit 0
fi

if [[ "${1:-}" == --reclaim-operator-go-cache ]]; then
  [[ $# -eq 1 && "$(id -un)" == theone ]] || {
    echo 'operator_go_reclaim=refused: operator account required' >&2; exit 2;
  }
  command -v go >/dev/null || { echo 'operator_go_reclaim=refused: Go unavailable' >&2; exit 1; }
  process_list="$(ps -axo comm)" || {
    echo 'operator_go_reclaim=refused: process list unavailable' >&2; exit 1;
  }
  if printf '%s\n' "$process_list" | awk '$1 ~ /(^|\/)(go|compile|link)$/ {found=1} END {exit !found}'; then
    echo 'operator_go_reclaim=refused: Go build is running' >&2; exit 1;
  fi
  before_kib="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  GOCACHE="$HOME/Library/Caches/go-build" go clean -cache
  after_kib="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  printf 'operator_go_reclaim=complete gained_kib=%s free_kib=%s\n' "$((after_kib - before_kib))" "$after_kib"
  exit 0
fi

if [[ "${1:-}" == --reclaim-operator-build-cache ]]; then
  [[ $# -eq 1 && "$(id -un)" == theone ]] || {
    echo 'operator_build_reclaim=refused: operator account required' >&2; exit 2;
  }
  repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
  process_list="$(ps -axo comm)" || {
    echo 'operator_build_reclaim=refused: process list unavailable' >&2; exit 1;
  }
  if printf '%s\n' "$process_list" | awk '$1 ~ /(^|\/)(swiftc|swift-frontend|swift-build|xcodebuild)$/ {found=1} END {exit !found}'; then
    echo 'operator_build_reclaim=refused: a build is running' >&2; exit 1;
  fi
  command -v lsof >/dev/null || { echo 'operator_build_reclaim=refused: lsof unavailable' >&2; exit 1; }
  for target in "$repo_dir/build/DD" "$repo_dir/build/DerivedData"; do
    [[ ! -e "$target" || ( -d "$target" && ! -L "$target" ) ]] || {
      echo 'operator_build_reclaim=refused: unexpected build path' >&2; exit 1;
    }
    if [[ -d "$target" ]]; then
      report="$(mktemp /private/tmp/vos-lsof.XXXXXX)"
      if lsof -nP +D "$target" > "$report" 2>&1 || [[ -s "$report" ]]; then
        rm -f "$report"
        echo 'operator_build_reclaim=refused: build files may be open' >&2; exit 1;
      fi
      rm -f "$report"
    fi
  done
  before_kib="$(df -Pk "$repo_dir" | awk 'NR==2 {print $4}')"
  for target in "$repo_dir/build/DD" "$repo_dir/build/DerivedData"; do
    [[ ! -d "$target" ]] || rm -rf -- "$target"
  done
  after_kib="$(df -Pk "$repo_dir" | awk 'NR==2 {print $4}')"
  printf 'operator_build_reclaim=complete gained_kib=%s free_kib=%s\n' "$((after_kib - before_kib))" "$after_kib"
  exit 0
fi

if [[ "${1:-}" == --cloud-disk-r2-upload-clone ]]; then
  [[ $# -eq 1 && "${VOS_CLOUD_DISK_UPLOAD_ACT:-}" == 1 &&
     -n "${VOS_CLOUD_DISK_CONFIG:-}" && -r "$VOS_CLOUD_DISK_CONFIG" &&
     -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" &&
     -n "${CLOUDFLARE_ACCOUNT_ID:-}" && -n "${CLOUDFLARE_R2_ENDPOINT:-}" ]] || {
    echo 'r2_clone_upload=refused: explicit activation, config, and pinned credentials required' >&2; exit 2;
  }
  source="$HOME/VirtualOSMachines/.virtualOS-r2-staging.bundle/Disk.img"
  [[ -f "$source" && ! -L "$source" && -f "${source%/*}/.complete" ]] || {
    echo 'r2_clone_upload=refused: completed isolated RAW clone required' >&2; exit 2;
  }
  command -v lsof >/dev/null || { echo 'r2_clone_upload=refused: lsof unavailable' >&2; exit 2; }
  open_handles="$(lsof -F p -- "$source" 2>&1)" && {
    echo 'r2_clone_upload=refused: clone RAW is open' >&2; exit 2;
  }
  [[ -z "$open_handles" ]] || { echo 'r2_clone_upload=refused: clone RAW state uncertain' >&2; exit 2; }
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  python3 "$script_dir/cloud_disk.py" r2-upload-clone "$VOS_CLOUD_DISK_CONFIG"
  exit 0
fi

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --reclaim-caches && "$1" != --reclaim-local-lab && "$1" != --vm-summary && "$1" != --raw-hole-lab && "$1" != --nbd-api-probe && "$1" != --nbd-source-build-check && "$1" != --nbd-r2-metadata-prepare && "$1" != --nbd-r2-metadata-check && "$1" != --nbd-bundle-serve && "$1" != --nbd-vz-lab && "$1" != --nbd-macos-config-check && "$1" != --nbd-r2-macos-config-check && "$1" != --nbd-macos-clone-test && "$1" != --nbd-clone-serve && "$1" != --guest-tree-probe && "$1" != --guest-tree-transport && "$1" != --guest-hex-smoke && "$1" != --guest-accessibility-open && "$1" != --intent-plan-compile && "$1" != --promote-debug-app && "$1" != --virtualization-api-inventory && "$1" != --cloud-disk-self-test && "$1" != --cloud-disk-checkpoint-self-test && "$1" != --cloud-disk-config-check && "$1" != --cloud-disk-lab-pack && "$1" != --cloud-disk-lab-serve && "$1" != --cloud-disk-r2-serve && "$1" != --cloud-disk-r2-boot-lab && "$1" != --cloud-disk-r2-checkpoint-lab ) ]]; then
  echo 'Usage: ./tools/computer-space.sh [--display-diagnostics|--guest-ax-verify finder_window_present|--reclaim-operator-playwright-cache|--reclaim-operator-puppeteer-cache|--reclaim-operator-uv-cache|--reclaim-operator-go-cache|--reclaim-operator-build-cache|--reclaim-caches|--reclaim-local-lab|--vm-summary|--raw-hole-lab|--nbd-api-probe|--nbd-source-build-check|--nbd-r2-metadata-prepare|--nbd-r2-metadata-check|--nbd-bundle-serve|--nbd-vz-lab|--nbd-macos-config-check|--nbd-r2-macos-config-check|--nbd-macos-clone-test|--nbd-clone-serve|--guest-tree-probe|--guest-tree-transport|--guest-hex-smoke|--guest-accessibility-open|--guest-accessibility-authorize|--intent-plan-compile|--promote-debug-app|--virtualization-api-inventory|--cloud-disk-self-test|--cloud-disk-config-check|--cloud-disk-lab-pack|--cloud-disk-lab-serve|--cloud-disk-r2-serve|--cloud-disk-r2-upload-clone|--cloud-disk-r2-boot-lab|--cloud-disk-r2-checkpoint-lab]' >&2
  exit 2
fi

if [[ "${1:-}" == --guest-hex-smoke ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  python3 "$script_dir/computer_use.py" guest-hex-smoke
  exit 0
fi

if [[ "${1:-}" == --reclaim-local-lab ]]; then
  process_list="$(ps -axo comm)" || { echo 'local_lab_reclaim=refused: process list unavailable' >&2; exit 1; }
  if printf '%s\n' "$process_list" | awk '$1 ~ /(^|\/)(swiftc|swift-frontend|xcodebuild)$/ {found=1} END {exit !found}'; then
    echo 'local_lab_reclaim=refused: a Swift or Xcode build is running' >&2
    exit 1
  fi
  before_kib="$(df -Pk /private/tmp | awk 'NR==2 {print $4}')"
  for target in /private/tmp/virtualos-build-check /private/tmp/vos-clang-module-cache; do
    if [[ -d "$target" && ! -L "$target" ]]; then rm -rf -- "$target"; fi
  done
  after_kib="$(df -Pk /private/tmp | awk 'NR==2 {print $4}')"
  printf 'local_lab_reclaim=complete gained_kib=%s free_kib=%s\n' "$((after_kib - before_kib))" "$after_kib"
  exit 0
fi

if [[ "${1:-}" == --intent-plan-compile ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  [[ -n "${VOS_INTENT_PLAN:-}" ]] || { echo 'intent_plan=refused: VOS_INTENT_PLAN path required' >&2; exit 2; }
  python3 "$script_dir/intent_plan.py" compile "$VOS_INTENT_PLAN"
  exit 0
fi

if [[ "${1:-}" == --promote-debug-app ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  repo_dir="$(cd "$script_dir/.." && pwd)"
  candidate="$repo_dir/build/DerivedData/Build/Products/Debug/virtualOS.app"
  installed=/Applications/virtualOS.app
  next="/Applications/.virtualOS-next-$$.app"
  previous="/Applications/.virtualOS-previous-$$.app"
  agent="$HOME/Library/LaunchAgents/com.github.yep.virtualos.start.plist"
  domain="gui/$(id -u)"
  disk="$HOME/VirtualOSMachines/virtualOS.bundle/Disk.img"
  [[ -d "$candidate" && -d "$installed" && -f "$agent" && -f "$disk" ]] || {
    echo 'app_promote=refused: candidate, installed app, agent, or production disk absent' >&2; exit 1;
  }
  codesign --verify --deep --strict "$candidate"
  agent_was_loaded=0
  moved_old=0
  promoted=0
  restore_app() {
    result=$?
    trap - EXIT
    if (( result != 0 )); then
      osascript -e 'tell application "virtualOS" to quit' >/dev/null 2>&1 || true
      if (( promoted )); then mv "$installed" "$next" || true; fi
      if (( moved_old )); then mv "$previous" "$installed" || true; fi
      VOS_INSTALLED_APP="$installed" "$repo_dir/start.sh" || true
      echo 'app_promote=rolled_back' >&2
    fi
    if (( agent_was_loaded )); then
      launchctl bootstrap "$domain" "$agent" || true
      launchctl kickstart "$domain/com.github.yep.virtualos.start" || true
    fi
    [[ ! -e "$next" ]] || rm -rf "$next"
    if (( result == 0 )); then rm -rf "$previous"; fi
    exit "$result"
  }
  trap restore_app EXIT
  ditto "$candidate" "$next"
  codesign --verify --deep --strict "$next"
  if launchctl list com.github.yep.virtualos.start >/dev/null 2>&1; then
    agent_was_loaded=1
    launchctl bootout "$domain" "$agent"
  fi
  "$repo_dir/tools/vosctl" stop
  for (( attempt=0; attempt<60; attempt++ )); do
    lsof "$disk" >/dev/null 2>&1 || break
    sleep 1
  done
  lsof "$disk" >/dev/null 2>&1 && { echo 'app_promote=failed: production disk still open' >&2; exit 1; }
  mv "$installed" "$previous"
  moved_old=1
  mv "$next" "$installed"
  promoted=1
  VOS_INSTALLED_APP="$installed" "$repo_dir/start.sh"
  echo 'app_promote=running_verified: previous app released after restart'
  exit 0
fi

if [[ "${1:-}" == --guest-tree-probe ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  python3 "$script_dir/computer_use.py" guest-tree
  exit 0
fi

if [[ "${1:-}" == --guest-tree-transport ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  VOS_GUEST_TREE_TRANSPORT_ONLY=1 python3 "$script_dir/computer_use.py" guest-tree
  exit 0
fi

if [[ "${1:-}" == --guest-accessibility-open ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  python3 "$script_dir/computer_use.py" guest-accessibility-open
  exit 0
fi

if [[ "${1:-}" == --nbd-r2-metadata-check ]]; then
  bundle="$HOME/VirtualOSMachines/.virtualOS-r2-metadata.bundle"
  [[ -d "$bundle" && ! -L "$bundle" && ! -e "$bundle/Disk.img" && ! -L "$bundle/Disk.img" ]] || {
    echo 'nbd_r2_metadata=refused: exact metadata-only bundle required' >&2; exit 1;
  }
  for name in HardwareModel MachineIdentifier AuxiliaryStorage Parameters.txt .r2-lab; do
    [[ -f "$bundle/$name" && ! -L "$bundle/$name" ]] || {
      echo "nbd_r2_metadata=refused: missing or linked $name" >&2; exit 1;
    }
  done
  [[ "$(cat "$bundle/.r2-lab")" == 'virtualos-r2-metadata/v1' ]] || {
    echo 'nbd_r2_metadata=refused: invalid lab marker' >&2; exit 1;
  }
  aux_bytes="$(stat -f %z "$bundle/AuxiliaryStorage")"
  [[ "$aux_bytes" =~ ^[0-9]+$ ]] && (( aux_bytes <= 64 * 1024 * 1024 )) || {
    echo 'nbd_r2_metadata=refused: auxiliary storage exceeds 64 MiB' >&2; exit 1;
  }
  printf 'nbd_r2_metadata=valid auxiliary_bytes=%s local_raw=absent\n' "$aux_bytes"
  exit 0
fi

if [[ "${1:-}" == --nbd-r2-metadata-prepare ]]; then
  source_bundle="$HOME/VirtualOSMachines/.virtualOS-r2-staging.bundle"
  target_bundle="$HOME/VirtualOSMachines/.virtualOS-r2-metadata.bundle"
  [[ -d "$source_bundle" && ! -L "$source_bundle" && -f "$source_bundle/.complete" &&
     ! -e "$target_bundle" && ! -L "$target_bundle" ]] || {
    echo 'nbd_r2_metadata=refused: completed staging clone required and target must be absent' >&2; exit 1;
  }
  for name in HardwareModel MachineIdentifier AuxiliaryStorage Parameters.txt; do
    [[ -f "$source_bundle/$name" && ! -L "$source_bundle/$name" ]] || {
      echo "nbd_r2_metadata=refused: source $name missing or linked" >&2; exit 1;
    }
  done
  aux_bytes="$(stat -f %z "$source_bundle/AuxiliaryStorage")"
  [[ "$aux_bytes" =~ ^[0-9]+$ ]] && (( aux_bytes <= 64 * 1024 * 1024 )) || {
    echo 'nbd_r2_metadata=refused: source auxiliary storage exceeds 64 MiB' >&2; exit 1;
  }
  mkdir "$target_bundle"
  prepared=0
  cleanup_r2_metadata() {
    if (( ! prepared )); then rm -rf -- "$target_bundle"; fi
  }
  trap cleanup_r2_metadata EXIT
  for name in HardwareModel MachineIdentifier AuxiliaryStorage Parameters.txt; do
    cp -p "$source_bundle/$name" "$target_bundle/$name"
  done
  printf 'virtualos-r2-metadata/v1\n' > "$target_bundle/.r2-lab"
  "$0" --nbd-r2-metadata-check
  prepared=1
  trap - EXIT
  echo 'nbd_r2_metadata=prepared: no RAW disk copied'
  exit 0
fi

if [[ "${1:-}" == --nbd-macos-clone-test ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  repo_dir="$(cd "$script_dir/.." && pwd)"
  bundle="$HOME/VirtualOSMachines/.virtualOS-r2-staging.bundle"
  production="$HOME/VirtualOSMachines/virtualOS.bundle/Disk.img"
  app="$repo_dir/build/DerivedData/Build/Products/Debug/virtualOS.app"
  agent="$HOME/Library/LaunchAgents/com.github.yep.virtualos.start.plist"
  domain="gui/$(id -u)"
  [[ -f "$bundle/.complete" && -f "$bundle/Disk.img" && -f "$production" &&
     -d "$app" && -f "$agent" ]] || {
    echo 'nbd_clone_test=refused: source clone, production disk, app, or startup agent unavailable' >&2; exit 1;
  }
  free_kib="$(df -Pk /System/Volumes/Data | awk 'NR==2 {print $4}')"
  (( free_kib >= 11534336 )) || {
    echo 'nbd_clone_test=refused: less than 11 GiB host free' >&2; exit 1;
  }
  curl -fsS --max-time 5 http://127.0.0.1:8845/vm/health \
    | python3 -c 'import json,sys; x=json.load(sys.stdin); assert x.get("ready") and x.get("state")=="running"' \
    || { echo 'nbd_clone_test=refused: production VM is not running' >&2; exit 1; }
  lab="$(mktemp -d /private/tmp/vos-nbd-clone-XXXXXXXX)"
  server_pid=''
  agent_was_loaded=0
  restored=0
  restore_clone_test() {
    result=$?
    trap - EXIT
    osascript -e 'tell application "virtualOS" to quit' >/dev/null 2>&1 || true
    for (( attempt=0; attempt<30; attempt++ )); do
      lsof "$bundle/Disk.img" >/dev/null 2>&1 || break
      sleep 1
    done
    if [[ -n "$server_pid" ]]; then
      kill "$server_pid" >/dev/null 2>&1 || true
      wait "$server_pid" 2>/dev/null || true
    fi
    if [[ -f "$lab/server.log" ]]; then
      tail -n 5 "$lab/server.log"
    fi
    rm -rf "$lab"
    if (( agent_was_loaded )); then
      launchctl bootstrap "$domain" "$agent" || true
      launchctl kickstart "$domain/com.github.yep.virtualos.start" || true
    else
      VOS_INSTALLED_APP=/Applications/virtualOS.app "$repo_dir/start.sh" || true
    fi
    for (( attempt=0; attempt<90; attempt++ )); do
      if curl -fsS --max-time 2 http://127.0.0.1:8845/vm/health 2>/dev/null \
        | python3 -c 'import json,sys; x=json.load(sys.stdin); assert x.get("ready") and x.get("state")=="running"' 2>/dev/null; then
        restored=1
        break
      fi
      sleep 1
    done
    if (( restored )); then echo 'nbd_clone_test=production_restored';
    else echo 'nbd_clone_test=production_restore_failed' >&2; result=1; fi
    exit "$result"
  }
  trap restore_clone_test EXIT
  if launchctl list com.github.yep.virtualos.start >/dev/null 2>&1; then
    agent_was_loaded=1
    launchctl bootout "$domain" "$agent"
  fi
  "$repo_dir/tools/vosctl" stop
  for (( attempt=0; attempt<60; attempt++ )); do
    lsof "$production" >/dev/null 2>&1 || break
    sleep 1
  done
  if lsof "$production" >/dev/null 2>&1; then
    echo 'nbd_clone_test=failed: production disk still open' >&2
    exit 1
  fi
  VOS_NBD_CLONE_OVERLAY="$lab/overlay.sqlite" "$script_dir/computer-space.sh" --nbd-clone-serve > "$lab/server.log" 2>&1 &
  server_pid=$!
  for (( attempt=0; attempt<100; attempt++ )); do
    if grep -q '"ready"' "$lab/server.log"; then break; fi
    kill -0 "$server_pid" 2>/dev/null || { cat "$lab/server.log" >&2; exit 1; }
    sleep 0.1
  done
  grep -q '"ready"' "$lab/server.log" || { echo 'nbd_clone_test=failed: NBD server not ready' >&2; exit 1; }
  open -g "$app" --args --background-vm --auto-start-vm --vm-bundle "$bundle" --nbd-clone-lab
  desktop=0
  for (( attempt=0; attempt<150; attempt++ )); do
    if curl -fsS --max-time 3 http://127.0.0.1:8845/vm/health 2>/dev/null \
      | python3 -c 'import json,sys; x=json.load(sys.stdin); assert x.get("ready") and x.get("state")=="running"' 2>/dev/null; then
      if python3 - "$script_dir" "$lab/frame.jpg" <<'PY'
import json
import subprocess
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from computer_use import GuestVM
frame = GuestVM().frame(allow_blank=True)
if frame.likely_blank or len(frame.jpeg) < 30000:
    raise SystemExit(1)
Path(sys.argv[2]).write_bytes(frame.jpeg)
ocr = subprocess.run([sys.argv[1] + '/virtualos_stream_server/inputctl', 'ocr', sys.argv[2]],
                     capture_output=True, text=True, timeout=15, check=True)
words = {row['text'] for row in json.loads(ocr.stdout)}
if not ({'File', 'Edit', 'View', 'Window', 'Help'} <= words and
        ({'Terminal', 'Finder', 'Chrome', 'Safari'} & words)):
    raise SystemExit(1)
print(f"nbd_clone_test=desktop_ocr_verified bytes={len(frame.jpeg)} width={frame.width} height={frame.height}")
PY
      then desktop=1; break; fi
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then
      echo 'nbd_clone_test=failed: NBD server exited before desktop verification' >&2
      exit 1
    fi
    sleep 2
  done
  (( desktop )) || { echo 'nbd_clone_test=failed: guest desktop not verified within five minutes' >&2; exit 1; }
  echo 'nbd_clone_test=desktop_verified: production RAW unchanged, bounded overlay only'
  exit 0
fi

if [[ "${1:-}" == --nbd-clone-serve ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  overlay="${VOS_NBD_CLONE_OVERLAY:-}"
  [[ -n "$overlay" ]] || { echo 'nbd_clone_serve=refused: VOS_NBD_CLONE_OVERLAY required' >&2; exit 2; }
  python3 "$script_dir/cloud_disk.py" clone-serve "$overlay"
  exit 0
fi

if [[ "${1:-}" == --nbd-bundle-serve ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  [[ -n "${VOS_NBD_BUNDLE_ROOT:-}" && -n "${VOS_NBD_BUNDLE_NAME:-}" &&
     -n "${VOS_NBD_BUNDLE_OVERLAY:-}" && "${VOS_NBD_BUNDLE_PORT:-}" =~ ^[0-9]+$ ]] || {
    echo 'nbd_bundle_serve=refused: root, name, overlay, and numeric port required' >&2; exit 2;
  }
  python3 "$script_dir/cloud_disk.py" bundle-serve "$VOS_NBD_BUNDLE_ROOT" \
    "$VOS_NBD_BUNDLE_NAME" "$VOS_NBD_BUNDLE_OVERLAY" "$VOS_NBD_BUNDLE_PORT"
  exit 0
fi

if [[ "${1:-}" == --nbd-macos-config-check || "${1:-}" == --nbd-r2-macos-config-check ]]; then
  if [[ "$1" == --nbd-r2-macos-config-check ]]; then
    bundle="$HOME/VirtualOSMachines/.virtualOS-r2-metadata.bundle"
    "$0" --nbd-r2-metadata-check >/dev/null
    source_kind=metadata_only
  else
    bundle="$HOME/VirtualOSMachines/.virtualOS-r2-staging.bundle"
    [[ -r "$bundle/Disk.img" && -r "$bundle/HardwareModel" &&
       -r "$bundle/MachineIdentifier" && -r "$bundle/AuxiliaryStorage" ]] || {
      echo 'nbd_macos_config=refused: isolated staging clone is unavailable' >&2; exit 1;
    }
    source_kind=isolated_clone
  fi
  lab="$(mktemp -d /private/tmp/vos-nbd-macos-config.XXXXXX)"
  trap 'rm -rf "$lab"' EXIT
  cp "$bundle/AuxiliaryStorage" "$lab/AuxiliaryStorage"
  cat > "$lab/check.swift" <<'SWIFT'
import Foundation
import Virtualization

let bundle = URL(fileURLWithPath: CommandLine.arguments[1])
let lab = URL(fileURLWithPath: CommandLine.arguments[2])
let sourceKind = CommandLine.arguments[3]
guard let hardware = VZMacHardwareModel(dataRepresentation: try Data(contentsOf: bundle.appendingPathComponent("HardwareModel"))),
      let machineID = VZMacMachineIdentifier(dataRepresentation: try Data(contentsOf: bundle.appendingPathComponent("MachineIdentifier"))) else {
    fatalError("invalid Mac platform data")
}
let platform = VZMacPlatformConfiguration()
platform.hardwareModel = hardware
platform.machineIdentifier = machineID
platform.auxiliaryStorage = VZMacAuxiliaryStorage(contentsOf: lab.appendingPathComponent("AuxiliaryStorage"))
let attachment = try VZNetworkBlockDeviceStorageDeviceAttachment(
    url: URL(string: "nbd://127.0.0.1:40809/virtualos-test")!, timeout: 30,
    isForcedReadOnly: false, synchronizationMode: .full)
let config = VZVirtualMachineConfiguration()
config.cpuCount = 3
config.memorySize = 4 * 1024 * 1024 * 1024
config.platform = platform
config.bootLoader = VZMacOSBootLoader()
let graphics = VZMacGraphicsDeviceConfiguration()
graphics.displays = [VZMacGraphicsDisplayConfiguration(widthInPixels: 1500, heightInPixels: 900, pixelsPerInch: 144)]
config.graphicsDevices = [graphics]
config.keyboards = [VZUSBKeyboardConfiguration()]
config.pointingDevices = [VZUSBScreenCoordinatePointingDeviceConfiguration()]
config.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
config.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: attachment)]
let network = VZVirtioNetworkDeviceConfiguration()
network.attachment = VZNATNetworkDeviceAttachment()
config.networkDevices = [network]
try config.validate()
print("nbd_macos_config=valid source=\(sourceKind) disk=nbd aux=disposable_copy boot_started=false")
SWIFT
  SWIFT_MODULECACHE_PATH="${SWIFT_MODULECACHE_PATH:-/private/tmp/vos-swift-module-cache}" \
    CLANG_MODULE_CACHE_PATH="${CLANG_MODULE_CACHE_PATH:-/private/tmp/vos-clang-module-cache}" \
    swiftc "$lab/check.swift" -o "$lab/check"
  cat > "$lab/entitlements.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>com.apple.security.virtualization</key><true/><key>com.apple.security.network.client</key><true/></dict></plist>
PLIST
  codesign --force --sign - --entitlements "$lab/entitlements.plist" "$lab/check" >/dev/null 2>&1
  "$lab/check" "$bundle" "$lab" "$source_kind"
  exit 0
fi

if [[ "${1:-}" == --nbd-vz-lab ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  free_kib="$(df -Pk /private/tmp | awk 'NR==2 {print $4}')"
  [[ "$free_kib" =~ ^[0-9]+$ ]] && (( free_kib >= 2097152 )) || {
    echo 'nbd_vz_lab=refused: less than 2 GiB host free' >&2; exit 1;
  }
  lab="$(mktemp -d /private/tmp/vos-nbd-vz-lab.XXXXXX)"
  server_pid=''
  cleanup_vz_lab() {
    if [[ -n "$server_pid" ]]; then kill "$server_pid" >/dev/null 2>&1 || true; wait "$server_pid" 2>/dev/null || true; fi
    rm -rf "$lab"
  }
  trap cleanup_vz_lab EXIT
  python3 - "$lab/source.raw" <<'PY'
import sys
with open(sys.argv[1], 'wb') as image:
    image.truncate(16 * 1024 * 1024)
PY
  backend="${VOS_NBD_VZ_BACKEND:-objects}"
  server_program="$script_dir/cloud_disk.py"
  if [[ "$backend" == objects ]]; then
    python3 "$script_dir/cloud_disk.py" lab-pack "$lab/source.raw" "$lab/objects" >/dev/null
    server_args=(lab-serve "$lab/objects" "$lab/overlay.sqlite")
  elif [[ "$backend" == raw ]]; then
    server_args=(raw-lab-serve "$lab/source.raw" "$lab/overlay.sqlite")
  elif [[ "$backend" == r2-fake ]]; then
    python3 - "$lab/source.raw" <<'PY'
import sys
with open(sys.argv[1], 'r+b') as image:
    image.write(b'VOS-R2-VZ-LAB')
PY
    python3 "$script_dir/cloud_disk.py" lab-pack "$lab/source.raw" "$lab/objects" >/dev/null
    server_program="$script_dir/cloud_disk_vz_fake.py"
    server_args=("$lab/objects" "$lab/overlay.sqlite" "$lab/cache")
  else
    echo 'nbd_vz_lab=refused: backend must be objects, raw, or r2-fake' >&2
    exit 2
  fi
  port="$((30000 + RANDOM % 20000))"
  python3 "$server_program" "${server_args[@]}" "$port" > "$lab/server.log" 2>&1 &
  server_pid=$!
  for (( attempt=0; attempt<50; attempt++ )); do
    if grep -q '"ready"' "$lab/server.log"; then break; fi
    kill -0 "$server_pid" 2>/dev/null || { cat "$lab/server.log" >&2; echo 'nbd_vz_lab=failed: server exited' >&2; exit 1; }
    sleep 0.1
  done
  grep -q '"ready"' "$lab/server.log" || { echo 'nbd_vz_lab=failed: server not ready' >&2; exit 1; }
  cat > "$lab/probe.swift" <<'SWIFT'
import Foundation
import Virtualization

let port = Int(CommandLine.arguments[1])!
let url = URL(string: "nbd://127.0.0.1:\(port)/virtualos-test")!
let attachment = try VZNetworkBlockDeviceStorageDeviceAttachment(
    url: url, timeout: 5, isForcedReadOnly: false, synchronizationMode: .full)
let config = VZVirtualMachineConfiguration()
config.cpuCount = 2
config.memorySize = 2 * 1024 * 1024 * 1024
config.platform = VZGenericPlatformConfiguration()
let boot = VZEFIBootLoader()
boot.variableStore = try VZEFIVariableStore(
    creatingVariableStoreAt: URL(fileURLWithPath: CommandLine.arguments[2]), options: [])
config.bootLoader = boot
config.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: attachment)]
try config.validate()
print("nbd_vz_lab=config_valid")
fflush(stdout)
let queue = DispatchQueue(label: "virtualos.nbd.lab")
let machine = VZVirtualMachine(configuration: config, queue: queue)
let started = DispatchSemaphore(value: 0)
var didStart = false
queue.async {
    machine.start { result in
        switch result {
        case .success: didStart = true; print("nbd_vz_lab=started")
        case .failure(let error): print("nbd_vz_lab=start_failed \(error)")
        }
        fflush(stdout)
        started.signal()
    }
}
guard started.wait(timeout: .now() + 25) == .success else {
    fputs("nbd_vz_lab=start_timeout\n", stderr)
    exit(1)
}
guard didStart else { exit(1) }
Thread.sleep(forTimeInterval: 2)
let stopped = DispatchSemaphore(value: 0)
queue.async {
    if machine.canStop {
        machine.stop { error in
            print(error == nil ? "nbd_vz_lab=stopped" : "nbd_vz_lab=stop_failed \(error!)")
            fflush(stdout)
            stopped.signal()
        }
    } else {
        print("nbd_vz_lab=already_stopped")
        stopped.signal()
    }
}
guard stopped.wait(timeout: .now() + 15) == .success else {
    fputs("nbd_vz_lab=stop_timeout\n", stderr)
    exit(1)
}
SWIFT
  swiftc -swift-version 5 "$lab/probe.swift" -o "$lab/probe"
  cat > "$lab/entitlements.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>com.apple.security.virtualization</key><true/><key>com.apple.security.network.client</key><true/></dict></plist>
PLIST
  codesign --force --sign - --entitlements "$lab/entitlements.plist" "$lab/probe" >/dev/null 2>&1
  "$lab/probe" "$port" "$lab/nvram"
  for (( attempt=0; attempt<50; attempt++ )); do
    if ! kill -0 "$server_pid" 2>/dev/null; then break; fi
    sleep 0.1
  done
  if kill -0 "$server_pid" 2>/dev/null; then
    echo 'nbd_vz_lab=failed: Apple client did not complete the NBD connection' >&2
    exit 1
  fi
  python3 - "$lab/server.log" "$backend" <<'PY'
import json, sys
rows = []
for line in open(sys.argv[1]):
    try: rows.append(json.loads(line))
    except ValueError: continue
connections = [row.get('cloud_disk_lab_connection', row.get('fake_r2_vz_connection'))
               for row in rows if 'cloud_disk_lab_connection' in row or 'fake_r2_vz_connection' in row]
if not connections or not connections[-1].get('negotiated') or connections[-1].get('requests', 0) < 1:
    raise SystemExit('nbd_vz_lab=failed: no verified NBD negotiation and read')
if sys.argv[2] == 'r2-fake' and not any(row.get('fake_r2_vz_server') == 'passed'
                                     and row.get('signed_gets') and row.get('corruption_rejected')
                                     for row in rows):
    raise SystemExit('nbd_vz_lab=failed: fake R2 integrity checks did not finish')
print('nbd_vz_lab=apple_client_verified ' + json.dumps(connections[-1], sort_keys=True))
PY
  echo "nbd_vz_lab=finished: disposable EFI guest only backend=$backend"
  exit 0
fi

if [[ "${1:-}" == --cloud-disk-r2-boot-lab ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  config="${VOS_CLOUD_DISK_CONFIG:-}"
  [[ -r "$config" ]] || { echo 'r2_boot_lab=refused: VOS_CLOUD_DISK_CONFIG required' >&2; exit 2; }
  [[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" &&
     -n "${CLOUDFLARE_ACCOUNT_ID:-}" && -n "${CLOUDFLARE_R2_ENDPOINT:-}" ]] || {
    echo 'r2_boot_lab=refused: pinned environment credentials required' >&2; exit 2; }
  free_kib="$(df -Pk /System/Volumes/Data | awk 'NR==2 {print $4}')"
  [[ "$free_kib" =~ ^[0-9]+$ ]] && (( free_kib >= 11534336 )) || {
    echo 'r2_boot_lab=refused: less than 11 GiB host free' >&2; exit 1; }
  lab="$(mktemp -d /private/tmp/vos-r2-boot-lab.XXXXXX)"
  server_pid=''
  cleanup_r2_boot() {
    if [[ -n "$server_pid" ]]; then kill "$server_pid" >/dev/null 2>&1 || true; wait "$server_pid" 2>/dev/null || true; fi
    rm -rf "$lab"
  }
  trap cleanup_r2_boot EXIT
  # 16 MiB disposable image: three patterned chunks plus one zero chunk, so the
  # upload exercises the chunk and zero-chunk paths against the real bucket.
  python3 - "$lab/source.raw" <<'PY'
import sys
with open(sys.argv[1], 'wb') as image:
    for index in range(4):
        if index == 2:
            image.write(bytes(4 * 1024**2))
        else:
            image.write((bytes([65 + index]) * 4096) * (4 * 1024**2 // 4096))
PY
  manifest="$(python3 - "$script_dir" "$lab/source.raw" "$config" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from cloud_disk import validate_config
from cloud_disk_r2_upload import stream_pack_upload, R2UploadStore
config = validate_config(sys.argv[3])
print(stream_pack_upload(sys.argv[2], R2UploadStore(config))["manifest_sha256"])
PY
)"
  [[ "$manifest" =~ ^[0-9a-f]{64}$ ]] || { echo 'r2_boot_lab=failed: upload returned no manifest' >&2; exit 1; }
  port="$((30000 + RANDOM % 20000))"
  python3 "$script_dir/cloud_disk.py" r2-serve "$config" "$manifest" "$port" > "$lab/server.log" 2>&1 &
  server_pid=$!
  for (( attempt=0; attempt<200; attempt++ )); do
    if grep -q '"ready"' "$lab/server.log"; then break; fi
    kill -0 "$server_pid" 2>/dev/null || { cat "$lab/server.log" >&2; echo 'r2_boot_lab=failed: server exited' >&2; exit 1; }
    sleep 0.1
  done
  grep -q '"ready"' "$lab/server.log" || { echo 'r2_boot_lab=failed: server not ready' >&2; exit 1; }
  cat > "$lab/probe.swift" <<'SWIFT'
import Foundation
import Virtualization

let port = Int(CommandLine.arguments[1])!
let url = URL(string: "nbd://127.0.0.1:\(port)/virtualos-test")!
let attachment = try VZNetworkBlockDeviceStorageDeviceAttachment(
    url: url, timeout: 15, isForcedReadOnly: false, synchronizationMode: .full)
let config = VZVirtualMachineConfiguration()
config.cpuCount = 2
config.memorySize = 2 * 1024 * 1024 * 1024
config.platform = VZGenericPlatformConfiguration()
let boot = VZEFIBootLoader()
boot.variableStore = try VZEFIVariableStore(
    creatingVariableStoreAt: URL(fileURLWithPath: CommandLine.arguments[2]), options: [])
config.bootLoader = boot
config.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: attachment)]
try config.validate()
print("r2_boot_lab=config_valid")
fflush(stdout)
let queue = DispatchQueue(label: "virtualos.r2.boot.lab")
let machine = VZVirtualMachine(configuration: config, queue: queue)
let started = DispatchSemaphore(value: 0)
var didStart = false
queue.async {
    machine.start { result in
        switch result {
        case .success: didStart = true; print("r2_boot_lab=started")
        case .failure(let error): print("r2_boot_lab=start_failed \(error)")
        }
        fflush(stdout); started.signal()
    }
}
guard started.wait(timeout: .now() + 30) == .success else {
    fputs("r2_boot_lab=start_timeout\n", stderr); exit(1)
}
guard didStart else { exit(1) }
Thread.sleep(forTimeInterval: 3)
let stopped = DispatchSemaphore(value: 0)
queue.async {
    if machine.canStop {
        machine.stop { error in
            print(error == nil ? "r2_boot_lab=stopped" : "r2_boot_lab=stop_failed \(error!)")
            fflush(stdout); stopped.signal()
        }
    } else { print("r2_boot_lab=already_stopped"); stopped.signal() }
}
guard stopped.wait(timeout: .now() + 20) == .success else {
    fputs("r2_boot_lab=stop_timeout\n", stderr); exit(1)
}
SWIFT
  swiftc -swift-version 5 "$lab/probe.swift" -o "$lab/probe"
  cat > "$lab/entitlements.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>com.apple.security.virtualization</key><true/><key>com.apple.security.network.client</key><true/></dict></plist>
PLIST
  codesign --force --sign - --entitlements "$lab/entitlements.plist" "$lab/probe" >/dev/null 2>&1
  "$lab/probe" "$port" "$lab/nvram"
  python3 - "$lab/server.log" <<'PY'
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip().startswith('{')]
connections = [row['r2_nbd_connection'] for row in rows if 'r2_nbd_connection' in row]
if not connections or not connections[-1].get('negotiated') or connections[-1].get('requests', 0) < 1:
    raise SystemExit('r2_boot_lab=failed: no verified NBD negotiation and read from real R2')
row = connections[-1]
print('r2_boot_lab=real_r2_verified reads=%s bytes=%s cold_us_max=%s' % (
    row['reads'], row['metrics']['read']['request_bytes_total'], row['metrics']['read']['latency_us_max']))
PY
  echo 'r2_boot_lab=finished: guest booted from real R2, overlay local only'
  exit 0
fi

if [[ "${1:-}" == --cloud-disk-lab-pack ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  [[ -n "${VOS_CLOUD_DISK_LAB_SOURCE:-}" && -n "${VOS_CLOUD_DISK_LAB_ROOT:-}" ]] || {
    echo 'lab_pack=refused: source and root environment paths required' >&2; exit 2;
  }
  python3 "$script_dir/cloud_disk.py" lab-pack "$VOS_CLOUD_DISK_LAB_SOURCE" "$VOS_CLOUD_DISK_LAB_ROOT"
  exit 0
fi

if [[ "${1:-}" == --cloud-disk-lab-serve ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  [[ -n "${VOS_CLOUD_DISK_LAB_ROOT:-}" && -n "${VOS_CLOUD_DISK_LAB_OVERLAY:-}" &&
     "${VOS_CLOUD_DISK_LAB_PORT:-}" =~ ^[0-9]+$ ]] || {
    echo 'lab_serve=refused: root, overlay, and numeric port required' >&2; exit 2;
  }
  python3 "$script_dir/cloud_disk.py" lab-serve "$VOS_CLOUD_DISK_LAB_ROOT" \
    "$VOS_CLOUD_DISK_LAB_OVERLAY" "$VOS_CLOUD_DISK_LAB_PORT"
  exit 0
fi

if [[ "${1:-}" == --cloud-disk-r2-serve ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  [[ -n "${VOS_CLOUD_DISK_CONFIG:-}" && -r "$VOS_CLOUD_DISK_CONFIG" &&
     "${VOS_CLOUD_DISK_MANIFEST_SHA256:-}" =~ ^[0-9a-f]{64}$ &&
     "${VOS_CLOUD_DISK_PORT:-}" == 40809 ]] || {
    echo 'r2_serve=refused: config, pinned manifest SHA256, and VM port 40809 required' >&2; exit 2;
  }
  [[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" &&
     -n "${CLOUDFLARE_ACCOUNT_ID:-}" && -n "${CLOUDFLARE_R2_ENDPOINT:-}" ]] || {
    echo 'r2_serve=refused: pinned environment credentials required' >&2; exit 2;
  }
  python3 "$script_dir/cloud_disk.py" r2-serve "$VOS_CLOUD_DISK_CONFIG" \
    "$VOS_CLOUD_DISK_MANIFEST_SHA256" "$VOS_CLOUD_DISK_PORT"
  exit 0
fi

if [[ "${1:-}" == --cloud-disk-r2-checkpoint-lab ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  config="${VOS_CLOUD_DISK_CONFIG:-}"
  [[ -r "$config" ]] || { echo 'r2_checkpoint_lab=refused: VOS_CLOUD_DISK_CONFIG required' >&2; exit 2; }
  [[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" &&
     -n "${CLOUDFLARE_ACCOUNT_ID:-}" && -n "${CLOUDFLARE_R2_ENDPOINT:-}" ]] || {
    echo 'r2_checkpoint_lab=refused: pinned environment credentials required' >&2; exit 2; }
  lab="$(mktemp -d /private/tmp/vos-r2-chk-lab.XXXXXX)"
  trap 'rm -rf "$lab"' EXIT
  python3 - "$script_dir" "$lab" "$config" <<'PY'
import json, pathlib, sqlite3, sys
sys.path.insert(0, sys.argv[1])
from cloud_disk import BLOCK, digest, lab_path, pack, validate_config
from cloud_disk_r2 import R2ReadOnly
from cloud_disk_r2_upload import R2UploadStore
from cloud_disk_checkpoint import (snapshot_overlay, upload_checkpoint,
                                   restore_remote_checkpoint)

lab = pathlib.Path(sys.argv[2])
base_config = validate_config(sys.argv[3])

# 1. A disposable 16 MiB base image -> an APFS-free packed dir we can serve the
#    manifest from. Upload it to the configured bucket/prefix.
source = lab / "source.raw"
with source.open("wb") as image:
    for index in range(4):
        image.write(bytes(4 * 1024**2) if index == 2
                    else (bytes([66 + index]) * 4096) * (4 * 1024**2 // 4096))
packed = lab / "packed"
pack(str(source), str(packed))
manifest_bytes = (packed / "manifest.json").read_bytes()
manifest_sha = digest(manifest_bytes)
store = R2UploadStore(base_config)
# Upload base objects + manifest (manifest last).
manifest = json.loads(manifest_bytes)
for entry in manifest["chunks"]:
    if "key" not in entry:
        continue
    encoded = (packed / "objects" / (entry["key"] + ".z")).read_bytes()
    store.put_if_absent("objects/" + entry["key"] + ".z", encoded)
store.put_if_absent("manifest.json", manifest_bytes)

# 2. A local overlay with two full written blocks against that base.
base_dir = lab / "base"
base_dir.mkdir()
(base_dir / "manifest.json").write_bytes(manifest_bytes)
overlay = lab / "overlay.sqlite"
db = sqlite3.connect(str(overlay))
db.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
db.execute("CREATE TABLE blocks (idx INTEGER PRIMARY KEY, data BLOB)")
db.execute("INSERT INTO metadata VALUES ('manifest_sha256', ?)", (manifest_sha,))
db.execute("INSERT INTO blocks VALUES (0, ?)", (bytes([0x11]) * BLOCK,))
db.execute("INSERT INTO blocks VALUES (3, ?)", (bytes([0x22]) * BLOCK,))
db.commit()
db.close()

# 3. Snapshot + upload the overlay checkpoint to real R2 under /checkpoints/<sha>.
snapshot = lab / "checkpoint"
result = snapshot_overlay(str(base_dir), str(overlay), str(snapshot))
checkpoint_sha = result["checkpoint_sha256"]
checkpoint_config = dict(base_config)
checkpoint_config["object_prefix"] = base_config["object_prefix"] + "/checkpoints/" + checkpoint_sha
checkpoint_config_path = lab / "checkpoint-config.json"
checkpoint_config_path.write_text(json.dumps(checkpoint_config))
checkpoint_config = validate_config(str(checkpoint_config_path))
upload = upload_checkpoint(str(snapshot), str(base_dir), R2UploadStore(checkpoint_config),
                           checkpoint_sha)

# 4. Restore from real R2 into a fresh overlay and verify byte-identical blocks.
restored = lab / "restored.sqlite"
restore_remote_checkpoint(str(base_dir), str(restored),
                          R2ReadOnly(checkpoint_config, checkpoint_sha), checkpoint_sha)
db = sqlite3.connect(str(restored))
blocks = {index: len(data) for index, data in db.execute("SELECT idx,data FROM blocks ORDER BY idx")}
identity = db.execute("SELECT value FROM metadata WHERE name='manifest_sha256'").fetchone()[0]
db.close()
if blocks != {0: BLOCK, 3: BLOCK} or identity != manifest_sha:
    raise SystemExit("r2_checkpoint_lab=failed: restored overlay mismatch")
print(json.dumps({
    "r2_checkpoint_lab": "real_r2_verified",
    "base_manifest_sha256": manifest_sha,
    "checkpoint_sha256": checkpoint_sha,
    "blocks": upload["blocks"],
    "uploaded_bytes": upload["uploaded_bytes"],
    "restored_blocks": len(blocks),
    "identity_matches": identity == manifest_sha,
    "manifest_last": upload["manifest_last"],
}, sort_keys=True))
PY
  exit 0
fi

if [[ "${1:-}" == --cloud-disk-config-check ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  config="${VOS_CLOUD_DISK_CONFIG:-$script_dir/cloudflare/cloud-disk.example.json}"
  python3 "$script_dir/cloud_disk.py" check-config "$config"
  exit 0
fi

if [[ "${1:-}" == --cloud-disk-self-test ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  python3 "$script_dir/cloud_disk.py" self-test
  exit 0
fi

if [[ "${1:-}" == --cloud-disk-checkpoint-self-test ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  python3 "$script_dir/cloud_disk_checkpoint.py" self-test
  exit 0
fi

if [[ "${1:-}" == --virtualization-api-inventory ]]; then
  VOS_VZ_HEADER_DIR="$(xcrun --show-sdk-path)/System/Library/Frameworks/Virtualization.framework/Versions/A/Headers" swift - <<'SWIFT'
import Foundation
import Virtualization
import ObjectiveC.runtime

guard let headerDir = ProcessInfo.processInfo.environment["VOS_VZ_HEADER_DIR"],
      let files = try? FileManager.default.contentsOfDirectory(atPath: headerDir) else {
    fputs("virtualization_inventory=failed: SDK headers unavailable\n", stderr)
    exit(1)
}
var entries: [[String: String]] = []
var classCount = 0
for file in files.sorted() where file.hasPrefix("VZ") && file.hasSuffix(".h") {
    let name = String(file.dropLast(2))
    guard let cls = NSClassFromString(name) else { continue }
    classCount += 1
    for (kind, target) in [("instance", cls), ("class", object_getClass(cls))] {
        guard let target else { continue }
        var methodCount: UInt32 = 0
        guard let methods = class_copyMethodList(target, &methodCount) else { continue }
        for methodIndex in 0..<Int(methodCount) {
            let method = methods[methodIndex]
            let selector = NSStringFromSelector(method_getName(method))
            guard selector.hasPrefix("_") else { continue }
            let signature = method_getTypeEncoding(method).map { String(cString: $0) } ?? ""
            entries.append(["class": name, "kind": kind,
                            "selector": selector, "encoding": signature])
        }
    }
}
let output: [String: Any] = ["scope": "loaded Objective-C VZ classes; private underscore selectors only",
                             "class_count": classCount, "selector_count": entries.count,
                             "selectors": entries.sorted { left, right in
                                 (left["class"]!, left["selector"]!) < (right["class"]!, right["selector"]!)
                             }]
let data = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data([10]))
SWIFT
  exit 0
fi

if [[ "${1:-}" == --raw-hole-lab ]]; then
  python3 - <<'PY'
import fcntl
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile

MIB = 1024 * 1024

def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(MIB):
            digest.update(chunk)
    return digest.digest()

with tempfile.TemporaryDirectory(prefix='vos-raw-hole-', dir='/private/tmp') as lab:
    raw = Path(lab) / 'disposable.raw'
    with raw.open('wb') as stream:
        for symbol, count in ((b'A', 4), (b'\0', 8), (b'B', 4)):
            for _ in range(count):
                stream.write(symbol * MIB)
        stream.flush()
        os.fsync(stream.fileno())
    before = raw.stat()
    before_hash = sha256(raw)
    fd = os.open(raw, os.O_RDWR)
    try:
        # Darwin SDK sys/fcntl.h: F_PUNCHHOLE=99, fpunchhole_t={u32,u32,off_t,off_t}.
        fcntl.fcntl(fd, 99, struct.pack('=IIqq', 0, 0, 4 * MIB, 8 * MIB))
        os.fsync(fd)
    finally:
        os.close(fd)
    after = raw.stat()
    assert after.st_size == before.st_size == 16 * MIB
    assert sha256(raw) == before_hash
    assert after.st_blocks < before.st_blocks
    print(json.dumps({'raw_hole_lab': 'passed', 'logical_bytes': after.st_size,
                      'allocated_before_bytes': before.st_blocks * 512,
                      'allocated_after_bytes': after.st_blocks * 512,
                      'file_blocks_reclaimed_bytes': (before.st_blocks - after.st_blocks) * 512,
                      'byte_hash_unchanged': True, 'production_disk_touched': False}))
PY
  exit 0
fi

if [[ "${1:-}" == --nbd-api-probe ]]; then
  swift - <<'SWIFT'
import Foundation
import Virtualization

final class NBDProbeObserver: NSObject, VZNetworkBlockDeviceStorageDeviceAttachmentDelegate {
    func attachmentWasConnected(_ attachment: VZNetworkBlockDeviceStorageDeviceAttachment) {}
    func attachment(_ attachment: VZNetworkBlockDeviceStorageDeviceAttachment, didEncounterError error: Error) {}
}

let url = URL(string: "nbd://127.0.0.1:10809/virtualos-probe")!
do {
    let attachment = try VZNetworkBlockDeviceStorageDeviceAttachment(
        url: url, timeout: 5, isForcedReadOnly: true, synchronizationMode: .full)
    let observer = NBDProbeObserver()
    attachment.delegate = observer
    _ = VZVirtioBlockDeviceConfiguration(attachment: attachment)
    print("nbd_api=available url_valid=true delegate_set=\(attachment.delegate != nil) connection_attempted=false")
} catch {
    fputs("nbd_api=failed error=\(error)\n", stderr)
    exit(1)
}
SWIFT
  exit 0
fi

if [[ "${1:-}" == --nbd-source-build-check ]]; then
  repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
  free_kib="$(df -Pk /System/Volumes/Data | awk 'NR==2 {print $4}')"
  [[ "$free_kib" =~ ^[0-9]+$ ]] && (( free_kib >= 11 * 1024 * 1024 )) || {
    echo 'nbd_source_build=refused: less than 11 GiB free' >&2; exit 1;
  }
  process_list="$(ps -axo comm)" || { echo 'nbd_source_build=refused: process list unavailable' >&2; exit 1; }
  if printf '%s\n' "$process_list" | awk '$1 ~ /(^|\/)(swiftc|swift-frontend|xcodebuild)$/ {found=1} END {exit !found}'; then
    echo 'nbd_source_build=refused: another build is running' >&2; exit 1
  fi
  cd "$repo_dir"
  xcodebuild -quiet -project virtualOS.xcodeproj -scheme virtualOS -configuration Debug \
    -destination 'platform=macOS,arch=arm64' -derivedDataPath build/DerivedData \
    CODE_SIGN_IDENTITY=- DEVELOPMENT_TEAM= CODE_SIGN_STYLE=Manual build
  echo 'nbd_source_build=passed: app built, no installation or launch'
  exit 0
fi

if [[ "${1:-}" == --vm-summary ]]; then
  bundle="$HOME/VirtualOSMachines/virtualOS.bundle"
  clone="$HOME/VirtualOSMachines/.virtualOS-r2-staging.bundle"
  free_kib="$(df -Pk /System/Volumes/Data | awk 'NR==2 {print $4}')"
  printf 'host_free_kib=%s\n' "$free_kib"
  if [[ -f "$bundle/Disk.img" ]]; then
    logical="$(stat -f %z "$bundle/Disk.img")"
    printf 'raw_logical_bytes=%s\nraw_alloc_kib=%s\n' "$logical" \
      "$(du -sk "$bundle/Disk.img" | awk '{print $1}')"
    printf 'safe_conversion_free_kib=%s\n' "$((logical / 1024 + 2097152))"
  fi
  if [[ -f "$bundle/Disk.asif" ]]; then
    printf 'asif_alloc_kib=%s\nasif_ready=%s\n' \
      "$(du -sk "$bundle/Disk.asif" | awk '{print $1}')" \
      "$([[ -f "$bundle/Disk.asif.ready" ]] && echo yes || echo no)"
  fi
  if [[ -d "$clone" ]]; then
    printf 'clone_present=yes\nclone_alloc_kib=%s\n' "$(du -sk "$clone" | awk '{print $1}')"
  else
    echo 'clone_present=no'
  fi
  sysctl vm.swapusage
  exit 0
fi

if [[ "${1:-}" == --reclaim-caches ]]; then
  artifact="$HOME/Library/Caches/org.swift.swiftpm/artifacts/https___dl_google_com_cpdc_20260911_163655_MediaPipeTaskGraphs_1_0_1_xcframework_zip"
  process_list="$(ps -axo comm)" || { echo 'reclaim=refused: process list unavailable' >&2; exit 1; }
  if printf '%s\n' "$process_list" | awk '$1 ~ /(^|\/)(swiftc|swift-frontend|swift-build|xcodebuild)$/ {found=1} END {exit !found}'; then
    echo 'reclaim=refused: a Swift or Xcode build is running' >&2
    exit 1
  fi
  before_kb="$(df -Pk /System/Volumes/Data | awk 'NR==2 {print $4}')"
  cache_package="$(mktemp -d /private/tmp/vos-swift-cache.XXXXXX)"
  trap 'rm -rf "$cache_package"' EXIT
  printf '// swift-tools-version: 5.9\nimport PackageDescription\nlet package = Package(name: "virtualos-cache-maintenance")\n' > "$cache_package/Package.swift"
  /usr/bin/swift package --package-path "$cache_package" purge-cache
  if [[ -f "$artifact" && ! -L "$artifact" ]]; then
    artifact_kb="$(du -sk "$artifact" | awk '{print $1}')"
    if (( artifact_kb > 500000 )); then
      /bin/rm -- "$artifact"
    fi
  fi
  after_kb="$(df -Pk /System/Volumes/Data | awk 'NR==2 {print $4}')"
  printf 'reclaim=finished gained_kib=%s free_kib=%s\n' "$((after_kb - before_kb))" "$after_kb"
  exit 0
fi

trace_dir="${VOS_CUA_TRACE_DIR:-$HOME/Library/Application Support/virtualOS/computer-use}"
df -h /System/Volumes/Data
du -sk "$trace_dir" "$HOME/Library/Caches" "$HOME/Library/Developer/CoreSimulator" \
  "$HOME/Library/Logs" /private/tmp 2>/dev/null | sort -nr
echo 'largest_cache_directories_kib:'
du -sk "$HOME"/Library/Caches/* 2>/dev/null | sort -nr | head -n 10 || true
echo 'largest_tmp_directories_kib:'
du -sk /private/tmp/* 2>/dev/null | sort -nr | head -n 10 || true
