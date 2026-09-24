"""Hermetic tests for managed CLI profiles and attempt receipts."""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
import pytest

from graphify import cache, execution, llm


def _profile(backend: str = "openai-cli") -> dict:
    binary = "codex" if backend == "openai-cli" else "claude"
    model = "gpt-5.6-sol" if backend == "openai-cli" else "claude-opus-4-1"
    return {
        "schema_version": 1,
        "backend": backend,
        "model": model,
        "effort": "high",
        "binary_expectation": {
            "path": f"/reviewed/bin/{binary}",
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
            "allowed_reported_models": [model],
        },
    }


def _context(tmp_path: Path, *, capture_required: bool = True) -> dict:
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "stage_id": "extract-1",
        "project_root": str(tmp_path.resolve()),
        "cwd": str(tmp_path.resolve()),
        "source_identity": {"algorithm": "sha256", "digest": "b" * 64, "scope": ["."]},
        "prompt_identity": {"algorithm": "sha256", "digest": "c" * 64},
        "extractor_identity": {"name": "graphify", "version": "test", "digest": "d" * 64},
        "configuration_identity": {"digest": "e" * 64, "sources": ["config.toml"]},
        "instruction_identity": {"digest": "f" * 64, "sources": ["AGENTS.md"]},
        "parent_receipts": [],
        "cache_ancestry": [],
        "capture_required": capture_required,
    }


def _invocation(tmp_path: Path, profile: dict | None = None) -> dict:
    selected = execution.resolve_execution_profile(
        None,
        None,
        None,
        execution_profile=profile or _profile(),
        purpose="extract",
        environment={},
    )
    return execution.build_cli_invocation(
        "extract this",
        purpose="extract",
        max_tokens=100,
        profile=selected,
        output_path=tmp_path / "answer.json",
        project_root=tmp_path,
        cwd=tmp_path,
    )


def _legacy_invocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(
        execution.shutil,
        "which",
        lambda name: "/reviewed/bin/codex" if name == "codex" else None,
    )
    selected = execution.resolve_execution_profile(
        "openai-cli",
        "legacy-model",
        None,
        execution_profile=None,
        purpose="extract",
        environment={},
    )
    return execution.build_cli_invocation(
        "extract this",
        purpose="extract",
        max_tokens=100,
        profile=selected,
        output_path=tmp_path / "legacy-answer.json",
        project_root=tmp_path,
        cwd=tmp_path,
    )


def _process(
    invocation: dict,
    *,
    reported_model: str = "gpt-5.6-sol",
    payload: bytes = b'{"nodes":[],"edges":[],"hyperedges":[]}',
) -> dict:
    binary = invocation["requested_profile"]["binary_expectation"]
    result = {
        "returncode": 0,
        "stdout": b'{"type":"turn.completed"}\n',
        "stderr": b"",
        "stdout_eof": True,
        "stderr_eof": True,
        "finalized": True,
        "binary": dict(binary),
        "raw_capture_refs": {"stdout": "raw/stdout", "stderr": "raw/stderr"},
        "provider_events": [{"type": "turn.completed"}],
        "reported_model": reported_model,
    }
    if invocation.get("output_contract") == "last-message-file":
        result["result_artifact"] = {
            "requested_path": invocation["output_path"],
            "payload": payload,
            "byte_count": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "raw_ref": "raw/result",
            "eof": True,
            "finalized": True,
        }
    return result


def _parser(process: dict) -> dict:
    return {
        "value": {"nodes": [], "edges": [], "hyperedges": []},
        "completion": "completed_empty",
        "usage": {},
        "observations": [],
        "responses": [{"response_id": "response-1", "reported_model": process["reported_model"]}],
        "coverage": {"status": "complete", "reasons": []},
    }


def _ack(receipts: list[dict]):
    def sink(receipt: dict) -> dict:
        receipts.append(receipt)
        return {
            "receipt_id": receipt["receipt_id"],
            "sha256": execution._digest(receipt),
            "finalized": True,
            "durable_ref": f"receipts/{receipt['receipt_id']}.json",
        }

    return sink


def test_profile_rejects_conflicting_explicit_and_environment_selectors():
    with pytest.raises(ValueError, match="conflicting explicit model selectors"):
        execution.resolve_execution_profile(
            "openai-cli",
            None,
            "high",
            execution_profile=_profile(),
            purpose="extract",
            environment={"GRAPHIFY_OPENAI_CLI_MODEL": "gpt-other"},
        )


def test_profile_requires_an_allowed_model_when_identity_is_required():
    profile = _profile()
    profile["identity_policy"]["allowed_reported_models"] = []
    with pytest.raises(ValueError, match="cannot be empty"):
        execution.resolve_execution_profile(
            None,
            None,
            None,
            execution_profile=profile,
            purpose="extract",
            environment={},
        )


def test_builder_uses_shared_codex_profile_for_auxiliary_calls(tmp_path):
    invocation = _invocation(tmp_path)
    argv = invocation["argv"]
    assert argv[:4] == ["/reviewed/bin/codex", "exec", "--skip-git-repo-check", "--json"]
    assert ["-c", "model_reasoning_effort=high"] == argv[argv.index("-c") : argv.index("-c") + 2]
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    assert invocation["project_root"] == str(tmp_path.resolve())
    assert invocation["cwd"] == str(tmp_path.resolve())


def test_capture_required_rejects_missing_runner_and_sink_before_launch(tmp_path):
    called = False

    def runner(_request):
        nonlocal called
        called = True
        return {}

    with pytest.raises(ValueError, match="complete process_runner and durable receipt_sink"):
        execution.run_cli_invocation(
            _invocation(tmp_path), run_context=_context(tmp_path), process_runner=runner
        )
    assert called is False


def test_managed_invocation_rejects_missing_context_before_default_launch(
    tmp_path, monkeypatch
):
    calls: list[dict] = []
    monkeypatch.setattr(execution, "_default_process_runner", calls.append)

    with pytest.raises(ValueError, match="managed invocation requires run_context"):
        execution.run_cli_invocation(_invocation(tmp_path))

    assert calls == []


def test_managed_invocation_rejects_false_capture_before_custom_launch(tmp_path):
    calls: list[dict] = []

    with pytest.raises(ValueError, match="managed invocation requires capture_required true"):
        execution.run_cli_invocation(
            _invocation(tmp_path),
            run_context=_context(tmp_path, capture_required=False),
            process_runner=lambda request: calls.append(request),
            receipt_sink=lambda receipt: {},
        )

    assert calls == []


def test_managed_invocation_rejects_missing_runner_before_default_launch(
    tmp_path, monkeypatch
):
    calls: list[dict] = []
    monkeypatch.setattr(execution, "_default_process_runner", calls.append)

    with pytest.raises(ValueError, match="complete process_runner and durable receipt_sink"):
        execution.run_cli_invocation(
            _invocation(tmp_path),
            run_context=_context(tmp_path),
            receipt_sink=lambda receipt: {},
        )

    assert calls == []


def test_managed_invocation_cannot_toggle_legacy_text_io_to_bypass_capture(
    tmp_path, monkeypatch
):
    invocation = _invocation(tmp_path)
    invocation["legacy_text_io"] = True
    calls: list[dict] = []
    monkeypatch.setattr(execution, "_default_process_runner", calls.append)

    with pytest.raises(ValueError, match="legacy_text_io differs from requested_profile"):
        execution.run_cli_invocation(invocation)

    assert calls == []


def test_legacy_invocation_keeps_no_context_default_runner_compatibility(
    tmp_path, monkeypatch
):
    invocation = _legacy_invocation(tmp_path, monkeypatch)
    calls: list[dict] = []

    def runner(request):
        calls.append(request)
        return _process(request)

    monkeypatch.setattr(execution, "_default_process_runner", runner)
    outcome = execution.run_cli_invocation(invocation, result_parser=_parser)

    assert calls == [invocation]
    assert outcome["receipt"]["run_context"] is None
    assert outcome["sink_ack"] is None


def test_legacy_invocation_keeps_opt_in_capture(tmp_path, monkeypatch):
    invocation = _legacy_invocation(tmp_path, monkeypatch)
    calls: list[dict] = []
    receipts: list[dict] = []

    def runner(request):
        calls.append(request)
        return _process(request)

    outcome = execution.run_cli_invocation(
        invocation,
        run_context=_context(tmp_path),
        process_runner=runner,
        result_parser=_parser,
        receipt_sink=_ack(receipts),
    )

    assert calls == [invocation]
    assert receipts == [outcome["receipt"]]
    assert outcome["sink_ack"]["finalized"] is True


def test_run_context_must_match_invocation_cwd_before_launch(tmp_path):
    other = tmp_path / "nested"
    other.mkdir()
    context = _context(tmp_path)
    context["cwd"] = str(other.resolve())
    with pytest.raises(ValueError, match="invocation cwd differs"):
        execution.run_cli_invocation(
            _invocation(tmp_path),
            run_context=context,
            process_runner=lambda request: _process(request),
            receipt_sink=lambda receipt: {},
        )


def test_complete_empty_result_gets_durable_receipt(tmp_path):
    receipts: list[dict] = []
    outcome = execution.run_cli_invocation(
        _invocation(tmp_path),
        run_context=_context(tmp_path),
        process_runner=lambda request: _process(request),
        result_parser=_parser,
        receipt_sink=_ack(receipts),
    )
    assert outcome["value"] == {"nodes": [], "edges": [], "hyperedges": []}
    assert outcome["receipt"]["completion"] == "completed_empty"
    assert outcome["receipt"]["coverage"]["status"] == "complete"
    assert outcome["receipt"]["process"]["provider_events"] == [
        {"type": "turn.completed"}
    ]
    assert receipts == [outcome["receipt"]]


def test_run_context_rejects_unstructured_identity_records(tmp_path):
    context = _context(tmp_path)
    context["instruction_identity"] = {"sha256": "f" * 64}
    with pytest.raises(ValueError, match="instruction_identity must contain exactly"):
        execution.run_cli_invocation(
            _invocation(tmp_path), run_context=context,
            process_runner=lambda request: _process(request), receipt_sink=lambda receipt: {},
        )


def test_binary_mismatch_and_missing_response_identity_leave_coverage_unproved(tmp_path):
    receipts: list[dict] = []
    process = _process(_invocation(tmp_path))
    process["binary"]["sha256"] = "0" * 64

    def parser(_process):
        parsed = _parser(process)
        parsed["responses"] = []
        return parsed

    outcome = execution.run_cli_invocation(
        _invocation(tmp_path),
        run_context=_context(tmp_path),
        process_runner=lambda _request: process,
        result_parser=parser,
        receipt_sink=_ack(receipts),
    )
    reasons = outcome["receipt"]["coverage"]["reasons"]
    assert "binary_identity_mismatch" in reasons
    assert "response_identity_missing" in reasons
    assert outcome["receipt"]["completion"] == "completed_empty"


def test_model_outside_profile_is_retained_and_gated(tmp_path):
    receipts: list[dict] = []
    outcome = execution.run_cli_invocation(
        _invocation(tmp_path),
        run_context=_context(tmp_path),
        process_runner=lambda request: _process(request, reported_model="gpt-unselected"),
        result_parser=_parser,
        receipt_sink=_ack(receipts),
    )
    assert outcome["receipt"]["responses"][0]["reported_model"] == "gpt-unselected"
    assert "reported_model_outside_profile" in outcome["receipt"]["coverage"]["reasons"]


def test_managed_openai_extraction_uses_shared_builder_and_accepts_empty_graph(tmp_path):
    requests: list[dict] = []
    receipts: list[dict] = []

    def runner(request):
        requests.append(request)
        Path(request["output_path"]).write_text("not the retained response")
        return _process(request)

    result = llm._call_openai_cli(
        "no entities here",
        model="gpt-5.6-sol",
        effort="high",
        execution_profile=_profile(),
        run_context=_context(tmp_path),
        process_runner=runner,
        receipt_sink=_ack(receipts),
    )

    assert result["nodes"] == []
    assert result["finish_reason"] == "stop"
    assert requests[0]["prompt_contract"] == "graph_json"
    assert "model_reasoning_effort=high" in requests[0]["argv"]
    assert receipts[0]["completion"] == "completed_empty"
    assert receipts[0]["process"]["result_artifact"] == {
        "requested_path": requests[0]["output_path"],
        "byte_count": 39,
        "sha256": hashlib.sha256(b'{"nodes":[],"edges":[],"hyperedges":[]}').hexdigest(),
        "raw_ref": "raw/result",
        "eof": True,
        "finalized": True,
    }
    assert receipts[0]["coverage"] == {
        "status": "unproved",
        "reasons": ["provider_response_identity_unavailable", "response_identity_missing"],
    }


@pytest.mark.parametrize("purpose", ["label", "dedup", "triage"])
def test_managed_openai_auxiliary_call_uses_same_profile(tmp_path, purpose):
    requests: list[dict] = []
    receipts: list[dict] = []
    usage: dict = {}

    def runner(request):
        requests.append(request)
        return _process(request, payload=b"A concise label")

    text = llm._call_llm(
        "name this cluster",
        backend="openai-cli",
        model="gpt-5.6-sol",
        effort="high",
        usage_out=usage,
        execution_profile=_profile(),
        run_context=_context(tmp_path),
        process_runner=runner,
        receipt_sink=_ack(receipts),
        purpose=purpose,
    )

    assert text == "A concise label"
    assert requests[0]["prompt_contract"] == "plain_text"
    assert requests[0]["requested_profile"]["model"] == "gpt-5.6-sol"
    assert requests[0]["requested_profile"]["effort"] == "high"
    assert requests[0]["purpose"] == purpose
    assert usage["_execution_receipts"] == receipts


@pytest.mark.parametrize(
    ("event", "expected", "counts"),
    [
        ({"type": "turn.completed"}, (False, False), (0, 0)),
        ({"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}},
         (True, True), (0, 0)),
        ({"type": "turn.completed", "usage": {"input_tokens": 7}},
         (True, False), (7, 0)),
        ({"type": "turn.completed", "usage": {"input_tokens": "7", "output_tokens": 0}},
         (False, True), (0, 0)),
        ({"type": "turn.completed", "usage": {"input_tokens": True, "output_tokens": -2}},
         (False, False), (0, 0)),
    ],
)
def test_managed_codex_receipt_distinguishes_unknown_usage_from_zero(
    tmp_path, event, expected, counts
):
    receipts: list[dict] = []
    aggregate: dict = {}

    def runner(request):
        process = _process(request, payload=b"A concise label")
        process["stdout"] = (json.dumps(event) + "\n").encode()
        return process

    llm._call_llm(
        "name this cluster", backend="openai-cli", model="gpt-5.6-sol",
        effort="high", execution_profile=_profile(), run_context=_context(tmp_path),
        process_runner=runner, receipt_sink=_ack(receipts), purpose="label",
        usage_out=aggregate,
    )

    usage = receipts[0]["usage"]
    assert (usage["input_tokens_known"], usage["output_tokens_known"]) == expected
    assert (usage["input_tokens"], usage["output_tokens"]) == counts
    assert (aggregate["input_tokens_known"], aggregate["output_tokens_known"]) == expected


@pytest.mark.parametrize("reported_usage, known", [(None, False), ({"input_tokens": 0, "output_tokens": 0}, True)])
def test_managed_claude_receipt_distinguishes_unknown_usage_from_zero(
    tmp_path, reported_usage, known
):
    receipts: list[dict] = []

    def runner(request):
        process = _process(request, reported_model="claude-opus-4-1")
        envelope = {"type": "result", "result": "A concise label"}
        if reported_usage is not None:
            envelope["usage"] = reported_usage
        process["stdout"] = json.dumps(envelope).encode()
        return process

    llm._call_llm(
        "name this cluster", backend="claude-cli", model="claude-opus-4-1",
        effort="high", execution_profile=_profile("claude-cli"),
        run_context=_context(tmp_path), process_runner=runner,
        receipt_sink=_ack(receipts), purpose="label",
    )

    usage = receipts[0]["usage"]
    assert usage["input_tokens"] == usage["output_tokens"] == 0
    assert usage["input_tokens_known"] is known
    assert usage["output_tokens_known"] is known


@pytest.mark.parametrize("backend", ["openai-cli", "claude-cli"])
def test_managed_extraction_retains_usage_certainty_in_receipt(tmp_path, backend):
    receipts: list[dict] = []

    def runner(request):
        process = _process(request)
        if backend == "claude-cli":
            process["stdout"] = json.dumps({
                "type": "result",
                "structured_output": {"nodes": [], "edges": [], "hyperedges": []},
            }).encode()
        return process

    outcome = llm._managed_cli_call(
        "extract this", backend=backend, purpose="extract", max_tokens=200,
        deep_mode=False, images=None, model=_profile(backend)["model"],
        effort="high", execution_profile=_profile(backend),
        run_context=_context(tmp_path), process_runner=runner,
        receipt_sink=_ack(receipts),
    )

    assert outcome["receipt"] == receipts[0]
    assert outcome["receipt"]["usage"]["input_tokens_known"] is False
    assert outcome["receipt"]["usage"]["output_tokens_known"] is False


def test_chunk_usage_unknown_absorbs_known_subtotal():
    merged = {"nodes": [], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0}
    llm._merge_into(merged, {
        "input_tokens": 5, "output_tokens": 7,
        "_execution_receipts": [{"usage": {"input_tokens_known": True, "output_tokens_known": True}}],
    })
    llm._merge_into(merged, {
        "input_tokens": 0, "output_tokens": 0,
        "_execution_receipts": [{"usage": {"input_tokens_known": False, "output_tokens_known": False}}],
    })
    assert (merged["input_tokens"], merged["output_tokens"]) == (5, 7)
    assert merged["input_tokens_known"] is False
    assert merged["output_tokens_known"] is False


def _two_community_graph():
    graph = nx.Graph()
    graph.add_node("orders", label="Orders")
    graph.add_node("payments", label="Payments")
    return graph, {0: ["orders"], 1: ["payments"]}


def test_managed_partial_label_omission_raises_with_salvaged_names(monkeypatch, tmp_path):
    graph, communities = _two_community_graph()
    monkeypatch.setattr(llm, "_call_llm", lambda *args, **kwargs: '{"0": "Orders"}')

    with pytest.raises(RuntimeError, match="missing") as caught:
        llm.generate_community_labels(
            graph, communities, backend="openai-cli", execution_profile=_profile(),
            run_context=_context(tmp_path), process_runner=object(),
            receipt_sink=object(),
        )

    assert caught.value.graphify_partial_labels == {0: "Orders"}


def test_managed_partial_label_retry_failure_retains_earlier_name(monkeypatch, tmp_path):
    graph, communities = _two_community_graph()
    calls = 0

    def fake_call(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return '{"0": "Orders"}'
        raise RuntimeError("transport uncertain")

    monkeypatch.setattr(llm, "_call_llm", fake_call)
    with pytest.raises(RuntimeError, match="transport uncertain") as caught:
        llm.generate_community_labels(
            graph, communities, backend="openai-cli", execution_profile=_profile(),
            run_context=_context(tmp_path), process_runner=object(),
            receipt_sink=object(),
        )

    assert calls == 2
    assert caught.value.graphify_partial_labels == {0: "Orders"}


def test_managed_malformed_label_split_retains_successful_left_name(monkeypatch, tmp_path):
    graph, communities = _two_community_graph()
    replies = iter(['not json', '{"0": "Orders"}'])

    def fake_call(*args, **kwargs):
        try:
            return next(replies)
        except StopIteration:
            raise RuntimeError("right label call failed") from None

    monkeypatch.setattr(llm, "_call_llm", fake_call)
    with pytest.raises(RuntimeError, match="right label call failed") as caught:
        llm.generate_community_labels(
            graph, communities, backend="openai-cli", execution_profile=_profile(),
            run_context=_context(tmp_path), process_runner=object(),
            receipt_sink=object(),
        )

    assert caught.value.graphify_partial_labels == {0: "Orders"}


def test_extract_files_direct_forwards_managed_transport(monkeypatch, tmp_path):
    source = tmp_path / "source.md"
    source.write_text("hello")
    captured: dict = {}
    runner = object()
    sink = object()

    def fake_call(prompt, **kwargs):
        captured.update(prompt=prompt, **kwargs)
        return {
            "nodes": [], "edges": [], "hyperedges": [],
            "input_tokens": 0, "output_tokens": 0, "finish_reason": "stop",
        }

    monkeypatch.setattr(llm, "_call_openai_cli", fake_call)
    result = llm.extract_files_direct(
        [source], backend="openai-cli", model="gpt-5.6-sol", effort="high",
        root=tmp_path, execution_profile=_profile(), run_context=_context(tmp_path),
        process_runner=runner, receipt_sink=sink,
    )

    assert result["nodes"] == []
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["effort"] == "high"
    assert captured["execution_profile"] == _profile()
    assert captured["run_context"] == _context(tmp_path)
    assert captured["process_runner"] is runner
    assert captured["receipt_sink"] is sink


def test_generate_labels_uses_profile_and_propagates_managed_failure(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_labels(_graph, _communities, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("capture incomplete")

    monkeypatch.setattr(llm, "label_communities", fake_labels)
    monkeypatch.setattr(
        llm, "detect_backend", lambda: pytest.fail("managed labels must not auto-detect")
    )
    with pytest.raises(RuntimeError, match="capture incomplete"):
        llm.generate_community_labels(
            None, {1: ["node"]}, execution_profile=_profile(),
            run_context=_context(tmp_path), process_runner=lambda request: request,
            receipt_sink=lambda receipt: receipt,
        )
    assert captured["backend"] == "openai-cli"
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["effort"] == "high"


def test_watch_semantic_subprocess_forwards_model_and_effort(monkeypatch, tmp_path):
    from graphify.watch import _run_semantic_extract

    calls: list[list[str]] = []

    def fake_run(argv):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _run_semantic_extract(
        tmp_path, backend="openai-cli", fallback_backend="claude-cli",
        model="gpt-5.6-sol", effort="high",
    )
    assert calls == [[
        calls[0][0], "-m", "graphify", "extract", str(tmp_path),
        "--backend", "openai-cli", "--fallback-backend", "claude-cli",
        "--model", "gpt-5.6-sol", "--effort", "high",
    ]]


def test_build_forwards_dedup_model_and_effort(monkeypatch):
    build_module = importlib.import_module("graphify.build")
    dedup_module = importlib.import_module("graphify.dedup")
    captured: dict = {}

    def fake_dedup(nodes, edges, **kwargs):
        captured.update(kwargs)
        return nodes, edges

    monkeypatch.setattr(dedup_module, "deduplicate_entities", fake_dedup)
    build_module.build(
        [{"nodes": [{"id": "n1", "label": "Node", "type": "concept"}], "edges": []}],
        dedup_llm_backend="openai-cli", dedup_llm_model="gpt-5.6-sol",
        dedup_llm_effort="high",
    )
    assert captured["dedup_llm_model"] == "gpt-5.6-sol"
    assert captured["dedup_llm_effort"] == "high"


def test_build_dedup_false_skips_profile_paid_stage(monkeypatch):
    build_module = importlib.import_module("graphify.build")
    dedup_module = importlib.import_module("graphify.dedup")
    monkeypatch.setattr(
        dedup_module,
        "deduplicate_entities",
        lambda *_args, **_kwargs: pytest.fail("dedup=False must skip the paid stage"),
    )
    graph = build_module.build(
        [{"nodes": [{"id": "n1", "label": "Node", "type": "concept"}], "edges": []}],
        dedup=False,
        execution_profile=_profile(),
    )
    assert "n1" in graph


def test_triage_forwards_managed_profile_with_triage_purpose(monkeypatch, tmp_path):
    prs_module = importlib.import_module("graphify.prs")
    captured: dict = {}

    def fake_call(prompt, **kwargs):
        captured.update(prompt=prompt, **kwargs)
        return "#1 — review"

    monkeypatch.setattr(llm, "_call_llm", fake_call)
    pr = prs_module.PRInfo(
        number=1, title="Fix", branch="fix", base_branch="main", author="dev",
        is_draft=False, review_decision="", ci_status="SUCCESS",
        updated_at=datetime.now(timezone.utc),
    )
    prs_module.triage_with_opus(
        [pr], "main", execution_profile=_profile(), run_context=_context(tmp_path),
        process_runner=lambda request: request, receipt_sink=lambda receipt: receipt,
    )
    assert captured["backend"] == "openai-cli"
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["effort"] == "high"
    assert captured["purpose"] == "triage"


def test_managed_triage_propagates_attempt_failure(monkeypatch, tmp_path):
    prs_module = importlib.import_module("graphify.prs")
    attempt = {"receipt": {"receipt_id": "triage-attempt"}}

    def failed_call(*_args, **_kwargs):
        error = RuntimeError("triage capture failed")
        error.graphify_attempt = attempt
        raise error

    monkeypatch.setattr(llm, "_call_llm", failed_call)
    pr = prs_module.PRInfo(
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
    with pytest.raises(RuntimeError, match="triage capture failed") as caught:
        prs_module.triage_with_opus(
            [pr],
            "main",
            execution_profile=_profile(),
            run_context=_context(tmp_path),
            process_runner=lambda request: request,
            receipt_sink=lambda receipt: receipt,
        )
    assert caught.value.graphify_attempt is attempt


def test_profile_only_dedup_selects_profile_backend(monkeypatch, tmp_path):
    dedup_module = importlib.import_module("graphify.dedup")
    captured: dict = {}

    monkeypatch.setattr(
        dedup_module,
        "_llm_tiebreak",
        lambda *_args, **kwargs: captured.update(kwargs),
    )
    nodes = [
        {"id": "one", "label": "First concept", "file_type": "concept"},
        {"id": "two", "label": "Second concept", "file_type": "concept"},
    ]
    dedup_module.deduplicate_entities(
        nodes,
        [],
        communities={},
        execution_profile=_profile(),
        run_context=_context(tmp_path),
    )
    assert captured["backend"] == "openai-cli"
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["effort"] == "high"


def test_profile_only_dedup_with_no_ambiguous_pair_makes_no_paid_call(
    monkeypatch, tmp_path
):
    dedup_module = importlib.import_module("graphify.dedup")
    monkeypatch.setattr(
        llm, "_call_llm", lambda *_args, **_kwargs: pytest.fail("no paid call expected")
    )
    nodes = [
        {"id": "one", "label": "Completely unrelated", "file_type": "concept"},
        {"id": "two", "label": "Different", "file_type": "concept"},
    ]
    dedup_module.deduplicate_entities(
        nodes,
        [],
        communities={},
        execution_profile=_profile(),
        run_context=_context(tmp_path),
    )


def test_managed_dedup_propagates_attempt_failure(monkeypatch, tmp_path):
    dedup_module = importlib.import_module("graphify.dedup")
    monkeypatch.setattr(
        dedup_module,
        "JaroWinkler",
        SimpleNamespace(normalized_similarity=lambda *_args: 0.8),
    )
    for name in (
        "_is_variant_pair",
        "_short_label_blocked",
        "_numeric_tokens_differ",
        "_content_token_swap",
        "_crossfile_fileanchored_blocked",
    ):
        monkeypatch.setattr(dedup_module, name, lambda *_args: False)
    attempt = {"receipt": {"receipt_id": "dedup-attempt"}}

    def failed_call(*_args, **_kwargs):
        error = RuntimeError("dedup capture failed")
        error.graphify_attempt = attempt
        raise error

    monkeypatch.setattr(llm, "_call_llm", failed_call)
    candidates = [
        {"id": "one", "label": "Alpha service", "file_type": "concept"},
        {"id": "two", "label": "Beta service", "file_type": "concept"},
    ]
    with pytest.raises(RuntimeError, match="dedup capture failed") as caught:
        dedup_module._llm_tiebreak(
            candidates,
            dedup_module._UF(),
            {},
            backend="openai-cli",
            execution_profile=_profile(),
            run_context=_context(tmp_path),
        )
    assert caught.value.graphify_attempt is attempt


def test_managed_legacy_mcp_suppression_fails_closed_before_runner(tmp_path):
    profile = _profile()
    profile["cli_policy"]["mcp"] = "legacy-disable"
    called = False

    def runner(_request):
        nonlocal called
        called = True

    with pytest.raises(RuntimeError, match="complete reviewed server set"):
        llm._call_openai_cli(
            "prompt",
            execution_profile=profile,
            run_context=_context(tmp_path),
            process_runner=runner,
            receipt_sink=lambda receipt: {},
        )
    assert called is False


def test_managed_capture_false_fails_before_temp_or_runner(monkeypatch, tmp_path):
    monkeypatch.setattr(
        tempfile,
        "NamedTemporaryFile",
        lambda **_kwargs: pytest.fail("managed preflight must precede temp allocation"),
    )
    with pytest.raises(ValueError, match="capture_required=true"):
        llm._call_openai_cli(
            "prompt",
            execution_profile=_profile(),
            run_context=_context(tmp_path, capture_required=False),
            process_runner=lambda _request: pytest.fail("runner must not be called"),
            receipt_sink=lambda _receipt: pytest.fail("sink must not be called"),
        )


def test_malformed_managed_context_fails_before_temp_allocation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        tempfile,
        "NamedTemporaryFile",
        lambda **_kwargs: pytest.fail("context validation must precede temp allocation"),
    )
    context = _context(tmp_path)
    del context["instruction_identity"]
    with pytest.raises(ValueError, match="run_context fields differ"):
        llm._call_openai_cli(
            "prompt",
            execution_profile=_profile(),
            run_context=context,
            process_runner=lambda _request: pytest.fail("runner must not be called"),
            receipt_sink=lambda _receipt: pytest.fail("sink must not be called"),
        )


def test_managed_context_requires_existing_root_and_cwd(tmp_path):
    context = _context(tmp_path)
    context["cwd"] = str(tmp_path / "missing")
    with pytest.raises(ValueError, match="existing directories"):
        execution.validate_run_context(context)

    profile = execution.resolve_execution_profile(
        None, None, None, execution_profile=_profile(), purpose="extract", environment={}
    )
    with pytest.raises(ValueError, match="existing directories"):
        execution.build_cli_invocation(
            "prompt",
            purpose="extract",
            max_tokens=1,
            profile=profile,
            output_path=tmp_path / "answer.json",
            project_root=tmp_path,
            cwd=tmp_path / "missing",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("byte_count", True, "byte_count must be an integer"),
        ("byte_count", 1, "byte_count does not match"),
        ("sha256", "0" * 64, "sha256 does not match"),
        ("requested_path", "/wrong/path", "requested_path differs"),
        ("raw_ref", "", "raw_ref must be a non-empty"),
        ("eof", False, "eof and finalized true"),
        ("finalized", False, "eof and finalized true"),
        ("payload", {"not": "bytes"}, "payload must be bytes"),
    ],
)
def test_managed_result_artifact_rejects_invalid_metadata_and_retains_original(
    tmp_path, field, value, message
):
    receipts: list[dict] = []
    invocation = _invocation(tmp_path)
    process = _process(invocation)
    process["result_artifact"][field] = value

    with pytest.raises((TypeError, ValueError), match=message) as caught:
        execution.run_cli_invocation(
            invocation,
            run_context=_context(tmp_path),
            process_runner=lambda _request: process,
            result_parser=_parser,
            receipt_sink=_ack(receipts),
        )
    attempt = caught.value.graphify_attempt
    assert attempt["process"] is process
    assert attempt["process"]["result_artifact"][field] == value
    assert "payload" not in attempt["receipt"]["process"]["result_artifact"]
    assert json.loads(json.dumps(attempt["receipt"])) == attempt["receipt"]
    assert receipts == [attempt["receipt"]]


def test_managed_result_artifact_missing_is_receipted_and_retained(tmp_path):
    receipts: list[dict] = []
    invocation = _invocation(tmp_path)
    process = _process(invocation)
    del process["result_artifact"]
    with pytest.raises(ValueError, match="result_artifact fields differ") as caught:
        execution.run_cli_invocation(
            invocation,
            run_context=_context(tmp_path),
            process_runner=lambda _request: process,
            receipt_sink=_ack(receipts),
        )
    assert caught.value.graphify_attempt["process"] is process
    assert caught.value.graphify_attempt["receipt"]["process"]["result_artifact"] is None


def test_managed_codex_invalid_utf8_retains_artifact_evidence(tmp_path):
    receipts: list[dict] = []
    with pytest.raises(UnicodeDecodeError) as caught:
        llm._call_openai_cli(
            "prompt",
            execution_profile=_profile(),
            run_context=_context(tmp_path),
            process_runner=lambda request: _process(request, payload=b"\xff"),
            receipt_sink=_ack(receipts),
        )
    attempt = caught.value.graphify_attempt
    assert attempt["process"]["result_artifact"]["payload"] == b"\xff"
    assert attempt["receipt"]["process"]["result_artifact"]["sha256"] == hashlib.sha256(
        b"\xff"
    ).hexdigest()


def test_codex_temp_is_removed_when_builder_fails(monkeypatch, tmp_path):
    paths: list[Path] = []
    original = tempfile.NamedTemporaryFile

    def tracked_temp(**kwargs):
        handle = original(dir=tmp_path, **kwargs)
        paths.append(Path(handle.name))
        return handle

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", tracked_temp)
    monkeypatch.setattr(
        llm, "build_cli_invocation", lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("builder failed")
        )
    )
    with pytest.raises(RuntimeError, match="builder failed"):
        llm._call_openai_cli(
            "prompt",
            execution_profile=_profile(),
            run_context=_context(tmp_path),
            process_runner=lambda request: _process(request),
            receipt_sink=_ack([]),
        )
    assert len(paths) == 1
    assert not paths[0].exists()


def test_legacy_codex_temp_is_removed_when_binary_is_missing(monkeypatch, tmp_path):
    paths: list[Path] = []
    original = tempfile.NamedTemporaryFile

    def tracked_temp(**kwargs):
        handle = original(dir=tmp_path, **kwargs)
        paths.append(Path(handle.name))
        return handle

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", tracked_temp)
    monkeypatch.setattr(llm, "_codex_disable_mcp_args", lambda *_args: [])
    monkeypatch.setattr(shutil, "which", lambda _binary: None)
    with pytest.raises(RuntimeError, match="OpenAI Codex CLI not found"):
        llm._call_openai_cli("prompt")
    assert len(paths) == 1
    assert not paths[0].exists()


def test_invalid_runner_result_is_receipted_before_error(tmp_path):
    receipts: list[dict] = []
    with pytest.raises(ValueError, match="missing fields") as caught:
        execution.run_cli_invocation(
            _invocation(tmp_path),
            run_context=_context(tmp_path),
            process_runner=lambda _request: {"returncode": 0},
            receipt_sink=_ack(receipts),
        )
    attempt = caught.value.graphify_attempt
    assert attempt["receipt"]["completion"] == "incomplete_capture"
    assert attempt["receipt"]["process"]["runner_error"] == "invalid_runner_result"
    assert receipts == [attempt["receipt"]]


def test_sink_failure_preserves_the_exact_pre_ack_receipt(tmp_path):
    captured = None

    def sink(receipt):
        nonlocal captured
        captured = receipt
        raise OSError("disk unavailable")

    with pytest.raises(RuntimeError, match="durable receipt sink failed") as caught:
        execution.run_cli_invocation(
            _invocation(tmp_path),
            run_context=_context(tmp_path),
            process_runner=lambda request: _process(request),
            result_parser=_parser,
            receipt_sink=sink,
        )
    attempt = caught.value.graphify_attempt
    assert attempt["receipt"] == captured
    assert "sink_error" not in attempt["receipt"]
    unsigned = dict(captured)
    receipt_id = unsigned.pop("receipt_id")
    assert receipt_id == execution._digest(unsigned)
    assert attempt["sink_failure"] == "OSError: disk unavailable"


def test_runner_cancellation_is_receipted_before_propagation(tmp_path):
    receipts: list[dict] = []

    def cancel(_request):
        raise KeyboardInterrupt("cancel")

    with pytest.raises(KeyboardInterrupt) as caught:
        execution.run_cli_invocation(
            _invocation(tmp_path), run_context=_context(tmp_path),
            process_runner=cancel, receipt_sink=_ack(receipts),
        )
    attempt = caught.value.graphify_attempt
    assert attempt["receipt"]["completion"] == "incomplete_capture"
    assert attempt["receipt"]["process"]["runner_error"] == "KeyboardInterrupt: cancel"
    assert receipts == [attempt["receipt"]]


@pytest.mark.parametrize(
    ("receipts", "cache_ancestry", "expected"),
    [
        ([], [], "none"),
        (
            [{"completion": "failed_before_response", "coverage": {"status": "complete"}}],
            [],
            "none",
        ),
        ([{"completion": "partial", "coverage": {"status": "unproved"}}], [], "usable"),
        (
            [{"completion": "incomplete_capture", "coverage": {"status": "unproved"}}],
            [],
            "uncertain",
        ),
        ([], [{"producer_receipt_id": "prior"}], "usable"),
    ],
)
def test_paid_work_state_blocks_fallback_after_usable_or_uncertain_work(
    receipts, cache_ancestry, expected
):
    assert execution.paid_work_state(receipts, cache_ancestry) == expected


def test_receipt_digest_uses_canonical_bytes():
    left = {"a": 1, "b": [2, 3]}
    raw = json.dumps(left, sort_keys=True, separators=(",", ":")).encode()
    assert execution._digest(left) == hashlib.sha256(raw).hexdigest()


def _producer_receipt(receipt_id: str = "receipt-1", completion: str = "completed") -> dict:
    return {"receipt_id": receipt_id, "completion": completion}


def test_semantic_cache_is_namespaced_by_profile_and_retains_producer(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("hello")
    context = _context(tmp_path)
    profile = _profile()
    node = {"id": "n1", "label": "Hello", "source_file": str(source)}
    producer = _producer_receipt()

    assert (
        cache.save_semantic_cache(
            [node],
            [],
            root=tmp_path,
            cache_root=tmp_path,
            allowed_source_files=[source],
            prompt="extract-v1",
            execution_profile=profile,
            run_context=context,
            producer_receipt=producer,
        )
        == 1
    )

    evidence: list[dict] = []
    nodes, edges, hyperedges, uncached = cache.check_semantic_cache(
        [str(source)],
        root=tmp_path,
        cache_root=tmp_path,
        prompt="extract-v1",
        execution_profile=profile,
        run_context=context,
        cache_evidence_out=evidence,
    )
    assert ([item["id"] for item in nodes], edges, hyperedges, uncached) == (
        ["n1"],
        [],
        [],
        [],
    )
    assert evidence[0]["producer_receipts"] == [producer]

    changed_profile = _profile()
    changed_profile["model"] = "gpt-5.6-sol-revised"
    changed_profile["identity_policy"]["allowed_reported_models"] = ["gpt-5.6-sol-revised"]
    assert cache.check_semantic_cache(
        [str(source)],
        root=tmp_path,
        cache_root=tmp_path,
        prompt="extract-v1",
        execution_profile=changed_profile,
        run_context=context,
    )[3] == [str(source)]


def test_explicit_profile_does_not_replay_legacy_semantic_cache(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("hello")
    node = {"id": "n1", "label": "Hello", "source_file": str(source)}
    cache.save_semantic_cache([node], [], root=tmp_path, cache_root=tmp_path, prompt="extract-v1")
    assert cache.check_semantic_cache(
        [str(source)],
        root=tmp_path,
        cache_root=tmp_path,
        prompt="extract-v1",
        execution_profile=_profile(),
        run_context=_context(tmp_path),
    )[3] == [str(source)]


def test_cache_profile_conflict_fails_before_cache_lookup(monkeypatch, tmp_path):
    source = tmp_path / "source.md"
    source.write_text("hello")
    monkeypatch.setenv("GRAPHIFY_OPENAI_CLI_MODEL", "gpt-other")
    with pytest.raises(ValueError, match="conflicting explicit model selectors"):
        cache.check_semantic_cache(
            [str(source)], root=tmp_path, cache_root=tmp_path,
            execution_profile=_profile(), run_context=_context(tmp_path),
        )
    assert not (tmp_path / "graphify-out" / "cache").exists()


def test_completed_empty_managed_extraction_can_be_cached(tmp_path):
    source = tmp_path / "empty.md"
    source.write_text("no extractable entities")
    producer = _producer_receipt(completion="completed_empty")
    kwargs = {
        "root": tmp_path,
        "cache_root": tmp_path,
        "prompt": "extract-v1",
        "execution_profile": _profile(),
        "run_context": _context(tmp_path),
    }
    assert (
        cache.save_semantic_cache(
            [],
            [],
            allowed_source_files=[source],
            producer_receipt=producer,
            **kwargs,
        )
        == 1
    )
    evidence: list[dict] = []
    result = cache.check_semantic_cache([str(source)], cache_evidence_out=evidence, **kwargs)
    assert result == ([], [], [], [])
    assert evidence[0]["producer_receipts"] == [producer]


def test_partial_producer_is_retained_but_not_served_as_complete(tmp_path):
    source = tmp_path / "partial.md"
    source.write_text("partial")
    kwargs = {
        "root": tmp_path, "cache_root": tmp_path, "execution_profile": _profile(),
        "run_context": _context(tmp_path),
    }
    assert cache.save_semantic_cache(
        [{"id": "n1", "label": "Partial", "source_file": str(source)}], [],
        allowed_source_files=[source], producer_receipt=_producer_receipt(completion="partial"),
        **kwargs,
    ) == 1
    assert cache.check_semantic_cache([str(source)], **kwargs)[3] == [str(source)]


def test_adaptive_retry_preserves_left_leaf_when_right_leaf_fails(monkeypatch, tmp_path):
    calls = 0

    def fake_extract(files, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "nodes": [{"id": "initial"}],
                "edges": [],
                "hyperedges": [],
                "input_tokens": 1,
                "output_tokens": 1,
                "finish_reason": "length",
                "_execution_receipts": [_producer_receipt("initial", "partial")],
            }
        if calls == 2:
            return {
                "nodes": [{"id": "left"}],
                "edges": [],
                "hyperedges": [],
                "input_tokens": 2,
                "output_tokens": 3,
                "finish_reason": "stop",
                "_execution_receipts": [_producer_receipt("left")],
            }
        error = RuntimeError("right failed")
        error.graphify_attempt = {"receipt": _producer_receipt("right", "failed")}
        raise error

    monkeypatch.setattr(llm, "extract_files_direct", fake_extract)
    with pytest.raises(RuntimeError, match="right failed") as caught:
        llm._extract_with_adaptive_retry(
            [tmp_path / "left.md", tmp_path / "right.md"],
            "openai-cli",
            None,
            "gpt-5.6-sol",
            tmp_path,
            2,
        )
    partial = caught.value.graphify_partial_result
    assert [node["id"] for node in partial["nodes"]] == ["left"]
    assert [receipt["receipt_id"] for receipt in partial["_execution_receipts"]] == [
        "initial",
        "left",
        "right",
    ]
    assert partial["input_tokens"] == 3
    assert partial["output_tokens"] == 4
    assert str(tmp_path / "right.md") in partial["_partial_files"]


def test_corpus_merge_retains_partial_result_from_failed_chunk(monkeypatch, tmp_path):
    partial = {
        "nodes": [{"id": "left"}],
        "edges": [],
        "hyperedges": [],
        "input_tokens": 2,
        "output_tokens": 3,
        "_execution_receipts": [_producer_receipt("left")],
        "_partial_files": [str(tmp_path / "right.md")],
    }

    def failed_chunk(*_args, **_kwargs):
        error = RuntimeError("right failed")
        error.graphify_partial_result = partial
        raise error

    monkeypatch.setattr(llm, "_extract_with_adaptive_retry", failed_chunk)
    result = llm.extract_corpus_parallel(
        [tmp_path / "left.md", tmp_path / "right.md"],
        backend="openai-cli",
        root=tmp_path,
        token_budget=None,
        chunk_size=2,
    )
    assert result["failed_chunks"] == 1
    assert [node["id"] for node in result["nodes"]] == ["left"]
    assert result["_execution_receipts"] == [_producer_receipt("left")]


def test_adaptive_retry_preserves_success_before_cancellation(monkeypatch, tmp_path):
    calls = 0

    def fake_extract(files, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "nodes": [], "edges": [], "hyperedges": [],
                "input_tokens": 1, "output_tokens": 1, "finish_reason": "length",
                "_execution_receipts": [_producer_receipt("initial", "partial")],
            }
        if calls == 2:
            return {
                "nodes": [{"id": "left"}], "edges": [], "hyperedges": [],
                "input_tokens": 2, "output_tokens": 3, "finish_reason": "stop",
                "_execution_receipts": [_producer_receipt("left")],
            }
        error = KeyboardInterrupt("cancelled")
        error.graphify_attempt = {"receipt": _producer_receipt("cancel", "cancelled")}
        raise error

    monkeypatch.setattr(llm, "extract_files_direct", fake_extract)
    with pytest.raises(KeyboardInterrupt) as caught:
        llm._extract_with_adaptive_retry(
            [tmp_path / "left.md", tmp_path / "right.md"],
            "openai-cli", None, "gpt-5.6-sol", tmp_path, 2,
        )
    partial = caught.value.graphify_partial_result
    assert [node["id"] for node in partial["nodes"]] == ["left"]
    assert [receipt["receipt_id"] for receipt in partial["_execution_receipts"]] == [
        "initial", "left", "cancel",
    ]


def test_corpus_preserves_prior_chunk_before_cancellation(monkeypatch, tmp_path):
    calls = 0

    def fake_retry(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "nodes": [{"id": "first"}], "edges": [], "hyperedges": [],
                "input_tokens": 2, "output_tokens": 3,
                "_execution_receipts": [_producer_receipt("first")],
            }
        error = KeyboardInterrupt("cancelled")
        error.graphify_attempt = {"receipt": _producer_receipt("cancel", "cancelled")}
        raise error

    monkeypatch.setattr(llm, "_extract_with_adaptive_retry", fake_retry)
    with pytest.raises(KeyboardInterrupt) as caught:
        llm.extract_corpus_parallel(
            [tmp_path / "first.md", tmp_path / "second.md"],
            backend="openai-cli", root=tmp_path, token_budget=None,
            chunk_size=1, max_concurrency=1,
        )
    partial = caught.value.graphify_partial_result
    assert [node["id"] for node in partial["nodes"]] == ["first"]
    assert [receipt["receipt_id"] for receipt in partial["_execution_receipts"]] == [
        "first", "cancel",
    ]
