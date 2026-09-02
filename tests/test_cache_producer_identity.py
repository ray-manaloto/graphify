"""Tests encoding the openai-cli provenance goal as a RED test (#518).

The goal: a `graphify extract --backend <X>` cache entry carries no identity
for *which backend produced it*. Switching `--backend` over an already-cached
corpus therefore never re-dispatches — it silently serves the prior backend's
cached result as if it were the new backend's. That silent-cache-hit defect
is what makes a claude-vs-openai-cli provenance comparison untrustworthy: the
"second backend" arm may never have run at all.

Why this file's stub differs from `test_fallback_backend.py`'s
`_recording_stub`: that stub returns `"nodes": []` on every path (correct for
FALLBACK semantics — a zero-success pass is exactly what triggers a retry).
But `save_semantic_cache` groups items by `source_file` under an `if src:`
guard (graphify/cache.py:1463-1478) and a group with zero items writes zero
cache entries. Reusing that stub here would mean nothing is ever cached, so
run 2 would always look like a fresh miss regardless of the real defect —
the test would pass for the wrong reason. `_cacheable_stub` below returns one
node carrying `source_file` so a real cache entry is written and a real
cache hit is exercised, and it drives `on_chunk_done` so the CLI counts a
success (cli.py:3992-3999) — without that, `_chunk_stats["succeeded"] == 0`
and the pass is treated as all-failed (cli.py:4044), and nothing is saved
even though the stub returned a well-formed node.

Backend choice: `claude`/`openai` (key-gated), not `claude-cli`/`openai-cli`
(binary-gated). cli.py's key/binary check (cli.py:3736-3782) runs BEFORE the
semantic cache is read, so it fires on every call regardless of hit/miss —
but `claude-cli`/`openai-cli` fall back to `shutil.which(...)`, and in a
sandboxed test run those binaries may not resolve, which would produce a red
test that merely LOOKS like the defect. Monkeypatching both
ANTHROPIC_API_KEY and OPENAI_API_KEY sidesteps that entirely.

``--mode deep`` on every run (not just an incidental flag): without it, run
2 of case (i) goes green for the WRONG reason. `incremental_mode` (True once
`graph.json` exists from run 1) drops any doc whose content is unchanged
from `semantic_files` BEFORE the cache is ever consulted (cli.py:3454,
cli.py:3592) — so the stub is called exactly once not because the cache
served a hit, but because the incremental scan never even asked. That is
the false pass this file exists to prevent. `--mode deep` widens the
semantic pass back to the FULL live doc set and lets the mode-namespaced
cache decide hits/misses (cli.py:3607: "the manifest's changed-file gate is
not a valid proxy for deep coverage"), so a "1 hit / 0 miss" print
(cli.py:3956) is only possible if the CACHE, not the incremental scan, is
what kept the second backend from being dispatched.
"""
from __future__ import annotations

import hashlib
import json

import pytest

import graphify.__main__ as mainmod


class ProvenanceGoalUnmet(AssertionError):
    """The openai-cli cache-identity goal (#518) is not yet met.

    A dedicated exception type so the xfail below can pin
    `raises=ProvenanceGoalUnmet, strict=True` (C7): a bare
    `xfail(strict=True)` blesses ANY exception, including one raised by a
    broken fixture or a typo in this file, which would let a precondition
    bug masquerade as "the goal is unmet". Only THIS exception type is
    allowed to make that test count as xfail; anything else is a real
    failure.
    """


def _require(condition: bool, msg: str) -> None:
    if not condition:
        raise ProvenanceGoalUnmet(msg)


def _make_corpus(tmp_path):
    """Same minimal corpus as test_fallback_backend.py's `_make_corpus`:
    one code file (so the pass isn't code-only) + one doc file (so semantic
    extraction is requested at all).
    """
    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
    (tmp_path / "README.md").write_text("# Notes\nThe main function entry point.\n")
    return tmp_path


def _cacheable_stub(calls, *, payload_by_backend=None):
    """Stub extract_corpus_parallel that returns >=1 CACHEABLE node and
    drives on_chunk_done (C6), so save_semantic_cache actually persists an
    entry and the CLI counts the pass as a success rather than all-failed.

    `payload_by_backend` (backend -> label) lets a caller vary the emitted
    node's content per backend, so two runs' cache entries are
    byte-distinguishable (needed for the --force overwrite test) and so the
    final graph can be inspected to see WHICH backend's node survived
    (needed for the mechanism check in C8).
    """
    payload_by_backend = payload_by_backend or {}

    def _stub(paths, **kwargs):
        be = kwargs.get("backend")
        calls.append({"backend": be, "paths": sorted(str(p) for p in paths)})
        label = payload_by_backend.get(be, be)
        readme = next(p for p in paths if str(p).endswith("README.md"))
        node = {
            "id": f"doc:{label}",
            "kind": "doc",
            "source_file": str(readme),
            "label": f"produced by {label}",
        }
        on_chunk = kwargs.get("on_chunk_done")
        if on_chunk:
            on_chunk(0, 1, {"nodes": [node], "edges": [], "hyperedges": []})
        return {"nodes": [node], "edges": [], "hyperedges": [],
                "input_tokens": 10, "output_tokens": 5}

    return _stub


def _arm(monkeypatch, tmp_path, stub, *, backend, extra_argv=()):
    """Parameterised sibling of test_fallback_backend.py's `_arm` (C4): that
    helper hardcodes `--backend claude` in argv, so it cannot exercise a
    backend SWITCH. Added additively — the 11 existing fallback tests are
    untouched.

    Always passes `--mode deep` (C5) — see the module docstring for why an
    incremental-only run would false-pass case (i)/(ii).
    """
    corpus = _make_corpus(tmp_path)
    out_dir = tmp_path / "out"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key-2")
    monkeypatch.delenv("GRAPHIFY_FALLBACK_BACKEND", raising=False)
    monkeypatch.setattr("graphify.llm.extract_corpus_parallel", stub)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys, "argv",
        ["graphify", "extract", str(corpus), "--backend", backend,
         "--mode", "deep", "--out", str(out_dir), *extra_argv],
    )
    return corpus, out_dir


def _run_ok():
    # extract may still raise SystemExit at the end (clean exit code 0)
    # depending on platform; accept either no exception or SystemExit(0).
    try:
        mainmod.main()
    except SystemExit as exc:
        assert exc.code in (None, 0), f"unexpected exit code {exc.code}"


def _semantic_entry_files(out_dir):
    """Every semantic cache entry file under out_dir's graphify-out/cache.

    `--mode deep` writes into `cache/semantic-deep/` (cache.py's
    ``kind = "semantic" if mode is None else f"semantic-{mode}"``), which the
    `semantic*` glob segment already covers.
    """
    cache_dir = out_dir / "graphify-out" / "cache"
    return sorted(cache_dir.glob("semantic*/**/*.json"))


def _graph_doc_labels(out_dir):
    """The `label` field of every doc-kind node in the written graph.json."""
    graph_path = out_dir / "graphify-out" / "graph.json"
    graph = json.loads(graph_path.read_text())
    return [
        n.get("label") for n in graph.get("nodes", [])
        if n.get("kind") == "doc" or str(n.get("id", "")).startswith("doc:")
    ]


def test_backend_switch_is_a_silent_cache_hit_today(monkeypatch, tmp_path, capsys):
    """(i) --backend A then --backend B over one corpus: stub called ONCE,
    via the CACHE (not the incremental scan skipping the file — see the
    module docstring's C5 explanation for why `--mode deep` is required for
    this to test what it claims to test).

    Asserts the MECHANISM (C8), not just a call count:
      - both runs complete cleanly;
      - run B's stdout shows the semantic-cache hit/miss print
        (cli.py:3956, `if sem_cache_hits:`) reporting "1 hit / 0 miss" —
        proof the cache was actually consulted and served a hit, which an
        incremental skip (no cache read at all) could never print;
      - the FINAL graph still carries backend A's node content, not a
        default/empty placeholder — proof the cached entry (not a code
        path that silently dropped the doc) is what fed the graph;
      - the call list is exactly the one 'claude' dispatch.

    Must PASS today (this IS the current, unfixed behavior).
    """
    calls = []
    stub = _cacheable_stub(calls)

    _arm(monkeypatch, tmp_path, stub, backend="claude")
    _run_ok()
    capsys.readouterr()  # discard run-A output; we assert on run B's below
    assert [c["backend"] for c in calls] == ["claude"], (
        "sanity: the first run (fresh corpus, no cache) must dispatch"
    )

    calls.clear()
    _arm(monkeypatch, tmp_path, stub, backend="openai")
    _run_ok()
    out = capsys.readouterr().out

    assert calls == [], (
        "backend 'openai' was never dispatched on the second run — the "
        "cache entry written by 'claude' silently served the request "
        "instead (#518). If this assertion starts failing, the defect this "
        "test documents has been fixed; see the paired xfail below."
    )
    assert "semantic cache: 1 hit / 0 miss" in out, (
        "run B's stdout must show the CACHE reporting a hit "
        "(cli.py:3956) — without this line, a call count of zero could "
        "equally mean the incremental scan never asked the cache at all "
        "(the exact false-pass --mode deep exists to rule out)"
    )
    labels = _graph_doc_labels(tmp_path / "out")
    assert "produced by claude" in labels, (
        "the final graph must still carry backend A's ('claude') node "
        "content — proof the cache entry (not a dropped/silent doc) fed "
        "the graph"
    )


@pytest.mark.xfail(raises=ProvenanceGoalUnmet, strict=True, reason=(
    "#518: a semantic cache entry is not keyed by the backend that produced "
    "it, so switching --backend over an already-cached corpus is a silent "
    "cache hit instead of a genuine re-dispatch. "
    "DEFERRED BY DECISION, NOT ABANDONED (Ray, D1, 2026-09-02): D1 selected "
    "'stop at commit 1' — attribution and visible reuse — and dropped the "
    "per-profile cache partitioning (lane I2) that would have made this pass. "
    "So this stays RED on purpose, and it is deliberately not deleted: it is "
    "the only machine-checkable statement of the goal, and a deleted test "
    "records nothing about why. What commit 1 DOES deliver is next door — "
    "test_backend_switch_is_a_silent_cache_hit_today asserts the reuse is "
    "attributed and warned rather than silent. Remove this xfail only when "
    "the cache becomes backend-identity-aware, which is a decision Ray has "
    "to re-open, not a bug someone can fix. `raises=` is pinned to "
    "ProvenanceGoalUnmet (C7) so a fixture bug elsewhere in this test cannot "
    "masquerade as this goal being unmet."
))
def test_backend_switch_actually_dispatches_the_new_backend(monkeypatch, tmp_path, capsys):
    """(ii) Same setup as (i), asserting the GOAL instead of the defect:
    switching backends over a cached corpus must really invoke the new
    backend, and the final graph must carry ITS content, not the prior
    backend's.
    """
    calls = []
    stub = _cacheable_stub(calls)

    _arm(monkeypatch, tmp_path, stub, backend="claude")
    _run_ok()
    capsys.readouterr()

    calls.clear()
    _arm(monkeypatch, tmp_path, stub, backend="openai")
    _run_ok()
    out = capsys.readouterr().out

    _require(
        [c["backend"] for c in calls] == ["openai"],
        "switching --backend must re-dispatch on the new backend, not "
        "silently reuse the prior backend's cache entry",
    )
    _require(
        "semantic cache: 1 hit / 0 miss" not in out,
        "a genuine re-dispatch must NOT report a cache hit for the file "
        "backend B was supposed to (re-)extract",
    )
    labels = _graph_doc_labels(tmp_path / "out")
    _require(
        "produced by openai" in labels,
        "the final graph must carry backend B's ('openai') node content "
        "once the cache is backend-identity-aware",
    )


def test_force_overwrites_the_prior_backends_cache_entry(monkeypatch, tmp_path):
    """(iii) --force --backend B overwrites A's cache entry.

    --force only skips the cache READ (cli.py:3939-3948) — it does not make
    the cache backend-aware (N1, tracked separately from the #518 fix this
    file's xfail targets). The SAVE below the skipped read still runs
    unconditionally, so a --force pass with a different backend clobbers the
    entry a prior backend wrote. Proven by comparing the cache entry file's
    sha256 before/after, not merely by asserting the stub ran (asserting
    dispatch alone proves nothing about whether the on-disk entry changed).

    `--force` also forces `incremental_mode = False` regardless of `--mode
    deep` (cli.py:3455: `incremental_mode and not force`), so the C5 false
    -pass mechanism cannot apply to this run's dispatch decision either way
    — `--mode deep` is included here only for parity with the other two
    tests' invocation shape, not because this test depends on it.
    """
    calls = []
    stub = _cacheable_stub(
        calls, payload_by_backend={"claude": "claude-v1", "openai": "openai-v1"},
    )

    _arm(monkeypatch, tmp_path, stub, backend="claude")
    _run_ok()

    entries_a = _semantic_entry_files(tmp_path / "out")
    assert len(entries_a) == 1, (
        f"expected exactly one semantic cache entry after run 1, got {entries_a}"
    )
    digest_a = hashlib.sha256(entries_a[0].read_bytes()).hexdigest()

    calls.clear()
    _arm(monkeypatch, tmp_path, stub, backend="openai", extra_argv=["--force"])
    _run_ok()

    assert [c["backend"] for c in calls] == ["openai"], (
        "--force must re-dispatch even though a cache entry already exists"
    )

    entries_b = _semantic_entry_files(tmp_path / "out")
    assert len(entries_b) == 1, (
        f"expected exactly one semantic cache entry after run 2, got {entries_b}"
    )
    digest_b = hashlib.sha256(entries_b[0].read_bytes()).hexdigest()

    assert digest_a != digest_b, (
        "the cache entry's content did not change across the --force run — "
        "'openai' did not overwrite 'claude''s cache entry as expected"
    )
