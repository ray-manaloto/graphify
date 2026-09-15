"""Versioned execution-profile and CLI receipt primitives.

This module is deliberately independent of :mod:`graphify.llm`: callers own
provider response parsing, while this layer owns selector reconciliation,
closed invocation construction, byte-preserving process capture, and durable
attempt-receipt acknowledgement.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any


PROFILE_SCHEMA_VERSION = 1
RUN_CONTEXT_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1

_PROFILE_KEYS = {
    "schema_version",
    "backend",
    "model",
    "effort",
    "binary_expectation",
    "cli_policy",
    "identity_policy",
}
_RUN_CONTEXT_KEYS = {
    "schema_version",
    "run_id",
    "stage_id",
    "project_root",
    "cwd",
    "source_identity",
    "prompt_identity",
    "extractor_identity",
    "configuration_identity",
    "instruction_identity",
    "parent_receipts",
    "cache_ancestry",
    "capture_required",
}
_MODEL_ENV = {
    "claude-cli": "GRAPHIFY_CLAUDE_CLI_MODEL",
    "openai-cli": "GRAPHIFY_OPENAI_CLI_MODEL",
}
_EFFORT_ENV = {
    "claude-cli": "GRAPHIFY_CLAUDE_CLI_EFFORT",
    "openai-cli": "GRAPHIFY_OPENAI_CLI_EFFORT",
}
_PURPOSES = {"extract", "label", "dedup", "triage"}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validated_attachments(attachments: list[dict] | None) -> list[dict]:
    """Return closed, JSON-safe raster records plus legacy Claude directories."""
    from graphify.raster import validate_raster_attachment_record

    normalized: list[dict] = []
    for index, attachment in enumerate(attachments or []):
        if not isinstance(attachment, Mapping):
            raise TypeError(f"attachment {index} must be a mapping")
        # Preserve the ordinary unmanaged Claude helper's historical directory
        # attachment. Public raster extraction supplies closed raster records.
        if set(attachment) == {"parent"}:
            parent = attachment.get("parent")
            if not isinstance(parent, str) or not parent:
                raise ValueError(f"attachment {index}.parent must be a non-empty string")
            normalized.append({"parent": parent})
            continue
        raster = validate_raster_attachment_record(attachment)
        if raster["lifecycle"]["status"] != "active":
            raise ValueError(f"attachment {index} lifecycle must be active at invocation")
        normalized.append(raster)
    return normalized


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _selector(
    explicit: str | None,
    profile: str | None,
    env_value: str | None,
    *,
    name: str,
    required: bool = False,
) -> str | None:
    values = [("public", explicit), ("profile", profile), ("environment", env_value)]
    selected = [
        (source, value.strip())
        for source, value in values
        if isinstance(value, str) and value.strip()
    ]
    distinct = {value for _, value in selected}
    if len(distinct) > 1:
        detail = ", ".join(f"{source}={value!r}" for source, value in selected)
        raise ValueError(f"conflicting explicit {name} selectors: {detail}")
    if selected:
        return selected[0][1]
    if required:
        raise ValueError(f"managed execution profile requires {name}")
    return None


def resolve_execution_profile(
    backend: str | None,
    model: str | None,
    effort: str | None,
    *,
    execution_profile: dict | None = None,
    purpose: str,
    environment: Mapping[str, str] | None = None,
) -> dict:
    """Resolve selectors once and reject conflicting explicit sources.

    Absence of ``execution_profile`` is the legacy path. The caller supplies its
    already-resolved legacy defaults; no managed evidence claim is made.
    """
    if purpose not in _PURPOSES:
        raise ValueError(f"unknown execution purpose {purpose!r}")
    environ = os.environ if environment is None else environment
    if execution_profile is None:
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "backend": backend,
            "model": model,
            "effort": effort,
            "binary_expectation": {"path": None, "sha256": None, "version": None},
            "cli_policy": {
                "project_configuration": "legacy",
                "session_persistence": "legacy-disable",
                "mcp": "legacy-disable",
                "sandbox": "read-only",
            },
            "identity_policy": {
                "required_per_response": False,
                "allowed_reported_models": [],
            },
            "_explicit": False,
        }

    if not isinstance(execution_profile, dict):
        raise TypeError("execution_profile must be a dict")
    unknown = set(execution_profile) - _PROFILE_KEYS
    if unknown:
        raise ValueError(f"unknown execution_profile fields: {sorted(unknown)}")
    if execution_profile.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ValueError("unsupported execution_profile schema_version")

    profile_backend = _nonempty(execution_profile.get("backend"), "execution_profile.backend")
    resolved_backend = _selector(backend, profile_backend, None, name="backend", required=True)
    env_model = environ.get(_MODEL_ENV.get(resolved_backend, ""), "") or None
    env_effort = environ.get(_EFFORT_ENV.get(resolved_backend, ""), "") or None
    resolved_model = _selector(
        model,
        execution_profile.get("model"),
        env_model,
        name="model",
        required=resolved_backend in _MODEL_ENV,
    )
    resolved_effort = _selector(
        effort,
        execution_profile.get("effort"),
        env_effort,
        name="effort",
        required=resolved_backend in _EFFORT_ENV,
    )

    binary = execution_profile.get("binary_expectation")
    if not isinstance(binary, dict) or set(binary) != {"path", "sha256", "version"}:
        raise ValueError("binary_expectation must contain exactly path, sha256, and version")
    if resolved_backend in _MODEL_ENV:
        for key in ("path", "sha256", "version"):
            _nonempty(binary.get(key), f"binary_expectation.{key}")
        if not Path(binary["path"]).is_absolute():
            raise ValueError("binary_expectation.path must be absolute")

    policy = execution_profile.get("cli_policy")
    expected_policy = {"project_configuration", "session_persistence", "mcp", "sandbox"}
    if not isinstance(policy, dict) or set(policy) != expected_policy:
        raise ValueError(f"cli_policy must contain exactly {sorted(expected_policy)}")
    allowed_policy = {
        "project_configuration": {"inherit", "legacy"},
        "session_persistence": {"retain", "legacy-disable"},
        "mcp": {"inherit", "legacy-disable"},
        "sandbox": {"read-only", "provider-default"},
    }
    for key, choices in allowed_policy.items():
        if policy[key] not in choices:
            raise ValueError(f"invalid cli_policy.{key}: {policy[key]!r}")

    identity = execution_profile.get("identity_policy")
    if not isinstance(identity, dict) or set(identity) != {
        "required_per_response",
        "allowed_reported_models",
    }:
        raise ValueError(
            "identity_policy must contain exactly required_per_response and allowed_reported_models"
        )
    if not isinstance(identity["required_per_response"], bool):
        raise ValueError("identity_policy.required_per_response must be boolean")
    allowed_models = identity["allowed_reported_models"]
    if not isinstance(allowed_models, list) or not all(
        isinstance(item, str) and item for item in allowed_models
    ):
        raise ValueError("identity_policy.allowed_reported_models must be non-empty strings")
    if identity["required_per_response"] and not allowed_models:
        raise ValueError(
            "identity_policy.allowed_reported_models cannot be empty when "
            "required_per_response is true"
        )

    resolved = deepcopy(execution_profile)
    resolved.update(
        {"backend": resolved_backend, "model": resolved_model, "effort": resolved_effort}
    )
    resolved["_explicit"] = True
    return resolved


def validate_run_context(run_context: dict | None) -> dict | None:
    if run_context is None:
        return None
    if not isinstance(run_context, dict):
        raise TypeError("run_context must be a dict")
    unknown = set(run_context) - _RUN_CONTEXT_KEYS
    missing = _RUN_CONTEXT_KEYS - set(run_context)
    if unknown or missing:
        raise ValueError(
            f"run_context fields differ: missing={sorted(missing)} unknown={sorted(unknown)}"
        )
    if run_context.get("schema_version") != RUN_CONTEXT_SCHEMA_VERSION:
        raise ValueError("unsupported run_context schema_version")
    for key in ("run_id", "stage_id"):
        _nonempty(run_context.get(key), f"run_context.{key}")
    project_root = Path(_nonempty(run_context.get("project_root"), "run_context.project_root"))
    cwd = Path(_nonempty(run_context.get("cwd"), "run_context.cwd"))
    if not project_root.is_absolute() or not cwd.is_absolute():
        raise ValueError("run_context project_root and cwd must be absolute")
    if not project_root.is_dir() or not cwd.is_dir():
        raise ValueError("run_context project_root and cwd must be existing directories")
    if project_root != project_root.resolve(strict=True) or cwd != cwd.resolve(strict=True):
        raise ValueError("run_context project_root and cwd must be resolved paths")
    try:
        cwd.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("run_context cwd must be inside project_root") from exc

    def identity(name: str, fields: set[str]) -> dict:
        value = run_context.get(name)
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError(f"run_context.{name} must contain exactly {sorted(fields)}")
        digest = value.get("digest")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(ch not in "0123456789abcdefABCDEF" for ch in digest)
        ):
            raise ValueError(f"run_context.{name}.digest must be a SHA-256 hex digest")
        return value

    source = identity("source_identity", {"algorithm", "digest", "scope"})
    prompt = identity("prompt_identity", {"algorithm", "digest"})
    extractor = identity("extractor_identity", {"name", "version", "digest"})
    configuration = identity("configuration_identity", {"digest", "sources"})
    instruction = identity("instruction_identity", {"digest", "sources"})
    for name, value in (("source_identity", source), ("prompt_identity", prompt)):
        if value["algorithm"] != "sha256":
            raise ValueError(f"run_context.{name}.algorithm must be 'sha256'")
    for key in ("name", "version"):
        _nonempty(extractor[key], f"run_context.extractor_identity.{key}")
    for name, value, field in (
        ("source_identity", source, "scope"),
        ("configuration_identity", configuration, "sources"),
        ("instruction_identity", instruction, "sources"),
    ):
        entries = value[field]
        if not isinstance(entries, list) or not all(
            isinstance(item, str) and item for item in entries
        ):
            raise ValueError(f"run_context.{name}.{field} must contain strings")
    for key in ("parent_receipts", "cache_ancestry"):
        values = run_context.get(key)
        if not isinstance(values, list) or not all(
            isinstance(item, str) and item for item in values
        ):
            raise ValueError(f"run_context.{key} must be a list of receipt ids")
    if not isinstance(run_context.get("capture_required"), bool):
        raise ValueError("run_context.capture_required must be boolean")
    return deepcopy(run_context)


def validate_effective_managed_mode(
    execution_profile: dict | None, run_context: dict | None
) -> tuple[dict | None, bool]:
    """Return the validated context and whether capture controls are mandatory."""
    context = validate_run_context(run_context)
    if execution_profile is not None:
        if context is None:
            raise ValueError("managed execution requires run_context")
        if context["capture_required"] is not True:
            raise ValueError("managed execution requires capture_required=true")
    return context, execution_profile is not None or bool(
        context and context["capture_required"] is True
    )


def execution_profile_fingerprint(profile: dict) -> str:
    stable = {key: value for key, value in profile.items() if not key.startswith("_")}
    return _digest(stable)


def build_cli_invocation(
    prompt: str,
    *,
    purpose: str,
    max_tokens: int,
    deep_mode: bool = False,
    attachments: list[dict] | None = None,
    profile: dict,
    output_path: Path | None = None,
    project_root: Path | None = None,
    cwd: Path | None = None,
    legacy_mcp_args: list[str] | None = None,
) -> dict:
    """Build one closed Claude/Codex invocation for extraction or plain text."""
    if purpose not in _PURPOSES:
        raise ValueError(f"unknown execution purpose {purpose!r}")
    backend = profile.get("backend")
    if backend not in ("claude-cli", "openai-cli"):
        raise ValueError(f"CLI invocation requires a CLI backend, got {backend!r}")
    policy = profile["cli_policy"]
    expected_path = profile["binary_expectation"].get("path")
    binary_name = "claude" if backend == "claude-cli" else "codex"
    if expected_path:
        binary = expected_path
    elif backend == "claude-cli" and platform.system() == "Windows":
        binary = shutil.which("claude.cmd")
        if binary is None and shutil.which("claude") is not None:
            binary = "claude"
    elif backend == "claude-cli":
        binary = "claude" if shutil.which("claude") is not None else None
    else:
        binary = shutil.which(binary_name)
    if not binary:
        display = "Claude Code" if backend == "claude-cli" else "OpenAI Codex"
        raise RuntimeError(f"{display} CLI not found on $PATH")
    requested_cwd = Path(cwd or os.getcwd())
    requested_root = Path(project_root or requested_cwd)
    if profile.get("_explicit") and (not requested_root.is_dir() or not requested_cwd.is_dir()):
        raise ValueError("managed invocation project_root and cwd must be existing directories")
    actual_cwd = requested_cwd.resolve(strict=bool(profile.get("_explicit")))
    actual_root = requested_root.resolve(strict=bool(profile.get("_explicit")))
    try:
        actual_cwd.relative_to(actual_root)
    except ValueError as exc:
        raise ValueError("invocation cwd must be inside project_root") from exc

    normalized_attachments = _validated_attachments(attachments)
    raster_attachments = [item for item in normalized_attachments if item.get("kind") == "raster"]
    legacy_directories = [
        item["parent"] for item in normalized_attachments if set(item) == {"parent"}
    ]
    if legacy_directories and backend != "claude-cli":
        raise ValueError("legacy directory attachments are supported only by claude-cli")

    if backend == "claude-cli":
        argv = [str(binary), "-p", "--output-format", "json"]
        if policy["session_persistence"] == "legacy-disable":
            argv.append("--no-session-persistence")
        if profile.get("model"):
            argv.extend(["--model", profile["model"]])
        if profile.get("effort"):
            argv.extend(["--effort", profile["effort"]])
        seen_dirs: set[str] = set()
        attachment_parents = [
            str(Path(item["staged_bytes"]["transport_path"]).parent) for item in raster_attachments
        ]
        for parent in [*legacy_directories, *attachment_parents]:
            if parent and parent not in seen_dirs:
                seen_dirs.add(parent)
                argv.extend(["--add-dir", parent])
        output_contract = "stdout-json-envelope"
    else:
        if output_path is None:
            raise ValueError("openai-cli invocation requires output_path")
        argv = [str(binary), "exec", "--skip-git-repo-check", "--json"]
        if policy["sandbox"] == "read-only":
            argv.extend(["--sandbox", "read-only"])
        if policy["mcp"] == "legacy-disable":
            argv.extend(legacy_mcp_args or [])
        if profile.get("effort"):
            argv.extend(["-c", f"model_reasoning_effort={profile['effort']}"])
        if profile.get("model"):
            argv.extend(["--model", profile["model"]])
        if raster_attachments:
            image_paths = ",".join(
                item["staged_bytes"]["transport_path"] for item in raster_attachments
            )
            argv.extend(["--image", image_paths])
        argv.extend(["-o", str(output_path), "-"])
        output_contract = "last-message-file"

    return {
        "schema_version": 1,
        "backend": backend,
        "purpose": purpose,
        "prompt_contract": "graph_json" if purpose == "extract" else "plain_text",
        "argv": argv,
        "stdin": prompt.encode("utf-8"),
        "cwd": str(actual_cwd),
        "project_root": str(actual_root),
        "timeout_seconds": None,
        "max_tokens": max_tokens,
        "deep_mode": deep_mode,
        "output_contract": output_contract,
        "output_path": str(output_path) if output_path is not None else None,
        "attachments": normalized_attachments,
        "attachment_digest": _digest(normalized_attachments),
        "profile_fingerprint": execution_profile_fingerprint(profile),
        "requested_profile": {
            key: deepcopy(value) for key, value in profile.items() if not key.startswith("_")
        },
        "legacy_text_io": not profile.get("_explicit", False),
    }


def _default_process_runner(request: dict) -> dict:
    kwargs = {
        "input": request["stdin"],
        "capture_output": True,
        "cwd": request["cwd"],
        "timeout": request.get("timeout_seconds"),
        "check": False,
    }
    if request.get("legacy_text_io"):
        kwargs.update(
            input=request["stdin"].decode("utf-8"),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    if platform.system() == "Windows":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(request["argv"], **kwargs)
    stdout = getattr(proc, "stdout", b"")
    stderr = getattr(proc, "stderr", b"")
    if not isinstance(stdout, (str, bytes)):
        stdout = b""
    if not isinstance(stderr, (str, bytes)):
        stderr = b""
    result = {
        "returncode": proc.returncode,
        # Production subprocesses return bytes. Encoding string-valued test
        # doubles here keeps the legacy monkeypatch surface compatible without
        # weakening the runner contract exposed to managed callers.
        "stdout": stdout.encode() if isinstance(stdout, str) else stdout,
        "stderr": stderr.encode() if isinstance(stderr, str) else stderr,
        "stdout_eof": True,
        "stderr_eof": True,
        "finalized": True,
        "binary": {"path": request["argv"][0], "sha256": None, "version": None},
        "raw_capture_refs": {"stdout": None, "stderr": None},
        "provider_events": [],
    }
    if request.get("output_contract") == "last-message-file":
        requested_path = request.get("output_path")
        payload = b""
        eof = False
        finalized = False
        if isinstance(requested_path, str):
            try:
                payload = Path(requested_path).read_bytes()
                eof = True
                finalized = True
            except OSError:
                pass
        result["result_artifact"] = {
            "requested_path": requested_path,
            "payload": payload,
            "byte_count": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "raw_ref": None,
            "eof": eof,
            "finalized": finalized,
        }
    return result


def _stream_record(value: bytes, eof: bool, raw_ref: str | None) -> dict:
    return {
        "byte_count": len(value),
        "sha256": hashlib.sha256(value).hexdigest(),
        "eof": eof,
        "raw_ref": raw_ref,
    }


_RESULT_ARTIFACT_KEYS = {
    "requested_path",
    "payload",
    "byte_count",
    "sha256",
    "raw_ref",
    "eof",
    "finalized",
}


def _artifact_receipt_metadata(value: Any) -> dict | None:
    """Return only JSON-safe artifact metadata, never the retained byte payload."""
    if not isinstance(value, dict):
        return None
    byte_count = value.get("byte_count")
    return {
        "requested_path": value.get("requested_path")
        if isinstance(value.get("requested_path"), str)
        else None,
        "byte_count": byte_count
        if isinstance(byte_count, int) and not isinstance(byte_count, bool)
        else None,
        "sha256": value.get("sha256") if isinstance(value.get("sha256"), str) else None,
        "raw_ref": value.get("raw_ref") if isinstance(value.get("raw_ref"), str) else None,
        "eof": value.get("eof") if isinstance(value.get("eof"), bool) else None,
        "finalized": value.get("finalized") if isinstance(value.get("finalized"), bool) else None,
    }


def _validate_result_artifact(invocation: dict, value: Any, *, managed: bool) -> None:
    if not isinstance(value, dict) or set(value) != _RESULT_ARTIFACT_KEYS:
        raise ValueError("process_runner result_artifact fields differ")
    requested_path = value.get("requested_path")
    payload = value.get("payload")
    byte_count = value.get("byte_count")
    digest = value.get("sha256")
    raw_ref = value.get("raw_ref")
    if not isinstance(requested_path, str):
        raise TypeError("result_artifact requested_path must be a string")
    if requested_path != invocation.get("output_path"):
        raise ValueError("result_artifact requested_path differs from invocation output_path")
    if not isinstance(payload, bytes):
        raise TypeError("result_artifact payload must be bytes")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool):
        raise TypeError("result_artifact byte_count must be an integer")
    if byte_count != len(payload):
        raise ValueError("result_artifact byte_count does not match payload")
    if not isinstance(digest, str) or digest != hashlib.sha256(payload).hexdigest():
        raise ValueError("result_artifact sha256 does not match payload")
    if raw_ref is not None and (not isinstance(raw_ref, str) or not raw_ref):
        raise TypeError("result_artifact raw_ref must be a non-empty string or None")
    if managed and not raw_ref:
        raise ValueError("managed result_artifact requires a non-empty raw_ref")
    if value.get("eof") is not True or value.get("finalized") is not True:
        raise ValueError("result_artifact must have eof and finalized true")


def _invocation_uses_managed_profile(invocation: dict) -> bool:
    """Validate the builder's profile marker and return its capture mode."""
    if not isinstance(invocation, dict):
        raise TypeError("invocation must be a dict")
    requested_profile = invocation.get("requested_profile")
    if not isinstance(requested_profile, dict):
        raise TypeError("invocation requested_profile must be a dict")
    fingerprint = invocation.get("profile_fingerprint")
    if not isinstance(fingerprint, str) or (
        fingerprint != execution_profile_fingerprint(requested_profile)
    ):
        raise ValueError("invocation profile_fingerprint does not match requested_profile")
    legacy_text_io = invocation.get("legacy_text_io")
    if not isinstance(legacy_text_io, bool):
        raise TypeError("invocation legacy_text_io must be boolean")
    purpose = invocation.get("purpose")
    backend = invocation.get("backend")
    if requested_profile.get("backend") != backend:
        raise ValueError("invocation backend differs from requested_profile.backend")

    legacy_profile = resolve_execution_profile(
        backend,
        requested_profile.get("model"),
        requested_profile.get("effort"),
        execution_profile=None,
        purpose=purpose,
        environment={},
    )
    legacy_profile.pop("_explicit")
    profile_is_legacy = requested_profile == legacy_profile
    if legacy_text_io != profile_is_legacy:
        raise ValueError("invocation legacy_text_io differs from requested_profile")
    if profile_is_legacy:
        return False

    managed_profile = resolve_execution_profile(
        backend,
        requested_profile.get("model"),
        requested_profile.get("effort"),
        execution_profile=requested_profile,
        purpose=purpose,
        environment={},
    )
    managed_profile.pop("_explicit")
    if managed_profile != requested_profile:
        raise ValueError("invocation requested_profile is not fully resolved")
    return True


def run_cli_invocation(
    invocation: dict,
    *,
    run_context: dict | None = None,
    process_runner: Callable[[dict], dict] | None = None,
    result_parser: Callable[[dict], dict] | None = None,
    receipt_sink: Callable[[dict], dict] | None = None,
) -> dict:
    """Run, parse, and durably acknowledge one CLI attempt."""
    managed = _invocation_uses_managed_profile(invocation)
    context = validate_run_context(run_context)
    capture_required = bool(context and context["capture_required"])
    if managed and context is None:
        raise ValueError("managed invocation requires run_context")
    if managed and not capture_required:
        raise ValueError("managed invocation requires capture_required true")
    if (managed or capture_required) and (
        not callable(process_runner) or not callable(receipt_sink)
    ):
        raise ValueError(
            "capture_required needs a complete process_runner and durable receipt_sink"
        )
    runner = process_runner or _default_process_runner
    if context:
        if invocation.get("project_root") != context["project_root"]:
            raise ValueError("invocation project_root differs from run_context.project_root")
        if invocation.get("cwd") != context["cwd"]:
            raise ValueError("invocation cwd differs from run_context.cwd")
    try:
        process = runner(deepcopy(invocation))
    except BaseException as exc:
        process = {
            "returncode": None,
            "stdout": b"",
            "stderr": b"",
            "stdout_eof": False,
            "stderr_eof": False,
            "finalized": False,
            "binary": {"path": invocation["argv"][0], "sha256": None, "version": None},
            "raw_capture_refs": {"stdout": None, "stderr": None},
            "provider_events": [],
            "runner_error": f"{type(exc).__name__}: {exc}",
        }
        parsed = {
            "value": None,
            "completion": "failed",
            "usage": {},
            "observations": [],
            "responses": [],
            "coverage": {"status": "unproved", "reasons": ["runner_error"]},
        }
        outcome = _finish_attempt(
            invocation, context, process, parsed, receipt_sink, capture_required
        )
        exc.__dict__["graphify_attempt"] = outcome
        raise
    if not isinstance(process, dict):
        error = TypeError("process_runner must return a dict")
        _raise_with_attempt(
            error,
            invocation,
            context,
            _incomplete_process(invocation, "invalid_runner_result"),
            receipt_sink,
            capture_required,
        )
    required = {
        "returncode",
        "stdout",
        "stderr",
        "stdout_eof",
        "stderr_eof",
        "finalized",
        "binary",
        "raw_capture_refs",
        "provider_events",
    }
    missing = required - set(process)
    if missing:
        error = ValueError(f"process_runner result missing fields: {sorted(missing)}")
        _raise_with_attempt(
            error,
            invocation,
            context,
            _incomplete_process(invocation, "invalid_runner_result", process),
            receipt_sink,
            capture_required,
            original_process=process,
        )
    if not isinstance(process["stdout"], bytes) or not isinstance(process["stderr"], bytes):
        error = TypeError("process_runner stdout and stderr must be bytes")
        _raise_with_attempt(
            error,
            invocation,
            context,
            _incomplete_process(invocation, "invalid_runner_streams", process),
            receipt_sink,
            capture_required,
            original_process=process,
        )
    if invocation.get("output_contract") == "last-message-file" and (
        capture_required or process.get("returncode") == 0
    ):
        try:
            _validate_result_artifact(
                invocation, process.get("result_artifact"), managed=capture_required
            )
        except (TypeError, ValueError) as error:
            _raise_with_attempt(
                error,
                invocation,
                context,
                _incomplete_process(invocation, "invalid_result_artifact", process),
                receipt_sink,
                capture_required,
                original_process=process,
            )
    parser = result_parser or (
        lambda item: {
            "value": item,
            "completion": "completed" if item["returncode"] == 0 else "failed",
            "usage": {},
            "observations": [],
            "responses": [],
            "coverage": {"status": "unproved", "reasons": ["no_result_parser"]},
        }
    )
    try:
        parsed = parser(process)
        _validate_parsed_result(parsed)
    except BaseException as exc:
        failed = {
            "value": None,
            "completion": "incomplete_capture",
            "usage": {},
            "observations": [],
            "responses": [],
            "coverage": {"status": "unproved", "reasons": ["result_parser_failed"]},
        }
        attempt = _finish_attempt(
            invocation, context, process, failed, receipt_sink, capture_required
        )
        exc.__dict__["graphify_attempt"] = attempt
        raise
    return _finish_attempt(invocation, context, process, parsed, receipt_sink, capture_required)


def _incomplete_process(invocation: dict, reason: str, candidate: Any = None) -> dict:
    """Create a serializable process record when a runner breaks its contract."""
    stdout = candidate.get("stdout", b"") if isinstance(candidate, dict) else b""
    stderr = candidate.get("stderr", b"") if isinstance(candidate, dict) else b""
    return {
        "returncode": candidate.get("returncode") if isinstance(candidate, dict) else None,
        "stdout": stdout if isinstance(stdout, bytes) else b"",
        "stderr": stderr if isinstance(stderr, bytes) else b"",
        "stdout_eof": False,
        "stderr_eof": False,
        "finalized": False,
        "binary": candidate.get("binary")
        if isinstance(candidate, dict)
        else {
            "path": invocation["argv"][0],
            "sha256": None,
            "version": None,
        },
        "raw_capture_refs": candidate.get("raw_capture_refs", {})
        if isinstance(candidate, dict)
        else {},
        "provider_events": candidate.get("provider_events", [])
        if isinstance(candidate, dict)
        else [],
        "result_artifact": candidate.get("result_artifact")
        if isinstance(candidate, dict)
        else None,
        "runner_error": reason,
    }


def _raise_with_attempt(
    error: BaseException,
    invocation: dict,
    context: dict | None,
    process: dict,
    receipt_sink: Callable[[dict], dict] | None,
    capture_required: bool,
    *,
    original_process: Any = None,
) -> None:
    parsed = {
        "value": None,
        "completion": "incomplete_capture",
        "usage": {},
        "observations": [],
        "responses": [],
        "coverage": {"status": "unproved", "reasons": [process["runner_error"]]},
    }
    attempt = _finish_attempt(invocation, context, process, parsed, receipt_sink, capture_required)
    if original_process is not None:
        attempt["process"] = original_process
    error.__dict__["graphify_attempt"] = attempt
    raise error


def _validate_parsed_result(parsed: Any) -> None:
    if not isinstance(parsed, dict):
        raise TypeError("result_parser must return a dict")
    required = {"value", "completion", "usage", "observations", "responses", "coverage"}
    missing = required - set(parsed)
    if missing:
        raise ValueError(f"result_parser result missing fields: {sorted(missing)}")
    if not isinstance(parsed["completion"], str):
        raise TypeError("result_parser completion must be a string")
    if not isinstance(parsed["usage"], dict):
        raise TypeError("result_parser usage must be a dict")
    if not isinstance(parsed["observations"], list) or not isinstance(parsed["responses"], list):
        raise TypeError("result_parser observations and responses must be lists")
    if not isinstance(parsed["coverage"], dict):
        raise TypeError("result_parser coverage must be a dict")


def _finish_attempt(
    invocation: dict,
    context: dict | None,
    process: dict,
    parsed: dict,
    receipt_sink: Callable[[dict], dict] | None,
    capture_required: bool,
) -> dict:
    refs = process.get("raw_capture_refs") or {}
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "run_context": context,
        "profile_fingerprint": invocation["profile_fingerprint"],
        "request": {key: value for key, value in invocation.items() if key != "stdin"},
        "stdin_sha256": hashlib.sha256(invocation["stdin"]).hexdigest(),
        "process": {
            "returncode": process.get("returncode"),
            "finalized": bool(process.get("finalized")),
            "binary": process.get("binary"),
            "stdout": _stream_record(
                process.get("stdout", b""), bool(process.get("stdout_eof")), refs.get("stdout")
            ),
            "stderr": _stream_record(
                process.get("stderr", b""), bool(process.get("stderr_eof")), refs.get("stderr")
            ),
            "runner_error": process.get("runner_error"),
            "provider_events": process.get("provider_events") or [],
            "result_artifact": _artifact_receipt_metadata(process.get("result_artifact")),
        },
        "completion": parsed.get("completion", "incomplete_capture"),
        "usage": parsed.get("usage") or {},
        "observations": parsed.get("observations") or [],
        "responses": parsed.get("responses") or [],
        "coverage": parsed.get("coverage") or {"status": "unproved", "reasons": ["missing"]},
    }
    if capture_required:
        capture_reasons = []
        policy_reasons = []
        if (
            not process.get("finalized")
            or not process.get("stdout_eof")
            or not process.get("stderr_eof")
        ):
            capture_reasons.append("capture_not_finalized")
        if not refs.get("stdout") or not refs.get("stderr"):
            capture_reasons.append("raw_capture_refs_missing")
        if invocation.get("output_contract") == "last-message-file":
            artifact = receipt["process"]["result_artifact"]
            if artifact is None:
                capture_reasons.append("result_artifact_missing")
            elif not artifact.get("eof") or not artifact.get("finalized"):
                capture_reasons.append("result_artifact_not_finalized")
        binary = process.get("binary") or {}
        if not all(binary.get(key) for key in ("path", "sha256", "version")):
            capture_reasons.append("binary_identity_incomplete")
        expected_binary = invocation["requested_profile"]["binary_expectation"]
        if any(
            binary.get(key) != expected_binary.get(key) for key in ("path", "sha256", "version")
        ):
            policy_reasons.append("binary_identity_mismatch")
        identity = invocation["requested_profile"]["identity_policy"]
        if identity["required_per_response"]:
            responses = receipt["responses"]
            allowed = set(identity["allowed_reported_models"])
            if not responses:
                policy_reasons.append("response_identity_missing")
            elif any(
                not isinstance(response, dict)
                or not response.get("response_id")
                or not response.get("reported_model")
                for response in responses
            ):
                policy_reasons.append("response_identity_incomplete")
            elif any(response["reported_model"] not in allowed for response in responses):
                policy_reasons.append("reported_model_outside_profile")
        parsed_reasons = receipt["coverage"].get("reasons", [])
        reasons = list(dict.fromkeys([*parsed_reasons, *capture_reasons, *policy_reasons]))
        if reasons:
            if capture_reasons:
                receipt["completion"] = "incomplete_capture"
            receipt["coverage"] = {"status": "unproved", "reasons": reasons}
    unsigned = deepcopy(receipt)
    receipt_id = _digest(unsigned)
    receipt["receipt_id"] = receipt_id
    receipt_sha256 = _digest(receipt)
    ack = None
    if callable(receipt_sink):
        try:
            ack = receipt_sink(deepcopy(receipt))
        except BaseException as exc:
            if capture_required:
                err = RuntimeError("durable receipt sink failed")
                err.__dict__["graphify_attempt"] = {
                    "value": parsed.get("value"),
                    "process": process,
                    "receipt": receipt,
                    "sink_failure": f"{type(exc).__name__}: {exc}",
                }
                raise err from exc
        if capture_required:
            expected = {"receipt_id": receipt_id, "sha256": receipt_sha256, "finalized": True}
            if (
                not isinstance(ack, dict)
                or any(ack.get(k) != v for k, v in expected.items())
                or not ack.get("durable_ref")
            ):
                err = RuntimeError("durable receipt sink acknowledgement invalid")
                err.__dict__["graphify_attempt"] = {
                    "value": parsed.get("value"),
                    "process": process,
                    "receipt": receipt,
                    "sink_ack": ack,
                }
                raise err
    return {"value": parsed.get("value"), "process": process, "receipt": receipt, "sink_ack": ack}


def paid_work_state(receipts: list[dict], cache_ancestry: list[dict] | None = None) -> str:
    """Return ``none``, ``usable``, or ``uncertain`` for fallback decisions."""
    if cache_ancestry:
        return "usable"
    uncertain = False
    for receipt in receipts:
        completion = receipt.get("completion")
        if completion in {"completed", "completed_empty", "partial"}:
            return "usable"
        if completion in {"incomplete_capture", "cancelled", "timed_out", "hollow"}:
            uncertain = True
        if (receipt.get("coverage") or {}).get(
            "status"
        ) != "complete" and completion != "failed_before_response":
            uncertain = True
    return "uncertain" if uncertain else "none"
