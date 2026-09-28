---
name: graphify-fork-maintenance
description: Repository-local workflow for deterministic preview and apply of frozen Graphify fork-maintenance plans.
---

# Graphify fork maintenance

Use this repository-local skill only from a checkout containing `mise.toml` and `tools/`. The `tools` module is repository code, not an installed Graphify SDK. The source repository may be a different checkout.

## Preview

Use default release selection with:

```sh
mise run fork-maintenance -- preview \
  --source-repo /ABSOLUTE/PATH/TO/source-repo \
  --candidate REPLACE_WITH_EXACT_40_HEX_CANDIDATE_SHA \
  --upstream-repository Graphify-Labs/graphify \
  --upstream-url https://github.com/Graphify-Labs/graphify.git \
  --output-plan /ABSOLUTE/PATH/TO/evidence/plan.json
```

`--candidate` must be an explicit exact lowercase 40-hex commit, never `HEAD`, a branch, or a fabricated SHA. Default selection chooses the latest qualifying published stable, non-draft GitHub release by publication chronology whose `graphifyy` PyPI release has usable non-yanked distribution files; never guess a branch or version. Every release entry must supply boolean draft and prerelease flags. Missing or malformed eligibility refuses selection; it does not authorize choosing an older release.

Use an override only when the user explicitly requests one:

```sh
mise run fork-maintenance -- preview \
  --source-repo /ABSOLUTE/PATH/TO/source-repo \
  --candidate REPLACE_WITH_EXACT_40_HEX_CANDIDATE_SHA \
  --upstream-repository Graphify-Labs/graphify \
  --upstream-url https://github.com/Graphify-Labs/graphify.git \
  --output-plan /ABSOLUTE/PATH/TO/evidence/plan.json \
  --override-sha REPLACE_WITH_EXACT_40_HEX_UPSTREAM_OVERRIDE_SHA \
  --override-reason 'REPLACE_WITH_USER_REQUESTED_NONEMPTY_REASON'
```

`--override-sha` is the explicitly requested upstream target and requires a nonempty recorded reason. Never silently infer or fall back to an override.

## Frozen-plan handoff

Preview writes its plan outside the source repository and reports `plan_sha256`, the SHA-256 of the exact final JSON file bytes. It also freezes the exact candidate and target, source and upstream identities, `plan_id`, and internal integrity binding. Inspect the terminal JSON and frozen identities before invoking apply separately.

Source origins and service endpoints are admitted as structured URLs before publication. HTTP(S) service endpoints cannot contain userinfo, credential-bearing query parameters, malformed components, or fragments; PyPI base URLs also cannot contain query parameters. Git upstreams use HTTPS, SSH (with optional `git` user), `file://`, or an existing absolute local path, not plain HTTP. Recorded fixtures remain labeled as fixtures, and GitHub/PyPI provenance is reported independently. Each consulted PyPI version also records its own observed response final URL, redirect flag, and conservative provenance (a 404 reached through a redirect keeps the observed error location); the requested PyPI base alone never establishes official response provenance.

Each redirect and pagination destination is admitted before it is requested. It must retain the origin of the explicitly supplied metadata endpoint and cannot contain userinfo, sensitive query parameters, port zero or unsupported components. Recognized credential names are normalized consistently, including `apiKey`, `accessToken`, `clientSecret`, `api.key`, `api:key`, and `api/key`, for initial URLs, redirects and pagination. Raw, encoded, or repeatedly encoded semicolon and ampersand delimiters inside query components are refused because another parser could reinterpret a benign value as a credential parameter. A key with a residual percent sign after one URL decode is refused for the same reason. Rejected destinations are not echoed by the engine or retained as failure evidence. The public `fork-maintenance` mise task uses `quiet = true` to suppress mise's own command echo while retaining the engine's stdout and stderr; do not override it or enable shell tracing when supplying endpoint URLs. Valid same-origin redirects and explicitly selected controlled HTTP endpoints remain supported. These controls are separate from ingestion's `safe_fetch` policy.

Git commands run from an owned empty home with a repository-discovery ceiling. Explicit source/output repositories undergo key-only executable-configuration admission, including `core.alternateRefsCommand`. Partial-clone/promisor configurations are unsupported and must be refused before object traversal: nominally read-only Git operations can otherwise fetch missing objects. Local and enabled worktree configuration are admitted before source or output traversal; per-invocation lazy-fetch suppression is an additional defense, not a substitute for admission. Submodule layouts are unsupported: indexed gitlinks, a `.gitmodules` entry or a common modules directory cause refusal before status/traversal. Candidate, fork-base and target trees likewise refuse gitlinks and root `.gitmodules` before worktree acquisition. Do not initialize submodules, bypass refusal or silently ignore their state.

Retain the reported `plan_sha256` as the caller's approval pin and preserve the plan byte-for-byte. Do not hand-edit, rewrite, relocate inputs, substitute moving refs, or recompute a digest to bless a changed file. Internal plan hashes are consistency checks, not authenticated approval. The apply source repository and upstream URL must match the frozen plan.

## Apply

After inspection, replay the same plan and explicit inputs with:

```sh
mise run fork-maintenance -- apply \
  --plan /ABSOLUTE/PATH/TO/evidence/plan.json \
  --expected-plan-sha256 REPLACE_WITH_PREVIEW_REPORTED_PLAN_SHA256 \
  --committer-name 'REPLACE WITH INTENDED GIT COMMITTER NAME' \
  --committer-email 'REPLACE_WITH_INTENDED_GIT_COMMITTER_EMAIL' \
  --source-repo /ABSOLUTE/PATH/TO/source-repo \
  --upstream-url https://github.com/Graphify-Labs/graphify.git \
  --output-worktree /ABSOLUTE/PATH/TO/new-output-worktree \
  --branch codex/REPLACE_WITH_UNIQUE_ATTEMPT_NAME \
  --evidence-dir /ABSOLUTE/PATH/TO/evidence/apply-attempt
```

Apply requires the output worktree and evidence to be mutually separate and outside the resolved source worktree, common Git directory, and per-worktree Git directory, including symlink aliases. The caller supplies the intended ordinary Git committer identity explicitly; apply does not infer it from authorship or user/global configuration. A missing/invalid identity, changed exact-byte plan pin, source identity, frozen target, unrelated destination, branch, or lock is refused before attempt mutation.

A successful repeat with identical inputs may report `replayed` without creating another delivered change. Every replay validates the frozen source worktree and Git-directory identities as well as recorded state. A separate byte-identical repository does not match the approved source; resolved aliases may match only when they identify the same frozen repository. A retained `attempt.lock` entry, including a dangling entry, prevents successful replay even when `result.json` exists. Report the exact ownership/result paths and preserve both; result visibility alone does not prove finalized publication.

## Outcomes and stop rules

Capture the direct process exit code, stdout, and stderr separately.

- `planned`, `applied`, and `replayed` are stdout JSON with exit code 0. `conflict` is stdout JSON with exit code 3 and preserves conflict state and evidence.

- `refused` is stderr JSON with exit code 2 for invalid, tampered, stale, or mismatched state.

- `timeout` is stderr JSON with exit code 4. Retain partial state and bounded recovery information; timeout is not success or permission for a destructive retry.

- Argparse usage errors also use exit code 2 and may not be JSON.

Evidence records preserve original raw stdout and stderr bytes in separate durable files, plus byte lengths, SHA-256 bindings, EOF/completeness and settlement state, bounded summaries, command origins, and direct return codes. An admitted preview HTTP failure retains its captured streams and `command-records.json`; the nonzero response binds that metadata by path and SHA-256. HTTP worker-envelope bytes are distinct from response-byte hashes. EOF is recorded only when both pipes were actually drained, never inferred from a return code; every captured byte is kept once per command, including HTTP worker timeout or interruption. Incomplete bounded draining or settlement remains an explicit nonzero failure and is not complete evidence; owned process-group settlement is recorded as independently observed, so an escaped descendant holding a pipe leaves EOF false without claiming the group is alive. Historical argv may name scratch paths exactly; results label that scratch `ephemeral_scratch` (not durable; successful cleanup removes it), while `raw_path` links are durable. These are integrity-bound local evidence, not signed/authenticated claims. Follow referenced evidence; never infer success from an output tail or wrapper result.

Normal leader completion and pipe EOF do not establish owned process-group absence. A surviving or unobservable group produces non-success and consumes the same bounded shutdown allowance. Record group observation separately from EOF and direct return code. A denied termination request never proves absence; only a later independent absence observation within the same bound can establish settlement.

Once apply owns an attempt, successful finalization writes one exclusive integrity-bound result with observed content/index/ref/rebase state and recovery guidance. An unchanged repeat is read-only and returns the recorded outcome; meaningful drift is refused. State observation checks its deadline, never follows symlinks, and refuses special files. If observation cannot complete, the result records `state: unknown` and a repeat is refused read-only with `state_uncertain`; incomplete observation establishes neither equality nor drift.

Mutation closure after rebase, primary expiry, subprocess timeout, or catchable interruption starts one shared, non-extending 10-second shutdown allowance for owned-group settlement, draining, observation, and evidence publication. No further primary mutation or retry is permitted. Rebase exit alone does not establish `applied` or `conflict`: their postconditions still require verification within the remaining allowance. Successful and conflict results report the allowance under `timing`; a timeout names the bound actually in force (`subprocess`, `whole-attempt`, or `shutdown-allowance`).

Persistence checks the remaining allowance before each raw stream, after the final stream, around serialization and integrity work, and at publication and ownership handoff. Prepare the terminal response and complete scratch cleanup under that same allowance before releasing ownership. Expiry during preparation refuses successful publication. If an operating-system operation returns after the allowance, retain visible evidence and any ownership still present, report nonzero uncertainty, and do not treat the late return as success. If ownership release itself completed before a late return, do not recreate the lock or claim it is retained; report the actual observation or unknown. These are cooperative deadline checks, not an atomic guarantee that every filesystem operation finishes before the deadline. An expired preview can leave an unreferenced raw evidence directory; a filesystem operation that returns late may also leave a visible plan without a successful approval-pin handoff. Publication failure can leave a linked result visible without establishing complete or durable evidence. Retain the original `terminal_status`, return nonzero `evidence_error`, and report observed result/lock visibility or `unknown` when it cannot be observed within the allowance. Do not republish a different outcome, remove ownership paths, or infer successful finalization from file existence.

Git and HTTP workers retain ownership from acquisition through one command record, classification and result handoff. A late catchable signal preserves an already captured ordinary failure, timeout or check-enforced nonzero failure. Successful operations and accepted `check=False` nonzero operations interrupted before handoff remain interrupted; HTTP metadata records interruption even when an earlier failure remains primary. Their phase guards prevent replay of primary work while an interruption is handled. An ordinary apply failure defers catchable signals across the transition into owned finalization; releasing that hold does not itself deliver a new exception. The first operational failure remains the cause when catchable interruption arrives during its handling. Repeated catchable signals coalesce; recording and publication must not run twice. Inherited ignored signals stay ignored. SIGKILL, fatal runtime failures, and uninterruptible OS operations remain outside these cooperative bounds. On conflict, timeout, interruption, partial state, evidence error, or unexpected status, stop and report the plan pin, evidence, output worktree, branch, paths, state, direct exit code, and raw streams for caller arbitration. Never auto-resolve, reset, abort, continue, rebase, delete a worktree, branch, evidence or lock, or retry destructively.

Preview owns safe source observations from the admitted-source handoff through terminal delivery. Configuration, source origin and protected output paths must be admitted before those raw bytes become eligible for persistence; credential-unsafe admission still publishes no raw evidence. A signal after safe admission preserves the completed command records without restarting source observation or metadata requests. An existing plan is never overwritten: failure responses report observed absence or conservative `unknown`, with `plan_preexisting` distinguishing an observed existing entry from an unobservable destination, and `plan_published_by_attempt` recording only a successfully completed publication by this attempt.

Preview preserves its existing error precedence: a finalization I/O failure following an earlier operational failure reports top-level `evidence_error` with that first cause; an ordinary filesystem failure with no earlier cause remains `refused` with `terminal_status: evidence_error`. Both are nonzero and retain available path/visibility context.

Failure stderr delivery is flushed and checked again when the write returns. If it returned after the same shutdown deadline, the already-emitted original JSON cannot be recalled: on a writable recipient, a final stderr JSON reports `evidence_error`, `terminal_delivery: late_return`, original `primary_status`/`primary_error`, and the retained evidence/visibility fields, with exit code 2. Treat that last record as terminal uncertainty. This is one uncertainty report, not a retry of the original response or a new allowance; permanently blocked OS writes are not forcibly bounded. All terminal output, including help and usage diagnostics, is flushed. If an output write or flush returns an ordinary I/O or encoding error, public `main` returns 2 without another diagnostic on the failed transport, extending the deadline, or repeating evidence/publication. Structured delivery is best effort: a broken recipient may receive no JSON, a partial record, or only the earlier record. Durable command evidence and already-published state remain available for caller inspection. The native CLI replaces a failed standard descriptor with an available null sink so interpreter shutdown does not retry the broken recipient; this setup is best effort. Imported `main` leaves caller-owned streams unchanged.

## Bounded composite entry points

`mise run fork-maintenance -- run --workflow-config /ABSOLUTE/config.json --evidence-index /ABSOLUTE/index.json` starts one local run. `resume --evidence-index /ABSOLUTE/index.json --run-id ID --expected-plan-sha256 DIGEST` continues that same run; `status --evidence-index /ABSOLUTE/index.json --run-id ID --json` reads its projection without running a stage. The run ID comes from the first response or index. The composite never regards a preview's newly reported digest as caller approval: supply the exact final plan-byte digest independently before apply. A plan that already exists can carry this pin in immutable config.

The version-1 config has `schema_version`, optional `preview`, required `apply`, optional `expected_plan_sha256`, and optional absolute `qualification_sidecar`. `preview` uses the existing preview flag names as underscore keys. `apply` uses `plan`, `committer_name`, `committer_email`, `source_repo`, `upstream_url`, `output_worktree`, `branch`, and `evidence_dir`; its paths are absolute. The index is the composite's sole mutable checkpoint and retains the immutable config hash, stage direct exits, and paths and hashes of separately captured raw stdout/stderr. A stage is marked in progress before launch. Incomplete or failed stages cannot be retried by resume. On the outer wrapper timeout, available raw bytes are retained, `direct_rc` is null, and the stage remains non-retryable. The existing preview and apply each keep their own deadline; this slice does not impose a single deadline over both stages or prove settlement of a process that escaped the stage owner. Resume asks the existing apply command to verify its read-only replay before it accepts a qualification sidecar. A retained workflow or apply lock is a STOP requiring inspection.

The composite stops nonzero at missing pin, unsuccessful preview/apply, or missing ticket-788 capability qualification. When release fixtures are configured, their exact bytes and their canonical JSON bindings are frozen before dispatch and checked on resume. A preexisting plan must match the configured candidate, source, upstream and selection mode. Distinct stable releases tied on publication time are ambiguous and refused; no API list order breaks the tie.

The workflow index binds a recorded execution environment (engine, Python and Git executable bytes, repository runtime manifests and import-path settings). Completed preview/apply stages also bind a stage-specific input digest and exact plan/result output digest. `status` projects Graphify's frozen/applied/qualification state separately from KB and dotfiles external pending receipts; applied Graphify is still aggregate `partial`, not delivered. Status is read-only and labels a matching apply checkpoint `requires_read_only_replay`. `resume` refuses changed config, environment, plan or result checkpoint identities before stage dispatch; on matching identities it invokes the existing apply read-only replay to verify source/output state and ancestry. Unrelated metadata outside those identities does not rerun preview or apply mutation. Indexes lacking the new environment or checkpoint identity stop instead of being silently reused.

The separate version-1 ticket-788 sidecar must bind exact `plan_sha256`, `target_commit`, `result_commit`, `result_tree`, and `result_receipt_sha256`. Its `capabilities` list has one entry for each frozen `(path, status)` identity with an allowed classification, nonempty rationale and evidence references. Top-level `evidence_refs` contain absolute regular-file `path` and exact `sha256` objects; each capability cites at least one of those digests. Missing, changed, duplicated or unknown evidence stops. Resume checks the public apply replay, including actual target ancestry, before recording the sidecar. The sidecar never edits the frozen plan's pending rows and only advances the projection to `await_fork_gates`; it does not establish independent review, publication or completion. The source may be dirty when its frozen `status_sha256` still matches; any later drift is refused by the existing apply contract.

`currency --evidence-index /ABSOLUTE/index.json --run-id ID --boundary publication|integration|completion --subject /ABSOLUTE/subject.json --expected-subject-sha256 DIGEST` records a diagnostic selection immediately before the named external boundary. Supply an exact-byte pinned JSON subject with schema version 1, the boundary, run ID, frozen plan SHA-256, current target commit, and a nonempty `identity` object. Publication identity names the applied result commit/tree and distinct gate/review receipt paths and SHA-256 digests; integration names the immutable published ref/commit, publication receipt and consumer plan paths/digests; completion names the consumer result commit and result receipt path/digest. The engine verifies these local bytes and the applied output binding, but does not verify hosted review, remote publication, or consumer semantics. The caller must establish those independently. Every observation is timestamped, retains the selector's GitHub/PyPI evidence bindings and raw Git streams, and has `promotion_authorized: false`.

Release mode reuses the same stable, non-yanked selector at each boundary. Explicit override stays fixed with its original reason. A changed release or target records an immutable invalidation observation, preserves the frozen plan and earlier outputs, reserves another target attempt up to three total, and stops for a fresh approved plan and qualification. A fourth required target is a terminal stop. Unknown metadata or transport records an unknown receipt and stops without retry. Repeated checks of the same observed target do not consume another attempt. The command does not publish, integrate, complete, retarget an approved plan, or automatically infer that gate/review evidence is sufficient.

## Boundaries

Keep every capability-manifest classification `pending` until ticket 788 separately verifies it. Do not infer classification or coverage from filenames.

Do not write a rebase recipe, perform automatic semantic resolution, push or publish, create releases, edit dependencies, install a global skill, update the knowledge base, or run paid extraction.

These maintenance invocations alone do not establish full goal acceptance or publication readiness.

The two repository-local skill files may require later narrowly scoped force-add by the caller; add only their exact paths and do not change ignore rules.
