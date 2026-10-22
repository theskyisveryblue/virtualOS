# virtualOS resource work

The current host and guest run macOS. Optimize the shared paths used by browser
apps, the terminal, AI agents, and the virtualOS guest. Compare alternate
implementations against measured disk, memory, latency, and recovery behavior.

Run `./tools/computer-optimize.sh --status` for read-only Mini pressure and
clone state. Run `./tools/computer-optimize.sh --verify` for four local proofs
(cloud disk, playbook, image retention, and small model) and five Mini checks
(resource pressure, clone, offline learner, model status, and compact-model
API). A passing verification means those checks worked; the resource report
separately says whether new background work may start.

## Measured host baseline, 2026-10-04

`./tools/computer-resources.sh --report` groups process RSS without reading
private app data. During the Mini freeze, an early grouping counted 237
Chrome/WebKit-named processes, including children of an inactive iOS
simulator. The report now separates `simulator_runtime` from browser engines;
after cleanup it measured 100 browser-engine processes and zero simulator
runtime processes. Used swap reached 44 GiB. Summed RSS can count shared
pages twice and omits paged-out memory. The live guest is configured
for 3 CPUs and 4 GiB RAM on a 16 GiB host. Host load reached about 393.
After the host began recovering, load fell to about 12 and free disk rose to
about 8 GiB, but the guest health endpoint still timed out.
An old `launchd_sim` then remained with 158 children while all six simulator
devices reported Shutdown. The guarded
`./tools/computer-resources.sh --stale-simulator-check` verified that 152
children belonged to the simulator runtime before
`--reclaim-stale-simulator` sent SIGTERM. The service exited; the broad
Chrome/WebKit count fell from 237 to 99, used swap fell from about 38 GiB to 10.7 GiB,
and free disk rose to about 30 GiB. The follow-up check refused to run again
because the stale service was gone. The guest answered at its VM IP, but the
host control socket on port 8845 still timed out.

`tools/vosctl status` now matches the guest's saved MAC to the bridge ARP
entry. It reports `192.168.64.2` and `guest network reachable (control
unavailable)` when port 8845 fails. The stream service listening on port 8899 is an
`ai-share` runtime, so the viewer changes in this repository are not live on
that port. `VMCommandServer.swift` now caps concurrent clients at eight,
times out stalled writes, and backs off on accept errors; its arm64 macOS
build passes. The running app has not been replaced while guest writes lack
a verified backup.

`--report` includes a current memory-free percentage and the top executable
names, without process arguments. At the last check, the Mini had 28 GiB disk
free, 31% reported memory free, and about 11.5 GiB swap used. The work guard
continues to pause new workloads at that swap level.

The existing port 8845 later resumed accepting local connections without a
VM restart. Its health endpoint and screenshot both passed. A title-bar click
made the visible guest Terminal active, and the symbolic guest AX probe
measured one Finder process with one window. The guest script bootstrap now
sends 384 hex characters per chunk instead of 192. Its 753-byte long smoke
fixture passed with an exact guest-to-host callback in 45 seconds. The old
one-shot OCR smoke check missed output that was visible on the screenshot;
the callback verifies only the fixed test word and keeps no guest UI text.

The live `ai-share` viewer on port 8899 was updated after a canary on port
18998 passed with the existing authentication. It now allows one outstanding
frame request, waits at least 500 ms after each result, and pauses hidden tabs.
`./tools/computer-stream-reload.sh --status` verifies that the optimized
source is live without exposing the token. The reload changed only the
independent stream process; `/vm/health` still reported the guest running.

The Mini's `ollama ps` showed `qwen3.5:0.8b` occupying 2.1 GB of GPU memory
between requests. The virtualOS Ollama client now sends a validated
`VOS_OLLAMA_KEEP_ALIVE` value, defaulting to `60s`, on its own chat requests.
[Ollama's chat API](https://docs.ollama.com/api/chat) supports this duration.
A local request fixture verified the default, `0` override, and invalid-value
rejection. No model inference was started while the Mini's work guard was
paused, so a runtime memory saving has not yet been measured. An active
`opencode2` connection to Ollama was present at the last check; that agent
can keep the model resident independently of the virtualOS client setting.

## Work order

1. **Host admission:** keep background quality, research, and new VM work
   paused below 3 GiB free or above 8 GiB used swap. Preserve the live VM
   bundle. Recheck the guest only after host pressure falls.
2. **Browser path:** allow one outstanding screenshot request per viewer and
   pause hidden tabs. Next measure per-app process count, cold load, idle RSS,
   and frame capture time before changing embedded browser applications.
3. **Agent path:** keep bounded research concurrency and provider failure
   cutoffs. Test [Pi Durable](https://github.com/earendil-works/pi/blob/main/packages/durable/README.md)
   on a disposable operator-side task for crash recovery; its API is
   experimental, and durability alone does not lower memory use. Compare
   restart correctness, stored bytes, and idle RSS before adopting it.
4. **Terminal and guest:** measure command startup, persistent shell count,
   guest CPU, guest memory, and AX verification latency. Batch symbolic
   observations where it reduces round trips without hiding failures.
5. **Cloud disk:** measure R2 GETs per boot, cache hit rate, overlay write
   latency, and checkpoint restore. Complete block writes already skip a base
   fetch; future boot tests require host headroom and a verified image.

The current backup upload has no verified VM receipt. Its incomplete
multipart parts were not a restorable archive. The abandoned upload had
1,633 parts totaling 13,698,596,864 bytes and no running uploader; after the
staging clone was removed, `./tools/computer-r2-sync.sh vm-abort` cleared it.
`vm-usage` then reported zero pending parts and about 34 MB of existing objects.
Cloud boot and a new upload remain behind disk and guest-health gates.

## Small local decision model

`./tools/computer-decision.sh --self-test` trains only on declared synthetic
Finder states. A 599-byte model correctly chose or abstained on three synthetic
holdout rows; those rows reuse the same state patterns and establish format and
inference behavior only. The model accepts bounded symbolic facts and option
IDs learned during training, returns probabilities, and never executes an action. Unknown facts
and low confidence cause abstention. `--train FILE MODEL` requires observed,
independently verified, rights-cleared episodes with separate train, validation,
and holdout group IDs. It also rejects an evidence digest reused across splits
or a duplicate review ID. Those fields are provenance claims: a reviewer must
establish group independence from app, surface, goal, and shared evidence before
training. Existing candidate episodes have unknown training rights
and only two conservative leakage groups, so no real model has been trained.

Cloudflare's [Clef Flash](https://developers.cloudflare.com/workers-ai/models/clef-flash/)
offers typed choices and open weights under [Apache 2.0](https://blog.cloudflare.com/clef-decision-models/).
The [published files](https://huggingface.co/Cloudflare/clef-flash/tree/main) total
about 19 GB. Keep the full weights off the 16 GB Mini while swap is high. A
hosted Clef call using reviewed symbols may later supply comparison scores;
private screenshots and unreviewed task text must stay out of that request.
`./tools/computer-decision.sh --clef-fixture` prints a public synthetic request
in Cloudflare's typed-choice shape for an exact local-to-hosted comparison.
`./tools/cloudflare/poller-session.sh clef-smoke` sent that fixture through the
existing Ai account on 2026-10-04. Clef returned `dock_finder_click` with
0.9712 option probability and `abstain` with 0.0288. This is one synthetic API
response, not an evaluation of live computer use or local-model quality.

## Running offline learning loop

`tools/offline_policy.py` is a local learner for exact symbolic app or
HTTPS-origin states. It stores action success/failure counts in a capped SQLite
file under `~/Library/Application Support/virtualOS/offline-policy/`; no task
text, URL path, screenshot, or page content enters the file. Its `.sh` entrypoint
supports `--observe FEEDBACK.json`, `--suggest REQUEST.json`, `--stats`,
`--ingest-traces TRACE_DIR`, and `--export-local`. Suggestions include
`abstain` and cannot execute.
The app/site scope and interface version are part of the learned context, so a
different site or app starts with no action history.

The live Ollama Dock and Finder sidebar paths now read the offline policy
before choosing an action, give the local vision model a learned click
preference when present, and record executed clicks after separate OCR outcome
checks.
A history with at least three failures and at least 80% failures pauses that
action. Existing Mini traces were imported only after their before/after hashes
matched, and older traces also had to have a click inside the Dock region.
The first Dock import yielded 33 local feedback rows across four apps; the
other 256 traces lacked qualifying action evidence or retained frames.
Four guarded Finder journeys with matching before/after hashes were then
imported, bringing the local database to 37 feedback rows in a 45 KiB file
and eight exact contexts. Three older journeys lacked guarded action provenance
and remain excluded. `--export-local` wrote the 37 observed outcomes and
symbolic contexts to a 9,974-byte private JSON dataset on the Mini. Nineteen successful Dock clicks include
normalized Dock locators. It is marked `local-only` and has
no cloud or external training permission or train/validation/holdout claim.
For Terminal, five agreeing successful locators yield a stable Dock point.
The live path can replay that point, confirm its tooltip, and skip the local
vision-model call; a failed tooltip check falls back to the vision model. A
controlled full-path run completed with zero Ollama calls. A live guest click
was deferred while the Mini resource guard reports high swap.
The Finder journey entrypoint now records its verified outcome into the same
local learner. It can use the current OCR row and skip the vision model after
three successes at an 80% success rate. A reviewed Finder sidebar transfer
profile shares action evidence across distinct sidebar goals, while each click
still requires a unique target row in the current frame and an independently
verified result. The Mini's four successful goals now meet that profile; a
symbolic Recents request selected the sidebar action without a model call.
Repeated failures for the exact goal stop the journey. Guest execution remains
paused by the Mini's general background-work guard. A separate single-action
guard admits one model-free journey only with at least 8 GiB disk free, at
least 25% current memory free, load below the CPU count, and under 16 GiB used
swap. It activated Finder through a tooltip-confirmed Dock point, then clicked
Applications through the current OCR sidebar row; both outcomes were verified
without an Ollama call. The private learner now holds 39 outcomes in 53 KiB,
with an 11,369-byte dataset and 1,542-byte shadow model. Recents was not visible,
so it was not clicked. The curriculum now chooses the least-covered currently
visible sidebar goal, capped at three successes or three failures per goal.
The bounded `.sh` worker attempts at most one such action per hour while
the stricter background guard remains paused. Other app and site workflows
can call the same symbolic `--observe`/`--suggest` interface after they have
grounded actions and independent outcome checks. Local-only feedback is not
exported as a rights-cleared training dataset.

The second approach, `tools/offline_model.py`, trains a sparse linear chooser
from the local-only dataset and splits whole app/site scopes across train,
validation, and holdout. The current Mini artifact is 1,542 bytes from 39
outcomes and four app groups. It abstained on every validation and holdout row,
matching the always-abstain baseline, so it remains in shadow mode and cannot
control actions. `--suggest-all REQUEST.json` shows both approaches side by
side. The unattended worker exports updated feedback and retrains this model
when the verified row or source-app count changes. It also removes an old duplicate worker
before restarting, keeping one bounded background session.
An exact-state audit found 34 Dock rows with identical model inputs but mixed
outcomes. A metadata-only comparison added the pre-action frontmost app as a
fact: ambiguous rows fell to 18 and validation rose from 1/6 to 6/6, while
holdout stayed at 6/11, equal to always abstaining. This feature is not promoted
to the linear model. The local count policy now stores 34 source-app facts in
a 53 KiB SQLite file and can distinguish reviewed transitions: Safari to
Chrome and Chrome to Terminal each had four verified successes and no failure;
Terminal to Finder (2/4) and Terminal to Safari (5/10) abstain. These are
online local decisions from observed counts, not a cross-app generalization
claim. The hourly worker reimports hash-verified traces before dataset export
so an interrupted feedback write can be recovered. The 11,369-byte local-only
dataset also carries the source-app symbols, allowing comparison without
reopening the private frame journal.

`--suggest-playbook PLAYBOOK.json STATE.json` and
`--observe-playbook PLAYBOOK.json EVENT.json` bridge a validated versioned app
or HTTPS-origin playbook to the same local learner. Inputs remain symbolic; a
verified event must name the playbook step and its independent verifier. This
gives new app and site adapters one interface for local decisions and feedback.
`tools/playbooks/finder-recents.json` is the first declared local recipe. Its
symbolic action ID maps to the live Finder learner; on the Mini its symbolic
state selected `finder_sidebar_click` through the transfer profile, while the
linear model remained in shadow. The recipe has unknown training rights and
an unassigned split, and it has not been uploaded.
`tools/playbooks/terminal-dock.json` and `chrome-dock.json` cover two more
versioned app goals. With the source-app fact, their local suggestions select
the guarded Dock action for Chrome→Terminal and Safari→Chrome, each from four
verified successes and no failures. Chrome from Terminal abstains after four
verified failures, and the live Dock path checks that source-specific failure
history before it can call the vision model. They remain declared, local recipes with
unknown training rights; no recipe itself can click the guest.
The adapter still needs a grounded control locator and an outcome oracle for
each real app/site workflow before it can execute there.

## Hugging Face models and new design feedback

`./tools/computer-route.sh --suggest "TASK"` runs a 46,417-byte text router
offline. It was trained on the pinned Apache 2.0 `akashnaren/agent-ui-human`
training split (50 examples) and scored 7/10 on its separate test split,
versus a 5/10 majority baseline. The artifact and evaluation are in the
private [virtualos-ui-router-v1](https://huggingface.co/fdotinc/virtualos-ui-router-v1)
model repository. It recommends an interface mode; its confidence is
uncalibrated and it cannot operate Xcode or judge a design.
`./tools/computer-train.sh --router` reproduced the router locally on
2026-10-10 from the checksum-pinned public split. Its model and metrics matched
the checked-in copies byte for byte (SHA-256 `c71696cd3abc44bc104e37d9b7b3a56b0cc791dc6c7fafe80f3204db7e3ce2b7`
and `70f18f5eafe83d44a8b3f391a8204e6ed26f6b81313c1fcfb383bc3432c60eca`).
A separate `./tools/computer-decision.sh --train-local` run on the Mini trained
from 39 verified local outcomes, but held-out accuracy tied the always-abstain
baseline at 6/11, so that 1,542-byte model remains shadow-only.
A Hugging Face CPU Job was attempted with a ten-minute cap. The active fine-grained token
lacked `job.write`; the stored write token passed that check but the account
returned HTTP 402 for insufficient prepaid credits. Training therefore ran
locally from the verified Hugging Face download, followed by a private Hub
upload. No private virtualOS traces were sent to Hugging Face.

`./tools/computer-design.sh --compare LEFT_IMAGE RIGHT_IMAGE` uses three small
models trained on the pinned Apache 2.0 human-rating shards in
`creative-graphic-design/GraphicDesignEvaluation`. The models are in the
private [virtualos-design-principles-v1](https://huggingface.co/fdotinc/virtualos-design-principles-v1)
repository and `tools/models/design/`. They rank alternative images for
alignment, overlap, and whitespace. Every perturbation of a source design
stayed in the same split. On 22 unseen design groups, they agreed with human
mean ratings on 93/114 alignment pairs, 100/109 overlap pairs, and 98/108
whitespace pairs; pairs under a 0.5 rating gap were omitted. Inference uses
macOS `sips` plus Python's standard library, with the same features as
training. These tests concern perturbed graphic designs, not complete UI/UX
quality or Xcode screens.
`./tools/computer-train.sh --design` reproduced all three models from the
checksum-pinned public shards on 2026-10-10; all six model and metrics files
matched the checked-in copies byte for byte.

`./tools/computer-preference.sh --vote FAMILY DIMENSION LEFT RIGHT left|right`
records an explicit local preference, image hashes, and compact derived
features under ignored `tools/.private/design/`. It retains no screenshots or
task text. A vote immediately attempts a local update; `--status` reports
counts. `--train DIMENSION` can also retrain explicitly. The local
personal preference head only after 50 votes spanning ten independent
design families, with at least ten held-out votes. Promotion requires at
least ten percentage points and two correct predictions over the base model.
At present it has zero human votes, so no personal model was
promoted. `overall` preference needs its own votes; no combination of the
three principle scores is presented as an overall UX verdict.

`./tools/computer-critique.sh --score CRITIQUE.txt` runs a separate 43 KB
text model trained from human-sourced comments and ratings in the CC BY 4.0
[UICrit](https://github.com/google-research-datasets/uicrit) dataset. It
excludes 83 rows whose comment/source arrays do not align and holds out whole
screen IDs. On 547 unseen-screen ratings, usability MAE was 0.616 versus a
0.781 training-mean baseline; aesthetics MAE was 0.633 versus 0.814. The
artifact is in the private [virtualos-ui-critique-v1](https://huggingface.co/fdotinc/virtualos-ui-critique-v1)
repository. The model interprets an existing human critique; it does not see
or assess a screenshot and has no measured Xcode or current website accuracy.
`./tools/computer-train.sh --critique` reproduced the model from the public
UICrit CSV on 2026-10-10; its model and metrics matched the checked-in copies
byte for byte.

`./tools/computer-xcode.sh --collect` records the actual Xcode project,
scheme, target, configuration, and selected build settings in
`tools/data/xcode-inventory.json`. `--task list-schemes`, `--task
build-settings`, and three other task IDs parse fresh `xcodebuild` JSON and
return verified results without launching the Xcode GUI. `--route "What is
the deployment target?"` maps one unambiguous request to an observed task;
unknown or multi-task requests abstain. Xcode's small derived-data cache for
the settings query stays under `/private/tmp/vos-xcode-derived`. This is a
seed for complex-app navigation, not GUI action data.

`./tools/computer-data.sh --uipad-audit /private/tmp/vos-uipad` produced
`tools/data/uipad-audit.json` from the MIT-licensed MacPaw UiPad question
tables and seven VSCode AX trees. It outputs counts and role names only. The
public split has 828 train and 228 test questions, zero shared screen IDs,
but 38 apps appear in both splits; it is a screen holdout, not an unseen-app
test. It contains no Xcode questions. These public data were not mixed with
virtualOS private traces or trained into the router.

The three compact model families are also in private `fdotinc` Hugging Face
repositories. Their local copies total 192 KiB; temporary public training
downloads were removed after every hosted file passed SHA-256 comparison.
The verified Hub commits are `031f70bd8b7c` (router), `62aab3c22797`
(design principles), and `0fd57e80792c` (critique). Keep local copies for
offline inference; these commit IDs are remote recovery points.
`./tools/computer-hf.sh --verify` now checks the current private Hub heads and
compares every local model/metrics JSON file byte-for-byte with its remote copy.
On 2026-10-10, the operator's current Hugging Face token received HTTP 404 for
all three private repositories, so remote availability could not be reverified
with that token. This does not affect offline inference: the Mini's 15,342-byte
bundle was loaded by the model API, and its route, critique, design, and
unauthorized-request checks passed.
The same day, `VOS_CUA_WORK_HOURS=20 ./tools/computer-work.sh
--continuous-restart` started a detached 20-hour operator worker. Its guarded
cycle imported the existing 39 verified local feedback rows, retrained the
1,542-byte shadow model, passed the format and Mini model API checks, and
audited the R2 archive (12 examples, 24 images). The Mini quality runner is
active in observation mode. The production guest currently returns a visually
blank 980×700 frame (12,415 JPEG bytes, zero grayscale variation); one safe
`computer-wake.sh` key did not change it. No new guest task action was taken.
The worker continues bounded checks and will retry when the guest is usable.
The ten JSON model/metric files contain 118,383 bytes of decimal text. The
shipped bundle replaces every float with a two-byte int16 fixed-point value
(`tools/model_bundle.py`, `VOSB1` with a SHA-256 header) and Brotli-compresses
the result: `models.bundle.br` is 15,342 bytes on the Mini, 87% below the text
and about 40% below the best text-only codec (Brotli 11 on JSON was 25,365 B).
The decode is bit-defined and the API reads plain JSON unchanged when the bundle
is absent. Monte-Carlo re-scoring bounded the output change at 0.035 on the
design `score_delta`, 0.015 on router logits, and 0.0004 on critique ratings.
See `tools/WEIGHT_QUANT_EXPERIMENT.md` and `tools/COMPRESSION_DECISIONS.md`.
`./tools/computer-model-api.sh --bundle-mini` builds the bundle (needs the
`brotli` CLI); `--start-mini` serves route, written-critique, and design-variant
advice on the VM bridge at `192.168.64.1:8765` with a local bearer token and
loads the bundle automatically. The live process measured about 32 MiB RSS.
Eleven tests pass on both Macs, including the four API integration tests run
from the decoded bundle; the live Mini self-check returned HTTP 200 from all
three inference endpoints and HTTP 401 without a token. The Mini resource guard
still pauses background guest work at high swap. See `tools/MODEL_API.md` for
request formats and token handling.

Headless auto-start was broken by two app bugs, both fixed in
`MainViewController`: `attemptAutoStartIfNeeded` did not populate the selected
bundle/parameters before starting, and it did not open the sandbox scope for the
VM directory, so `Parameters.txt` was unreadable and the start silently aborted.
The installed app must also keep the `com.apple.security.virtualization`
entitlement: re-signing without it makes the VM refuse to start
(`VZErrorDomain code=2`). A signed build that omits `app-sandbox` (keeping
virtualization and network) starts the VM with the bundle under
`~/VirtualOSMachines`, which is outside the app container. With sandbox enabled,
macOS needs a user-granted security-scoped bookmark for that directory; a
programmatically created one is not sufficient, so a headless launch still
fails there.

The VM archive has no verified receipt because `vm-backup` never finished:
the local file `~/.config/virtualos/vm-r2-receipt.json` is written only after an
upload is re-downloaded and SHA-256 checked (`tools/computer-r2-sync.sh:707-739`),
and the Oct 4 run stopped at 12 GiB compressed. The incomplete multipart was
later aborted, so `vm-usage` now reports zero pending parts and the staging
clone is gone; the 4 stored objects (33.6 MB) are older metadata, not an
archive. The live RAW `Disk.img` is 30 GiB. A receipt requires a fresh
`vm-backup-detached`, which clones the bundle (without touching the live RAW),
restarts the VM, uploads, and re-verifies.

The Mini later reported a healthy VM while returning a genuinely black guest
frame: `./tools/computer-frame.sh --audit` measured zero grayscale variation
and zero edges, with a 12,415-byte JPEG. The existing
`./tools/computer-wake.sh` sent one space key after the blank check, and a
second audit measured a 162,054-byte usable frame. The hourly Finder target
probe now turns a failed screenshot read into a concise refusal rather than a
Python traceback. New guest actions remain paused when the Mini's offline
resource guard exceeds its swap limit; waking the display did not override
that guard.
Later on 2026-10-10 the Mini returned the same 12,415-byte black frame again.
`computer doctor` reported `screenshot_usable=false`, and a guarded Space key
plus one bottom-corner guest click did not wake it. The VM health endpoint still
reported `running` and `ready`, so those flags alone cannot admit training
actions. The Mini had neither a verified VM backup receipt nor a staging clone;
the production VM was left running. The quality runner now defers guest checks
when `computer doctor` reports an unusable screenshot, while its read-only
evaluation and dataset checks continue.

On 2026-10-11, `vm-backup-detached` created a consistent APFS clone and restarted
the production VM. The guest screen became usable again. The R2 multipart upload
is in progress from the clone; no verified VM backup receipt exists yet. The
backup script now retains the clone if upload, audit, or restart verification
fails. Its detached monitor stops a low-disk upload while retaining the clone.
The Mini's February `inputctl` binary was older than its October Swift source
and incorrectly required Accessibility permission for OCR. Rebuilding the
helper through `./tools/computer-check.sh --build-ocr` on the operator Mac and
staging the arm64 binary restored guest-only OCR; the Mini's no-model preflight
passed. A new 72-hour quality runner now performs hourly read-only evaluation
and dataset readiness checks even while memory pressure pauses guest actions.
Once the guard permits guest work, it will wake and unlock the guest before its
hourly guarded check. The Mini's current swap use exceeds the 8 GiB action
threshold, so no new model action has been claimed.

The later 2026-10-11 check found the operator worker, VM archive uploader, and
backup monitor stopped. R2 retained 349 pending archive parts totaling
2,927,624,192 bytes; the complete staging clone remained on the Mini. No verified
VM archive receipt exists. A guarded Space key temporarily restored the guest
display, but a later doctor check returned the blank 12,415-byte frame again.
The guest preflight also found `~/.config/virtualos/computer-stop`; that marker
was left in place. The observation quality runner remained active. The live
Mini model API passed route/critique/design (200) and unauthorized-request (401)
checks, and all 12 local plain/bundle API tests passed.

`tools/cloud_disk_delta_probe.py CONFIG` now provides a bounded real-R2 delta
check using three synthetic blocks under a fresh fleet prefix. Its first real
run passed exact upload/download comparison, immutable object reuse, and
wrong-pin rejection with the existing local delta preserved. It retained four
objects (66,177 compressed block bytes plus the manifest); the receipt is in
`tools/experiments/r2-delta-probe-receipt.json`. This is transport evidence, not
a real VM delta or verified VM archive.

The archive transfer is now resuming through
`./tools/computer-r2-sync.sh vm-resume-detached`. The Mini regenerated the gzip
archive and matched all 349 original multipart MD5s (2,927,624,192 bytes) before
forwarding only new bytes over SSH. New parts then uploaded. A controlled
halt/restart retained the parts and successfully reverified the enlarged
352-part prefix; the subsequent transfer advanced through part 354. The disk
monitor is active, and a low-disk halt now retains both the multipart upload
and clone. The pending receipt records the full archive hash and clone identity
before multipart completion, allowing an interrupted full-download audit to
retry against the same clone. Verified receipt publication precedes clone
release. Seventeen resume tests pass, including interrupted transfers,
wrong-prefix refusal, source/tail hashing, clone identity checks, and retention
of the pending receipt after a failed completion call. The full archive is still
in progress and has no verified receipt yet.

A subsequent continuation moved the multipart metadata from the SSH command
line to a separate stdin file descriptor. This avoids macOS command-length
failures when the archive has thousands of parts. A real bash pipeline test
passed with a multi-megabyte metadata list, forwarding no archive bytes until
the entire existing prefix matched. The transfer also reads one upcoming 8 MiB
part while the current part uploads to R2. Tests confirm that a failed upload
does not wait for a blocked prefetch before allowing source cleanup. Twenty
resume tests now pass. The live updated uploader reverified the 378-part prefix
and advanced through part 385 (3,229,614,080 compressed bytes), with the monitor
active and about 32 GiB free on the Mini. Full archive verification remains
pending; no throughput improvement is claimed from the short observation.

On 2026-10-12 the transfer had stopped after part 396
(3,321,888,768 compressed bytes) because Mini SSH became unavailable. The old
monitor exited without a receipt. Direct LAN SSH timed out, and the configured
`mac-mini.mergerealestate.ca` Cloudflare SSH hostname no longer resolved. The
read-only relay scoreboard still showed a fresh heartbeat and `vm-a:running`.
No full VM archive receipt or pending completion receipt exists.

The backup monitor now waits through SSH outages instead of exiting, and can
resume the existing multipart transfer after verifying a complete, closed clone
and at least 10 GiB of disk reserve. Integrity failures refuse automatic retries;
the restart count is bounded. Explicit monitor stops and upload halts leave a
stop marker, preventing automatic recovery from undoing an intentional pause.
VM backup and recovery accept `VOS_VM_SSH_HOST` for an alternate route. A new
48-hour detached monitor is active and has logged `waiting_for_ssh`. All 27
resume/recovery tests pass. The guest stop marker remains separate; no guest
automation or claim of completed cloud recovery was made.

## Extended storage work queue — 2026-10-21

Continue through the same no-argument shell entrypoints:

```sh
./tools/cloud-disk-observe.sh
./tools/cloud-disk-archive.sh
```

The detached supervisor runs for a bounded 24-hour window and samples every minute. It reconciles evidence and eligible existing jobs; it does not implement these engineering tasks or renew unattended agent leases. One agent task stays active at a time. Tasks need source-bound evidence before completion. Keep production and Linux running and preserve the original VM disk. Approval and resource gates remain in force.

This is an ordered backlog, not a claim that every item is already running. The persistent controller is authoritative for current status. The observer now also samples raw or compressed write overlays without decoding guest contents: changed-block count, logical bytes, database allocation, WAL size and reusable pages. Samples are timestamped in `/private/tmp/virtualos-archive-conversion/observer.log`; SQLite reads time out and cannot write guest blocks. Multi-hour verification remains pending until enough live samples exist. Queue reports now include a bounded overlay-growth summary: observation span, missing-sample gaps, latest sample age and recent growth rate. Forecasts require at least five minutes of fresh contiguous samples; stale data, codec switches and observation outages cannot produce a capacity forecast. A roughly 19-minute interruption was detected and is retained in the evidence rather than treated as continuous monitoring.

1. **Compress bounded VM write overlays with verified migration** (`compressed_mutable_overlay`). Done when: Preserve all disk bytes and existing limits; corruption/restart/FUA tests pass; offline forward/reverse conversions verified; independently reviewed guest boot if activated; production/Linux untouched.

2. **Measure current physical and logical storage** (`storage_baseline_refresh`). Done when: Record allocated versus logical bytes, filesystem free space, open files and time; distinguish shared APFS extents from reclaimable bytes.

3. **Review changed-source storage evidence** (`storage_evidence_refresh`). Done when: Revalidate affected acceptance checks against current source hashes; retain historical deployment hashes and mark unresolved evidence stale.

4. **Measure write-overlay growth over several hours** (`overlay_growth_profile`). Done when: Bounded read-only samples report rows, allocated bytes, WAL bytes and growth rate; no VM restarts or model inference.

5. **Round-trip a consistent real overlay snapshot** (`compressed_overlay_snapshot`). Done when: SQLite backup of the running overlay converts forward and back with identical logical fingerprints; respect 10 GiB reserve and preserve live source.

6. **Measure compression CPU and disk tradeoffs** (`overlay_codec_cost`). Done when: Use closed snapshots or small fixtures; report compression ratio, write/flush latency and bounded memory without guest load generation.

7. **Exercise malformed compressed writes** (`overlay_corruption_matrix`). Done when: Reject truncation, changed checksum, invalid lengths, trailing payload, unknown codec and decompression expansion; failed writes preserve prior bytes.

8. **Exercise interrupted write and conversion recovery** (`overlay_crash_matrix`). Done when: Fixture-only process interruption proves acknowledged FUA writes persist and incomplete conversions never replace the source.

9. **Verify compressed writes retain logical limits** (`overlay_capacity_boundary`). Done when: Compressible input cannot bypass the 2 GiB logical cap; multi-block failures are atomic and base-equivalent writes release rows.

10. **Prove compressed-to-raw rollback offline** (`overlay_rollback_drill`). Done when: Closed snapshot restores using the prior backend with matching disk bytes; document exact helper, config and service restoration order.

11. **Audit managed disk service identity** (`overlay_service_identity`). Done when: Config, seed pin, base identity, codec and running service agree; wrong-format reuse is rejected before a guest attaches.

12. **Observe the migrated guest over several hours** (`overlay_boot_soak`). Done when: After independently verified activation, record fresh relay frames and bounded overlay growth; report gaps and preserve production/Linux PIDs.

13. **Audit WAL and checkpoint storage** (`overlay_wal_audit`). Done when: Measure WAL growth and current checkpoint behavior; identify readers and explain safe checkpoint conditions without editing live SQLite files.

14. **Measure redundant changed-block retention** (`overlay_pruning_audit`). Done when: Closed snapshot audit counts blocks equal to immutable backing and models reclaimable pages without changing live data.

15. **Measure immutable seed density** (`immutable_delta_density`). Done when: Read-only audit reports compressed payload, index/page overhead and raw fallback counts; seed SHA remains unchanged.

16. **Measure shared-base physical allocation** (`base_extent_accounting`). Done when: Record original/base clones and link identities; do not count apparent clone sizes as independent reclaimable storage.

17. **Verify preserved original VM disk** (`original_disk_preservation`). Done when: Original disk exists, remains closed and matches recorded identity; no deletion or replacement of the rollback original.

18. **Map model manifests to blobs** (`model_reference_refresh`). Done when: Each installed manifest and blob is accounted for; shared references and resident models prevent false orphan classification.

19. **Verify the offloaded model backup** (`model_backup_restore_drill`). Done when: Manifest references and every backup blob hash verify from durable operator storage; test metadata restoration in an isolated temporary directory.

20. **Recheck compressed private OCR runtime** (`private_runtime_recheck`). Done when: Pinned runtime byte inventory, signatures and sandboxed help check pass; record actual allocation without running inference.

21. **Find bounded regenerable cache candidates** (`cache_candidate_inventory`). Done when: Read-only inventory identifies owner, regeneration path, allocated size, last use and open-file checks; exclude guest disks and model data.

22. **Find unused build and maintenance artifacts** (`artifact_reference_inventory`). Done when: Trace launch agents, symlinks, imports and rollback references before classifying artifacts; report only evidence-backed candidates.

23. **Measure logs and retained evidence growth** (`log_growth_inventory`). Done when: Report largest growing logs and retention policies; preserve acceptance receipts, private evidence and current service logs.

24. **Prepare exact reviewed reclamation actions** (`storage_reclaim_plan`). Done when: List exact paths, expected physical savings, ownership, references, backup and rollback evidence; no wildcard deletion or automatic approval.

25. **Audit cloud archive references** (`r2_archive_reference_audit`). Done when: Read-only check maps receipts and manifests to archive objects and pending multipart uploads; no cloud deletion.

26. **Prepare bounded cloud recovery verification** (`r2_recovery_cost_plan`). Done when: Estimate download, temporary disk, cache and guest resource needs; retain existing recovery approval/resource gates.

27. **Track disk, memory and swap pressure** (`host_resource_trend`). Done when: Bounded hourly observations identify competing load without stopping other apps or relaxing the existing admission thresholds.

28. **Review storage reserve and growth alerts** (`storage_alert_thresholds`). Done when: Derive warning thresholds from measured growth and existing reserves; report predicted exhaustion without lowering hard safety limits.

29. **Verify hours-long shell supervision** (`shell_supervisor_soak`). Done when: Observe multiple timed ticks, deadline and lock ownership; no duplicate supervisor, unattended worker heartbeat or false completion.

30. **Exercise supervisor outage recovery** (`shell_reconnect_drill`). Done when: Fixture tests retry classified connection failures, preserve completed jobs and refuse integrity-failure retries or gated guest starts.

31. **Document measured storage and recovery procedures** (`storage_runbook_refresh`). Done when: Update existing repository documentation with verified formats, entrypoints, reserves, migration/rollback order and unresolved blockers.

32. **Compare final storage with the baseline** (`storage_final_comparison`). Done when: Report measured physical savings, logical sizes, retained backups and open blockers; staged candidates and apparent clone sizes are not savings.

### Compressed-write rollback order

Rollback must convert the **current closed compressed overlay**, including writes
made since activation. Restoring an older raw overlay would discard those writes.
The original full VM disk remains preserved separately.

1. Use the authorized test-VM maintenance window. Pass the current action guard,
   save watchdog state, shut down the guest cleanly, release its VM object, and
   unload the managed NBD service. Confirm its process and overlay lock are closed.
2. Preserve the current config, state directory, helper target and overlay. Keep
   the shared base and immutable seed identities unchanged. Pending WAL or journal
   data must be resolved through a clean backend close, never deleted manually.
3. Run `compressed_write_overlay.convert_copy` with `to_compressed=False` into a
   new private file, retaining the compressed source. Use the default 10 GiB
   reserve. Verify logical block fingerprints and counts; the saved pre-migration
   raw overlay is historical evidence, not a substitute for this conversion.
4. Publish only the verified raw result in a fresh managed state directory. Remove
   `writeFormat` from the restored raw configuration, restore the previously proven
   helper target, and prepare/start its NBD service. Keep old state and config
   together; the helper correctly refuses a mismatched existing `disk.json`.
5. Pass fresh VM admission: at least 14 GiB disk free, 50% memory free, swap below
   8 GiB and load below CPU count. Start only the test guest, review its fresh
   desktop and input, restore the watchdog, and check the relay. Preserve the
   production and Linux processes throughout.
6. If any check fails, retain both representations and recover with the matching
   codec/config/helper. Release generated intermediates only after verification.
   Neither rollback nor preview cleanup authorizes deleting the preserved original.

The local rollback drill now writes a new block *after* conversion to compressed
storage and proves that reverse conversion retains it. Live activation remains
subject to the existing resource and maintenance guards.


### Verified storage state and operating limits — 2026-10-21

Continue through the same two no-argument `.sh` entrypoints above. The worker
reconciles the durable queue; the observer reports fresh measurements. Neither
launches a coding model or renews an unattended agent lease. The supervisor's
current deadline remains 2026-10-22 19:30 UTC (3:30 p.m. Toronto).

| Item | Measured bytes | Interpretation |
| --- | ---: | --- |
| Immutable VM seed | 1,192,714,240 | Active read-only seed; 34,475 changed blocks represent 2,259,353,600 raw bytes. |
| Live raw write database | 987,734,016 | Latest measured live format remains raw; logical write limit is 2 GiB. |
| Compressed preview | 417,488,896 | Round-trip verified, then removed; never installed as the live disk. |
| Temporary preview allocation released | 419,495,936 | Mini allocation reclaimed after source/identity/open-file checks. |
| Operator history allocation released | 33,566,720 | Full log retained in a SHA-256-verified gzip archive, plus a recent hot tail. |
| Private OCR runtime allocation | 211,062,784 | Latest byte-inventory verifier, 32 signatures and sandboxed help passed. |
| Offloaded model weight backup | 6,140,392,672 | Operator backup retained; all five backup files freshly hash-verified. |

The compressed-write candidate passed the prior 55 Mini checks and disk-plane
self-test. Six additional local integrity, real conversion-SIGKILL and rollback
tests passed. The small fixture cost comparison stored the same 3 MiB of writes
in 1,097,728 bytes compressed versus 3,186,688 raw; this is not a guest performance
benchmark. The source and immutable seed remain unchanged during conversions.

Current physical-volume alerts are separate from the logical overlay budget.
A fresh sample compares free disk with the existing 10 GiB reserve and 14 GiB
VM-start disk reference. Stale samples report unknown readiness. These references
never replace execution-time memory, swap, CPU, capacity and approval checks.
Do not lower guards to continue a migration. At the latest pressure audit, swap
was above 14 GiB and disk was below the restart requirement.

The observer log archive retains every original byte and was independently
expanded and hash-checked before retaining the latest 4 MiB as the hot log.
Monitoring gaps and context restarts remain disclosed; an elapsed 24-hour soak
is not claimed. Forecasts use only fresh contiguous measurements, never outage
intervals. The complete acceptance receipts and private evidence remain retained.

The live disk service still uses the proven previous backend package, its exact
seed pin and a 2 GiB write limit. The compressed-write codec is opt-in and has not
been activated. Production and Linux stay running; the preserved original full
disk remains closed and unchanged. Closed-snapshot pruning also waits for enough
space to preserve its 10 GiB reserve. Conversion interruption can leave unpublished
temporary files; retain the source and review exact owned paths before cleanup.

The remaining large global model blob is shared by two installed aliases.
Unknown cache last use and opaque maintenance/rollback references prevented
further deletion. The executable called “Google Chrome for Testing” belongs to
the live Moats Browser application. Its pages, profile and automation must not
be interrupted without the user's explicit answer to the pending close request.
No Moats quit action has been executed.

The existing shell supervisor now dispatches one additional fixed job:
`storage_pruning_job.py` checks the closed-snapshot audit every five minutes.
It waits for at least 50% free memory, swap below 8 GiB, load below CPU count,
and enough disk for its snapshot while preserving the 10 GiB reserve. It checks
pressure during backup and comparison and removes its own temporary snapshot.
Verified success is bound to the exact source hashes; integrity failures stop
for review. This job cannot stop guests, close apps, change the live overlay or
activate compressed writes. It neither claims nor renews an agent work lease.
The supervisor deadline remains unchanged. The audit has not yet run on the
real overlay; current resource pressure still blocks it.

A fresh read-only R2 audit verified the manifest and one chunk, matched the archive
size and found no pending multipart uploads in either relevant bucket. The
22,266,108,635-byte archive and 22,267,132,894 bytes of referenced compressed chunk
objects remain retained. A full raw readback is already verified; independent
cold guest boot is still gated and has not been substituted by a sample read.
See the recovery-cost plan receipt for cache bounds, transfer estimates and the
separate recovery approval/resource requirements.
