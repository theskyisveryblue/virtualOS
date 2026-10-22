# virtualOS

Run a virtual macOS machine on your Apple Silicon computer.

For the current Mac Mini continuation, run these two commands without arguments:

```sh
./tools/cloud-disk-observe.sh
./tools/cloud-disk-archive.sh
```

The observer records completed health snapshots. The worker starts archive
conversion, guarded Linux persistence checks, cloud recovery, and a 24-hour
supervisor. Repeating either command reuses its existing jobs. The supervisor
reconnects lost controllers and retries classified connection failures every
minute; content-integrity failures require repair. It preserves running VMs and
leaves active jobs running when its supervision window ends. Private state lives
under `/private/tmp/virtualos-archive-conversion`. Only the archive operator loads
the protected existing Ai R2 token and derives its current S3 identity in memory;
credentials stay on the operator Mac. No new account token is created.
The live Mini relay transport reads its token from the existing protected file;
the token is absent from its process arguments. Private relay traffic refuses
redirects and ignores ambient proxy settings.

The observer also checks the relay independently of SSH. A private
`ssh-route.json` in the job directory can select the verified local Mini route
(`{"route":"pinned-lan"}`) when the Cloudflare SSH transport is unavailable.
This retains the protected SSH configuration's identity and pinned host key.
Remove that route file to use the Cloudflare transport again.

New desktop VM configurations keep host clipboard forwarding disabled.
Set `VOS_SHARE_CLIPBOARD=1` in the app's launch environment to opt in deliberately.
This setting takes effect when creating the next VM instance; it does not change
an already running guest.

Guest typing now waits for delivery before the next action. Python clients also
refuse shifted characters on older headless supervisors that would replace them
with the wrong key. The replacement supervisor supports printable ASCII and
Return/Tab; its compiled mapping tests pass. Its new keyboard mapping requires
a controlled supervisor restart before it is available in existing fleet VMs.

> One program. Many Macs. Every computer a stream. See [MANIFESTO.md](MANIFESTO.md) for what virtualOS is becoming and the terrain it stands on — virtualization and containerization, filesystems, Linux, arm64 binaries, and web assemblies.

On first start, the latest macOS restore image can be downloaded from Apple servers. After installation has finished, you can start using the virtual machine by performing the initial operating system (OS) setup.

You can configure the following virtual machine parameters:
- CPU count
- RAM
- Screen size
- Shared folder

To use USB disks, you can set the location where VM files are stored.

Unlike other apps on the AppStore, no In-App purchases are required for managing multiple virtual machines, setting CPU count or the amount of RAM.

## Download

You can download this app from the [macOS AppStore](https://apps.apple.com/us/app/virtualos/id1614659226)

This application is free and open source software, source code is available at: https://github.com/yep/virtualOS

Mac and macOS are trademarks of Apple Inc., registered in the U.S. and other countries and regions.

## Standards

The repo operating standards for hidden remote control, secrets, logging, cleanup, and verification live in:

- `STANDARDS.md`

## Mac Mini Host Setup

If you want to use a Mac mini as the always-on `virtualOS` host, see:

- `tools/mac_mini/README.md`

Quick start on the Mac mini from the repo root:

```bash
chmod +x tools/mac_mini/bootstrap.sh tools/mac_mini/install_launch_agent.sh
./tools/mac_mini/bootstrap.sh \
  --enable-remote-login \
  --disable-sleep \
  --install-homebrew \
  --install-cursor \
  --install-tailscale
./tools/mac_mini/install_launch_agent.sh
```

Background startup checks the guest screen for its password prompt and unlocks
it when `VOS_VM_PASSWORD` is configured. Put the password in a local file:

```bash
mkdir -p ~/.config/virtualos
cat > ~/.config/virtualos/start.env <<'EOF'
VOS_VM_PASSWORD=replace-me
EOF
chmod 600 ~/.config/virtualos/start.env
```

If you prefer Finder double-click instead of Terminal:

- `tools/mac_mini/one_click_remote.command`
- `tools/mac_mini/setup_codex_access.command`
- `tools/mac_mini/start_remote_site.command`

## Guest computer use

`tools/vosctl computer` connects models to the running guest VM through `/vm/screen` and the direct guest input endpoints. It does not capture or control the host desktop. Run it on the Mac mini, or run it on an operator Mac through an SSH tunnel:

```bash
ssh -N -L 18845:127.0.0.1:8845 mac-mini-ai
```

In a second terminal on the operator Mac:

```bash
VOS_VM_URL=http://127.0.0.1:18845 ./tools/vosctl computer doctor
```

`doctor` checks the guest screenshot without sending input. If the image is
blank, model control stops before taking a task action. Wake or unlock the
guest explicitly with `./tools/computer-wake.sh`, then run the check again. The
wake script tries one Space key, checks the image three times, and then stops
if the display remains blank.
`./tools/computer-unlock.sh` uses the configured local guest password only
when OCR confirms the central password prompt and lock-screen clock.

Provider commands use the same guest control code:

```bash
./tools/vosctl computer foundry "Summarize the open guest window"
./tools/vosctl computer nova2 "Summarize the open guest window"
./tools/vosctl computer fara "Find the weather in guest Chrome"
./tools/vosctl computer nova-act "Check guest status, then research the issue in the browser" --starting-page https://www.google.com/
./tools/vosctl computer ollama "Click the blue smiling Finder icon in the guest Dock"
```

The installed `qwen3.5:0.8b` vision model can run locally on the Mini through
Ollama. Its click locations use normalized 0–1000 coordinates, which the
adapter maps to the guest screenshot. Add `--act` only when the model should
perform the bounded guest action; the default records a shadow proposal.
The fleet brain also offers an optional OCR perception mode for small text
models. It sends bounded detected text rather than the image and accepts clicks
only on detected target indexes. The request limits CPU threads, context and
output, requests no persistent model residency, and keeps the existing resource
guard. Additional admission checks require an installed model at most 512 MiB,
6 GiB available memory, no active swap-outs and a single VM/worker; a private
lock prevents overlapping OCR sessions. Requests use the checked manifest's full
local digest reference; unsupported servers must refuse instead of falling back
to a mutable model tag. Every request rechecks resources and model residency.
Frame byte and JPEG pixel bounds protect OCR admission; changed frames prevent
input. Transport and trace failures retain action evidence without automatic
replay. Its focused tests pass. A separate private signed runtime on the Mini
has resolved the exact small-model manifest without loading weights, and its
backend lifecycle and network sandbox checks pass. Every runtime file is
checked against a pinned inventory derived from the archive matching
[official release metadata](tools/experiments/private-runtime-provenance-20261019.json).
Live OCR inference and
independent task completion remain unverified while resource admission defers
the trial. See [the readiness receipt](tools/experiments/ocr-runner-readiness-20261019.json).
The host resource baseline and browser, terminal, agent, and VM optimization
order are in [tools/OPTIMIZATION_WORK.md](tools/OPTIMIZATION_WORK.md). Use
`./tools/computer-optimize.sh --status` for a read-only Mini snapshot and
`./tools/computer-optimize.sh --verify` for four local proofs and four Mini checks.
The local, metadata-only app/site learner and its live Dock integration are
described in [tools/OPTIMIZATION_WORK.md](tools/OPTIMIZATION_WORK.md).
The private trace in `~/Library/Application Support/virtualOS/computer-use/`
contains task, before and after images, proposed action, and visual-change
metadata. It stops at 64 MB of images or 8 MB of events. Visual change is a
signal for review, not proof that a task succeeded or that weights were trained.
For new tasks, `VOS_INTENT_PLAN=/path/to/task.intent
./tools/computer-space.sh --intent-plan-compile` validates a compact symbolic
plan before any action. Its default observation is a guest accessibility text
tree, and it requires an explicit fallback before screenshots can be used.
The compiler does not execute a task until its symbolic controls are bound to
a reviewed app profile. See `tools/COMPUTER_USE_METADATA.md` for the syntax.
For Dock tasks, the adapter also waits for the guest menu bar to name the
requested app and records `verified` separately from `changed`. It accepts only
clicks in the lower fifth of the guest screenshot for these tasks. In act mode,
it checks the Dock tooltip at the proposed point and nearby positions before
clicking; if no tooltip matches, it records an abstention. Run
`./tools/vosctl computer audit` on the Mini to count verified outcomes and list
candidate trace IDs. Inspect each candidate's before and after images before
using it for training; the audit does not export screenshots or upload data.
After inspecting a trace, record a local decision with
`./tools/vosctl computer review TRACE_ID --decision accept` (or `reject`).
The private `reviews.jsonl` ties accepted decisions to both frame hashes;
`audit` reports visually accepted IDs separately from OCR-only candidates.
`./tools/computer-dataset.sh` exports only manually accepted, hash-verified
examples into a private local `dataset.jsonl`. It does not train or upload a
model. `./tools/computer-dataset.sh --readiness` validates the accepted
examples and prints task counts, whether a model point was executed or corrected,
image uniqueness, and the
remaining training gaps without changing the dataset.

On the Mini, inspect status and run the read-only preflight first:

```bash
./tools/computer-status.sh
./tools/computer-space.sh
./tools/computer-check.sh
```

To test one model proposal without acting, or to execute one guarded Dock
task, use:

```bash
./tools/computer-verify.sh "Click the blue smiling Finder icon in the guest Dock"
./tools/computer-verify.sh --act "Click the blue smiling Finder icon in the guest Dock"
```

`computer-verify.sh` requires 1 GiB free because it performs only one bounded
step. The old four-step Dock diagnostic is available only with
`./tools/computer-work.sh --dock-practice`; it stops at the first unverified
result.
`./tools/computer-journey.sh` runs a bounded Finder task through Applications,
Downloads, and Documents, or one selected target with `--one-shot --target NAME`
(including Desktop and Recents). The model proposes each sidebar point; exact guest
OCR bounds the click, and the resulting Finder title verifies the view. A model
point inside the visible label's hit region is clicked directly; otherwise OCR
supplies the center and `action_source` records the correction. The first three
reviewed Finder traces predate direct model clicks: `computer-grounding.sh`
confirms their model proposals were in the hit regions, while their executed
action source remains `ocr`. Its
private traces are stored under `computer-use/journeys/`.
`./tools/computer-eval.sh` reports verified actions, abstentions, and model
proposal proximity for both Dock and Finder journey traces without reading or
exporting screenshots.

Reviewed traces can be compressed into a private R2 archive from the Mac that
has the Ai account's scoped R2 S3 credentials and AWS CLI. On the Mini, `computer-r2.sh pack` checks
every accepted review and both image hashes. From this Mac, run:

```bash
./tools/computer-r2-sync.sh status
./tools/computer-r2-sync.sh archive
./tools/computer-r2-sync.sh audit
./tools/computer-r2-sync.sh prune
```

`archive` copies only reviewed examples over SSH, uploads a gzip tar to the
private `mac-mini-computer-use-backups` bucket in the `ai@damidina.com` account after validating its exact file
list, downloads it, checks SHA-256,
and saves a private receipt on the Mini. `prune` rechecks that R2 object before
removing redundant Dock frames. It retains accepted examples, failed or
unverified actions, the ten latest traces, and three recent successful traces
per target; Finder journey frames are retained. The practice runner must be
stopped before pruning. `audit` verifies the current archive without changing
it. Raw journals and unreviewed images are not uploaded.
The relay configured in `tools/cloudflare/worker/wrangler.toml` targets the
same Ai account but needs separate Workers deployment credentials.

### VM disk and Cloudflare storage

`./tools/computer-r2-sync.sh vm-probe` uploads a disposable 1 MiB object to
the private bucket, downloads it, checks its SHA-256, and deletes it. This
checks the R2 path; it does not back up or move the VM. On 2026-10-03 the
Ai account's R2 S3 credentials passed the disposable upload, download, and
delete probe. The Mini's `virtualOS.bundle/Disk.img` was 30 GiB logical and
about 25.6 GiB allocated. The running VM still uses this local file; check
current host free space with `df -h /System/Volumes/Data`. R2 is suitable for
a private offline backup, not the writable random-access disk consumed by
the current Virtualization attachment.
If the operator Mac gets low on space,
`./tools/computer-space.sh --reclaim-operator-build-cache` removes only this repo's two regenerable Xcode
build directories after checking for active builds and open files. On
2026-10-04 it recovered 416,804 KiB; it does not alter the Mini's VM disk.
`./tools/computer-space.sh --reclaim-operator-go-cache` separately clears the
inactive Go build cache after a process check; it recovered 212,488 KiB the
same day. Guarded `--reclaim-operator-playwright-cache`,
`--reclaim-operator-puppeteer-cache`, and `--reclaim-operator-uv-cache`
clear only their named regenerable caches when the matching tool is idle; they
recovered another 1,329,080 KiB on 2026-10-04. These modes do not touch the
Mini's VM disk or unrelated simulators.

On macOS 26.2, a disposable ASIF image opened through the current attachment
API, and a 128 MiB RAW→ASIF→RAW conversion round-tripped byte-for-byte. Apple
now [lists ASIF as a supported Virtualization disk format](https://developer.apple.com/documentation/virtualization/vzdiskimagestoragedeviceattachment)
and calls it a [macOS 26 storage improvement](https://developer.apple.com/documentation/updates/virtualization).
The installed Xcode 26.3 Objective-C header and the URL initializer's detailed
documentation still describe RAW only, so the small attachment test does not
establish this macOS guest's ASIF boot or a space saving. The VM source still
selects `Disk.img`; ASIF remains an isolated conversion experiment.
RAW→ASIF in-place conversion failed. A separate 128 MiB stream test compared
both full writes and writes that skipped zero blocks; each ASIF image was
larger than the source sparse RAW image, although both round-tripped exactly.
Converting the real VM therefore requires a stopped
guest, a verified backup, and enough separate local storage for both images.
Do not replace or delete the running `Disk.img` based on the small-image test.
An external APFS volume with sufficient free space is the practical place to
test a full copy, boot it through `VOS_VM_BUNDLE`, and only then consider
moving the original.

`./tools/computer-space.sh --raw-hole-lab` tested Darwin's `F_PUNCHHOLE` on
a disposable 16 MiB RAW file on the Mini. Punching an aligned, verified-zero
8 MiB range reduced that file's allocated blocks from 16 MiB to 8 MiB while
its byte hash stayed identical. This is an in-place reclamation mechanism for
known-zero regions, not a way to discard live APFS data or infer that the
production disk has 8 MiB or more reclaimable space. The production RAW was
not opened. The guest APFS container is nearly full, and its existing RAW is
already sparse; a read-only zero-run inventory of an offline copy would be
needed before estimating any production saving.
The Mini's metadata-only `--vm-summary` currently reports the 30 GiB
production RAW as about 25.55 GiB allocated, so it is already roughly 4.45
GiB sparse. The staging clone reports similar per-file allocation, but APFS
shares copy-on-write extents; adding those two file totals would overstate
physical usage. The installed `lseek(2)` documents `SEEK_HOLE`/`SEEK_DATA`
and `fpathconf(_PC_MIN_HOLE_SIZE)` for locating existing holes without reading
bytes. After the backup stops using the staging clone, a bounded sample of
those offsets on the clone could show hole distribution. It cannot reveal
allocated blocks that contain zeros, so it cannot estimate additional
`F_PUNCHHOLE` savings. No production zero scan or hole punch is planned from
this metadata alone.

The current VM backup workflow uses the existing shell tools:
`./tools/computer-backup-status.sh` reports the detached backup, monitor,
receipt, R2 progress, clone, and guest screen without changing the VM or archive.
`./tools/computer-r2-sync.sh vm-backup-detached` takes a stopped APFS clone,
restarts the VM, streams a gzip archive to the private Ai R2 bucket, then
downloads the object and checks its SHA-256 before saving a local receipt and
discarding the clone. `vm-resume-detached` recovers one interrupted multipart
archive from the existing clone without restarting the VM. It regenerates the
archive on the Mini and checks every existing part's MD5 before uploading any new part;
a mismatch stops without discarding the clone or uploaded parts. It then
downloads the complete object and checks the full SHA-256. Its private pending
receipt records the clone identity and survives an interruption between completion
and audit, so a subsequent resume can retry the audit for that same clone.
Only new archive bytes cross SSH. The part list travels over stdin rather than
the command line, so large archives remain resumable within macOS command-length
limits. One upcoming 8 MiB read overlaps the current R2 upload. `vm-usage` logs
stored bytes, pending multipart bytes,
and a gross 30-day storage estimate to
`~/.config/virtualos/r2-usage.jsonl`; `vm-account-usage` logs account-wide
Class A and B counts plus this bucket's upload and download bytes. These are
usage measurements, not a Cloudflare bill. `vm-audit` repeats the full
download check. `vm-abort` aborts incomplete VM uploads and leaves the clone
in place; `vm-halt-preserve` stops the uploader while retaining both the clone
and incomplete multipart upload for resumption, and
`vm-stop` also releases the clone. The backup
log is `~/Library/Logs/virtualos-vm-backup.log` on the
operator Mac. `vm-estimate` uses the R2 multipart progress and the Mini's
current source read offset to estimate remaining time; the compression ratio
can change later in the disk. `vm-monitor-detached` checks bucket usage and
Mini free space for up to 16 hours, stopping the upload while retaining its clone
and multipart progress
if host free space falls below 10 GiB. If SSH disappears, the monitor keeps
waiting and resumes the existing transfer when the clone is complete, closed,
and has at least 10 GiB of host disk reserve. Integrity failures refuse automatic
recovery. Up to five restart attempts are allowed; `VOS_VM_MONITOR_RESTARTS`
sets that bound. `VOS_VM_MONITOR_HOURS=48` starts a longer bounded watch.
`vm-monitor-stop`, `vm-halt-preserve`, and `vm-stop` write an explicit monitor
stop marker so automatic recovery does not undo an intentional pause. Starting
`vm-monitor-detached` clears that marker. Guest `computer-stop` is separate.
Set `VOS_VM_SSH_HOST` to an existing SSH alias or address for VM backup, resume,
monitor, and status commands when the default LAN route is unavailable.
These commands do not replace the
Mini's live VM disk.

Cloudflare R2 Standard storage includes 10 GB-month and one million Class A
operations per month; the allowance is shared across the account. On
2026-10-04, Ai account analytics showed about 38.8 GB across 27 buckets before
the full VM backup. A 20 GB retained backup would therefore add roughly
$0.30 per month of storage, depending on its final size and billing rounding.
The VM bucket is in ENAM, so R2's free Local Uploads feature is unlikely to
improve this Eastern North America upload. R2 FUSE does not offer local disk
performance or full POSIX semantics, so it is not used for the writable VM
disk. See [R2 pricing](https://developers.cloudflare.com/r2/pricing/),
[R2 analytics](https://developers.cloudflare.com/r2/platform/metrics-analytics/),
[Local Uploads](https://developers.cloudflare.com/r2/buckets/local-uploads/),
and [R2 FUSE](https://developers.cloudflare.com/containers/examples/r2-fuse-mount/).

The guest's 30 GiB virtual disk is already partitioned: about 26.3 GB for
macOS APFS, 5.4 GB for Recovery, and 0.5 GB for the system's internal security
container. The APFS container reported only 154 MiB free on 2026-10-04.
The remaining virtual disk space is a Recovery partition, so a simple APFS
container expansion is not a fix. The checked user cache was about 188 MiB;
deleting it would make little difference. Do not remove Recovery or Preboot
without a verified recovery image and a successful boot test of a copy.

Apple's [Network Block Device attachment](https://developer.apple.com/documentation/virtualization/vznetworkblockdevicestoragedeviceattachment)
offers a research path: an immutable base image in R2, a bounded local read
cache, and a durable local writable overlay. R2 supports
[ranged reads](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/),
but R2 is still object storage, so this needs a real NBD server and measured
random-read latency. First test an offline, read-only clone with a local NBD
server; then test R2-backed cache misses and failure recovery. Keep the local
RAW boot path until the remote-backed copy boots repeatedly, survives network
loss, and passes integrity checks. Cloudflare
[Containers](https://developers.cloudflare.com/containers/platform/pricing/)
do not provide a free macOS VM host; their included compute is on Workers Paid.
Cloudflare Tunnel and Access can expose a protected browser-facing stream from
the Mini, but that changes remote access, not where the guest computes or stores
its active disk.
`./tools/computer-space.sh --nbd-api-probe` compiles and initializes a
read-only NBD attachment on the Mini without making a connection or touching
the running VM. This passed on the Mini; it proves API availability, not a
bootable remote disk. The next gate is a disposable NBD server and clone boot
with read, write, flush, reconnect, and failure tests.
The credential-free block prototype is invoked by
`./tools/computer-space.sh --cloud-disk-self-test`. It packs an 8 MiB test RAW
image into immutable, SHA-256 checked, zlib-compressed 4 MiB chunks with zero
chunks omitted. A 64 KiB SQLite WAL block overlay preserves unaligned writes
across reopen; its identity is bound to the immutable manifest. A bounded
object cache allows a previously fetched block to be read during a simulated
outage. Complete block replacements skip the base fetch; partial writes still
merge with the verified base. Chunk reads avoid a duplicate hash pass.
The self-test passed on the operator Mac and the Mini. This code has
an NBD fixed-newstyle socket-pair test for export negotiation, read, write
with FUA, flush, out-of-range errors, and reconnect. The test follows the
[NBD protocol](https://github.com/NetworkBlockDevice/nbd/blob/master/doc/proto.md).
It also tests the fallback export-name handshake over a loopback TCP socket.
`--cloud-disk-lab-pack` and `--cloud-disk-lab-serve` in the same shell script
can pack and serve one disposable image under `/private/tmp`, limited to
512 MiB and bound to `127.0.0.1`. Set `VOS_CLOUD_DISK_LAB_SOURCE` and
`VOS_CLOUD_DISK_LAB_ROOT` for packing, then
`VOS_CLOUD_DISK_LAB_ROOT`, `VOS_CLOUD_DISK_LAB_OVERLAY`, and
`VOS_CLOUD_DISK_LAB_PORT` for the one-connection server. It has no R2 fetcher
yet and is not attached to the live VM.
`./tools/computer-space.sh --nbd-vz-lab` creates a separate 16 MiB empty EFI
guest, signs a temporary probe with the Virtualization entitlement, and starts
it against that loopback server. On 2026-10-04 Apple's NBD client negotiated
successfully and issued 33 reads; the probe then stopped and removed its
temporary image. This confirms protocol compatibility for a disposable guest.
With `VOS_NBD_VZ_BACKEND=raw`, the same lab also passed using a read-only RAW
base and a bounded local write overlay.

The next isolated test used the Mini's APFS staging clone of the macOS VM.
`--nbd-macos-config-check` validated its real Mac hardware data and NBD disk
attachment. `--nbd-macos-clone-test` then booted that clone through a loopback
NBD server with the base opened read-only, a 1 GiB overlay limit, and a 10 GiB
host free-space guard. The guest reached its login view and then an interactive
desktop with Chrome; the server recorded 159,218 reads and 37,608 overlay
writes on that run. The first automated desktop predicate was too narrow and
timed out because it accepted only Finder or Terminal menus; it now accepts
the observed Chrome menu too. The production VM was restarted after the test.
This establishes local clone boot through NBD, not R2-backed boot, repeated
failure recovery, or safe removal of the production RAW disk.

The guest text probe uses `./tools/computer-space.sh --guest-tree-probe`.
Its unshifted-key hex bootstrap and fixed-marker guest-to-host callback passed
without retaining a screenshot or guest text. Before guest Accessibility was
enabled for Terminal, indexed window and menu bar queries returned AppleScript
`-1719`, and a numeric-only probe found one Terminal process but returned
`-25211` for both its window and menu bar. [Apple defines `-25211` as
Accessibility API disabled](https://developer.apple.com/documentation/applicationservices/axerror/kaxerrorapidisabled).
The guest's supported Privacy & Security > Accessibility UI showed Terminal
off; after enabling it through the standard macOS password prompt, the same
numeric probe found one process, one window, and one menu bar. The roles-only
probe then returned seven `AXMenuBarItem` roles from Terminal's menu bar.
Only role counts or numeric errors cross the guest-to-host callback; names,
values, and images are discarded. This proves a narrow guest AX observation,
not a general app tree or a task success label.

`./tools/computer-space.sh --guest-ax-verify finder_window_present` is a
bounded, read-only symbolic verifier for the reviewed Finder-window task
family. It checks only Finder process and window counts, returns false with a
nonzero exit when no Finder window exists, and does not depend on which app is
frontmost while Terminal runs the probe. It cannot verify a Finder folder,
title, or the effect of an unobserved click. On the Mini it returned
`verified=false`, process count 1, window count 0. One manually grounded
Finder Dock click then opened a visible Recents window; with Terminal returned
to the front, the same verifier returned `verified=true`, process count 1,
window count 1. This manual check was not added to model training traces.
The disposable `--guest-ax-delayed-focus` lab launched a one-shot AX script
from Terminal, switched to the reviewed Finder Dock target, and received
`finder_frontmost=true` with one Finder window while Finder remained in front.
It sent only a nonce, boolean, and count over the private guest bridge, then
exited through a self-cleaning script path. This establishes observation while
Terminal is in the background, not permission or restart recovery for a
separately signed persistent helper.

The Ai account configuration is `tools/cloudflare/cloud-disk.ai.json`; it
contains no secrets. The dedicated private `virtualos-cloud-disks` bucket was
created with `./tools/cloudflare/poller-session.sh cloud-disk-bucket` and
verified by a read-only HEAD on 2026-10-04. For a future separate account,
copy `tools/cloudflare/cloud-disk.example.json` and set its account ID and
matching endpoint. Run
`VOS_CLOUD_DISK_CONFIG=/path/to/config.json ./tools/computer-space.sh --cloud-disk-config-check`
to validate the configuration without credentials. Once the account and scoped
S3 credentials exist, set `VOS_CLOUD_DISK_CREDENTIAL_FILE` to a private env
file and run `./tools/computer-r2-sync.sh cloud-disk-preflight`. That command
checks credential/account consistency and the dedicated bucket, and performs
only a bucket HEAD request. The
manifest and example config contain no credential material.
`./tools/computer-space.sh --cloud-disk-r2-serve` is now the guarded read-only
R2 NBD entry point. It requires `VOS_CLOUD_DISK_CONFIG`, a pinned
`VOS_CLOUD_DISK_MANIFEST_SHA256`, `VOS_CLOUD_DISK_PORT=40809`, and matching account
credentials in the environment. It fetches only hash-checked immutable chunks,
keeps a bounded local cache and durable overlay, and listens on loopback. A
disposable fake S3 test passed signed GETs, corruption rejection, FUA writes,
and reconnect read-back. The dedicated bucket HEAD passed, but no chunk GET or
macOS boot from R2 has run; a reviewed, uploaded chunk manifest is still
required. The existing Ai account may be used with scoped credentials.
Start and verify this NBD server before `start.sh` in R2 lab mode; `start.sh`
does not launch the server. An established guest NBD socket now remains open
while idle, so a quiet guest does not consume the bounded reconnect slots.
The bounded `tools/cloud_disk_checkpoint.py` prototype snapshots up to
16 MiB of overlay blocks against a pinned base manifest and restores them
after verifying hashes and SQLite integrity. Its R2 path uploads immutable
objects, verifies readbacks and publishes the manifest last; see the durability
proof in `tools/CLOUD_DISK.md`. October 18 tests also proved that a checkpoint
taken with an active SQLite connection restores earlier contents into a new
overlay while preserving later edits in the current overlay. Checkpoint inputs
refuse symlinks, FIFOs, hard links and shared-writable files. Scheduled retention
and switching a running guest to an undo checkpoint remain unfinished; ordinary
guest writes still need an explicit checkpoint to survive loss of the host.
`./tools/computer-space.sh --cloud-disk-r2-upload-clone` is the matching
opt-in pack/upload path for the stopped APFS staging clone. It compresses one
4 MiB RAW chunk at a time, conditionally writes SHA-addressed objects, checks
each object by read-back, and publishes the immutable manifest last. The
disposable fake S3 upload/resume/corruption test passed. A real upload has not
run; it requires the dedicated bucket, explicit `VOS_CLOUD_DISK_UPLOAD_ACT=1`, and
the clone to remain closed throughout the stream.
The source-only `--nbd-r2-lab` app path now requires the separate
`.virtualOS-r2-metadata.bundle`, containing Mac platform metadata and no RAW
disk. `./tools/computer-space.sh --nbd-r2-metadata-prepare` copied only the
four platform files from the completed staging clone; the resulting bundle
occupies about 1.4 MiB on the Mini. The matching `--nbd-r2-metadata-check`
and `--nbd-r2-macos-config-check` passed with no local RAW or NBD connection.
`start.sh` requires both an explicit `VOS_NBD_R2_LAB=1` and the exact
`VOS_VM_BUNDLE` path, and refuses this mode while another VM is running.
`VOS_NBD_VZ_BACKEND=r2-fake ./tools/computer-space.sh --nbd-vz-lab` also
passed: a disposable 16 MiB EFI VM made 33 reads through Apple's NBD client,
the signed fake S3 GET path, and hash-checked chunks, with no read errors.
These checks made no real R2 request. The R2 app build has not been installed
or booted from R2. Keep the production RAW until a real R2 boot, restart,
outage, and restore test all pass.
Runtime inspection also found a private `VZGraphicsDisplay`
`_takeScreenshotWithCompletionHandler:` selector on the Mini. Its completion
type and compatibility are undocumented. The measured supported screenshot
endpoint is already 7–24 ms per warm frame, so there is no measured speed case
for adding that private dependency to the VM.
`./tools/computer-space.sh --virtualization-api-inventory` inventories
underscore-prefixed Objective-C methods of SDK-named Virtualization classes.
On the Mini it found 110 loaded classes and 355 selectors, including
`VZDiskImageStorageDeviceAttachment._updateDiskSize:` and VM state methods.
This inventory does not include Swift-only, C, or hidden classes and does not
invoke those methods.
Apple's public `VZNetworkBlockDeviceStorageDeviceAttachment` already supports
automatic reconnection and full synchronization. The local NBD server now
accepts bounded reconnects against the same SQLite overlay and logs aggregate
request sizes and latency bounds without contents or offsets. The source-only
Mac Mini build passed with a retained public NBD delegate; the newly built app
has not been installed over the running VM.
`./tools/computer-r2-sync.sh vm-range-probe` performs four read-only byte-range
requests against an existing private R2 object and reports latency without
printing object content. On 2026-10-04, the current AWS CLI path took a median
0.714 seconds for 64 KiB; a persistent client and cache must be measured before
deciding whether NBD is usable for boot. This probe adds four Class B reads and
no storage writes.

The intended model task is guest screenshot plus a short instruction to one
grounded click or an abstention. The first measured skills are opening an app
from the Dock and navigating Finder views. The twelve reviewed clicks archived
so far (six Dock, six Finder) are a checked seed dataset, not evidence of a
trained model. Of the six Dock clicks, two used the exact model point, three
were moved by the tooltip guard, and one lacks a recorded proposal. Future
data collection should add distinct screen layouts,
negative/abstention examples, and held-out tasks; `computer-eval.sh` supplies
the independent outcome counts. Repeating the same Dock task does not add a
new skill, so the Dock practice loop is stopped.

`./tools/computer-start.sh --quality` starts a ten-hour bounded quality run.
It checks saved-frame Finder guards, outcome counts, and dataset readiness
hourly even when disk is low. It is observation only: it does not run more
repetitive Dock or Finder clicks, store new screenshots, or auto-approve examples.
When free space remains above 6 GiB for two checks, it runs one cached,
read-only model benchmark on reviewed frames: needed click, already done, and
missing target. It sends no guest input and uploads no images. It uses
`~/.config/virtualos/computer-quality-stop` as its stop marker. The Dock
practice stop marker remains in place; one-shot Finder tasks have explicit
preflight and disk checks.

From the Mac with Ai R2 S3 access, `./tools/computer-work.sh --continuous`
is the single unattended entry point. It keeps the Mini observation runner
alive and checks the private R2 receipt hourly for up to ten hours. It downloads
and audits the archive when the receipt changes or six hours have passed since
the last audit.
`--once` checks one cycle; `~/.config/virtualos/computer-work-stop` ends the
local loop. `./tools/computer-work.sh --status` reports the detached worker and
its latest log lines. The archive audit waits while the operator Mac has less than 1 GiB
free. It never approves examples or uploads a new archive.

The current small-model work has separate `.sh` entrypoints:
`./tools/computer-route.sh --suggest "TASK"` advises which interface mode
fits a request; `./tools/computer-design.sh --compare LEFT RIGHT` compares
alignment, overlap, and whitespace in two local images;
`./tools/computer-xcode.sh --route "What is the deployment target?"` answers
five exact Xcode project inventory tasks from fresh JSON output; and
`./tools/computer-preference.sh --status` reports local design votes.
`./tools/computer-critique.sh --score CRITIQUE.txt` estimates usability and
aesthetics ratings from written human feedback.
The text router, three design principle rankers, and critique interpreter were
trained from pinned, licensed public datasets. Their reviewed artifacts are
hosted in private `fdotinc` Hugging Face model repositories; small local copies
remain for offline inference. They use no private guest screenshots. The router scored
7/10 on its small held-out set; the design models scored 93/114, 100/109,
and 98/108 held-out within-design pairs. These scores do not establish
general Xcode GUI control or overall UX judgment. See
`tools/OPTIMIZATION_WORK.md` for provenance, commands, and limits.
`./tools/computer-hf.sh --verify` audits the private Hub copies when the current
token can read them; `./tools/computer-model-test.sh` checks both plain and
bundled local API inference on loopback.
`./tools/computer-train.sh --router|--design|--critique` reproduces each public
training run in a private temporary directory and checks the output against the
local served artifacts without replacing them.
`./tools/computer-model-api.sh --start-mini` serves these compact models on the
Mini's private VM bridge with bearer authentication. The running API exposes
route advice, written-critique ratings, and design variant comparison. See
`tools/MODEL_API.md` for bounded JSON requests and VM connection details.

The same existing shell entry point can queue 50 bounded, read-only research
jobs on the operator Mac: `./tools/computer-work.sh --research-smoke`, then
`./tools/computer-work.sh --research-swarm`, and
`./tools/computer-work.sh --research-status`. The queue runs at most two
OpenCode jobs concurrently, with a four-minute limit per job and an 8 GiB
operator free space guard. All agent tools
are disabled. Results are private, small, and explicitly labeled unverified
in `/private/tmp/virtualos-research`; they are hypotheses for source review,
not implementation evidence or training examples. The queue does not run on
the Mini or access the VM disk or Cloudflare credentials.

Apple documents the existing URL-based disk attachment initializer for a
[local RAW image](https://developer.apple.com/documentation/virtualization/vzdiskimagestoragedeviceattachment/init%28url%3Areadonly%3A%29-9qeco).
Its newer [DiskImageKit initializer](https://developer.apple.com/documentation/virtualization/vzdiskimagestoragedeviceattachment/init%28diskimage%3Acachingmode%3Asynchronizationmode%3A%29)
is a separate API and was unavailable in the inspected installed SDK. A
disposable ASIF roundtrip therefore does not authorize switching the VM boot
attachment to ASIF. The running and source boot paths use RAW.

The saved-frame check found all six reviewed Finder model points inside their
target hit regions. The 0.8B model did not reliably abstain for a deliberately
absent target, while the independent OCR guard rejected it on all six frames.
In a live three-view journey, two verified clicks used model points and one
verified view used an OCR click after a false `done`. The false-`done` retry
produced a guarded click on the saved frame; a live retry remains to be measured.
On a saved Finder screen, both Desktop and Recents produced guarded model points
after at most one retry. Desktop has since passed a live, visually reviewed
model click; the live Recents outcome remains to be measured.

For a bounded Dock practice run, use the `.sh` entry point:

```bash
./tools/computer-start.sh
```

`computer-start.sh` checks disk space, wakes and unlocks the guest, runs the
full preflight, and starts `computer-night.sh` in the background.

The entry point refuses to start below 3 GiB free, without a confirmed guest
desktop, or when another runner is active. The loop rotates Finder,
Terminal, Safari, and Chrome targets and stops if disk space falls below 3 GiB
or five rounds fail in a row. It does not upload traces. Before each act round,
it runs the guest wake and guarded unlock scripts. If they cannot confirm the
desktop, the loop stops. Set
`VOS_CUA_OVERNIGHT_ACT=0` to gather shadow proposals. Set
`VOS_CUA_OVERNIGHT_TASK` for one of the four Dock tasks in act mode. Create
`~/.config/virtualos/computer-stop` to stop the loop. The scripts collect
evaluation traces only; cloud model training still requires curated success
labels and a training job.

For cloud training, the current practical route is to review these guest-only
image/action traces, move approved images and a JSONL dataset to S3, and fine
tune a vision-capable model such as [Nova 2 Lite](https://docs.aws.amazon.com/bedrock/latest/userguide/nova-2-sft-data-prep.html).
Cloudflare R2 can hold the trace archive, while [Workers AI LoRA inference](https://developers.cloudflare.com/workers-ai/features/fine-tunes/)
requires a separately trained adapter on a supported base model. Neither
storage nor action logging updates model weights by itself.
Workers AI offers vision inference models, but its documented LoRA workflow
uploads adapter files trained elsewhere; it is not a training job for these
guest screenshot/action examples. Its free inference allowance is measured in
daily Neurons, separately from R2 storage and request allowances.

Set `VOS_CUA_PYTHON` to the Python interpreter with your provider SDKs. Fara pins Playwright 1.51 while Nova Act currently requires 1.54–1.56, so keep them in separate environments and set `VOS_CUA_FARA_PYTHON` and `VOS_CUA_NOVA_ACT_PYTHON` to their respective interpreters. The existing `vosctl` command selects the matching interpreter.

- **Microsoft Foundry:** Set `FOUNDRY_PROJECT_ENDPOINT` and sign in with `az login`, or set `FOUNDRY_TOKEN`. The `computer-use-preview` deployment needs separately granted access. Its safety checks require an interactive `yes`; noninteractive runs stop and report the pending check.
- **Amazon Nova 2 Lite:** Install `boto3` in the operator environment and configure standard AWS credentials with Bedrock model access. `NOVA_MODEL_ID` overrides the default model ID. This uses Nova's image understanding to propose one guest action per screenshot.
- **Microsoft Fara 1.5:** Install the official `microsoft/fara` Python package in the operator environment and set `FARA_ENDPOINT_CONFIG_PATH` to a private JSON file containing `model`, `base_url`, and `api_key`. The adapter uses the official Fara browser agent against guest Chrome; browser page text extraction is unavailable through the guest pixel API.
- **Amazon Nova Act:** Install `nova-act`, set `NOVA_ACT_API_KEY`, and install `boto3`. Nova Act runs its own headless browser and receives guest control tools. Its `guest_observe` tool uses Nova 2 Lite to describe guest screenshots; this also requires AWS credentials with Bedrock model access. Nova Act's native browser actuator does not see the guest pixels directly.

Provider packages and model weights are optional. Keep credentials outside this repository. The Mac mini can stay on its existing `start.sh` and auto-login setup; no model download or additional background service is required there.
