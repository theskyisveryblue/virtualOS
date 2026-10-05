# `bin-inspect.sh` — read-only arm64 / Mach-O binary inspection

> **Read-only. Understanding-only.**
> This tool exists so we can *read* the binaries we run on — inspect arches,
> dump symbols, look at load commands, and (only when asked) glance at
> disassembly. It never modifies, patches, resigns, or writes to a target.
> There is no network access. It is a magnifying glass, not a scalpel.
>
> This follows the manifesto line: *"We do not fear the binary. We read it —
> inspect arches and fat binaries, dump symbols, disassemble, trace
> dependencies … we label it exactly that: inspection, not a supported
> contract."*

`bin-inspect.sh` is a thin, safe wrapper around the tools every Mac already
ships with: `file`, `lipo`, `otool`, `nm`, `codesign`, and `swift-demangle`.
It prints one structured report. Nothing is installed, nothing is fetched,
nothing is written.

---

## Quick start

```bash
# Inspect any binary
tools/bin-inspect.sh /usr/bin/file
tools/bin-inspect.sh /Applications/Safari.app/Contents/MacOS/Safari

# Only the arm64 slice, plus a capped disassembly
tools/bin-inspect.sh --arch arm64 --disasm /bin/ls

# Include a capped printable-string list (off by default)
tools/bin-inspect.sh --strings ./some-binary
```

### Options

| Flag | Meaning |
| --- | --- |
| `--disasm` | Include a capped disassembly via `otool -tV` (`__TEXT,__text`). Off by default. |
| `--strings` | Include a capped printable-string listing. Off by default; no string dump otherwise. |
| `--arch ARCH` | Restrict `otool`/`nm` sections to one slice: `arm64`, `arm64e`, `x86_64`. |
| `--max-lines N` | Cap for disassembly and strings sections (defaults: 120 disasm, 200 strings). |
| `-h`, `--help` | Usage. |

The only required argument is the path to the binary. Exit code is `0` on a
successful inspection and non-zero if the path is missing, unreadable, a
directory, or not Mach-O.

---

## What each section means

### TARGET
Confirms the path exists, is a regular readable file, and that `file(1)`
reports it as **Mach-O**. Non-Mach-O input (scripts, images, text) is rejected
with a clear message rather than producing noise. Prints the byte size.

### ARCHITECTURES
`lipo -info` tells you whether the binary is **fat (universal)** or **thin**,
and which slices it carries:

- `arm64` — Apple silicon native.
- `arm64e` — arm64 with pointer authentication (hardened ABI; used by system
  daemons and some frameworks). A binary carrying `arm64e` is not automatically
  hardened *for your use*; it just has an `arm64e` slice.
- `x86_64` — Intel slice, runs under Rosetta 2.

### CODE SIGNATURE
- `codesign -dv` shows the signing identity, code directory, flags, team id,
  and whether it is ad-hoc `linker-signed` (typical for locally compiled tools).
- `codesign -d --entitlements -` prints the entitlements plist when present.
- **Unsigned binaries are handled cleanly**: instead of an error wall you get
  `unsigned (no code signature)` and `no entitlements`. This is the common case
  for binaries you just built with `swiftc`.

### LINKED LIBRARIES (`otool -L`)
Every `LC_LOAD_DYLIB` / weak dylib the binary links against, with compatibility
and current version numbers. This is the dependency view: what the binary needs
to actually run. The total count is printed and long lists are capped.

### LOAD COMMANDS (selected)
A distilled view of `otool -l`, because the raw dump is enormous:

- **Load command census** — a count of each command type in the binary, a good
  fingerprint of how it was built and linked.
- **`LC_BUILD_VERSION` / `LC_VERSION_MIN_*`** — the **platform** and **minimum
  OS / SDK** the binary was built against. See section 7 below.
- **`LC_RPATH`** — runtime search paths. These are how a `.app` finds embedded
  frameworks under `@rpath/...`.
- **`LC_LOAD_DYLIB`** — the raw install names (full versions in the section
  above).
- **`LC_CODE_SIGNATURE`** — offset/size of the embedded signature blob; absent
  on unsigned binaries.
- **`LC_UUID` / `LC_MAIN`** — the unique build UUID and the entry point offset.

### SYMBOLS (`nm -m`, summarized)
Counts first, samples second — never a full dump:

- **total / defined / undefined** symbol counts.
- **objc-ish** (`_OBJC_`) and **swift-mangled** (`$s`) counts.
- A small sample of defined symbols, with **Swift names demangled** through
  `swift-demangle` so `$s…` becomes a readable type/method name.
- A sample of **undefined imports** (the symbols the binary expects the loader
  to resolve from dylibs).

Stripped binaries produce a clear `nm produced no symbols` warning.

### DISASSEMBLY (opt-in)
Only with `--disasm`. Runs `otool -tV` on `__TEXT,__text` and shows the **first
N lines**, with a note stating the true total and how many were shown. Never a
full disassembly by default.

### STRINGS (opt-in)
Only with `--strings`. Reads `__TEXT,__cstring` (falling back to `strings`),
capped at N lines. **No string dump happens by default**, precisely because
string tables can quietly contain paths or tokens. This is the one section to
review before sharing output.

---

## Platform / minimum-OS (section 7 in the task)

Both are read straight from the load commands, surfaced in the
`LOAD COMMANDS (selected)` section:

```
  -- LC_BUILD_VERSION / LC_VERSION_MIN_* (platform + min OS) --
    cmd: LC_BUILD_VERSION
    platform : 1        # 1 = macOS, 2 = iOS, 3 = tvOS, 4 = watchOS, 5 = visionOS, 6 = Mac Catalyst
    minos    : 15.7     # minimum OS the binary will run on
    sdk      : 15.7     # SDK it was built against
```

For the fleet supervisor you should expect `platform 1` (macOS) and a
`minos`/`sdk` in the macOS 15.x range, since it links `Virtualization.framework`.

---

## Using it on the built `virtualOS.app`

The `.app` is a bundle; point the tool at the **executable inside**
`Contents/MacOS/`:

```bash
# From an Xcode build
APP=$(find ~/Library/Developer/Xcode/DerivedData -name 'virtualOS.app' -maxdepth 6 2>/dev/null | head -1)
tools/bin-inspect.sh "$APP/Contents/MacOS/virtualOS"

# Or from a copied / archived build
tools/bin-inspect.sh ./build/virtualOS.app/Contents/MacOS/virtualOS
```

Useful things to look for in the report:

- **Entitlements** — a macOS VM app needs entitlements such as
  `com.apple.security.virtualization`; if the entitlements are empty, the app
  cannot create VMs.
- **Linked libraries** — you should see `Virtualization.framework` and
  `AppKit.framework`.
- **`LC_RPATH`** — embedded framework search paths.
- **Signing** — a real distribution build is signed (team id present); a local
  debug build may be ad-hoc.

To enumerate the embedded frameworks too:

```bash
F="$APP/Contents/Frameworks"
[ -d "$F" ] && for b in "$F"/*; do tools/bin-inspect.sh "$b"; done
```

## Using it on the fleet supervisor binary

Build it first if `tools/fleet/fleet-supervisor` is missing:

```bash
swiftc -O -framework Virtualization -framework AppKit \
    tools/fleet/FleetSupervisor.swift -o tools/fleet/fleet-supervisor
```

Then inspect it:

```bash
tools/bin-inspect.sh tools/fleet/fleet-supervisor

# arm64 only, with a peek at the entry point
tools/bin-inspect.sh --arch arm64 --disasm tools/fleet/fleet-supervisor
```

What to expect (verified on this project):

- **Thin `arm64`** executable (not universal) when built with `swiftc` on
  Apple silicon.
- Signed **ad-hoc / linker-signed**, no entitlements — as expected for a
  locally compiled tool.
- Links **`Virtualization.framework`** and **`AppKit.framework`**, plus the
  usual Swift runtime dylibs (`libswiftCore.dylib`, `libswiftFoundation…`).
- Swift symbols present and demangleable.

---

## Limitations

- **Read-only by construction.** Uses only read/report flags; no `install_name_tool`,
  no `codesign` mutation, no `otool` writing, no temp files.
- **No network.** Nothing is fetched or uploaded.
- **Caps are heuristic.** Disassembly and strings are capped so a huge binary
  cannot flood your terminal; raise with `--max-lines`.
- **`swift-demangle` may not be on `PATH`.** The script falls back to
  `xcrun swift-demangle`; if neither is available, symbols are shown raw.
- **`--arch` on a thin binary** that does not contain the requested slice falls
  back to the default slice with a warning (otool would otherwise error).
- **Disassembly is armor without meaning.** It shows instructions; it does not
  annotate them. For a readable view, pair with `--arch arm64` and use your own
  knowledge of the ABI.
- **Only Mach-O/arm64 family.** Object files, archives (`.a`), and generic
  binaries outside the Mach-O family are rejected by the `file(1)` check.
- **Entitlements for unsigned binaries simply do not exist**; the tool says so
  rather than pretending.
