"""Tests for extraction provenance (#518 commit 1): the `producers` per-item
list (dedup.py union + cli.py stamp), the `producers_complete` lossy-merge
flag, and the run-level `extractor` block in export.py's `to_json`.

Companion to `test_cache_producer_identity.py`, which covers the CLI-level
cache-identity behaviour (criteria a-e, j) via subprocess-free `main()` runs.
This file covers the criteria that need direct access to dedup.py/export.py
internals: (f) the union actually unions, (f2) a lossy merge is marked
incomplete, (g) producer metadata survives cache relativization, and the
export.py carry-forward mechanism §3c depends on.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import networkx as nx
import pytest

from graphify.build import build_from_json
from graphify.cluster import cluster
from graphify.dedup import _merge_missing_attributes, _union_producers, deduplicate_entities
from graphify.export import _read_existing_extractor, to_json


# ── _union_producers ──────────────────────────────────────────────────────

def test_union_producers_dedupes_and_sorts():
    a = [{"backend": "openai-cli", "model_selector": "gpt-5.6-sol",
          "model_reported": None, "effort": "ultra"}]
    b = [{"backend": "claude-cli", "model_selector": "opus", "model_reported": None,
          "effort": "ultra"},
         {"backend": "openai-cli", "model_selector": "gpt-5.6-sol",
          "model_reported": None, "effort": "ultra"}]  # exact dup of a[0]
    merged = _union_producers(a, b)
    assert len(merged) == 2
    backends = sorted(r["backend"] for r in merged)
    assert backends == ["claude-cli", "openai-cli"]


def test_union_producers_tolerates_none_and_non_list():
    assert _union_producers(None, None) == []
    assert _union_producers(None, [{"backend": "x", "model_selector": "y",
                                     "model_reported": None, "effort": "z"}]) == [
        {"backend": "x", "model_selector": "y", "model_reported": None, "effort": "z"}
    ]


# ── _merge_missing_attributes: producers is UNIONED, not fill-if-absent ────

def test_merge_missing_attributes_unions_producers_when_both_present():
    survivor = {"id": "n1", "producers": [
        {"backend": "claude-cli", "model_selector": "opus", "model_reported": None, "effort": "ultra"}
    ]}
    duplicate = {"id": "n1", "producers": [
        {"backend": "openai-cli", "model_selector": "gpt-5.6-sol", "model_reported": None, "effort": "ultra"}
    ]}
    merged = _merge_missing_attributes(survivor, duplicate)
    assert len(merged["producers"]) == 2, (
        "a survivor that already has `producers` must not silently discard "
        "the loser's — that is the exact defect spec 3b exists to fix. "
        "ARM: comment out the `if key == 'producers':` branch in "
        "_merge_missing_attributes and this drops to 1."
    )


def test_merge_missing_attributes_origin_still_excluded():
    """Control: the pre-existing _origin exclusion (a DIFFERENT key, DIFFERENT
    semantics — see dedup.py's comment) must be untouched by the producers
    branch added beside it."""
    survivor = {"id": "n1", "_origin": "semantic"}
    duplicate = {"id": "n1", "_origin": "ast"}
    merged = _merge_missing_attributes(survivor, duplicate)
    assert merged["_origin"] == "semantic"


# ── (f) the union actually unions, through the full dedup pipeline ────────

def test_id_collision_same_source_unions_producers():
    """(f): two same-id, same-source_file nodes with different `producers`
    merge to a two-record list. This is the ONE of four merge paths §3b
    actually covers (`_same_source_entity` requires equal, non-empty
    source_file) — see test_cross_file_collision_marks_incomplete below for
    the uncovered path.
    """
    nodes = [
        {"id": "fn:foo", "label": "foo", "source_file": "a.py",
         "producers": [{"backend": "claude-cli", "model_selector": "opus",
                        "model_reported": None, "effort": "ultra"}]},
        {"id": "fn:foo", "label": "foo", "source_file": "a.py",
         "producers": [{"backend": "openai-cli", "model_selector": "gpt-5.6-sol",
                        "model_reported": None, "effort": "ultra"}]},
    ]
    deduped, _ = deduplicate_entities(nodes, [], communities={})
    assert len(deduped) == 1
    producers = deduped[0]["producers"]
    assert len(producers) == 2, (
        "green (f) is NOT evidence the plural shape is populated generally "
        "(spec: it constructs exactly the one covered path) — paired with "
        "test_cross_file_collision_marks_incomplete for the uncovered paths."
    )
    assert deduped[0].get("producers_complete") is not False, (
        "the covered (same-source) path is NOT lossy — nothing here should "
        "mark it incomplete"
    )


# ── (f2) a lossy merge marks producers_complete: False ─────────────────────

def test_cross_file_collision_marks_incomplete():
    """A same-id collision across DIFFERENT source_file values is excluded
    from `_same_source_entity` (dedup.py:390-403) — the loser's `producers`
    is dropped entirely, never unioned. That must flip
    `producers_complete: False` on the survivor (spec 3f / MISSING-1).

    ARM: without the len(same_source) < len(losers) branch in
    deduplicate_entities, this silently stays absent (reads as complete).
    """
    nodes = [
        {"id": "fn:foo", "label": "foo", "source_file": "a.py",
         "producers": [{"backend": "claude-cli", "model_selector": "opus",
                        "model_reported": None, "effort": "ultra"}]},
        {"id": "fn:foo", "label": "foo", "source_file": "b.py",
         "producers": [{"backend": "openai-cli", "model_selector": "gpt-5.6-sol",
                        "model_reported": None, "effort": "ultra"}]},
    ]
    deduped, _ = deduplicate_entities(nodes, [], communities={})
    assert len(deduped) == 1
    # The cross-file loser was never merged in at all: exactly 1 record survives.
    assert len(deduped[0]["producers"]) == 1
    assert deduped[0]["producers_complete"] is False, (
        "a cross-file id collision drops the loser's producers without a "
        "merge attempt — the survivor's list cannot be complete"
    )


def test_fuzzy_merge_marks_incomplete():
    """The fuzzy/label-merge path (dedup.py ~:834, `deduped_nodes = [n for n
    in unique_nodes if n["id"] not in remap]`) drops losers WHOLE with no
    attribute merge at all — refuting an earlier claim that this path
    CREATES multi-producer items (spec P7). The WINNER must be marked
    producers_complete: False when it has `producers` and real losers exist.

    ARM: without the `winner["producers_complete"] = False` mutation in the
    components loop, this silently stays absent.
    """
    nodes = [
        {"id": "concept:auth-manager", "label": "AuthenticationManager",
         "source_file": "auth.py", "file_type": "concept",
         "producers": [{"backend": "claude-cli", "model_selector": "opus",
                        "model_reported": None, "effort": "ultra"}]},
        {"id": "concept:auth-manager-2", "label": "AuthenticationManager",
         "source_file": "auth2.py", "file_type": "concept",
         "producers": [{"backend": "openai-cli", "model_selector": "gpt-5.6-sol",
                        "model_reported": None, "effort": "ultra"}]},
    ]
    deduped, _ = deduplicate_entities(nodes, [], communities={})
    assert len(deduped) == 1, "the two identical concept labels must fuzzy/exact-merge"
    survivor = deduped[0]
    # Only the winner's own producers survive — the loser's list is gone,
    # not unioned in, on this path.
    assert len(survivor["producers"]) == 1
    assert survivor["producers_complete"] is False, (
        "the fuzzy/exact label-merge path drops the loser's producers "
        "whole — the survivor cannot be marked complete"
    )


def test_ast_only_nodes_untouched_by_producers_complete():
    """Control for (f2)/(h): a node with no `producers` key at all (AST-only)
    must never gain a `producers_complete` key, lossy merge or not — a pure
    AST run's graph must stay byte-identical (spec criterion h)."""
    nodes = [
        {"id": "fn:foo", "label": "foo", "source_file": "a.py"},
        {"id": "fn:foo", "label": "foo", "source_file": "b.py"},
    ]
    deduped, _ = deduplicate_entities(nodes, [], communities={})
    assert len(deduped) == 1
    assert "producers" not in deduped[0]
    assert "producers_complete" not in deduped[0]


# ── (g) producer metadata survives semantic-cache relativization ──────────

def test_producers_survive_cache_relativization(tmp_path):
    from graphify.cache import save_semantic_cache, check_semantic_cache

    src = tmp_path / "doc.md"
    src.write_text("# hi\n")
    node = {
        "id": "doc:hi", "kind": "doc", "source_file": str(src),
        "label": "hi",
        "producers": [{"backend": "openai-cli", "model_selector": "gpt-5.6-sol",
                       "model_reported": None, "effort": "ultra"}],
    }
    save_semantic_cache([node], [], [], root=tmp_path, cache_root=tmp_path)
    cached_nodes, _, _, uncached = check_semantic_cache(
        [str(src)], root=tmp_path, cache_root=tmp_path,
    )
    assert uncached == []
    assert len(cached_nodes) == 1
    assert cached_nodes[0]["producers"] == node["producers"], (
        "producer identity (backend/model_selector/effort — none of which "
        "match a path or id anchor) must round-trip through "
        "_relativize_ids_in byte-for-byte (spec criterion g / MISSING-5)"
    )


# ── export.py: extractor block emission + carry-forward ───────────────────

FIXTURES = Path(__file__).parent / "fixtures"


def _make_graph():
    return build_from_json(json.loads((FIXTURES / "extraction.json").read_text()))


def test_extractor_block_omitted_when_none_and_no_prior_file():
    """(h): extractor=None with no existing graph.json at the target path ->
    no `extractor` key at all. This is the plain-AST-run shape."""
    G = _make_graph()
    communities = cluster(G)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "graph.json"
        to_json(G, communities, str(out))
        data = json.loads(out.read_text())
        assert "extractor" not in data


def test_extractor_block_written_when_backend_present():
    G = _make_graph()
    communities = cluster(G)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "graph.json"
        to_json(G, communities, str(out), extractor={
            "backend": "openai-cli", "model": "gpt-5.6-sol", "mode": "deep",
            "graphify_version": "0.9.53", "fork_commit": "a" * 40,
            "executed": True, "producers_complete": True,
        })
        data = json.loads(out.read_text())
        assert data["extractor"]["backend"] == "openai-cli"
        assert data["extractor"]["executed"] is True
        assert data["extractor"]["producers_complete"] is True


def test_extractor_block_omitted_when_backend_model_mode_all_falsy():
    """Even when SOME extractor fields are present (graphify_version,
    executed), the block must be omitted entirely if backend/model/mode are
    all falsy — e.g. a run that dispatched nothing and requested no --mode."""
    G = _make_graph()
    communities = cluster(G)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "graph.json"
        to_json(G, communities, str(out), extractor={
            "backend": None, "model": None, "mode": None,
            "graphify_version": "0.9.53", "fork_commit": None,
            "executed": False, "producers_complete": True,
        })
        data = json.loads(out.read_text())
        assert "extractor" not in data


def test_extractor_carries_forward_across_a_relabel_only_write():
    """extractor=None on a SECOND write, over a file that already carries a
    real stamp, must carry that stamp forward rather than wiping it — this
    is the relabel/cluster-only shape §3c calls out by name.

    ARM: without _read_existing_extractor being consulted when extractor is
    None, the second write's graph.json loses `extractor` entirely.
    """
    G = _make_graph()
    communities = cluster(G)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "graph.json"
        to_json(G, communities, str(out), extractor={
            "backend": "claude-cli", "model": "opus", "mode": None,
            "graphify_version": "0.9.53", "fork_commit": "b" * 40,
            "executed": True, "producers_complete": True,
        })
        # Second write: no new extraction ran (extractor=None), as a relabel
        # or cluster-only pass would call it.
        to_json(G, communities, str(out), force=True)
        data = json.loads(out.read_text())
        assert data["extractor"]["backend"] == "claude-cli"
        assert data["extractor"]["fork_commit"] == "b" * 40


def test_read_existing_extractor_missing_file_returns_none(tmp_path):
    assert _read_existing_extractor(tmp_path / "does-not-exist.json") is None


def test_extractor_strings_are_truncated_at_256(tmp_path):
    G = _make_graph()
    communities = cluster(G)
    long_model = "m" * 500
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "graph.json"
        to_json(G, communities, str(out), extractor={
            "backend": "openai-cli", "model": long_model, "mode": None,
            "graphify_version": None, "fork_commit": None,
            "executed": True, "producers_complete": True,
        })
        data = json.loads(out.read_text())
        assert len(data["extractor"]["model"]) == 256
