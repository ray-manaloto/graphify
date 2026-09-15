"""Invocation-boundary controls for staged raster attachments."""

from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from graphify import execution, raster
from graphify import llm


def _profile(backend: str) -> dict:
    binary = "codex" if backend == "openai-cli" else "claude"
    model = "gpt-test" if backend == "openai-cli" else "claude-test"
    return execution.resolve_execution_profile(
        backend,
        model,
        None,
        execution_profile=None,
        purpose="extract",
        environment={},
    ) | {"binary_expectation": {"path": f"/reviewed/{binary}", "sha256": None, "version": None}}


def _attachment(path: Path, *, label: str, storage_mode: str = "ephemeral_local") -> dict:
    staged = {
        "status": "complete",
        "transport_path": str(path),
        "sha256": "a" * 64,
        "byte_count": 12,
        "finalized": True,
    }
    lifecycle = {"scope": "request", "id": "life-1", "status": "active"}
    if storage_mode == "durable":
        staged |= {"raw_ref": "raw-1", "receipt_ref": "receipt-1"}
        lifecycle = {"scope": "caller_retained", "id": "life-1", "status": "active"}
    return {
        "schema_version": 1,
        "kind": "raster",
        "storage_mode": storage_mode,
        "original_source": {
            "label": label,
            "path": label,
            "canonical_path": f"/source/{label}",
            "sha256": "a" * 64,
            "byte_count": 12,
            "decoded_format": "PNG",
            "width": 2,
            "height": 2,
            "mode": "RGBA",
            "frame_count": 1,
            "exif_orientation": None,
        },
        "preflight": {
            "identity": {
                "name": "graphify.raster.preflight",
                "version": 1,
                "policy_sha256": "b" * 64,
            },
            "decoder": {"name": "Pillow", "version": "test"},
            "limits": {"max_bytes": 5, "max_direct_images": 20, "max_pixels": 40},
            "batch_sha256": "c" * 64,
        },
        "staged_bytes": staged,
        "lifecycle": lifecycle,
        "cli_preprocessing": {
            "status": "unobserved",
            "reported_sha256": None,
            "reported_byte_count": None,
            "reported_format": None,
            "reported_width": None,
            "reported_height": None,
            "reported_resize_mode": None,
        },
    }


def _result(*, finish_reason: str = "stop") -> dict:
    return {
        "nodes": [],
        "edges": [],
        "hyperedges": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "finish_reason": finish_reason,
    }


def _managed_profile(backend: str = "openai-cli") -> dict:
    is_codex = backend == "openai-cli"
    return {
        "schema_version": 1,
        "backend": backend,
        "model": "gpt-5.6-sol" if is_codex else "claude-test",
        "effort": "high",
        "binary_expectation": {
            "path": "/reviewed/codex" if is_codex else "/reviewed/claude",
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
            "allowed_reported_models": ["gpt-5.6-sol" if is_codex else "claude-test"],
        },
    }


def _run_context(root: Path, *, capture_required: bool = True) -> dict:
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "stage_id": "stage-1",
        "project_root": str(root.resolve()),
        "cwd": str(root.resolve()),
        "source_identity": {"algorithm": "sha256", "digest": "a" * 64, "scope": ["."]},
        "prompt_identity": {"algorithm": "sha256", "digest": "b" * 64},
        "extractor_identity": {"name": "graphify", "version": "test", "digest": "c" * 64},
        "configuration_identity": {"digest": "d" * 64, "sources": ["config"]},
        "instruction_identity": {"digest": "e" * 64, "sources": ["instructions"]},
        "parent_receipts": [],
        "cache_ancestry": [],
        "capture_required": capture_required,
    }


@pytest.fixture
def identity_attachment_validator(monkeypatch):
    monkeypatch.setattr(
        raster,
        "validate_raster_attachment_record",
        lambda record: deepcopy(record),
        raising=False,
    )


def test_codex_invocation_keeps_one_ordered_image_argument_and_manifest(
    identity_attachment_validator, tmp_path
):
    attachments = [
        _attachment(tmp_path / "safe one.png", label="one.png"),
        _attachment(tmp_path / "safe-two.png", label="two.png", storage_mode="durable"),
    ]
    invocation = execution.build_cli_invocation(
        "prompt",
        purpose="extract",
        max_tokens=10,
        attachments=attachments,
        profile=_profile("openai-cli"),
        output_path=tmp_path / "answer.json",
        project_root=tmp_path,
        cwd=tmp_path,
    )

    assert invocation["argv"].count("--image") == 1
    image_index = invocation["argv"].index("--image")
    assert image_index < invocation["argv"].index("-o")
    assert invocation["argv"][image_index + 1] == ",".join(
        item["staged_bytes"]["transport_path"] for item in attachments
    )
    assert invocation["attachments"] == attachments
    expected = json.dumps(
        attachments, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    assert invocation["attachment_digest"] == hashlib.sha256(expected).hexdigest()


def test_claude_invocation_adds_unique_staged_parent_directories(
    identity_attachment_validator, tmp_path
):
    attachments = [
        _attachment(tmp_path / "a" / "one.png", label="one.png"),
        _attachment(tmp_path / "a" / "two.png", label="two.png"),
        _attachment(tmp_path / "b" / "three.png", label="three.png"),
    ]
    invocation = execution.build_cli_invocation(
        "prompt",
        purpose="extract",
        max_tokens=10,
        attachments=attachments,
        profile=_profile("claude-cli"),
        project_root=tmp_path,
        cwd=tmp_path,
    )

    pairs = list(zip(invocation["argv"], invocation["argv"][1:]))
    assert [(arg, value) for arg, value in pairs if arg == "--add-dir"] == [
        ("--add-dir", str(tmp_path / "a")),
        ("--add-dir", str(tmp_path / "b")),
    ]
    assert "--image" not in invocation["argv"]


def test_codex_rejects_legacy_directory_attachment(identity_attachment_validator, tmp_path):
    with pytest.raises(ValueError, match="only by claude-cli"):
        execution.build_cli_invocation(
            "prompt",
            purpose="extract",
            max_tokens=10,
            attachments=[{"parent": str(tmp_path)}],
            profile=_profile("openai-cli"),
            output_path=tmp_path / "answer.json",
            project_root=tmp_path,
            cwd=tmp_path,
        )


def test_builder_uses_raster_validator_and_stores_its_copy(monkeypatch, tmp_path):
    source = _attachment(tmp_path / "one.png", label="one.png")
    normalized = deepcopy(source)
    normalized["original_source"]["label"] = "normalized.png"
    seen = []

    def validate(record):
        seen.append(deepcopy(record))
        return deepcopy(normalized)

    monkeypatch.setattr(raster, "validate_raster_attachment_record", validate, raising=False)
    invocation = execution.build_cli_invocation(
        "prompt",
        purpose="extract",
        max_tokens=10,
        attachments=[source],
        profile=_profile("openai-cli"),
        output_path=tmp_path / "answer.json",
        project_root=tmp_path,
        cwd=tmp_path,
    )
    source["original_source"]["label"] = "changed.png"

    assert seen[0]["original_source"]["label"] == "one.png"
    assert invocation["attachments"] == [normalized]


def test_builder_accepts_actual_ephemeral_record(tmp_path):
    source = tmp_path / "source.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    preflight = raster.preflight_raster_batch(
        [{"path": str(source), "label": "source.png"}], root=tmp_path
    )
    source_record = raster.raster_attachment_source_records(preflight)[0]

    with raster.stage_ephemeral_raster_attachments([source_record], root=tmp_path) as records:
        invocation = execution.build_cli_invocation(
            "prompt",
            purpose="extract",
            max_tokens=10,
            attachments=records,
            profile=_profile("openai-cli"),
            output_path=tmp_path / "answer.json",
            project_root=tmp_path,
            cwd=tmp_path,
        )

        assert invocation["attachments"] == records
        assert invocation["attachments"] is not records
        assert (
            invocation["argv"][invocation["argv"].index("--image") + 1]
            == records[0]["staged_bytes"]["transport_path"]
        )


def test_builder_rejects_cleaned_ephemeral_record(tmp_path):
    source = tmp_path / "source.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    preflight = raster.preflight_raster_batch(
        [{"path": str(source), "label": "source.png"}], root=tmp_path
    )
    source_record = raster.raster_attachment_source_records(preflight)[0]
    with raster.stage_ephemeral_raster_attachments([source_record], root=tmp_path) as records:
        cleaned = records[0]

    with pytest.raises(ValueError, match="lifecycle must be active"):
        execution.build_cli_invocation(
            "prompt",
            purpose="extract",
            max_tokens=10,
            attachments=[cleaned],
            profile=_profile("openai-cli"),
            output_path=tmp_path / "answer.json",
            project_root=tmp_path,
            cwd=tmp_path,
        )


def test_managed_rejects_injected_ephemeral_attachment_before_runner(tmp_path):
    source = tmp_path / "source.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    preflight = raster.preflight_raster_batch(
        [{"path": str(source), "label": "source.png"}], root=tmp_path
    )
    source_record = raster.raster_attachment_source_records(preflight)[0]
    with raster.stage_ephemeral_raster_attachments([source_record], root=tmp_path) as records:
        with pytest.raises(ValueError, match="durable raster attachments"):
            llm._call_openai_cli(
                "prompt",
                prepared_attachments=records,
                model="gpt-5.6-sol",
                effort="high",
                execution_profile=_managed_profile(),
                run_context=_run_context(tmp_path),
                process_runner=lambda _request: pytest.fail("runner must not be called"),
                receipt_sink=lambda _receipt: pytest.fail("sink must not be called"),
            )


def test_managed_claude_rejects_legacy_parent_attachment_before_runner(tmp_path):
    with pytest.raises(ValueError, match="durable raster attachments"):
        llm._call_claude_cli(
            "prompt",
            prepared_attachments=[{"parent": str(tmp_path)}],
            model="claude-test",
            effort="high",
            execution_profile=_managed_profile("claude-cli"),
            run_context=_run_context(tmp_path),
            process_runner=lambda _request: pytest.fail("runner must not be called"),
            receipt_sink=lambda _receipt: pytest.fail("sink must not be called"),
        )


def test_capture_required_without_profile_uses_managed_runner_and_retains_receipt(
    monkeypatch, tmp_path
):
    source = tmp_path / "source.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    preflight = raster.preflight_raster_batch(
        [{"path": str(source), "label": "source.png"}], root=tmp_path
    )
    source_record = raster.raster_attachment_source_records(preflight)[0]
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    staged = snapshot_root / "image.png"
    shutil.copyfile(source, staged)
    payload = staged.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    attachment = raster.verify_raster_snapshot_ack(
        {
            "schema_version": 1,
            "status": "complete",
            "raw_ref": "raw:image",
            "receipt_ref": "receipt:image",
            "transport_path": str(staged.resolve()),
            "source_sha256": digest,
            "source_byte_count": len(payload),
            "staged_sha256": digest,
            "staged_byte_count": len(payload),
            "finalized": True,
        },
        source_record=source_record,
        snapshot_root=snapshot_root,
    )
    receipts: list[dict] = []

    def runner(request):
        result = b'{"nodes":[],"edges":[],"hyperedges":[]}'
        return {
            "returncode": 0,
            "stdout": b'{"type":"turn.completed"}\n',
            "stderr": b"",
            "stdout_eof": True,
            "stderr_eof": True,
            "finalized": True,
            "binary": dict(request["requested_profile"]["binary_expectation"]),
            "raw_capture_refs": {"stdout": "raw/stdout", "stderr": "raw/stderr"},
            "provider_events": [{"type": "turn.completed"}],
            "reported_model": request["requested_profile"]["model"],
            "result_artifact": {
                "requested_path": request["output_path"],
                "payload": result,
                "byte_count": len(result),
                "sha256": hashlib.sha256(result).hexdigest(),
                "raw_ref": "raw/result",
                "eof": True,
                "finalized": True,
            },
        }

    def sink(receipt):
        receipts.append(receipt)
        return {
            "receipt_id": receipt["receipt_id"],
            "sha256": execution._digest(receipt),
            "finalized": True,
            "durable_ref": f"receipts/{receipt['receipt_id']}.json",
        }

    monkeypatch.setattr(
        llm,
        "_codex_disable_mcp_args",
        lambda *_args: pytest.fail("capture-required call must not probe native MCP state"),
    )
    result = llm._call_openai_cli(
        "prompt",
        prepared_attachments=[attachment],
        run_context=_run_context(tmp_path),
        process_runner=runner,
        receipt_sink=sink,
    )

    assert result["_execution_receipts"][0]["receipt_id"] == receipts[0]["receipt_id"]
    assert receipts[0]["request"]["attachments"][0]["storage_mode"] == "durable"


def test_capture_required_without_profile_rejects_missing_runner_before_auxiliary_probe(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        llm,
        "_codex_disable_mcp_args",
        lambda *_args: pytest.fail("auxiliary probe must not run"),
    )
    with pytest.raises(ValueError, match="process_runner and durable receipt_sink"):
        llm._call_openai_cli("prompt", run_context=_run_context(tmp_path))


def test_capture_required_claude_uses_strict_utf8_and_retains_failed_receipt(monkeypatch, tmp_path):
    receipts: list[dict] = []

    def runner(_request):
        return {
            "returncode": 0,
            "stdout": b"\xff",
            "stderr": b"",
            "stdout_eof": True,
            "stderr_eof": True,
            "finalized": True,
            "binary": {"path": "/reviewed/claude", "sha256": "a" * 64, "version": "test"},
            "raw_capture_refs": {"stdout": "raw/stdout", "stderr": "raw/stderr"},
            "provider_events": [],
        }

    def sink(receipt):
        receipts.append(receipt)
        return {
            "receipt_id": receipt["receipt_id"],
            "sha256": execution._digest(receipt),
            "finalized": True,
            "durable_ref": f"receipts/{receipt['receipt_id']}.json",
        }

    monkeypatch.setattr(execution.shutil, "which", lambda _name: "/reviewed/claude")
    monkeypatch.setattr(
        llm,
        "_claude_cli_supports_json_schema",
        lambda *_args: pytest.fail("capture-required call must not probe Claude CLI schema"),
    )
    with pytest.raises(UnicodeDecodeError) as caught:
        llm._call_claude_cli(
            "prompt",
            model="claude-test",
            run_context=_run_context(tmp_path),
            process_runner=runner,
            receipt_sink=sink,
        )

    assert caught.value.graphify_attempt["receipt"] == receipts[0]
    assert receipts[0]["completion"] == "incomplete_capture"
    assert "result_parser_failed" in receipts[0]["coverage"]["reasons"]


def test_capture_required_rejects_unmanaged_api_backend_before_paid_call(monkeypatch, tmp_path):
    source = tmp_path / "source.md"
    source.write_text("hello")
    monkeypatch.setattr(
        llm,
        "_call_openai_compat",
        lambda *_args, **_kwargs: pytest.fail("API backend must not be called"),
    )

    with pytest.raises(ValueError, match="registered CLI backend"):
        llm.extract_files_direct(
            [source],
            backend="deepseek",
            api_key="test-only",
            root=tmp_path,
            run_context=_run_context(tmp_path),
            process_runner=lambda _request: pytest.fail("runner must not be called"),
            receipt_sink=lambda _receipt: pytest.fail("sink must not be called"),
        )


def test_unmanaged_codex_stage_lives_through_request_and_is_removed(monkeypatch, tmp_path):
    source = tmp_path / "source.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    observed: list[dict] = []

    def call(_prompt, **kwargs):
        attachments = kwargs["prepared_attachments"]
        assert len(attachments) == 1
        assert Path(attachments[0]["staged_bytes"]["transport_path"]).is_file()
        observed.extend(deepcopy(attachments))
        return _result()

    monkeypatch.setattr(llm, "_call_openai_cli", call)
    result = llm.extract_files_direct([source], backend="openai-cli", root=tmp_path)

    assert len(observed) == 1
    assert not Path(observed[0]["staged_bytes"]["transport_path"]).exists()
    assert result["_raster_evidence"]["preflight_batches"]
    assert result["_raster_evidence"]["attachments"]


def test_ephemeral_cleanup_failure_retains_raster_evidence(monkeypatch, tmp_path):
    source = tmp_path / "source.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    moved = tmp_path / "retained-stage"
    swapped = None

    def call(_prompt, **kwargs):
        nonlocal swapped
        attachment = kwargs["prepared_attachments"][0]
        stage_root = Path(attachment["staged_bytes"]["transport_path"]).parent
        stage_root.rename(moved)
        stage_root.symlink_to(tmp_path, target_is_directory=True)
        swapped = stage_root
        return _result()

    monkeypatch.setattr(llm, "_call_openai_cli", call)
    try:
        with pytest.raises(raster.RasterPreflightError, match="root identity changed") as caught:
            llm.extract_files_direct([source], backend="openai-cli", root=tmp_path)

        evidence = caught.value.graphify_raster_evidence
        assert evidence["preflight_batches"]
        assert evidence["attachments"][0]["lifecycle"]["status"] == "active"
        assert moved.is_dir()
    finally:
        if swapped is not None and swapped.is_symlink():
            swapped.unlink()
        if moved.exists():
            shutil.rmtree(moved)


def test_duplicate_display_labels_keep_distinct_codex_transports(monkeypatch, tmp_path):
    sources = [tmp_path / "one.png", tmp_path / "two.png"]
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", sources[0])
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/grayscale.png", sources[1])
    seen: list[dict] = []

    monkeypatch.setattr(llm, "_image_source_label", lambda _path, _root: "same.png")

    def call(_prompt, **kwargs):
        attachments = kwargs["prepared_attachments"]
        images = kwargs["images"]
        assert len(attachments) == len(images) == 2
        for source, attachment, image in zip(sources, attachments, images):
            assert attachment["original_source"]["path"] == str(source)
            assert image.path == Path(attachment["staged_bytes"]["transport_path"])
        seen.extend(deepcopy(attachments))
        return _result()

    monkeypatch.setattr(llm, "_call_openai_cli", call)
    llm.extract_files_direct(sources, backend="openai-cli", root=tmp_path)

    assert len({item["staged_bytes"]["transport_path"] for item in seen}) == 2


def test_corpus_splits_more_than_direct_limit_into_actual_cli_calls(monkeypatch, tmp_path):
    sources = []
    for index in range(raster.MAX_DIRECT_RASTERS + 1):
        source = tmp_path / f"source-{index}.png"
        shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
        sources.append(source)

    call_sizes: list[int] = []

    def call(_prompt, **kwargs):
        call_sizes.append(len(kwargs["prepared_attachments"]))
        return _result()

    monkeypatch.setattr(llm, "_call_openai_cli", call)
    result = llm.extract_corpus_parallel(
        sources,
        backend="openai-cli",
        root=tmp_path,
        chunk_size=len(sources),
        max_concurrency=1,
        max_retry_depth=0,
        token_budget=None,
    )

    assert [len(batch["images"]) for batch in result["_raster_evidence"]["preflight_batches"]] == [
        raster.MAX_DIRECT_RASTERS,
        1,
    ]
    assert len(result["_raster_evidence"]["attachments"]) == len(sources)
    assert call_sizes == [raster.MAX_DIRECT_RASTERS, 1]


def test_direct_call_rejects_more_than_raster_limit_before_cli(monkeypatch, tmp_path):
    sources = []
    for index in range(raster.MAX_DIRECT_RASTERS + 1):
        source = tmp_path / f"source-{index}.png"
        shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
        sources.append(source)
    monkeypatch.setattr(
        llm,
        "_call_openai_cli",
        lambda *_args, **_kwargs: pytest.fail("oversized direct call must not reach CLI"),
    )

    with pytest.raises(raster.RasterPreflightError, match="at most 20 raster images"):
        llm.extract_files_direct(sources, backend="openai-cli", root=tmp_path)


def test_corpus_rejects_raster_changed_after_cache_admission(monkeypatch, tmp_path):
    source = tmp_path / "source.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    admission = llm._preflight_raster_cache_admission([source], root=tmp_path)
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/grayscale.png", source)
    monkeypatch.setattr(
        llm,
        "_call_openai_cli",
        lambda *_args, **_kwargs: pytest.fail("changed raster must fail before CLI"),
    )

    with pytest.raises(raster.RasterPreflightError, match="changed after cache admission"):
        llm.extract_corpus_parallel(
            [source],
            backend="openai-cli",
            root=tmp_path,
            max_concurrency=1,
            max_retry_depth=0,
            token_budget=None,
            attachment_compatibility=admission["attachment_compatibility"],
        )


def test_corpus_hollow_retry_reuses_one_ephemeral_stage(monkeypatch, tmp_path):
    source = tmp_path / "source.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    transport_paths: list[str] = []

    def call(_prompt, **kwargs):
        attachment = kwargs["prepared_attachments"][0]
        path = attachment["staged_bytes"]["transport_path"]
        assert Path(path).is_file()
        transport_paths.append(path)
        return _result(finish_reason="hollow" if len(transport_paths) == 1 else "stop")

    monkeypatch.setattr(llm, "_call_openai_cli", call)
    monkeypatch.setattr(llm, "_HOLLOW_BACKOFF_S", (0,))
    monkeypatch.setattr(llm.time, "sleep", lambda _delay: None)
    result = llm.extract_corpus_parallel(
        [source],
        backend="openai-cli",
        root=tmp_path,
        max_concurrency=1,
        max_retry_depth=1,
        token_budget=None,
    )

    assert len(transport_paths) == 2
    assert len(set(transport_paths)) == 1
    assert not Path(transport_paths[0]).exists()
    assert result["_raster_evidence"]["attachments"]


def test_late_invalid_raster_fails_before_stager_or_cli(monkeypatch, tmp_path):
    valid = tmp_path / "valid.png"
    invalid = tmp_path / "invalid.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", valid)
    invalid.write_bytes(b"not an image")
    calls = {"stage": 0, "cli": 0}

    def stage(_request):
        calls["stage"] += 1
        pytest.fail("stager must not run until every raster passes")

    def call(*_args, **_kwargs):
        calls["cli"] += 1
        pytest.fail("CLI must not run after raster admission failure")

    monkeypatch.setattr(llm, "_call_openai_cli", call)
    with pytest.raises(raster.RasterPreflightError, match="integrity|decoding"):
        llm.extract_files_direct(
            [valid, invalid],
            backend="openai-cli",
            root=tmp_path,
            attachment_stager=stage,
            attachment_snapshot_root=tmp_path / "snapshots",
        )
    assert calls == {"stage": 0, "cli": 0}


def test_inline_raster_changed_after_preflight_fails_before_api_call(monkeypatch, tmp_path):
    source = tmp_path / "source.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    original_preflight = raster.preflight_raster_batch

    def preflight_then_change(*args, **kwargs):
        result = original_preflight(*args, **kwargs)
        shutil.copyfile(Path(__file__).parent / "fixtures/raster/grayscale.png", source)
        return result

    monkeypatch.setattr(raster, "preflight_raster_batch", preflight_then_change)
    monkeypatch.setattr(
        llm,
        "_call_openai_compat",
        lambda *_args, **_kwargs: pytest.fail("changed raster must not reach API call"),
    )

    with pytest.raises(raster.RasterPreflightError, match="bytes changed after preflight"):
        llm.extract_files_direct([source], backend="openai", api_key="test-only", root=tmp_path)


def test_supported_raster_rejects_nonvision_backend_before_api_call(monkeypatch, tmp_path):
    source = tmp_path / "source.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    monkeypatch.setattr(
        llm,
        "_call_openai_compat",
        lambda *_args, **_kwargs: pytest.fail("unsupported raster must not reach API call"),
    )

    with pytest.raises(raster.RasterPreflightError, match="does not support raster transport"):
        llm.extract_files_direct([source], backend="deepseek", api_key="test-only", root=tmp_path)


def test_managed_codex_uses_verified_durable_stager(monkeypatch, tmp_path):
    source = tmp_path / "source.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    requests: list[dict] = []
    observed: list[dict] = []

    def stage(request):
        requests.append(deepcopy(request))
        staged = snapshot_root / "image.png"
        shutil.copyfile(source, staged)
        payload = staged.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        return {
            "schema_version": 1,
            "status": "complete",
            "raw_ref": "raw:image",
            "receipt_ref": "receipt:image",
            "transport_path": str(staged.resolve()),
            "source_sha256": digest,
            "source_byte_count": len(payload),
            "staged_sha256": digest,
            "staged_byte_count": len(payload),
            "finalized": True,
        }

    def call(_prompt, **kwargs):
        observed.extend(deepcopy(kwargs["prepared_attachments"]))
        return _result()

    monkeypatch.setattr(llm, "_call_openai_cli", call)
    result = llm.extract_files_direct(
        [source],
        backend="openai-cli",
        root=tmp_path,
        execution_profile=_managed_profile(),
        run_context=_run_context(tmp_path),
        process_runner=lambda _request: pytest.fail("mocked CLI boundary must not run"),
        receipt_sink=lambda _receipt: pytest.fail("mocked CLI boundary must not run"),
        attachment_stager=stage,
        attachment_snapshot_root=snapshot_root,
    )

    assert set(requests[0]) == {
        "schema_version",
        "operation",
        "snapshot_root",
        "source_record",
        "preflight_identity",
        "decoder",
        "batch_sha256",
        "limits",
    }
    assert requests[0]["operation"] == "stage_raster_attachment"
    assert observed[0]["storage_mode"] == "durable"
    assert observed[0]["staged_bytes"]["receipt_ref"] == "receipt:image"
    assert result["_raster_evidence"]["attachments"] == observed


def test_managed_cli_failure_retains_verified_durable_attachment(monkeypatch, tmp_path):
    source = tmp_path / "source.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()

    def stage(request):
        staged = snapshot_root / "image.png"
        shutil.copyfile(Path(request["source_record"]["original_source"]["path"]), staged)
        payload = staged.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        return {
            "schema_version": 1,
            "status": "complete",
            "raw_ref": "raw:image",
            "receipt_ref": "receipt:image",
            "transport_path": str(staged.resolve()),
            "source_sha256": digest,
            "source_byte_count": len(payload),
            "staged_sha256": digest,
            "staged_byte_count": len(payload),
            "finalized": True,
        }

    monkeypatch.setattr(
        llm,
        "_call_openai_cli",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic CLI failure")),
    )
    with pytest.raises(RuntimeError, match="synthetic CLI failure") as caught:
        llm.extract_files_direct(
            [source],
            backend="openai-cli",
            root=tmp_path,
            execution_profile=_managed_profile(),
            run_context=_run_context(tmp_path),
            process_runner=lambda _request: pytest.fail("mocked CLI boundary must not run"),
            receipt_sink=lambda _receipt: pytest.fail("mocked CLI boundary must not run"),
            attachment_stager=stage,
            attachment_snapshot_root=snapshot_root,
        )

    evidence = caught.value.graphify_raster_evidence
    assert evidence["preflight_batches"]
    assert evidence["attachments"][0]["storage_mode"] == "durable"
    assert evidence["attachments"][0]["staged_bytes"]["receipt_ref"] == "receipt:image"


def test_later_durable_stage_failure_retains_earlier_ack_and_never_launches(monkeypatch, tmp_path):
    sources = [tmp_path / "one.png", tmp_path / "two.png"]
    for source in sources:
        shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    calls = 0

    def stage(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic second stage failure")
        staged = snapshot_root / "one.png"
        shutil.copyfile(Path(request["source_record"]["original_source"]["canonical_path"]), staged)
        payload = staged.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        return {
            "schema_version": 1,
            "status": "complete",
            "raw_ref": "raw:one",
            "receipt_ref": "receipt:one",
            "transport_path": str(staged.resolve()),
            "source_sha256": digest,
            "source_byte_count": len(payload),
            "staged_sha256": digest,
            "staged_byte_count": len(payload),
            "finalized": True,
        }

    monkeypatch.setattr(
        llm,
        "_call_openai_cli",
        lambda *_args, **_kwargs: pytest.fail("CLI must not run after staging failure"),
    )
    with pytest.raises(RuntimeError, match="second stage failure") as caught:
        llm.extract_files_direct(
            sources,
            backend="openai-cli",
            root=tmp_path,
            execution_profile=_managed_profile(),
            run_context=_run_context(tmp_path),
            process_runner=lambda _request: pytest.fail("runner must not be called"),
            receipt_sink=lambda _receipt: pytest.fail("sink must not be called"),
            attachment_stager=stage,
            attachment_snapshot_root=snapshot_root,
        )

    evidence = caught.value.graphify_raster_evidence
    assert calls == 2
    assert len(evidence["attachments"]) == 1
    assert evidence["attachments"][0]["staged_bytes"]["receipt_ref"] == "receipt:one"


def test_relative_managed_raster_correlates_to_durable_snapshot(monkeypatch, tmp_path):
    source = tmp_path / "source.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    observed: list[dict] = []

    def stage(request):
        original = Path(request["source_record"]["original_source"]["canonical_path"])
        staged = snapshot_root / "source.png"
        shutil.copyfile(original, staged)
        payload = staged.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        return {
            "schema_version": 1,
            "status": "complete",
            "raw_ref": "raw:source",
            "receipt_ref": "receipt:source",
            "transport_path": str(staged.resolve()),
            "source_sha256": digest,
            "source_byte_count": len(payload),
            "staged_sha256": digest,
            "staged_byte_count": len(payload),
            "finalized": True,
        }

    def call(_prompt, **kwargs):
        observed.extend(deepcopy(kwargs["prepared_attachments"]))
        assert kwargs["images"][0].path == snapshot_root / "source.png"
        return _result()

    monkeypatch.setattr(llm, "_call_openai_cli", call)
    llm.extract_files_direct(
        [Path("source.png")],
        backend="openai-cli",
        root=tmp_path,
        execution_profile=_managed_profile(),
        run_context=_run_context(tmp_path),
        process_runner=lambda _request: pytest.fail("mocked CLI boundary must not run"),
        receipt_sink=lambda _receipt: pytest.fail("mocked CLI boundary must not run"),
        attachment_stager=stage,
        attachment_snapshot_root=snapshot_root,
    )

    assert observed[0]["original_source"]["path"] == "source.png"
    assert observed[0]["original_source"]["canonical_path"] == str(source.resolve())


@pytest.mark.parametrize(
    "execution_profile",
    [
        _managed_profile(),
        None,
    ],
)
def test_capture_contract_rejects_missing_durable_stager(monkeypatch, tmp_path, execution_profile):
    source = tmp_path / "source.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    context = _run_context(tmp_path, capture_required=True)
    with pytest.raises(ValueError, match="requires attachment_stager"):
        llm.extract_files_direct(
            [source],
            backend="openai-cli",
            root=tmp_path,
            execution_profile=execution_profile,
            run_context=context,
            process_runner=lambda _request: pytest.fail("runner must not be called"),
            receipt_sink=lambda _receipt: pytest.fail("sink must not be called"),
        )
