"""Public entrypoint controls for capture-required execution without a profile."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from graphify import cache, execution, llm


def _context(root: Path, *, capture_required: bool = True) -> dict:
    return {
        "schema_version": 1,
        "run_id": "managed-entrypoints",
        "stage_id": "stage-1",
        "project_root": str(root.resolve()),
        "cwd": str(root.resolve()),
        "source_identity": {"algorithm": "sha256", "digest": "b" * 64, "scope": ["."]},
        "prompt_identity": {"algorithm": "sha256", "digest": "c" * 64},
        "extractor_identity": {"name": "graphify", "version": "test", "digest": "d" * 64},
        "configuration_identity": {"digest": "e" * 64, "sources": ["config.toml"]},
        "instruction_identity": {"digest": "f" * 64, "sources": ["AGENTS.md"]},
        "parent_receipts": [],
        "cache_ancestry": [],
        "capture_required": capture_required,
    }


def _profile() -> dict:
    return {
        "schema_version": 1,
        "backend": "openai-cli",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "binary_expectation": {
            "path": "/reviewed/bin/codex",
            "sha256": "a" * 64,
            "version": "reviewed-1",
        },
        "cli_policy": {
            "project_configuration": "inherit",
            "session_persistence": "retain",
            "mcp": "inherit",
            "sandbox": "read-only",
        },
        "identity_policy": {
            "required_per_response": True,
            "allowed_reported_models": ["gpt-5.6-sol"],
        },
    }


def test_effective_managed_mode_matrix(tmp_path):
    context, managed = execution.validate_effective_managed_mode(None, None)
    assert (context, managed) == (None, False)

    context, managed = execution.validate_effective_managed_mode(None, _context(tmp_path))
    assert context is not None and managed is True

    with pytest.raises(ValueError, match="capture_required=true"):
        execution.validate_effective_managed_mode(
            _profile(), _context(tmp_path, capture_required=False)
        )


@pytest.mark.parametrize("entrypoint", ["text", "direct", "corpus", "label"])
def test_capture_required_rejects_provider_routes_before_discovery(
    monkeypatch, tmp_path, entrypoint
):
    def unexpected(*_args, **_kwargs):
        pytest.fail("provider discovery or worker work must not start")

    context = _context(tmp_path)
    monkeypatch.setattr(llm, "_get_backend_api_key", unexpected)
    monkeypatch.setattr(llm, "detect_backend", unexpected)
    monkeypatch.setattr(llm, "_claude_cli_available", unexpected)
    monkeypatch.setattr(llm, "_pack_chunks_by_tokens", unexpected)
    if entrypoint == "text":
        call = lambda: llm._call_llm("prompt", backend="kimi", run_context=context)
    elif entrypoint == "direct":
        call = lambda: llm.extract_files_direct(
            [], backend=None, root=tmp_path, run_context=context
        )
    elif entrypoint == "corpus":
        call = lambda: llm.extract_corpus_parallel(
            [], backend=None, root=tmp_path, run_context=context
        )
    else:
        call = lambda: llm.generate_community_labels(
            None, {1: ["node"]}, backend=None, run_context=context
        )
    with pytest.raises(ValueError, match="registered CLI backend"):
        call()


def test_capture_required_text_cli_retains_receipt(monkeypatch, tmp_path):
    receipt = {"receipt_id": "managed-text", "usage": {"input_tokens": 2, "output_tokens": 3}}
    monkeypatch.setattr(
        llm,
        "_managed_cli_call",
        lambda *_args, **_kwargs: {"value": "answer", "receipt": receipt},
    )
    usage: dict = {}
    assert (
        llm._call_llm(
            "prompt",
            backend="openai-cli",
            run_context=_context(tmp_path),
            process_runner=object(),
            receipt_sink=object(),
            usage_out=usage,
        )
        == "answer"
    )
    assert usage == {"input": 2, "output": 3, "_execution_receipts": [receipt]}


def test_managed_label_partial_batch_propagates_and_retains_attempt(monkeypatch, tmp_path):
    import networkx as nx

    graph = nx.Graph()
    graph.add_node("a", label="Alpha")
    graph.add_node("b", label="Beta")
    communities = {0: ["a"], 1: ["b"]}
    calls = 0
    failed_receipt = {"receipt_id": "label-failed", "completion": "incomplete_capture"}

    def label_batch(cids, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {cids[0]: "Named zero"}
        error = RuntimeError("label capture failed")
        error.graphify_attempt = {"receipt": failed_receipt}
        raise error

    monkeypatch.setattr(llm, "_label_batch_with_retry", label_batch)
    usage: dict = {}
    with pytest.raises(RuntimeError, match="label capture failed") as caught:
        llm.label_communities(
            graph,
            communities,
            backend="openai-cli",
            batch_size=1,
            max_concurrency=1,
            usage_out=usage,
            run_context=_context(tmp_path),
            process_runner=object(),
            receipt_sink=object(),
        )
    assert caught.value.graphify_partial_labels[0] == "Named zero"
    assert caught.value.graphify_partial_labels[1] == "Community 1"
    assert usage["_execution_receipts"] == [failed_receipt]


def test_managed_label_two_batch_failure_retains_both_sink_receipts(tmp_path):
    import networkx as nx

    graph = nx.Graph()
    graph.add_node("a", label="Alpha")
    graph.add_node("b", label="Beta")
    communities = {0: ["a"], 1: ["b"]}
    receipts: list[dict] = []
    runner_calls = 0

    def runner(invocation: dict) -> dict:
        nonlocal runner_calls
        runner_calls += 1
        payload = b'{"0":"Named zero"}' if runner_calls == 1 else b""
        return {
            "returncode": 0 if runner_calls == 1 else 7,
            "stdout": b'{"type":"turn.completed"}\n',
            "stderr": b"" if runner_calls == 1 else b"managed label failure",
            "stdout_eof": True,
            "stderr_eof": True,
            "finalized": True,
            "binary": dict(invocation["requested_profile"]["binary_expectation"]),
            "raw_capture_refs": {
                "stdout": f"raw/label-{runner_calls}.stdout",
                "stderr": f"raw/label-{runner_calls}.stderr",
            },
            "provider_events": [],
            "result_artifact": {
                "requested_path": invocation["output_path"],
                "payload": payload,
                "byte_count": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "raw_ref": f"raw/label-{runner_calls}.result",
                "eof": True,
                "finalized": True,
            },
        }

    def sink(receipt: dict) -> dict:
        receipts.append(receipt)
        return {
            "receipt_id": receipt["receipt_id"],
            "sha256": execution._digest(receipt),
            "finalized": True,
            "durable_ref": f"receipts/{receipt['receipt_id']}.json",
        }

    with pytest.raises(RuntimeError, match="codex exec exited 7") as caught:
        llm.label_communities(
            graph,
            communities,
            backend="openai-cli",
            batch_size=1,
            max_concurrency=1,
            execution_profile=_profile(),
            run_context=_context(tmp_path),
            process_runner=runner,
            receipt_sink=sink,
        )

    assert runner_calls == 2
    assert [receipt["process"]["returncode"] for receipt in receipts] == [0, 7]
    assert {receipt["run_context"]["run_id"] for receipt in receipts} == {"managed-entrypoints"}
    assert {receipt["run_context"]["stage_id"] for receipt in receipts} == {"stage-1"}
    assert len({receipt["profile_fingerprint"] for receipt in receipts}) == 1
    assert caught.value.graphify_partial_labels[0] == "Named zero"
    assert caught.value.graphify_partial_labels[1] == "Community 1"
    assert caught.value.graphify_attempt["receipt"] == receipts[1]


def test_explicit_profile_conflict_rejects_before_backend_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(
        llm,
        "_get_backend_api_key",
        lambda *_args, **_kwargs: pytest.fail("conflict must reject before backend discovery"),
    )
    monkeypatch.setattr(
        llm,
        "_default_model_for_backend",
        lambda *_args, **_kwargs: pytest.fail("conflict must reject before model discovery"),
    )
    with pytest.raises(ValueError, match="conflicting explicit backend selectors"):
        llm._call_llm(
            "prompt",
            backend="claude-cli",
            execution_profile=_profile(),
            run_context=_context(tmp_path),
        )


def test_capture_required_dedup_rejects_non_cli_before_tiebreak(monkeypatch, tmp_path):
    from graphify import dedup

    monkeypatch.setattr(
        dedup, "_llm_tiebreak", lambda *_args, **_kwargs: pytest.fail("no paid tiebreak")
    )
    with pytest.raises(ValueError, match="registered CLI backend"):
        dedup.deduplicate_entities(
            [
                {"id": "a", "label": "Alpha concept"},
                {"id": "b", "label": "Different subject"},
            ],
            [],
            communities={},
            dedup_llm_backend="kimi",
            run_context=_context(tmp_path),
        )


def test_capture_required_triage_rejects_before_backend_resolution(monkeypatch, tmp_path):
    from graphify import prs

    monkeypatch.setattr(
        prs, "_resolve_triage_backend", lambda: pytest.fail("no provider discovery")
    )
    item = prs.PRInfo(
        number=1,
        title="Fix",
        branch="fix",
        base_branch="main",
        author="dev",
        is_draft=False,
        review_decision="",
        ci_status="SUCCESS",
        updated_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ValueError, match="registered CLI backend"):
        prs.triage_with_opus([item], "main", run_context=_context(tmp_path))


def test_capture_required_profile_none_cache_fails_closed(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("hello")
    node = {"id": "legacy", "label": "Legacy", "source_file": str(source)}
    cache.save_semantic_cache([node], [], root=tmp_path, cache_root=tmp_path, prompt="p")

    context = _context(tmp_path)
    assert cache.check_semantic_cache(
        [str(source)], root=tmp_path, cache_root=tmp_path, prompt="p", run_context=context
    )[3] == [str(source)]

    with pytest.raises(ValueError, match="producer_receipt"):
        cache.save_semantic_cache(
            [node], [], root=tmp_path, cache_root=tmp_path, prompt="p", run_context=context
        )

    producer = {"receipt_id": "managed-cache", "completion": "completed"}
    assert (
        cache.save_semantic_cache(
            [{**node, "id": "managed"}],
            [],
            root=tmp_path,
            cache_root=tmp_path,
            prompt="p",
            run_context=context,
            producer_receipt=producer,
        )
        == 0
    )
    nodes, edges, hyperedges, uncached = cache.check_semantic_cache(
        [str(source)],
        root=tmp_path,
        cache_root=tmp_path,
        prompt="p",
        run_context=context,
    )
    assert (nodes, edges, hyperedges) == ([], [], [])
    assert uncached == [str(source)]


def test_explicit_profile_managed_cache_retains_compatible_warm_hit(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("hello")
    node = {"id": "managed", "label": "Managed", "source_file": str(source)}
    context = _context(tmp_path)
    profile = _profile()
    producer = {"receipt_id": "managed-cache", "completion": "completed"}

    assert (
        cache.save_semantic_cache(
            [node],
            [],
            root=tmp_path,
            cache_root=tmp_path,
            prompt="p",
            execution_profile=profile,
            run_context=context,
            producer_receipt=producer,
        )
        == 1
    )
    evidence: list[dict] = []
    nodes, _, _, uncached = cache.check_semantic_cache(
        [str(source)],
        root=tmp_path,
        cache_root=tmp_path,
        prompt="p",
        execution_profile=profile,
        run_context=context,
        cache_evidence_out=evidence,
    )
    assert [item["id"] for item in nodes] == ["managed"]
    assert uncached == []
    assert evidence[0]["producer_receipts"] == [producer]


def test_explicit_profile_cache_rejects_capture_false_before_lookup(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("hello")
    with pytest.raises(ValueError, match="capture_required=true"):
        cache.check_semantic_cache(
            [str(source)],
            root=tmp_path,
            execution_profile=_profile(),
            run_context=_context(tmp_path, capture_required=False),
        )
