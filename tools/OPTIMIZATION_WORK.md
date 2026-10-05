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
The ten-hour `.sh` worker attempts at most one such action per hour while
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
uncalibrated and it cannot operate Xcode or judge a design. A Hugging Face
CPU Job was attempted with a ten-minute cap. The active fine-grained token
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
