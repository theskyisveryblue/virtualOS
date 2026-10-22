"""Prepare and supervise a fleet machine's shared-base disk for go-live.sh."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import struct
import subprocess
import sys
import time

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
from cloud_disk import base_delta_overlay, NBD_MAGIC, NBD_OPTION_MAGIC


def read_config(root, name):
    path = Path(os.environ.get("VOS_SHARED_DISK_CONFIG", root / "shared-disks" / (name + ".json")))
    if not path.exists() and "VOS_SHARED_DISK_CONFIG" not in os.environ:
        return None
    cfg = json.loads(path.read_text())
    if not isinstance(cfg, dict) or set(cfg) - {"baseName", "platformName", "delta", "port", "deltaFormat", "deltaSHA256", "writeFormat"}:
        raise ValueError("shared disk config requires baseName, platformName, port, and optional delta")
    for key in ("baseName", "platformName"):
        if not isinstance(cfg.get(key), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", cfg[key]):
            raise ValueError("invalid " + key)
    if type(cfg.get("port")) is not int or not 20000 <= cfg["port"] <= 60999:
        raise ValueError("shared disk port must be 20000..60999")
    for key in ("baseName", "platformName"):
        bundle = root / (cfg[key] + ".bundle")
        if bundle.is_symlink() or not bundle.is_dir():
            raise ValueError(key + " must name a bundle inside the fleet")
    disk = root / (cfg["baseName"] + ".bundle") / "Disk.img"
    if disk.is_symlink() or not disk.is_file() or disk.stat().st_size < 512 or disk.stat().st_size % 512:
        raise ValueError("shared base must be a regular, aligned disk")
    production = (Path.home() / "VirtualOSMachines").resolve()
    if root == production or production in root.parents:
        raise ValueError("refusing the production machine store")
    prod_disk = production / "virtualOS.bundle/Disk.img"
    if prod_disk.exists() and os.path.samefile(disk, prod_disk):
        raise ValueError("refusing the production RAW disk")
    delta = cfg.get("delta", "")
    if not isinstance(delta, str):
        raise ValueError("delta must be a path string")
    if delta:
        delta_path = Path(delta) if Path(delta).is_absolute() else root / delta
        if delta_path.is_symlink() or not delta_path.is_file():
            raise ValueError("delta must be a regular file")
        cfg["delta"] = str(delta_path.resolve())
    else:
        cfg["delta"] = ""
    if 'deltaFormat' in cfg or 'deltaSHA256' in cfg:
        if (cfg.get('deltaFormat') != 'compressed-v1' or not cfg['delta']
                or not isinstance(cfg.get('deltaSHA256'), str)
                or not re.fullmatch('[0-9a-f]{64}', cfg['deltaSHA256'])):
            raise ValueError('compressed-v1 requires a delta and exact deltaSHA256')
    if 'writeFormat' in cfg and (cfg['writeFormat'] != 'zlib-v1' or cfg.get('deltaFormat') != 'compressed-v1'):
        raise ValueError('zlib-v1 writes require a pinned compressed-v1 delta')
    return cfg


def prepare(root, name, cfg):
    bundle = root / (name + ".bundle")
    source = root / (cfg["platformName"] + ".bundle")
    platform = {key: (source / key).read_bytes()
                for key in ("HardwareModel", "MachineIdentifier", "AuxiliaryStorage")}
    expected_url = "nbd://127.0.0.1:%d/virtualos-test" % cfg["port"]
    params = {"cpuCount": 4, "memorySizeInGB": 4, "screenWidth": 1500,
              "screenHeight": 900, "nbdURL": expected_url}
    overlay = base_delta_overlay(root, root / ".virtualos-nbd" / name / "overlay.sqlite", True)
    state = overlay.parent
    pin = state / "disk.json"
    label = "com.virtualos.disk." + name
    plist = state / (label + ".plist")
    for path in (pin, plist, state / "server.log"):
        if path.is_symlink():
            raise ValueError("shared disk metadata must not be symlinked")
    if pin.exists() and json.loads(pin.read_text()) != cfg:
        raise ValueError("machine already has a different shared disk configuration")
    if bundle.exists() or bundle.is_symlink():
        if bundle.is_symlink() or not bundle.is_dir() or (bundle / "Disk.img").exists():
            raise ValueError("refusing to convert an existing local-disk bundle")
        current = json.loads((bundle / "Parameters.txt").read_text())
        if current.get("nbdURL") != expected_url:
            raise ValueError("existing bundle points to a different NBD disk")
        for key in platform:
            if not (bundle / key).is_file():
                raise ValueError("existing shared bundle lacks " + key)
    else:
        # Build alongside the target, then publish only a complete bundle.
        import tempfile
        import shutil
        staging = Path(tempfile.mkdtemp(prefix="." + name + "-", dir=root))
        try:
            for key, data in platform.items():
                (staging / key).write_bytes(data)
            (staging / "Parameters.txt").write_text(json.dumps(params, indent=2))
            staging.rename(bundle)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    pin.write_text(json.dumps(cfg, indent=2))
    env = {"VOS_NBD_BUNDLE_ROOT": str(root), "VOS_NBD_BASE_NAME": cfg["baseName"],
           "VOS_NBD_BUNDLE_OVERLAY": str(overlay), "VOS_NBD_BUNDLE_PORT": str(cfg["port"]),
           "VOS_NBD_BASE_DELTA": cfg["delta"], "VOS_NBD_PERSISTENT": "1"}
    if cfg.get('deltaFormat') == 'compressed-v1':
        env['VOS_NBD_COMPRESSED_DELTA_SHA256'] = cfg['deltaSHA256']
    if cfg.get('writeFormat') == 'zlib-v1':
        env['VOS_NBD_COMPRESSED_WRITES'] = '1'
    # Preserve the operator's explicit reserve, including bounded test fixtures.
    if "VOS_NBD_MIN_HOST_FREE" in os.environ:
        env["VOS_NBD_MIN_HOST_FREE"] = os.environ["VOS_NBD_MIN_HOST_FREE"]
    # Launchd does not inherit the interactive shell's Homebrew PATH.
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    plist.write_bytes(plistlib.dumps({"Label": label,
        "ProgramArguments": ["/bin/bash", str(TOOLS / "computer-space.sh"), "--nbd-base-delta-serve"],
        "EnvironmentVariables": env, "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": 10, "StandardOutPath": str(state / "server.log"),
        "StandardErrorPath": str(state / "server.log")}))
    return state, label, plist


def start(root, cfg, state, label, plist):
    domain = "gui/%d" % os.getuid()
    target = domain + "/" + label
    status = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
    previous = re.search(r"\bpid\s*=\s*(\d+)", status.stdout)
    previous_pid = int(previous[1]) if status.returncode == 0 and previous else None
    if status.returncode != 0:
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    subprocess.run(["launchctl", "kickstart", target], check=True)
    startup_timeout = int(os.environ.get("VOS_NBD_START_TIMEOUT", "180"))
    if not 1 <= startup_timeout <= 600:
        raise ValueError("VOS_NBD_START_TIMEOUT must be 1..600 seconds")
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        status = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
        match = re.search(r"\bpid\s*=\s*(\d+)", status.stdout)
        if match:
            try:
                with (state / "server.log").open("rb") as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 65536))
                    rows = stream.read().decode(errors="replace").splitlines()
                for row in reversed(rows):
                    try:
                        ready = json.loads(row)
                    except ValueError:
                        continue
                    if (ready.get("base_delta_listener") == "ready" and
                        ready.get("pid") == int(match[1]) and ready.get("persistent") is True and
                        ready.get("base") == cfg["baseName"] and ready.get("port") == cfg["port"] and
                        ready.get("delta_sqlite") == (cfg["delta"] or None) and
                        ready.get('compressed_delta_sha256') == cfg.get('deltaSHA256') and
                        ready.get('compressed_writes', False) == (cfg.get('writeFormat') == 'zlib-v1') and
                        ready.get("overlay") == str(state / "overlay.sqlite")):
                        if previous_pid == int(match[1]):
                            # A running VM owns the sequential NBD connection.
                            # Reuse this proven, still-running managed process
                            # without trying to interrupt or probe its session.
                            print("   persistent shared disk already running on :%d" % cfg["port"])
                            return
                        with socket.create_connection(("127.0.0.1", cfg["port"]), timeout=2) as sock:
                            greeting = b""
                            while len(greeting) < 18:
                                piece = sock.recv(18 - len(greeting))
                                if not piece:
                                    raise OSError("incomplete NBD greeting")
                                greeting += piece
                            if struct.unpack(">QQH", greeting)[:2] != (NBD_MAGIC, NBD_OPTION_MAGIC):
                                raise ValueError("unexpected NBD listener")
                            sock.sendall(struct.pack(">I", 0) + struct.pack(">QII", NBD_OPTION_MAGIC, 2, 0))
                        print("   persistent shared disk ready on :%d" % cfg["port"])
                        return
            except (OSError, ValueError):
                pass
        time.sleep(0.2)
    raise RuntimeError("shared disk did not become ready; inspect " + str(state / "server.log"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "start"))
    parser.add_argument("root", type=Path)
    parser.add_argument("name")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.name):
        raise ValueError("invalid machine name")
    root = args.root.resolve()
    cfg = read_config(root, args.name)
    if cfg is None:
        return
    state, label, plist = prepare(root, args.name, cfg)
    if args.mode == "start":
        start(root, cfg, state, label, plist)
    else:
        print("   prepared shared disk and launch agent for " + args.name)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        sys.exit("shared_disk: " + str(exc))
