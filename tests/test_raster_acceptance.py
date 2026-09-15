"""Independent acceptance controls for raster staging and public cache flow."""

from __future__ import annotations

import hashlib
import json
import shutil
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pytest

import graphify.__main__ as mainmod
from graphify import execution, llm, raster


_RASTER_FIXTURE = Path(__file__).parent / "fixtures/raster/alpha.png"


@pytest.fixture
def synthetic_cli_binaries(monkeypatch):
    real_which = shutil.which
    synthetic = {"codex": "/test-bin/codex", "claude": "/test-bin/claude"}
    monkeypatch.setattr(
        execution.shutil,
        "which",
        lambda name: synthetic.get(name) or real_which(name),
    )


def _run_extract(
    monkeypatch,
    corpus: Path,
    out: Path,
    *,
    backend: str,
    fallback_backend: str | None = None,
) -> None:
    argv = ["graphify", "extract", str(corpus), "--backend", backend, "--out", str(out)]
    if fallback_backend is not None:
        argv.extend(["--fallback-backend", fallback_backend])
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(mainmod.sys, "argv", argv)
    try:
        mainmod.main()
    except SystemExit as exc:
        assert exc.code in (None, 0), f"unexpected exit code {exc.code}"


def _managed_profile(backend: str = "claude-cli") -> dict:
    return {
        "schema_version": 1,
        "backend": backend,
        "model": "claude-test",
        "effort": "high",
        "binary_expectation": {
            "path": "/reviewed/claude",
            "sha256": "d" * 64,
            "version": "reviewed",
        },
        "cli_policy": {
            "project_configuration": "inherit",
            "session_persistence": "retain",
            "mcp": "inherit",
            "sandbox": "read-only",
        },
        "identity_policy": {
            "required_per_response": True,
            "allowed_reported_models": ["claude-test"],
        },
    }


def _run_context(root: Path) -> dict:
    resolved = str(root.resolve())
    return {
        "schema_version": 1,
        "run_id": "acceptance-run",
        "stage_id": "acceptance-stage",
        "project_root": resolved,
        "cwd": resolved,
        "source_identity": {"algorithm": "sha256", "digest": "a" * 64, "scope": ["."]},
        "prompt_identity": {"algorithm": "sha256", "digest": "b" * 64},
        "extractor_identity": {"name": "graphify", "version": "test", "digest": "c" * 64},
        "configuration_identity": {"digest": "d" * 64, "sources": ["config"]},
        "instruction_identity": {"digest": "e" * 64, "sources": ["instructions"]},
        "parent_receipts": [],
        "cache_ancestry": [],
        "capture_required": True,
    }


def _durable_stager(source: Path, snapshot_root: Path):
    def stage(request: dict) -> dict:
        original = Path(request["source_record"]["original_source"]["canonical_path"])
        assert original == source.resolve()
        staged = snapshot_root / "managed-image.png"
        shutil.copyfile(original, staged)
        payload = staged.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        return {
            "schema_version": 1,
            "status": "complete",
            "raw_ref": "raw:managed-image",
            "receipt_ref": "receipt:managed-image",
            "transport_path": str(staged.resolve()),
            "source_sha256": digest,
            "source_byte_count": len(payload),
            "staged_sha256": digest,
            "staged_byte_count": len(payload),
            "finalized": True,
        }

    return stage


def _receipt_sink(receipts: list[dict]):
    def sink(receipt: dict) -> dict:
        receipts.append(deepcopy(receipt))
        return {
            "receipt_id": receipt["receipt_id"],
            "sha256": execution._digest(receipt),
            "finalized": True,
            "durable_ref": f"receipts/{receipt['receipt_id']}.json",
        }

    return sink


def _failed_before_response() -> RuntimeError:
    error = RuntimeError("synthetic pre-response failure")
    error.graphify_attempt = {
        "receipt": {
            "receipt_id": "synthetic-pre-response",
            "completion": "failed_before_response",
            "coverage": {"status": "complete", "reasons": []},
        }
    }
    return error


def test_public_raster_cache_cold_then_warm_uses_one_extraction(monkeypatch, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "valid.png"
    shutil.copyfile(_RASTER_FIXTURE, source)
    out = tmp_path / "out"
    calls: list[dict] = []

    def extract(paths, **kwargs):
        calls.append({"paths": list(paths), "compatibility": kwargs["attachment_compatibility"]})
        callback = kwargs.get("on_chunk_done")
        result = {
            "nodes": [
                {
                    "id": "cold-image",
                    "label": "cold image",
                    "type": "concept",
                    "source_file": "valid.png",
                }
            ],
            "edges": [],
            "hyperedges": [],
            "input_tokens": 0,
            "output_tokens": 0,
        }
        if callback is not None:
            callback(0, 1, result)
        return result

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    monkeypatch.setattr(llm, "extract_corpus_parallel", extract)
    _run_extract(monkeypatch, corpus, out, backend="claude")

    assert len(calls) == 1
    assert calls[0]["paths"] == [source]
    assert calls[0]["compatibility"]

    monkeypatch.setattr(
        llm,
        "extract_corpus_parallel",
        lambda *_args, **_kwargs: pytest.fail("warm cache hit must skip extraction"),
    )
    _run_extract(monkeypatch, corpus, out, backend="claude")

    graph = out / "graphify-out/graph.json"
    assert graph.is_file()
    assert "cold-image" in graph.read_text(encoding="utf-8")


def test_managed_claude_relative_raster_uses_staged_prompt_and_add_dir(monkeypatch, tmp_path):
    source = tmp_path / "source.png"
    shutil.copyfile(_RASTER_FIXTURE, source)
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    staged = snapshot_root / "managed-image.png"
    requests: list[dict] = []
    receipts: list[dict] = []

    def runner(request: dict) -> dict:
        requests.append(deepcopy(request))
        argv = request["argv"]
        add_dir = argv.index("--add-dir")
        assert argv[add_dir + 1] == str(snapshot_root.resolve())
        assert str(staged.resolve()).encode() in request["stdin"]
        assert str(source.resolve()).encode() not in request["stdin"]
        assert request["attachments"][0]["staged_bytes"]["transport_path"] == str(staged.resolve())
        graph = json.dumps({"nodes": [], "edges": [], "hyperedges": []})
        stdout = json.dumps(
            {
                "type": "result",
                "result": graph,
                "is_error": False,
                "usage": {},
                "modelUsage": {"claude-test": {}},
            }
        ).encode()
        return {
            "returncode": 0,
            "stdout": stdout,
            "stderr": b"",
            "stdout_eof": True,
            "stderr_eof": True,
            "finalized": True,
            "binary": dict(request["requested_profile"]["binary_expectation"]),
            "raw_capture_refs": {"stdout": "raw/stdout", "stderr": "raw/stderr"},
            "provider_events": [],
        }

    result = llm.extract_files_direct(
        [Path("source.png")],
        backend="claude-cli",
        root=tmp_path,
        execution_profile=_managed_profile(),
        run_context=_run_context(tmp_path),
        process_runner=runner,
        receipt_sink=_receipt_sink(receipts),
        attachment_stager=_durable_stager(source, snapshot_root),
        attachment_snapshot_root=snapshot_root,
    )

    assert len(requests) == len(receipts) == 1
    assert result["_raster_evidence"]["attachments"][0]["storage_mode"] == "durable"
    assert result["_execution_receipts"][0]["receipt_id"] == receipts[0]["receipt_id"]


def test_zero_success_fallback_reuses_original_raster_stage(
    synthetic_cli_binaries, monkeypatch, tmp_path
):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "valid.png"
    shutil.copyfile(_RASTER_FIXTURE, source)
    out = tmp_path / "out"
    primary: list[dict] = []
    fallback: list[dict] = []

    def fail_primary(_prompt, **kwargs):
        attachment = deepcopy(kwargs["prepared_attachments"][0])
        assert Path(attachment["staged_bytes"]["transport_path"]).is_file()
        primary.append(attachment)
        raise _failed_before_response()

    def succeed_fallback(_prompt, **kwargs):
        assert kwargs["prepared_attachments"], "fallback must reuse the admitted raster stage"
        attachment = deepcopy(kwargs["prepared_attachments"][0])
        assert Path(attachment["staged_bytes"]["transport_path"]).is_file()
        fallback.append(attachment)
        return {
            "nodes": [
                {
                    "id": "fallback-image",
                    "label": "fallback image",
                    "type": "concept",
                    "source_file": "valid.png",
                }
            ],
            "edges": [],
            "hyperedges": [],
            "input_tokens": 0,
            "output_tokens": 0,
            "finish_reason": "stop",
        }

    monkeypatch.setattr(llm, "_call_openai_cli", fail_primary)
    monkeypatch.setattr(llm, "_call_claude_cli", succeed_fallback)
    _run_extract(
        monkeypatch,
        corpus,
        out,
        backend="openai-cli",
        fallback_backend="claude-cli",
    )

    assert len(primary) == len(fallback) == 1
    assert (
        fallback[0]["staged_bytes"]["transport_path"]
        == primary[0]["staged_bytes"]["transport_path"]
    )
    assert fallback[0]["original_source"] == primary[0]["original_source"]


def test_zero_success_fallback_rejects_source_change_without_restaging(
    synthetic_cli_binaries, monkeypatch, tmp_path
):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "valid.png"
    shutil.copyfile(_RASTER_FIXTURE, source)
    out = tmp_path / "out"
    stage_calls = 0
    original_stage = raster.stage_ephemeral_raster_attachments

    @contextmanager
    def count_stages(source_records, *, root):
        nonlocal stage_calls
        stage_calls += 1
        with original_stage(source_records, root=root) as attachments:
            yield attachments

    def fail_and_change_source(_prompt, **kwargs):
        assert kwargs["prepared_attachments"]
        shutil.copyfile(Path(__file__).parent / "fixtures/raster/grayscale.png", source)
        raise _failed_before_response()

    monkeypatch.setattr(raster, "stage_ephemeral_raster_attachments", count_stages)
    monkeypatch.setattr(llm, "_call_openai_cli", fail_and_change_source)
    monkeypatch.setattr(
        llm,
        "_call_claude_cli",
        lambda *_args, **_kwargs: pytest.fail("changed source must block fallback provider"),
    )
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        [
            "graphify",
            "extract",
            str(corpus),
            "--backend",
            "openai-cli",
            "--fallback-backend",
            "claude-cli",
            "--out",
            str(out),
        ],
    )

    with pytest.raises(SystemExit) as caught:
        mainmod.main()

    assert caught.value.code == 1
    assert stage_calls == 1
    assert not (out / "graphify-out/graph.json").exists()
