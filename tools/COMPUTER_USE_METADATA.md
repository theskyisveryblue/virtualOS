# Computer-use app metadata contract

## Decision

Keep **one small app profile per app version** and **one task episode per attempted goal**. An app profile describes what can be observed and operated. An episode is the unit that can later be reviewed for training or held out for evaluation. Do not use a stream of undirected clicks as a training unit.

This is a proposed contract, not a claim that the current captures already contain these fields. The current virtualOS dataset contains manually reviewed successful clicks for a few Dock and Finder tasks; it has no curated negative examples or held-out evaluation set.

## Repository boundaries

| Repository | Owns | Existing evidence |
| --- | --- | --- |
| `xcanvas` | App and surface inventory, UI and document evidence, visual review and ownership decisions | `docs/reverse-engineering-workbench.md`, `src/editor/visualPairManifest.ts`, `src/editor/visualReviewMailbox.ts` |
| `agent` | Optional local recorder of human actions and active app identity | `utils/types.py`, `agent-server.py` |
| `virtualOS` | Guest-only execution, outcome checks, private trace review, training export and held-out evaluation | `tools/computer_use.py`, `tools/computer-dataset.sh`, `tools/computer-eval.sh` |

Exchange versioned, reviewed manifests. No repository should read another's private capture directory as a runtime database. OpenEditor launch configuration identifies a project, but is not the authority for app behavior or training labels.

`xcanvas/src/editor/visualPairManifest.ts` currently defines evaluation, validation, and holdout splits plus explicit `owned`, `licensed`, `evaluation-only`, or `unknown` rights. Its visual review summary promotes a pair to **evaluation**, not to computer-use training. Keep that boundary when joining evidence: a pair ID and source commit can be provenance references, but its prompt, screenshot, DOM text, and ownership authority must not be copied into a computer-use app profile. `evaluation-only` and `unknown` rights must never be interpreted as training approval.

The `agent` repository's `WindowInfo` and action records include bundle ID alongside window and action context. A future consented adapter may use the bundle ID to select a reviewed app profile, but must discard window title, typed text, coordinates, and screen recording data before producing a symbolic episode candidate. Its host recorder is not a substitute for guest-only evidence from virtualOS.

## App profile (`computer-use.app-profile/v1`)

Required: `app_id` (bundle ID, or a stable web origin ID), `platform`, `app_version`, `profile_id`, `evidence_level`, and `surfaces`. A surface has a stable `surface_id`, a short purpose, a bounded set of task families, and the observation and action channels actually available there (for example screenshot, accessibility, CDP, or guest input). Record the app version because a later UI can invalidate coordinates, control names, and success checks.

Optional: visible control roles, safe locator hints, supported document types, and source evidence references. Each fact carries `observed`, `declared`, `inferred`, or `possible`; only observed or declared facts may authorize automatic action. Keep profile references to source captures by content hash and local evidence ID, not absolute paths or copied app contents. Inventory of packaged code is evidence of inclusion, not proof of a runtime capability.

Do not put user style, screenshots, window titles, typed text, full URLs, account names, process IDs, or secrets in a shared app profile. A browser origin ID must omit path and query. App profile IDs must not be used as a way to track a person's activity.

## Task episode (`computer-use.episode/v1`)

Minimum fields:

```json
{
  "schema": "computer-use.episode/v1",
  "episode_id": "random-local-id",
  "app_profile_id": "com.apple.finder@version",
  "surface_id": "finder.sidebar",
  "task_family": "navigate_location",
  "intent": "Open Desktop in Finder",
  "initial_state": "finder_visible",
  "final_state": "desktop_visible",
  "action": {"type": "click", "target_role": "sidebar_item", "target_label": "Desktop"},
  "result": "verified_success",
  "verifier": {"kind": "visible_state", "evidence_level": "observed"},
  "evidence": {"before_sha256": "...", "after_sha256": "..."},
  "review": "pending",
  "split": "unassigned",
  "training_rights": "unknown"
}
```

The example is illustrative. The actual export must validate bounded values and hashes. Keep attempted action and executed action separate when an OCR or accessibility guard corrects a model proposal. Represent `abstain`, `already_complete`, `verified_miss`, and `unverified` explicitly. A changed screenshot is not a success label. Record the exact verifier and its result. A multi-step task needs a shared episode ID, ordered steps, and a final task-level check.

## Style and privacy

User style belongs to a private **task policy profile**, not to app metadata. The existing `style-profile.json` contains abstract preferences for intent, organization, outcome evidence, minimal capture, and follow-through; it is not model weights or a transcript. Only add a style signal after repeated, relevant observations or an explicit instruction. Record its scope and source class; allow correction and deletion. Do not infer sensitive traits from app usage. The style profile stays local and is excluded from the current R2 package.

Raw screenshots and action traces stay local until reviewed. Exclude password fields, secret-bearing windows, raw typed content, full URLs, clipboard content, and unrelated apps. A reviewer must decide whether each image and label can leave the machine. Compress only accepted, deduplicated evidence, verify every member hash, and upload only through an allowlisted manifest. Third-party reference captures marked `evaluation-only` must never become training examples.

## Promotion path

1. **Inventory:** xcanvas emits a reviewed app profile from its existing capture workbench. Native apps use accessibility or guest capture when CDP is unavailable. An inventory does not by itself authorize actions or training.
2. **Episode:** virtualOS (or a consented agent recorder) links a goal and app profile to before/action/after evidence. It assigns an outcome only after an independent verifier checks the requested state.
3. **Review:** validate hashes, version, task intent, privacy, ownership, and verifier result. Reject unclear or repetitive captures. Preserve misses and abstentions as candidate negative examples, subject to review.
4. **Split:** group by app version, task family, and related starting state before assigning train/validation/holdout, so nearly identical captures cannot cross splits.
5. **Export:** publish a compact manifest and approved evidence to private storage. Keep raw material and style policy local. R2 is storage, not training. A separate training job consumes only an explicitly approved training split.

## First implementation slice

Start with Finder and one xcanvas-owned editor surface. Add `app_profile_id`, `surface_id`, `task_family`, `episode_id`, `verifier`, `review`, and `training_rights` to a **new versioned export**, while leaving existing trace and R2 schemas intact. Demonstrate one success, one verified miss, one abstention, and one held-out task family. Require the export validator to refuse missing ownership or verification and to prove that private style data is absent. Only then consider a training job. This gives a real test of whether app metadata improves intent and outcome handling before generalizing to every captured app.

The first local candidate pass is available through `./tools/computer-dataset.sh --episode-candidates` and `--episode-audit`. It maps only exact known Finder/Dock tasks to symbolic app, surface, goal, outcome, review, and evidence-hash fields. The audit cross-checks each candidate against its original guest trace and refuses changed outcome or evidence hashes. It omits task text, coordinates, image paths, screenshot bytes, and private style. On 2026-10-04 the Mini audit passed for 279 candidates: 201 verified successes, 14 verified misses, 54 abstentions, 9 already complete, and 1 unverified. Every candidate has `app_profile_id: null`, `training_rights: unknown`, and `split: unassigned`; this is a review queue, not a training-ready export or a cloud upload. Binding reviewed app versions, independent task-level verifiers, ownership, and held-out splits remains required.

`./tools/computer-dataset.sh --episode-leakage` reports counts only. On the Mini, the 279 candidates span four goal IDs but form only two connected groups when episodes sharing an app/surface/goal or an evidence hash are kept together; the larger group has 214 episodes. There are 12 reviewed examples across Dock and Finder journeys, with six Dock examples also present in the candidate journal. No split was assigned. The current related-task volume is too narrow for a meaningful train/validation/holdout result; adding distinct task families and starting states is the next data requirement.

## Intent plan shorthand (`virtualos.intent-plan/v1`)

A task starts with a small symbolic plan. It describes the requested state and what may be done before any observation or model call. The default observation is an accessibility text tree (`ax`). OCR is a fallback for a guest surface without usable accessibility data. Pixels are a last resort, explicitly requested by `fallback screenshot-on-ambiguity`; they are never the default training record.

## Private cloud playbook (`virtualos.playbook/v1`)

A playbook is a reviewed, versioned recipe of symbolic affordances and expected states. It is useful for general computer use and, only when rights and consent permit, learning. It contains no captured page content or executable coordinates. `xcanvas` can contribute reviewed app version and ownership facts; `agent` can contribute only projected symbolic actions after consent; virtualOS links outcome checks. None of their raw capture formats is directly importable.

```json
{
  "schema": "virtualos.playbook/v1",
  "playbook_id": "finder.desktop",
  "revision": 1,
  "scope": {"kind": "app", "id": "com.apple.finder", "version": "15.0", "profile_id": "finder.profile.v1"},
  "goal": "navigate.desktop",
  "steps": [{"action": {"verb": "click", "target": "sidebar.desktop"}, "expect": "finder.desktop", "verify": {"kind": "ax", "state": "finder.desktop"}}],
  "rights": {"ownership": "unknown", "training": "unknown", "consent_id": null},
  "privacy": {"mode": "metadata-only", "cloud": "private-r2"},
  "split": {"role": "unassigned", "group_id": "finder.desktop", "policy": "goal-and-state-v1", "review_id": null},
  "source": {"kind": "declared-fixture", "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "evidence": "declared"}
}
```

For a website, set `scope.kind` to `site`, `scope.id` to its HTTPS origin only, and `scope.version` to a reviewed release or interface fingerprint. The existing `./tools/computer-dataset.sh --playbook-validate FILE` checks a 16 KiB strict allowlist, and `--playbook-export FILE` prints canonical JSON plus a private R2 object key under `playbooks/v1/`; the key hashes the scope, playbook ID, and full canonical content to avoid collisions. Both commands are local and read-only. They do not upload, assign rights, execute steps, or inspect private captures. Every assigned model split requires owned or licensed rights, a consent ID, and observed evidence. Source SHA-256 and group ID are provenance claims to verify during review, not proof by themselves. Before assigning splits, compare every playbook's app version, goal, starting state, and shared evidence across the full manifest so related playbooks cannot cross train, validation, or holdout. `--playbook-self-test` checks duplicate-key, type, rights, and key-collision rejection.

Keep the editable source and canonical JSON human-readable. On the small example above, canonical JSON is 707 bytes and zlib level 6 is 344 bytes; base64-wrapped zlib is 460 bytes. Compress reviewed manifests as a batch at a later approved upload, rather than writing one cloud object per action. A shared symbol dictionary adds versioning and decoding costs and is premature until a multi-playbook corpus shows it saves meaningful bytes beyond compression.

For a live guest observation, `./tools/computer-space.sh --ax-codec-measure` checks a separate `VA/1` symbolic payload: five bounded fixtures occupy 97 bytes individually or 55 bytes as one batch, against 792 bytes of compact JSON. A batch shares one registry tag and accepts 1–64 canonical records; decoding rejects unknown versions, kinds, truncated records, and trailing bytes. This wire format carries no transport authentication, private text, pixels, or evidence of task success beyond the verifier status supplied to it.

`./tools/computer-decision.sh --suggest-playbook PLAYBOOK.json STATE.json` reads
one validated playbook and a `{"facts":["symbol"]}` state. It returns the local
count policy and shadow linear-model view over the playbook's allowed symbolic
steps plus abstention; it never executes a step. `--observe-playbook
PLAYBOOK.json EVENT.json` records a local-only verified outcome for one step
when the supplied verifier kind and state match that step. The calling app/site
adapter must actually ground the control and check the outcome independently.
`--export-local` writes only symbolic feedback and normalized Dock locators to
the Mini's private offline-policy directory; it does not grant cloud or
external model-training rights.

```text
app com.apple.finder
goal navigate.desktop
expect finder.desktop
observe ax ocr
allow click sidebar.desktop
verify ax finder.desktop
privacy local metadata-only
```

The syntax is one directive per line, with space-separated identifiers and no quoted free text. `app` is a bundle ID or stable web origin ID, `goal` and `expect` are reviewed symbolic IDs, `allow` pairs a verb with a reviewed control ID, and `verify` names an independent state check. A real app profile must bind those IDs to current app-version locators and check logic; the plan alone cannot drive input. The verifier state must equal `expect`. Actions absent from `allow` are forbidden. `privacy local metadata-only` is mandatory. Values such as URLs, document text, passwords, window titles, account names, and clipboard contents are invalid plan data. The compiler outputs policy metadata, not observations or labels.

Run it through an existing shell entry point, without introducing another `.sh` file:

```sh
VOS_INTENT_PLAN=/path/to/task.intent ./tools/computer-space.sh --intent-plan-compile
```

For the existing shell pipeline, `computer-verify.sh` or `vosctl` should call the Python validator before model control, resolve the IDs against a reviewed app profile, obtain the smallest useful text tree, and evaluate the declared state afterward. If the tree is unavailable or ambiguous, the controller must abstain or use a separately authorized fallback. It must never treat a tree change, image change, or a model's `done` as success. Emit only the symbolic plan ID, app profile version, attempted/executed action IDs, verifier result, and review status to the episode. A screenshot can be retained only under the existing local review policy, not because this plan enables a fallback.

## Current boundary audit

- `intent_plan.py` validates syntax and privacy constraints only. Its `execution` field remains disabled until a reviewed, version-matched app profile binds every symbol to an observation, control, and verifier. Do not translate a valid plan directly into coordinates.
- `computer-dataset.sh --episode-candidates` emits `app_profile_id: null`, `training_rights: unknown`, and `split: unassigned`; `--episode-audit` now cross-checks both source traces and the manual review journal. These are local metadata candidates, even when an outcome is verified. The existing reviewed click archive has no held-out set and must not be called training-ready.
- `xcanvas` workbench snapshots include URL, title, visible text, and screenshots. Its app inventory cannot cross this boundary wholesale; export a reviewed profile with stable IDs, version, ownership decision, and only allowed locator facts. The workbench already warns that its raw captures need review and redaction.
- The separate `agent` recorder's `TextAction.content` and recording `DSL.text` can contain typed private material; its active window process ID also tracks a local session. None of these fields may enter an app profile, symbolic plan, episode candidate, or R2 package. A recorder export needs an explicit projection and review step before any virtualOS import.

The Mini's October 2026 metadata-only audit measured 279 episode candidates and 12 reviewed examples (six Dock, six Finder journeys). The candidates contain four goal groups but only **two independent connected groups** when episodes with the same app/surface/goal or any shared before/after evidence hash stay together; the larger group has 214 episodes. Exact duplicate starting-frame hashes were zero. All 279 candidates lack an app profile version and training rights, and no split is assigned. A random episode split would mix repeated goals across evaluation and training; the present two groups cannot supply separate train, validation, and held-out sets.

For a first defensible held-out evaluation, collect at least **three consented task families that remain separate under the same grouping rule**: one for training, one for validation, and one held out before any model changes. Use varied disposable starting states within each family, and keep related attempts in one group. A practical set is: (1) navigate Finder to a named disposable test folder, verified by the guest's canonical folder URL or filesystem identity; (2) open a chosen test app, verified by its guest frontmost bundle ID; (3) make a reversible change in an owned editor fixture, verified by the saved document model or application API. Each verifier must observe task state independently of the action proposal and model's completion claim. Record symbolic goal/action/verifier IDs and aggregate outcomes, not raw text trees, private document content, screenshots, or personal style. Review ownership and consent for each family before assigning training rights; keep the held-out family's rights and examples separate from training export.
