#!/usr/bin/env bash
# Extract an uncompressed Linux kernel Image from a distro vmlinuz.
#
# VZLinuxBootLoader requires an uncompressed kernel Image. Distro vmlinuz files
# are usually a compressed EFI-stub PE wrapping the real Image; this scans for
# the embedded compression stream and decompresses it.
#
#   tools/linux-extract-image.sh /path/vmlinuz /path/Image
set -euo pipefail

src="${1:-}"; dst="${2:-}"
[[ -n "$src" && -n "$dst" ]] || { echo "usage: $0 VMLINUZ OUT_IMAGE" >&2; exit 2; }
[[ -e "$src" ]] || { echo "not found: $src" >&2; exit 1; }

python3 - "$src" "$dst" <<'PY'
import gzip, lzma, sys, zlib
src, dst = sys.argv[1], sys.argv[2]
data = open(src, "rb").read()
magics = [(b"\x1f\x8b\x08", "gzip"), (b"\xfd7zXZ", "xz"),
          (b"\x28\xb5\x2f\xfd", "zstd"), (b"\x02\x21\x4c\x18", "lz4")]
for magic, name in magics:
    start = 0
    while True:
        off = data.find(magic, start)
        if off < 0:
            break
        start = off + 1
        try:
            if name == "gzip":
                out = gzip.decompress(data[off:])
            elif name == "xz":
                out = lzma.decompress(data[off:])
            elif name == "zstd":
                import zstandard  # optional
                out = zstandard.ZstdDecompressor().decompress(data[off:])
            else:
                raise RuntimeError("lz4 unsupported")
        except Exception:
            continue
        if len(out) > 1_000_000:
            open(dst, "wb").write(out)
            print("extracted %s Image: %d bytes from offset %d" % (name, len(out), off))
            raise SystemExit(0)
print("no embedded kernel stream found", file=sys.stderr)
raise SystemExit(1)
PY
file "$dst"
