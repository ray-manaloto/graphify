# graphify drops functions from its own extraction: diagnosis + fix

## Summary

The 43.2% (622/1441) function-loss figure measured against the knowledge-base
repo's committed `graphify-out/graph.json` has **three separate causes**, only
one of which is a live bug in graphify's extraction code:

1. **Measurement artifact** (biggest single contributor to the raw number).
   Method/property nodes carry a label of the form `.methodname()` — a
   **leading dot**, no class-name qualification — not `methodname()`. A probe
   that strips only the trailing `()` and compares against a bare AST function
   name will treat every method/property as "missing" even though it is
   present. Correcting the label match (strip the leading `.` too) drops a
   naive single-file miss count from ~11% to ~2% on the SAME extraction output.

2. **Stale build, not a current-code bug** (config/staleness, per spec section 7).
   `graphify-out/graph.json` in the knowledge-base repo was built by an OLDER
   graphify version, before #1504's path-qualified node-ID fix landed. Proof:
   `graphify.build.graph_has_legacy_ids()` -- graphify's OWN read-only
   diagnostic, emitted verbatim by `mise run kb-query` -- returns `True` against
   that graph ("this graph uses the pre-#1504 node-ID scheme"). Pre-#1504 IDs
   are NOT reliably path-qualified, so in a giant aggregate corpus merging
   hundreds of repos (492,654 nodes across many source repos, including
   graphify's own source, which is itself an ingested corpus source), two
   files or symbols sharing a bare stem/name anywhere in the merged graph can
   collide and one silently loses via `add_node`'s `if nid in seen_ids: return`.
   This class explains the WHOLE-FILE-100%-missing symptom (`graphify_baseline.py`
   58/58, `graphify_sdk.py` 42/42, `source_groups.py` 35/35, etc.) -- none of
   this reproduces in a clean single-repo extraction with the CURRENT
   (0.9.49) fork. This is a staleness/config issue, not something I changed
   code for -- rebuilding via `mise run kb-build` with the currently pinned
   graphify should recover the great majority of this class. (I did not run
   `kb-build` -- forbidden by the spec's constraints; this is inferred from
   graphify's own diagnostic plus the clean single-repo re-extraction below.)

3. **A real, current graphify bug -- FIXED in this fork.** See below.

## Root cause (bug #3, fixed)

`graphify/ids.py`'s `make_id`/`normalize_id` -- shared by every extractor,
untouched by this fix -- casefold, collapse repeated underscores, and strip
leading/trailing underscores from the FINAL joined id string. This is
correct and tested behavior for a single opaque label (`"__dunder__"` ->
`"dunder"` is an explicit, locked contract-test case), but it makes the
multi-part composition `make_id(scope, name)` **non-injective**: two
DIFFERENT declared names in the same file/scope can normalize to the SAME id,
and whichever one `_extract_generic`'s `add_node()` sees SECOND is silently
dropped (`if nid in seen_ids: return` -- no warning, no trace).

Three confirmed collision shapes, all reproduced directly against this
repo's `python/src/kb_setup`:

- **private/public siblings**: `_source_path_evidence` / `source_path_evidence`
  (`graph.py`), `_verify_candidate` / `verify_candidate` (`graphify_baseline.py`),
  `_reviewed_tasks` / `reviewed_tasks` (`skillopt_reviewed.py`),
  `_is_commitment` / `is_commitment` (`handoff_reconcile.py`). Cause:
  `make_id(stem, "_foo")` and `make_id(stem, "foo")` both normalize to
  `stem_foo` (the join separator's `_` and the name's own leading `_` collapse
  into one).
- **case-only collision across kinds**: class `Dropped` vs. function `dropped`
  in `handoff_reconcile.py` -- `make_id` casefolds, so both normalize to the
  same id regardless of underscores.
- **cross-boundary collision (function vs. method)**: top-level function
  `_provider_plan` vs. `Provider.plan` (a `Protocol` method) in
  `artifact_download.py`. `_make_id(stem, "_provider_plan")` normalizes to
  `..._provider_plan`; `Provider.plan`'s method id is
  `_make_id(_make_id(stem, "Provider"), "plan")` = `..._provider_plan` too --
  same collapse mechanism, just crossing a class boundary instead of two
  top-level names.

This is the SAME failure class as graphify's own precedent fix
`1e68a7a` "fix(go): keep both halves of a case-only symbol collision
(#2779)" -- Go's `Run`/`run` case-fold collision, fixed by salting the
non-canonical half of a same-file collision rather than touching
`ids.py`/`normalize_id` (which every other producer depends on). This fix
follows the exact same design, generalized to the shared `_extract_generic`
engine (so it covers every language routed through it, not just Python).

## The fix

`graphify/extractors/engine.py`:

1. New pre-scan `_collect_same_scope_symbol_collisions(root, config, source, stem)`
   walks the parse tree once, resolving each `class_types`/`function_types`
   declaration's name (via new small helpers `_prescan_class_name` /
   `_prescan_function_name`, deliberately self-contained duplicates of the
   main walk's existing name-resolution logic so this addition cannot perturb
   already-exercised code) and groups them by the id they WOULD produce.
   Scope is tracked through `class_types` nesting only (not `namespace_stack`
   / Ruby module-segment joining / the C#/Swift-specific id paths elsewhere in
   this file) -- a deliberate, documented narrowing: a miss there degrades to
   "no salting applied", i.e. today's pre-existing behavior, never to a wrong
   or unstable id. Only groups with 2+ distinct names are kept.

2. `symbol_nid(candidate_id, name)` -- computed once per file, closed over the
   pre-scan's groups. For a non-colliding id it is a byte-for-byte pass-through
   (returns `candidate_id` unchanged), so the overwhelming majority of files
   see ZERO behavior change. For a colliding group: the member whose name has
   no leading/trailing underscore keeps the plain id IF it is the unique such
   member (matches cross-file references, which almost always target the
   public name); every other member -- or every member, if none is uniquely
   "clean" -- gets a short deterministic `sha1(name)[:6]` salt appended, mirroring
   Go's `symbol_nid` in `graphify/extractors/go.py` exactly.

3. Applied at the three places `_extract_generic`'s main walk computes a
   class/function/method id: `class_nid = symbol_nid(_make_id(...), class_name)`
   and both `func_nid = symbol_nid(_make_id(...), func_name)` sites (top-level
   and method). `ids.py`/`normalize_id` are untouched -- the id contract holds,
   and non-colliding files/languages are unaffected.

**Known, explicit gap NOT fixed by this change** (out of scope, appears to be
by design rather than a bug): a function/closure DEFINED INSIDE another
function is never emitted as its own graph node at all -- not even under a
colliding id, it is simply never visited as a `function_types` match, since
`_extract_generic`'s function branch does not recurse into a function's own
body looking for further `function_types` nodes (unlike the class branch,
which explicitly recurses into class bodies). This is consistent with how
`function_boundary_types`/`walk_calls` treat a function as a call-tracking
boundary that folds nested closures into the enclosing scope rather than
graphing them separately. Confirmed via direct repro (`/tmp/repro_nested.py`):
`inner_helper()` nested inside `outer()` produces ZERO nodes, with or without
the fix. This is the ENTIRE remaining residual after the fix (15/1459, 1.0%,
all nested/closure functions -- verified via AST parent-walk on every residual
miss in kb_setup). Given it looks intentional and changing it would need a
larger design decision (how to id/scope nested defs without reintroducing
collision risk), I did not attempt a code change for it and am reporting it
as a dissent/known-gap rather than forcing a fix.

## Verification

All measurements below are against `python/src/kb_setup` extracted STANDALONE
(single repo, `graphify extract <dir> --code-only --no-cluster`, `GRAPHIFY_FORCE=1`)
-- this isolates the current-code bug from the two staleness/measurement
confounds above.

| | node count | AST-vs-graph miss rate (label-corrected matcher) |
|---|---|---|
| BEFORE fix (unmodified fork HEAD, `282976b`) | 3447 nodes | 27/1459 = 1.9% |
| AFTER fix | 3460 nodes | 15/1459 = 1.0% |

Direct per-pair arm (the clean, unambiguous check -- does each confirmed
colliding declaration survive as its own node?):

| file | pair | OLD: both present? | NEW: both present? |
|---|---|---|---|
| graph.py | `_source_path_evidence` / `source_path_evidence` | (True, FALSE) | (True, True) |
| graphify_baseline.py | `_verify_candidate` / `verify_candidate` | (True, FALSE) | (True, True) |
| skillopt_reviewed.py | `_reviewed_tasks` / `reviewed_tasks` | (True, FALSE) | (True, True) |
| handoff_reconcile.py | `Dropped` / `dropped` | (True, FALSE) | (True, True) |
| handoff_reconcile.py | `_is_commitment` / `is_commitment` | (True, FALSE) | (True, True) |
| artifact_download.py | `Provider.plan` / `_provider_plan` | (True, FALSE) | (True, True) |
| artifact_download.py | `Provider.download` / `_provider_download` | (True, FALSE) | (True, True) |

Every one of the 7 confirmed collisions: dropped by the unfixed code, both
members survive with the fix. This IS the "arm it" requirement from the
spec -- the measurement discriminates (reports a real miss on the OLD code,
reports no miss on the NEW code for the same declarations), not a check that
can only pass.

Minimal, self-contained repros used during diagnosis (kept for reference,
not part of the fork's test suite): `/tmp/repro_dataclass.py` (first isolated
the leading-dot method-label measurement artifact), `/tmp/repro_collision.py`
and `/tmp/repro_method_collision.py` (isolated and confirmed all three
collision shapes independent of kb_setup's real code), `/tmp/repro_nested.py`
(isolated the unfixed, by-design nested-closure gap).

### Test suite

Targeted: `tests/test_id_normalization_contract.py`, `tests/test_extraction_spec_ids.py`,
`tests/test_extract.py`, `tests/test_python_decorators.py`,
`tests/test_python_import_resolution.py`, `tests/test_go_qualified_resolution.py`
(the Go precedent's own tests -- go's collision fix is independent code in
`extractors/go.py`, unaffected, kept as a sanity check the two mechanisms
don't interact), `tests/test_extractors_registry.py` -- **355 passed, 0 failed**.

Full suite (`uv run pytest tests/ -q`, all languages/fixtures, 5034 tests
excluding skips): **15 failed, 5019 passed, 11 skipped**. All 15 failures
confirmed PRE-EXISTING and unrelated to this change -- re-ran the exact same
15 tests with the fix reverted (`git stash`) in the same environment: 14 of 15
fail IDENTICALLY without the fix (all are graph-size-cap tests relying on a
`monkeypatch`d size limit that isn't tripping in this environment, and LLM
backend-detection tests sensitive to ambient env vars in this session --
neither touches `extractors/engine.py`); the 15th
(`test_labeling.py::test_label_communities_batches_when_over_batch_size`)
passed in isolation both with and without the fix, i.e. order-dependent
flakiness unrelated to labeling code, which this change does not touch.

## Working-tree invariant

`/Users/rmanaloto/dev/github/ray-manaloto/knowledge-base` was NOT touched.
Baseline `git status --short` captured before starting (empty -- clean tree
at `2b1cdc3a`) matches its state now. All work happened in `/tmp/graphify-fix`
(the fork clone) and scratch dirs under `/tmp`.

## GitHub repos touched

- [ray-manaloto/graphify](https://github.com/ray-manaloto/graphify) -- the tool
  under diagnosis; cloned fresh to `/tmp/graphify-fix`, read `extract.py`,
  `extractors/engine.py`, `extractors/go.py`, `ids.py`, `build.py`, and their
  tests; committed the fix here.
