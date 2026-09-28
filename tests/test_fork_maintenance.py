from __future__ import annotations

import contextlib
import errno
import hashlib
import importlib.util
import inspect
import io
import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
# Fixture repositories use real Git from fixed system locations, never a PATH shim
# (version-manager shims cannot load their config under the sandboxed HOME).
SYSTEM_PATH = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"
GIT = shutil.which("git", path=SYSTEM_PATH) or "/usr/bin/git"


def fixture_git_env() -> dict[str, str]:
    return {
        "PATH": SYSTEM_PATH,
        "HOME": os.environ["HOME"],
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_EDITOR": "true",
        "GIT_TERMINAL_PROMPT": "0",
    }


def ignore_sigterm() -> None:
    # Ignored dispositions survive exec, so every Git descendant ignores SIGTERM and
    # only a process-group SIGKILL can settle them.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def run_cli(
    *args: str,
    env: dict[str, str] | None = None,
    descendants_ignore_sigterm: bool = False,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    # On expiry subprocess.run kills and reaps the CLI before re-raising.
    return subprocess.run(
        [sys.executable, "-m", "tools.fork_maintenance", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        preexec_fn=ignore_sigterm if descendants_ignore_sigterm else None,
    )


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [GIT, "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env=fixture_git_env(),
        timeout=60,
    )


def commit_file(repo: Path, relative: str, content: str, message: str) -> str:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", relative)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def make_repositories(tmp_path: Path) -> dict[str, Any]:
    upstream_work = tmp_path / "upstream-work"
    upstream_bare = tmp_path / "upstream.git"
    source = tmp_path / "source"
    upstream_work.mkdir()
    git(upstream_work, "init", "-b", "main")
    git(upstream_work, "config", "user.name", "Fixture")
    git(upstream_work, "config", "user.email", "fixture@example.invalid")
    base = commit_file(upstream_work, "shared.txt", "base\n", "base")
    git(tmp_path, "clone", "--bare", str(upstream_work), str(upstream_bare))
    git(tmp_path, "clone", str(upstream_bare), str(source))
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    candidate = commit_file(source, "fork.txt", "fork delta\n", "fork delta")
    target = commit_file(upstream_work, "upstream.txt", "upstream target\n", "target")
    git(upstream_work, "remote", "add", "publish", str(upstream_bare))
    git(upstream_work, "push", "publish", "main")
    for tag in ("v9.9.0", "v1.5.0", "v1.4.0", "v1.3.0", "v1.2.0", "v1.1.0"):
        git(upstream_work, "tag", tag, target)
    git(upstream_work, "push", "publish", "--tags")
    return {
        "upstream_work": upstream_work,
        "upstream": upstream_bare,
        "source": source,
        "base": base,
        "candidate": candidate,
        "target": target,
    }


def source_fingerprint(repo: Path) -> dict[str, str]:
    fetch_head = repo / ".git" / "FETCH_HEAD"
    return {
        "head": git(repo, "rev-parse", "HEAD").stdout,
        "refs": git(repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout,
        "status": git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout,
        "objects": git(repo, "count-objects", "-v").stdout,
        "fetch_head": fetch_head.read_text(encoding="utf-8") if fetch_head.exists() else "<absent>",
    }


def preview_sha(
    repos: dict[str, Any],
    plan_path: Path,
    sha: str,
    *reason: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_cli(
        "preview",
        "--source-repo",
        str(repos["source"]),
        "--candidate",
        repos["candidate"],
        "--upstream-repository",
        "Graphify-Labs/graphify",
        "--upstream-url",
        str(repos["upstream"]),
        "--override-sha",
        sha,
        *reason,
        "--output-plan",
        str(plan_path),
        env=env,
    )


def preview_override(repos: dict[str, Any], plan_path: Path) -> subprocess.CompletedProcess[str]:
    return preview_sha(
        repos, plan_path, repos["target"], "--override-reason", "bounded fixture target"
    )


def apply_args(
    repos: dict[str, Any],
    plan: Path,
    output: Path,
    evidence: Path,
    branch: str,
    *,
    expected_plan_sha256: str | None = None,
) -> tuple[str, ...]:
    plan_sha256 = expected_plan_sha256 or (
        hashlib.sha256(plan.read_bytes()).hexdigest() if plan.exists() else "0" * 64
    )
    return (
        "apply",
        "--plan",
        str(plan),
        "--expected-plan-sha256",
        plan_sha256,
        "--committer-name",
        "Maintenance Fixture",
        "--committer-email",
        "maintenance-fixture@example.invalid",
        "--source-repo",
        str(repos["source"]),
        "--upstream-url",
        str(repos["upstream"]),
        "--output-worktree",
        str(output),
        "--branch",
        branch,
        "--evidence-dir",
        str(evidence),
    )


def test_help_exposes_only_explicit_preview_and_apply_operations() -> None:
    result = run_cli("--help")

    assert result.returncode == 0
    assert "preview" in result.stdout
    assert "apply" in result.stdout
    assert "frozen" in result.stdout.lower()


def test_preview_selects_by_publication_time_and_does_not_mutate_source(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    releases = [
        {
            "tag_name": "v1.5.0",
            "published_at": "2026-09-22T15:00:00Z",
            "draft": True,
            "prerelease": False,
        },
        {
            "tag_name": "v1.4.0",
            "published_at": "2026-09-22T14:00:00Z",
            "draft": False,
            "prerelease": True,
        },
        {
            "tag_name": "v1.3.0",
            "published_at": "2026-09-22T13:00:00Z",
            "draft": False,
            "prerelease": False,
        },
        {
            "tag_name": "v1.2.0",
            "published_at": "2026-09-22T12:00:00Z",
            "draft": False,
            "prerelease": False,
        },
    ]
    older_page = [
        {
            "tag_name": "v1.1.0",
            "published_at": "2026-09-22T11:00:00Z",
            "draft": False,
            "prerelease": False,
        },
        {
            "tag_name": "v9.9.0",
            "published_at": "2026-01-01T00:00:00Z",
            "draft": False,
            "prerelease": False,
        },
    ]
    pypi = {
        "1.3.0": {"urls": [{"url": "https://files.invalid/yanked.whl", "yanked": True}]},
        "1.2.0": None,
        "1.1.0": {"urls": [{"url": "https://files.invalid/ok.whl", "yanked": False}]},
        "9.9.0": {"urls": [{"url": "https://files.invalid/old.whl", "yanked": False}]},
    }
    plan_path = tmp_path / "plan.json"
    releases_fixture = tmp_path / "releases.json"
    pypi_fixture = tmp_path / "pypi.json"
    releases_fixture.write_text(json.dumps({"pages": [releases, older_page]}), encoding="utf-8")
    pypi_fixture.write_text(json.dumps(pypi), encoding="utf-8")
    before = source_fingerprint(repos["source"])

    result = run_cli(
        "preview",
        "--source-repo",
        str(repos["source"]),
        "--candidate",
        repos["candidate"],
        "--upstream-repository",
        "Graphify-Labs/graphify",
        "--upstream-url",
        str(repos["upstream"]),
        "--github-releases-fixture",
        str(releases_fixture),
        "--pypi-fixture",
        str(pypi_fixture),
        "--output-plan",
        str(plan_path),
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["schema_version"] == 1
    assert plan["selection"]["mode"] == "release"
    assert plan["selection"]["release"]["tag"] == "v1.1.0"
    assert plan["selection"]["release"]["version"] == "1.1.0"
    assert plan["selection"]["target_commit"] == repos["target"]
    assert plan["selection"]["evidence_kind"] == "recorded_fixture"
    assert plan["evidence_bindings"]["release_inputs"] == {
        "github": {
            "canonical_json_sha256": hashlib.sha256(
                json.dumps(
                    {"pages": [releases, older_page]},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            "pages_observed": 2,
            "provenance": "recorded_fixture",
        },
        "pypi": {
            "canonical_json_sha256": hashlib.sha256(
                json.dumps(pypi, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "provenance": "recorded_fixture",
        },
    }
    assert plan["candidate"]["commit"] == repos["candidate"]
    assert plan["candidate"]["fork_base"] == repos["base"]
    assert plan["capability_manifest"] == [
        {"classification": "pending", "path": "fork.txt", "status": "A"}
    ]
    assert plan["integrity"]["algorithm"] == "sha256"
    outcome = json.loads(result.stdout)
    assert outcome["plan_sha256"] == hashlib.sha256(plan_path.read_bytes()).hexdigest()
    for record in plan["command_evidence"]:
        for stream in ("stdout", "stderr"):
            raw = Path(record[stream]["raw_path"]).read_bytes()
            assert len(raw) == record[stream]["bytes"]
            assert hashlib.sha256(raw).hexdigest() == record[stream]["sha256"]
    assert source_fingerprint(repos["source"]) == before


def test_preview_accepts_only_exact_advertised_sha_override_with_reason(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    plan_path = tmp_path / "override-plan.json"
    before = source_fingerprint(repos["source"])

    invalid = run_cli(
        "preview",
        "--source-repo",
        str(repos["source"]),
        "--candidate",
        repos["candidate"],
        "--upstream-repository",
        "Graphify-Labs/graphify",
        "--upstream-url",
        str(repos["upstream"]),
        "--override-sha",
        "main",
        "--override-reason",
        "not an exact commit",
        "--output-plan",
        str(plan_path),
    )

    assert invalid.returncode != 0
    assert not plan_path.exists()
    assert source_fingerprint(repos["source"]) == before

    valid = preview_override(repos, plan_path)
    assert valid.returncode == 0, valid.stderr
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["selection"]["mode"] == "override"
    assert plan["selection"]["override"] == {
        "reason": "bounded fixture target",
        "sha": repos["target"],
    }
    assert source_fingerprint(repos["source"]) == before


def test_apply_is_isolated_and_exact_replay_is_idempotent(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    source = repos["source"]
    hook_effect = tmp_path / "hook-ran"
    hooks = source / ".git" / "hooks"
    hook_body = f"#!/bin/sh\nprintf ran > {hook_effect}\n"
    for name in ("post-checkout", "pre-rebase", "post-rewrite"):
        hook = hooks / name
        hook.write_text(hook_body, encoding="utf-8")
        hook.chmod(0o755)
    git(source, "config", "rebase.updateRefs", "true")
    git(source, "config", "rebase.autoStash", "true")
    git(source, "config", "rerere.enabled", "true")
    git(source, "config", "rerere.autoUpdate", "true")
    git(source, "branch", "protected", repos["candidate"])
    (source / "shared.txt").write_text("dirty source stays dirty\n", encoding="utf-8")
    plan = tmp_path / "plan.json"
    preview = preview_override(repos, plan)
    assert preview.returncode == 0, preview.stderr
    output = tmp_path / "attempt-worktree"
    evidence = tmp_path / "evidence"
    branch = "codex/fixture-attempt"
    protected_before = git(source, "rev-parse", "protected").stdout.strip()
    source_head_before = git(source, "rev-parse", "HEAD").stdout.strip()
    source_status_before = git(source, "status", "--porcelain=v1").stdout
    fetch_head = source / ".git" / "FETCH_HEAD"
    fetch_before = fetch_head.read_bytes() if fetch_head.exists() else None

    first = run_cli(*apply_args(repos, plan, output, evidence, branch))

    assert first.returncode == 0, first.stderr
    first_result = json.loads(first.stdout)
    assert first_result["status"] == "applied"
    result_commit = git(output, "rev-parse", "HEAD").stdout.strip()
    assert git(output, "merge-base", "--is-ancestor", repos["target"], result_commit).returncode == 0
    assert (output / "fork.txt").read_text(encoding="utf-8") == "fork delta\n"
    assert (output / "upstream.txt").read_text(encoding="utf-8") == "upstream target\n"
    assert git(output, "status", "--porcelain=v1").stdout == ""
    assert not hook_effect.exists()
    assert git(source, "rev-parse", "protected").stdout.strip() == protected_before
    assert git(source, "rev-parse", "HEAD").stdout.strip() == source_head_before
    assert git(source, "status", "--porcelain=v1").stdout == source_status_before
    assert (fetch_head.read_bytes() if fetch_head.exists() else None) == fetch_before
    evidence_result = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
    assert evidence_result["acquisition"]["status"] == "acquired"
    assert all(record["direct_rc"] == 0 for record in evidence_result["mutations"])
    commits_before = git(output, "rev-list", "--count", repos["target"] + "..HEAD").stdout

    replay = run_cli(*apply_args(repos, plan, output, evidence, branch))

    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["status"] == "replayed"
    assert git(output, "rev-parse", "HEAD").stdout.strip() == result_commit
    assert git(output, "rev-list", "--count", repos["target"] + "..HEAD").stdout == commits_before
    assert git(output, "status", "--porcelain=v1").stdout == ""
    assert not hook_effect.exists()


def test_successful_replay_refuses_output_worktree_filter_before_status(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    arguments = apply_args(repos, plan, output, evidence, "codex/replay-filter")
    assert run_cli(*arguments).returncode == 0
    helper = tmp_path / "clean-helper.sh"
    marker = tmp_path / "filter-ran"
    helper.write_text(f"#!/bin/sh\nprintf ran > '{marker}'\ncat\n", encoding="utf-8")
    helper.chmod(0o755)
    git(repos["source"], "config", "extensions.worktreeConfig", "true")
    git(output, "config", "--worktree", "filter.replay.clean", str(helper))
    git(output, "config", "--worktree", "filter.replay.required", "true")
    (output / ".gitattributes").write_text("fork.txt filter=replay\n", encoding="utf-8")
    (output / "fork.txt").write_text("fork drift\n", encoding="utf-8")
    assert git(output, "hash-object", "--path=fork.txt", "fork.txt").returncode == 0
    assert marker.exists()  # positive ordinary-Git helper control
    marker.unlink()

    replay = run_cli(*arguments)

    assert replay.returncode == 2, replay.stderr
    assert "filter.*.executable" in json.loads(replay.stderr)["error"]
    assert not marker.exists()


def test_public_receipt_rebase_command_disables_autostash_in_a_dirty_replay(
    tmp_path: Path,
) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    applied = run_cli(
        *apply_args(repos, plan, output, evidence, "codex/autostash-evidence")
    )
    assert applied.returncode == 0, applied.stderr
    receipt = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
    rebase_argv = next(
        record["argv"]
        for record in receipt["mutations"]
        if "rebase" in record["argv"]
    )

    def dirty_clone(name: str) -> tuple[Path, bytes]:
        repo = tmp_path / name
        git(tmp_path, "clone", str(repos["source"]), str(repo))
        git(repo, "config", "rebase.autoStash", "true")
        git(repo, "fetch", str(repos["upstream"]), repos["target"])
        original = (repo / "shared.txt").read_bytes()
        (repo / "shared.txt").write_bytes(b"dirty replay bytes\n")
        return repo, original

    control, _ = dirty_clone("plain-control")
    plain = git(
        control,
        "rebase",
        "--onto",
        repos["target"],
        repos["base"],
        "main",
        check=False,
    )
    assert plain.returncode == 0, plain.stderr
    assert (control / "shared.txt").read_bytes() == b"dirty replay bytes\n"
    assert git(control, "stash", "list").stdout == ""

    replay, _ = dirty_clone("engine-replay")
    exact = list(rebase_argv)
    exact[exact.index(str(output))] = str(replay)
    exact[-1] = "main"
    before = (replay / "shared.txt").read_bytes()
    refused = subprocess.run(
        exact,
        check=False,
        capture_output=True,
        text=True,
        env=fixture_git_env(),
        timeout=60,
    )
    assert refused.returncode != 0
    assert (replay / "shared.txt").read_bytes() == before
    assert git(replay, "stash", "list").stdout == ""


def test_conflict_stops_nonzero_and_repeat_preserves_attempt(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    source = repos["source"]
    repos["candidate"] = commit_file(source, "shared.txt", "fork version\n", "fork conflict")
    repos["target"] = commit_file(
        repos["upstream_work"], "shared.txt", "upstream version\n", "upstream conflict"
    )
    git(repos["upstream_work"], "push", "publish", "main")
    git(source, "config", "rerere.enabled", "true")
    git(source, "config", "rerere.autoUpdate", "true")
    plan = tmp_path / "conflict-plan.json"
    preview = preview_override(repos, plan)
    assert preview.returncode == 0, preview.stderr
    output = tmp_path / "conflict-worktree"
    evidence = tmp_path / "conflict-evidence"
    arguments = apply_args(repos, plan, output, evidence, "codex/conflict-attempt")

    first = run_cli(*arguments)

    assert first.returncode != 0
    assert first.stdout, first.stderr
    outcome = json.loads(first.stdout)
    assert outcome["status"] == "conflict"
    assert outcome["conflicted_paths"] == ["shared.txt"]
    assert outcome["recovery"]["automatic_action"] == "none"
    assert git(output, "diff", "--name-only", "--diff-filter=U").stdout == "shared.txt\n"

    git_dir = Path(git(output, "rev-parse", "--git-dir").stdout.strip())
    if not git_dir.is_absolute():
        git_dir = output / git_dir
    assert (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()
    conflict_bytes = (output / "shared.txt").read_bytes()
    result_bytes = (evidence / "result.json").read_bytes()

    repeated = run_cli(*arguments)

    assert repeated.returncode != 0
    repeated_outcome = json.loads(repeated.stdout)
    assert repeated_outcome["status"] == "conflict"
    assert repeated_outcome["repeated"] is True
    assert (output / "shared.txt").read_bytes() == conflict_bytes
    assert (evidence / "result.json").read_bytes() == result_bytes
    assert git(output, "diff", "--name-only", "--diff-filter=U").stdout == "shared.txt\n"


def test_alternate_refs_helper_direct_fetch_then_preview_refuses(tmp_path: Path) -> None:
    """A real alternates fetch executes the helper; preview refuses before it can."""
    repos = make_repositories(tmp_path)
    source = repos["source"]
    marker = tmp_path / "alternate-helper-invoked"
    alternates = source / ".git" / "objects" / "info" / "alternates"
    alternates.write_text(str(repos["upstream"] / "objects") + "\n", encoding="utf-8")
    command = (
        f"printf invoked > {shlex.quote(str(marker))}; "
        f"printf '{repos['target']}\\n'"
    )
    git(source, "config", "core.alternateRefsCommand", command)

    control = git(
        source, "fetch", "--no-tags", "--no-write-fetch-head",
        str(repos["upstream"]), repos["target"], check=False,
    )
    assert control.returncode == 0, control.stderr
    assert marker.is_file(), "the positive Git control must invoke the helper"
    marker.unlink()
    source_before = source_fingerprint(source)
    plan = tmp_path / "alternate-plan.json"

    result = preview_override(repos, plan)

    assert result.returncode == 2, result.stderr
    assert "alternateRefsCommand" in result.stderr
    assert not marker.exists() and not plan.exists()
    assert source_fingerprint(source) == source_before


@pytest.mark.parametrize("layout", ["gitlink", "gitmodules"])
def test_preview_refuses_unsupported_fork_base_only_tree(
    tmp_path: Path, layout: str
) -> None:
    """The inferred base is checked even when both tips removed the layout."""
    upstream_work = tmp_path / "upstream-work"
    upstream_bare = tmp_path / "upstream.git"
    source = tmp_path / "source"
    upstream_work.mkdir()
    git(upstream_work, "init", "-b", "main")
    git(upstream_work, "config", "user.name", "Fixture")
    git(upstream_work, "config", "user.email", "fixture@example.invalid")
    first = commit_file(upstream_work, "shared.txt", "base\n", "base")
    if layout == "gitlink":
        git(upstream_work, "update-index", "--add", "--cacheinfo", "160000", first, "nested")
        git(upstream_work, "commit", "-m", "base gitlink")
    else:
        commit_file(
            upstream_work, ".gitmodules", '[submodule "nested"]\n',
            "base gitmodules",
        )
    fork_base = git(upstream_work, "rev-parse", "HEAD").stdout.strip()
    git(tmp_path, "clone", "--bare", str(upstream_work), str(upstream_bare))
    git(tmp_path, "clone", str(upstream_bare), str(source))
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    removed = "nested" if layout == "gitlink" else ".gitmodules"
    git(source, "rm", removed)
    git(source, "commit", "-m", "fork removes unsupported base layout")
    candidate = git(source, "rev-parse", "HEAD").stdout.strip()
    git(upstream_work, "rm", removed)
    git(upstream_work, "commit", "-m", "upstream removes unsupported base layout")
    target = git(upstream_work, "rev-parse", "HEAD").stdout.strip()
    git(upstream_work, "remote", "add", "publish", str(upstream_bare))
    git(upstream_work, "push", "publish", "main")
    git(source, "fetch", "--no-tags", str(upstream_bare), target)
    repos = {
        "source": source, "upstream": upstream_bare, "candidate": candidate,
        "target": target, "base": fork_base,
    }
    assert git(source, "merge-base", candidate, target).stdout.strip() == fork_base
    assert removed not in git(source, "ls-tree", "--name-only", candidate).stdout
    assert removed not in git(source, "ls-tree", "--name-only", target).stdout
    plan = tmp_path / "fork-base-plan.json"
    source_before = source_fingerprint(source)

    result = preview_override(repos, plan)

    assert result.returncode == 2, result.stderr
    assert "fork base" in result.stderr.lower()
    assert "submodule" in result.stderr.lower()
    assert not plan.exists()
    assert source_fingerprint(source) == source_before


def test_conflict_replay_refuses_nonconflicted_content_drift(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    repos["candidate"] = commit_file(
        repos["source"], "shared.txt", "fork version\n", "fork conflict"
    )
    repos["target"] = commit_file(
        repos["upstream_work"], "shared.txt", "upstream version\n", "upstream conflict"
    )
    git(repos["upstream_work"], "push", "publish", "main")
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    arguments = apply_args(repos, plan, output, evidence, "codex/conflict-drift")
    assert run_cli(*arguments).returncode == 3
    (output / "fork.txt").write_text("changed outside conflict\n", encoding="utf-8")

    repeated = run_cli(*arguments)

    assert repeated.returncode == 2, repeated.stderr
    assert "drift" in json.loads(repeated.stderr)["error"]


def test_apply_refuses_evidence_inside_source_before_artifact_creation(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    evidence = repos["source"] / "maintenance-evidence"
    before = source_fingerprint(repos["source"])

    refused = run_cli(
        *apply_args(repos, plan, tmp_path / "attempt", evidence, "codex/contained-evidence")
    )

    assert refused.returncode == 2, refused.stderr
    assert "protected repository paths" in json.loads(refused.stderr)["error"]
    assert not evidence.exists()
    assert source_fingerprint(repos["source"]) == before


def test_preview_refuses_credential_bearing_origin_without_publication(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    synthetic = "SYNTHETIC_DO_NOT_PUBLISH"
    git(repos["source"], "remote", "set-url", "origin", f"https://user:{synthetic}@example.invalid/x")
    plan = tmp_path / "plan.json"

    refused = preview_override(repos, plan)

    assert refused.returncode == 2
    assert synthetic not in refused.stdout + refused.stderr
    assert not plan.exists()
    assert not list(tmp_path.glob(".plan.json.preview-evidence-*"))


def test_malformed_conflict_replay_recovery_is_a_structured_refusal(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    repos["candidate"] = commit_file(
        repos["source"], "shared.txt", "fork version\n", "fork conflict"
    )
    repos["target"] = commit_file(
        repos["upstream_work"], "shared.txt", "upstream version\n", "upstream conflict"
    )
    git(repos["upstream_work"], "push", "publish", "main")
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    arguments = apply_args(repos, plan, output, evidence, "codex/malformed-replay")
    assert run_cli(*arguments).returncode == 3
    result_path = evidence / "result.json"
    malformed = json.loads(result_path.read_text(encoding="utf-8"))
    malformed.pop("integrity")
    malformed.pop("recovery")
    digest = hashlib.sha256(
        json.dumps(malformed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    malformed["integrity"] = {"algorithm": "sha256", "sha256": digest}
    result_path.write_text(json.dumps(malformed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    state = (output / "shared.txt").read_bytes()

    replay = run_cli(*arguments)

    assert replay.returncode == 2, replay.stderr
    refusal = json.loads(replay.stderr)
    assert refusal["status"] == "refused"
    assert "recovery" in refusal["error"]
    assert "Traceback" not in replay.stderr
    assert (output / "shared.txt").read_bytes() == state


def test_long_git_diagnostics_are_retained_as_complete_raw_bytes(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    source = repos["source"]
    upstream = repos["upstream_work"]
    for index in range(2200):
        relative = f"conflicts/very-long-diagnostic-path-{index:04d}.txt"
        source_path = source / relative
        upstream_path = upstream / relative
        source_path.parent.mkdir(parents=True, exist_ok=True)
        upstream_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("fork\n", encoding="utf-8")
        upstream_path.write_text("upstream\n", encoding="utf-8")
    git(source, "add", "conflicts")
    git(source, "commit", "-m", "many fork conflicts")
    repos["candidate"] = git(source, "rev-parse", "HEAD").stdout.strip()
    git(upstream, "add", "conflicts")
    git(upstream, "commit", "-m", "many upstream conflicts")
    repos["target"] = git(upstream, "rev-parse", "HEAD").stdout.strip()
    git(upstream, "push", "publish", "main")
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    evidence = tmp_path / "evidence"

    result = run_cli(
        *apply_args(repos, plan, tmp_path / "attempt", evidence, "codex/long-output"),
        timeout=180,
    )

    assert result.returncode == 3, result.stderr
    receipt = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
    streams = [
        record[stream]
        for record in receipt["command_records"]
        for stream in ("stdout", "stderr")
    ]
    long_stream = max(streams, key=lambda item: item["bytes"])
    assert long_stream["bytes"] > 128 * 1024
    assert long_stream["truncated"] is True
    raw = Path(long_stream["raw_path"]).read_bytes()
    assert len(raw) == long_stream["bytes"]
    assert hashlib.sha256(raw).hexdigest() == long_stream["sha256"]


def blocking_upstream(tmp_path: Path, upstream: Path, target: str) -> tuple[Path, Path]:
    """Block a real target object read in git-upload-pack, including direct SHA fetches."""
    blocked = tmp_path / "blocked-upstream.git"
    git(tmp_path, "clone", "--bare", "--no-hardlinks", str(upstream), str(blocked))
    assert git(blocked, "cat-file", "-t", target).stdout.strip() == "commit"
    object_path = blocked / "objects" / target[:2] / target[2:]
    assert object_path.is_file() and object_path.stat().st_nlink == 1
    object_path.unlink()
    os.mkfifo(object_path)
    return blocked, object_path


def fifo_has_blocked_reader(fifo: Path) -> bool:
    try:
        descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        assert exc.errno == errno.ENXIO
        return False
    os.close(descriptor)
    return True


@pytest.mark.parametrize(
    "limit", [("--subprocess-timeout", "1"), ("--attempt-timeout", "2")], ids=["git", "attempt"]
)
def test_timeout_reports_status_and_settles_git_descendants(
    tmp_path: Path, limit: tuple[str, str]
) -> None:
    repos = make_repositories(tmp_path)
    blocked, blocked_object = blocking_upstream(tmp_path, repos["upstream_work"], repos["target"])
    plan_path = tmp_path / "plan.json"
    before = source_fingerprint(repos["source"])

    result = run_cli(
        "preview",
        "--source-repo",
        str(repos["source"]),
        "--candidate",
        repos["candidate"],
        "--upstream-repository",
        "Graphify-Labs/graphify",
        "--upstream-url",
        str(blocked),
        "--override-sha",
        repos["target"],
        "--override-reason",
        "blocked fixture",
        "--output-plan",
        str(plan_path),
        *limit,
        descendants_ignore_sigterm=True,
    )

    assert result.returncode == 4, result.stderr
    outcome = json.loads(result.stderr)
    assert outcome["status"] == "timeout"
    assert outcome["process_group_settled"] is True
    assert "fetch" in outcome["timed_out_argv"]
    assert str(blocked) in outcome["timed_out_argv"]
    assert not fifo_has_blocked_reader(blocked_object)
    assert not plan_path.exists()
    assert source_fingerprint(repos["source"]) == before


HANG_GUARD = 15.0  # generous outer guard only; the CLI bounds under test are far shorter
PACE = 0.25


def release_entry(tag: str, index: int) -> dict[str, Any]:
    published = f"2026-09-22T12:{index // 60:02d}:{index % 60:02d}Z"
    return {"tag_name": tag, "published_at": published, "draft": False, "prerelease": False}


def send_json(handler: Any, payload: Any, status: int = 200, link: str | None = None) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    if link:
        handler.send_header("Link", f'<{link}>; rel="next"')
    handler.end_headers()
    handler.wfile.write(body)


def send_json_links(handler: Any, payload: Any, links: list[str]) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for link in links:
        handler.send_header("Link", link)
    handler.end_headers()
    handler.wfile.write(body)


class MetadataHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        server = self.server
        server.requests.append(self.path)  # explicit readiness: the request reached the server
        route = server.routes.get(self.path.split("?")[0], server.routes["*"])
        try:
            route(self, server)
        except OSError:
            pass  # the CLI settled its connection first


class MetadataServer(ThreadingHTTPServer):
    """Loopback GitHub/PyPI stand-in; settle() releases stalls and joins every thread."""

    daemon_threads = False
    block_on_close = True

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[str] = []
        self.stop = threading.Event()
        super().__init__(("127.0.0.1", 0), MetadataHandler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        self.thread = threading.Thread(target=self.serve_forever)
        self.thread.start()

    def settle(self) -> None:
        self.stop.set()
        self.shutdown()
        self.server_close()
        self.thread.join(HANG_GUARD)
        assert not self.thread.is_alive()


def stall_headers(handler: Any, server: MetadataServer) -> None:
    server.stop.wait()


def trickle_body(handler: Any, server: MetadataServer) -> None:
    handler.send_response(200)
    handler.send_header("Content-Length", str(1 << 20))
    handler.end_headers()
    while not server.stop.wait(0.05):  # every byte lands well inside any socket timeout
        handler.wfile.write(b" ")


def paced(route: Callable[..., None]) -> Callable[..., None]:
    def respond(handler: Any, server: MetadataServer) -> None:
        if not server.stop.wait(PACE):
            route(handler, server)

    return respond


def missing(handler: Any, server: MetadataServer) -> None:
    send_json(handler, {"message": "Not Found"}, status=404)


def endless_pages(handler: Any, server: MetadataServer) -> None:
    page = int(handler.path.rpartition("=")[2])
    send_json(handler, [], link=f"{server.url}/releases?page={page + 1}")


def pypi_file(yanked: bool) -> Callable[..., None]:
    payload = {"urls": [{"url": "https://files.invalid/x.whl", "yanked": yanked}]}
    return lambda handler, server: send_json(handler, payload)


LIVE_ROUTES: dict[str, Callable[..., None]] = {
    "/releases": lambda handler, server: send_json(
        handler,
        [release_entry("v1.3.0", 3), release_entry("v1.2.0", 2)],
        link=f"{server.url}/older",
    ),
    "/older": lambda handler, server: send_json(handler, [release_entry("v1.1.0", 1)]),
    "/pypi/graphifyy/1.3.0/json": pypi_file(yanked=True),
    "/pypi/graphifyy/1.1.0/json": pypi_file(yanked=False),
    "*": missing,
}
MANY_UNPUBLISHED = [release_entry(f"v2.0.{index}", index) for index in range(90)]


@pytest.fixture
def metadata_server() -> Iterator[Callable[[dict[str, Any]], MetadataServer]]:
    started: list[MetadataServer] = []

    def start(routes: dict[str, Any]) -> MetadataServer:
        started.append(MetadataServer(routes))
        return started[-1]

    yield start
    for server in started:  # runs even when the test body failed
        server.settle()


def preview_live(
    repos: dict[str, Any], server: MetadataServer, plan_path: Path, *limits: str,
    initial_url: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_cli(
        "preview",
        "--source-repo", str(repos["source"]),
        "--candidate", repos["candidate"],
        "--upstream-repository", "Graphify-Labs/graphify",
        "--upstream-url", str(repos["upstream"]),
        "--github-releases-url", initial_url or f"{server.url}/releases?page=1",
        "--pypi-base-url", server.url,
        "--output-plan", str(plan_path),
        *limits,
        env={**os.environ, "no_proxy": "127.0.0.1"},
        timeout=HANG_GUARD,
    )


def assert_worker_gone(outcome: dict[str, Any]) -> None:
    pid = outcome["worker_pid"]
    assert isinstance(pid, int) and pid > 0
    assert outcome["worker_settled"] is True
    for probe in (os.kill, os.killpg):  # neither the worker nor its process group remains
        with pytest.raises(ProcessLookupError):
            probe(pid, 0)


def run_http_timeout(
    tmp_path: Path,
    metadata_server: Any,
    routes: dict[str, Any],
    *limits: str,
    launched: bool = False,
) -> None:
    repos = make_repositories(tmp_path)
    server = metadata_server(routes)
    plan_path = tmp_path / "plan.json"
    before = source_fingerprint(repos["source"])

    result = preview_live(repos, server, plan_path, *limits)

    assert result.returncode == 4, result.stderr
    outcome = json.loads(result.stderr)
    assert outcome["status"] == "timeout"
    assert result.stdout == ""
    assert "/releases?page=1" in server.requests  # the bound fired after HTTP began
    assert not plan_path.exists()
    assert source_fingerprint(repos["source"]) == before
    # Worker diagnostics exist only when the bound fired after an HTTP worker launched;
    # prelaunch expiry legitimately reports only the bound.
    if launched or "worker_pid" in outcome:
        assert_worker_gone(outcome)
    else:
        assert "bound" in outcome


def test_preview_selects_live_release_over_loopback_http(
    tmp_path: Path, metadata_server: Any
) -> None:
    repos = make_repositories(tmp_path)
    server = metadata_server(LIVE_ROUTES)
    plan_path = tmp_path / "plan.json"
    before = source_fingerprint(repos["source"])

    result = preview_live(repos, server, plan_path)

    assert result.returncode == 0, result.stderr
    frozen = json.loads(plan_path.read_text(encoding="utf-8"))
    selection = frozen["selection"]
    assert (selection["evidence_kind"], selection["release"]["tag"]) == (
        "controlled_http",
        "v1.1.0",
    )
    assert selection["target_commit"] == repos["target"]
    assert server.requests == ["/releases?page=1", "/older"] + [
        f"/pypi/graphifyy/{version}/json" for version in ("1.3.0", "1.2.0", "1.1.0")
    ]
    assert frozen["evidence_bindings"]["release_inputs"]["github"]["provenance"] == (
        "controlled_http"
    )
    assert frozen["evidence_bindings"]["release_inputs"]["pypi"]["base_provenance"] == (
        "controlled_http"
    )
    http_records = [
        record for record in frozen["command_evidence"] if record.get("origin") == "http_worker_envelope"
    ]
    assert len(http_records) == 5
    for record in http_records:
        assert record["direct_rc"] == 0
        assert record["raw_complete"] is True
        assert record["response_sha256"]
        for stream in ("stdout", "stderr"):
            raw = Path(record[stream]["raw_path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == record[stream]["sha256"]
    assert source_fingerprint(repos["source"]) == before


def test_terminal_page_with_exactly_100_releases_is_complete(
    tmp_path: Path, metadata_server: Any
) -> None:
    repos = make_repositories(tmp_path)
    page = [release_entry("v1.1.0", 1) for _ in range(100)]
    server = metadata_server(
        {
            "/releases": lambda handler, service: send_json(handler, page),
            "/pypi/graphifyy/1.1.0/json": pypi_file(yanked=False),
            "*": missing,
        }
    )
    plan = tmp_path / "plan.json"

    result = preview_live(repos, server, plan)

    assert result.returncode == 0, result.stderr
    assert json.loads(plan.read_text(encoding="utf-8"))["selection"]["release"]["tag"] == "v1.1.0"
    assert server.requests == ["/releases?page=1", "/pypi/graphifyy/1.1.0/json"]


def test_repeated_link_headers_follow_the_single_valid_next(
    tmp_path: Path, metadata_server: Any
) -> None:
    repos = make_repositories(tmp_path)

    def first(handler: Any, server: MetadataServer) -> None:
        send_json_links(
            handler,
            [release_entry("v1.3.0", 3)],
            [
                f'<{server.url}/ignored>; rel="prev"',
                f'<{server.url}/older>; rel="next"',
            ],
        )

    routes = {
        "/releases": first,
        "/older": lambda handler, server: send_json(
            handler, [release_entry("v1.1.0", 1)]
        ),
        "/pypi/graphifyy/1.3.0/json": pypi_file(yanked=True),
        "/pypi/graphifyy/1.1.0/json": pypi_file(yanked=False),
        "*": missing,
    }
    server = metadata_server(routes)
    plan = tmp_path / "plan.json"

    result = preview_live(repos, server, plan)

    assert result.returncode == 0, result.stderr
    assert "/older" in server.requests


@pytest.mark.parametrize("case", ["malformed", "port", "ipv6", "cycle", "origin", "budget"])
def test_invalid_pagination_refuses_without_plan_or_source_mutation(
    tmp_path: Path, metadata_server: Any, case: str
) -> None:
    repos = make_repositories(tmp_path)
    holder: dict[str, MetadataServer] = {}

    def page(handler: Any, server: MetadataServer) -> None:
        holder["server"] = server
        if case == "malformed":
            links = [f'<{server.url}/older>; rel=next']
        elif case == "port":
            links = ['<http://127.0.0.1:invalid/older>; rel="next"']
        elif case == "ipv6":
            links = ['<http://[invalid/older>; rel="next"']
        elif case == "cycle":
            links = [f'<{server.url}/releases?page=1>; rel="next"']
        elif case == "origin":
            links = ['<https://example.invalid/older>; rel="next"']
        else:
            links = [f'<{server.url}/older>; rel="next"']
        send_json_links(handler, [], links)

    server = metadata_server(
        {"/releases": page, "/older": lambda h, s: send_json(h, []), "*": missing}
    )
    plan = tmp_path / "plan.json"
    before = source_fingerprint(repos["source"])
    limits = ("--max-pages", "1") if case == "budget" else ()

    result = preview_live(repos, server, plan, *limits)

    assert result.returncode == 2, result.stderr
    assert json.loads(result.stderr)["status"] == "refused"
    assert not plan.exists()
    assert source_fingerprint(repos["source"]) == before


@pytest.mark.parametrize("stall", [stall_headers, trickle_body], ids=["headers", "body"])
def test_network_timeout_bounds_each_whole_http_request(
    tmp_path: Path, metadata_server: Any, stall: Callable[..., None]
) -> None:
    routes = {"/releases": stall, "*": missing}
    run_http_timeout(
        tmp_path, metadata_server, routes, "--network-timeout", "1", launched=True
    )


@pytest.mark.parametrize(
    "routes",
    [
        {"/releases": stall_headers, "*": missing},
        {"/releases": paced(endless_pages), "*": missing},
        {"/releases": lambda h, s: send_json(h, MANY_UNPUBLISHED), "*": paced(missing)},
    ],
    ids=["stalled-request", "successive-pages", "successive-pypi"],
)
def test_attempt_timeout_bounds_all_http_metadata_collectively(
    tmp_path: Path, metadata_server: Any, routes: dict[str, Any]
) -> None:
    # --network-timeout cannot fire before the hang guard; only the single attempt budget can.
    limits = ("--network-timeout", "60", "--attempt-timeout", "3", "--max-pages", "10000")
    launched = routes["/releases"] is stall_headers  # the only case with a known live worker
    run_http_timeout(tmp_path, metadata_server, routes, *limits, launched=launched)


@pytest.mark.parametrize("flag", ["--subprocess-timeout", "--attempt-timeout", "--network-timeout"])
@pytest.mark.parametrize("value", ["inf", "nan", "0", "-1"])
def test_preview_rejects_nonfinite_or_nonpositive_timeouts_before_work(
    tmp_path: Path, metadata_server: Any, flag: str, value: str
) -> None:
    repos = make_repositories(tmp_path)
    server = metadata_server(LIVE_ROUTES)
    plan_path = tmp_path / "plan.json"
    before = source_fingerprint(repos["source"])

    result = preview_live(repos, server, plan_path, f"{flag}={value}")

    assert result.returncode == 2, result.stderr
    assert json.loads(result.stderr)["status"] == "refused"
    assert server.requests == []
    assert not plan_path.exists()
    assert source_fingerprint(repos["source"]) == before


def resign(plan: dict[str, Any]) -> dict[str, Any]:
    """Recompute the documented plan binding (sha256 of canonical JSON) as a forger could."""

    def digest(value: dict[str, Any]) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode()).hexdigest()

    unsigned = {key: value for key, value in plan.items() if key not in {"integrity", "plan_id"}}
    unsigned["plan_id"] = digest(unsigned)
    unsigned["integrity"] = {"algorithm": "sha256", "sha256": digest(unsigned)}
    return unsigned


def attempt_absent(repos: dict[str, Any], output: Path, branch: str) -> bool:
    ref = git(repos["source"], "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return not output.exists() and ref.returncode != 0


def test_apply_refuses_tampered_plans_and_mismatched_paths_without_mutation(
    tmp_path: Path,
) -> None:
    repos = make_repositories(tmp_path)
    plan_path = tmp_path / "plan.json"
    assert preview_override(repos, plan_path).returncode == 0
    original = json.loads(plan_path.read_text(encoding="utf-8"))
    output = tmp_path / "attempt"
    branch = "codex/tampered"
    edited = json.loads(json.dumps(original))
    edited["selection"]["target_commit"] = repos["base"]
    forged_base = resign(json.loads(json.dumps(original)))
    forged_base["candidate"]["fork_base"] = repos["candidate"]
    forged_base = resign(forged_base)
    forged_url = json.loads(json.dumps(original))
    forged_url["upstream_repository"]["url"] = str(tmp_path / "upstream-work")
    forged_url = resign(forged_url)
    cases: list[tuple[str, dict[str, Any], dict[str, str]]] = [
        ("edited-without-binding", edited, {}),
        ("forged-fork-base", forged_base, {}),
        ("forged-upstream-url", forged_url, {}),
        ("explicit-url-mismatch", original, {"--upstream-url": str(tmp_path / "upstream-work")}),
    ]
    before = source_fingerprint(repos["source"])

    for name, plan, overrides in cases:
        candidate_plan = tmp_path / f"{name}.json"
        candidate_plan.write_text(json.dumps(plan), encoding="utf-8")
        arguments = list(apply_args(repos, candidate_plan, output, tmp_path / name, branch))
        for flag, value in overrides.items():
            arguments[arguments.index(flag) + 1] = value

        result = run_cli(*arguments)

        assert result.returncode == 2, (name, result.stdout, result.stderr)
        assert json.loads(result.stderr)["status"] == "refused", name
        assert attempt_absent(repos, output, branch), name
        assert source_fingerprint(repos["source"]) == before, name


def test_apply_requires_the_preview_exact_byte_digest_before_any_mutation(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    plan_path = tmp_path / "plan.json"
    preview = preview_override(repos, plan_path)
    assert preview.returncode == 0, preview.stderr
    preview_outcome = json.loads(preview.stdout)
    expected = preview_outcome["plan_sha256"]
    assert expected == hashlib.sha256(plan_path.read_bytes()).hexdigest()
    original = json.loads(plan_path.read_text(encoding="utf-8"))
    changed = json.loads(json.dumps(original))
    changed["observed_at"] = "2030-01-01T00:00:00Z"
    changed["selection"]["override"]["reason"] = "rehashed but not caller-approved"
    changed = resign(changed)
    plan_path.write_text(json.dumps(changed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    branch = "codex/exact-plan-bytes"
    before = source_fingerprint(repos["source"])

    result = run_cli(
        *apply_args(
            repos,
            plan_path,
            output,
            evidence,
            branch,
            expected_plan_sha256=expected,
        )
    )

    assert result.returncode == 2, result.stderr
    assert "exact-byte" in json.loads(result.stderr)["error"]
    assert not evidence.exists()
    assert attempt_absent(repos, output, branch)
    assert source_fingerprint(repos["source"]) == before


@pytest.mark.parametrize(
    ("remove_flag", "replace_flag", "replacement"),
    [
        ("--expected-plan-sha256", None, None),
        (None, "--expected-plan-sha256", "not-a-digest"),
        ("--committer-name", None, None),
        ("--committer-email", None, None),
    ],
)
def test_apply_requires_pin_and_complete_identity_at_the_public_cli(
    tmp_path: Path,
    remove_flag: str | None,
    replace_flag: str | None,
    replacement: str | None,
) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    branch = "codex/required-inputs"
    arguments = list(apply_args(repos, plan, output, evidence, branch))
    if remove_flag is not None:
        index = arguments.index(remove_flag)
        del arguments[index : index + 2]
    else:
        assert replace_flag is not None and replacement is not None
        arguments[arguments.index(replace_flag) + 1] = replacement
    before = source_fingerprint(repos["source"])

    result = run_cli(*arguments)

    assert result.returncode == 2
    assert not evidence.exists()
    assert attempt_absent(repos, output, branch)
    assert source_fingerprint(repos["source"]) == before


def test_concurrent_preview_publication_has_one_winner_and_preserves_exact_bytes(
    tmp_path: Path,
) -> None:
    repos = make_repositories(tmp_path)
    output = tmp_path / "plan.json"
    argv = (
        "preview",
        "--source-repo",
        str(repos["source"]),
        "--candidate",
        repos["candidate"],
        "--upstream-repository",
        "Graphify-Labs/graphify",
        "--upstream-url",
        str(repos["upstream"]),
        "--override-sha",
        repos["target"],
        "--override-reason",
        "exclusive publication",
        "--output-plan",
        str(output),
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "tools.fork_maintenance", *argv],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=120) for process in processes]
    assert sorted(process.returncode for process in processes) == [0, 2]
    winner = next(json.loads(stdout) for process, (stdout, _) in zip(processes, results) if process.returncode == 0)
    frozen = output.read_bytes()
    assert hashlib.sha256(frozen).hexdigest() == winner["plan_sha256"]
    loser_stderr = next(stderr for process, (_, stderr) in zip(processes, results) if process.returncode == 2)
    assert "overwrite" in json.loads(loser_stderr)["error"]
    assert output.read_bytes() == frozen


def test_apply_uses_only_the_explicit_committer_and_preserves_authorship(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    source = repos["source"]
    git(source, "config", "--unset", "user.name")
    git(source, "config", "--unset", "user.email")
    caller_home = tmp_path / "caller-home"
    caller_home.mkdir()
    (caller_home / ".gitconfig").write_text(
        "[user]\n\tname = Global Caller\n\temail = global@example.invalid\n", encoding="utf-8"
    )
    plan = tmp_path / "plan.json"
    preview = preview_override(repos, plan)
    assert preview.returncode == 0, preview.stderr
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    arguments = apply_args(repos, plan, output, evidence, "codex/explicit-identity")
    env = {**os.environ, "HOME": str(caller_home)}

    applied = run_cli(*arguments, env=env)

    assert applied.returncode == 0, applied.stderr
    identities = git(output, "show", "-s", "--format=%an|%ae|%cn|%ce", "HEAD").stdout.strip()
    assert identities == (
        "Fixture|fixture@example.invalid|Maintenance Fixture|"
        "maintenance-fixture@example.invalid"
    )
    receipt = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
    assert receipt["attempt"]["committer"] == {
        "name": "Maintenance Fixture",
        "email": "maintenance-fixture@example.invalid",
    }
    assert "Global Caller" not in json.dumps(receipt)


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--committer-name", ""),
        ("--committer-name", "Bad\nName"),
        ("--committer-email", "not-an-email"),
        ("--committer-email", "bad@example.invalid>"),
    ],
)
def test_apply_refuses_invalid_explicit_identity_before_mutation(
    tmp_path: Path, flag: str, value: str
) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    branch = "codex/invalid-identity"
    arguments = list(apply_args(repos, plan, output, evidence, branch))
    arguments[arguments.index(flag) + 1] = value
    before = source_fingerprint(repos["source"])

    result = run_cli(*arguments)

    assert result.returncode == 2, result.stderr
    assert json.loads(result.stderr)["status"] == "refused"
    assert not evidence.exists()
    assert attempt_absent(repos, output, branch)
    assert source_fingerprint(repos["source"]) == before


def test_preview_refuses_local_clean_filter_before_it_can_execute(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    source = repos["source"]
    marker = tmp_path / "filter-ran"
    helper = tmp_path / "clean-helper"
    helper.write_text(
        f"#!/bin/sh\nprintf ran >> '{marker}'\ncat\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    attributes = source / ".gitattributes"
    attributes.write_text("fork.txt filter=danger\n", encoding="utf-8")
    git(source, "add", ".gitattributes")
    git(source, "commit", "-m", "add filter attributes")
    repos["candidate"] = git(source, "rev-parse", "HEAD").stdout.strip()
    git(source, "config", "filter.danger.clean", str(helper))
    # Positive control: force ordinary Git to convert changed, same-size tracked content.
    tracked = source / "fork.txt"
    tracked.write_bytes(b"dirty data\n")
    stat = tracked.stat()
    os.utime(tracked, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    git(source, "hash-object", "--path=fork.txt", "fork.txt")
    assert marker.read_bytes() == b"ran"
    marker.unlink()
    plan = tmp_path / "plan.json"

    refused = preview_override(repos, plan)

    assert refused.returncode == 2, refused.stderr
    outcome = json.loads(refused.stderr)
    assert "filter.*.executable" in outcome["error"]
    assert str(helper) not in outcome["error"]
    assert not marker.exists()
    assert not plan.exists()


def test_preview_refuses_worktree_clean_filter_before_status_can_execute(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    source = repos["source"]
    marker = tmp_path / "worktree-clean-ran"
    helper = tmp_path / "worktree-clean-helper"
    helper.write_text(
        f"#!/bin/sh\nprintf ran >> '{marker}'\ncat\n", encoding="utf-8"
    )
    helper.chmod(0o755)
    (source / ".gitattributes").write_text("fork.txt filter=danger\n", encoding="utf-8")
    git(source, "add", ".gitattributes")
    git(source, "commit", "-m", "worktree filter attributes")
    repos["candidate"] = git(source, "rev-parse", "HEAD").stdout.strip()
    git(source, "config", "extensions.worktreeConfig", "true")
    git(source, "config", "--worktree", "filter.danger.clean", str(helper))
    tracked = source / "fork.txt"
    tracked.write_bytes(b"dirty data\n")
    stat = tracked.stat()
    os.utime(tracked, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    git(source, "hash-object", "--path=fork.txt", "fork.txt")
    assert marker.read_bytes() == b"ran"
    marker.unlink()

    refused = preview_override(repos, tmp_path / "plan.json")

    assert refused.returncode == 2, refused.stderr
    outcome = json.loads(refused.stderr)
    assert "filter.*.executable" in outcome["error"]
    assert str(helper) not in outcome["error"]
    assert not marker.exists()


def test_preview_structures_output_parent_filesystem_failure(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_bytes(b"preserve exact bytes\n")
    output = parent_file / "plan.json"
    before = source_fingerprint(repos["source"])

    result = preview_override(repos, output)

    assert result.returncode == 2, result.stderr
    outcome = json.loads(result.stderr)
    assert outcome["status"] == "refused"
    assert "filesystem operation failed" in outcome["error"]
    assert "Traceback" not in result.stderr
    assert parent_file.read_bytes() == b"preserve exact bytes\n"
    assert source_fingerprint(repos["source"]) == before


def test_preview_refuses_local_smudge_filter_before_it_can_execute(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    source = repos["source"]
    marker = tmp_path / "smudge-ran"
    helper = tmp_path / "smudge-helper"
    helper.write_text(
        f"#!/bin/sh\nprintf ran >> '{marker}'\ncat\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    (source / ".gitattributes").write_text("fork.txt filter=danger\n", encoding="utf-8")
    git(source, "add", ".gitattributes")
    git(source, "commit", "-m", "add smudge attributes")
    repos["candidate"] = git(source, "rev-parse", "HEAD").stdout.strip()
    git(source, "config", "filter.danger.smudge", str(helper))
    (source / "fork.txt").unlink()
    git(source, "checkout", "--", "fork.txt")
    assert marker.read_bytes() == b"ran"
    marker.unlink()

    refused = preview_override(repos, tmp_path / "plan.json")

    assert refused.returncode == 2, refused.stderr
    assert "filter.*.executable" in json.loads(refused.stderr)["error"]
    assert not marker.exists()


def test_preview_refuses_local_url_rewrite_before_ext_helper_can_execute(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    source = repos["source"]
    marker = tmp_path / "ext-ran"
    helper = tmp_path / "ext-helper"
    helper.write_text(f"#!/bin/sh\nprintf ran > '{marker}'\nexit 1\n", encoding="utf-8")
    helper.chmod(0o755)
    git(source, "config", "protocol.ext.allow", "always")
    git(source, "config", f"url.ext::{helper} %S.insteadOf", "marker://")
    control = git(source, "ls-remote", "marker://repository", check=False)
    assert control.returncode != 0 and marker.read_bytes() == b"ran"
    marker.unlink()

    refused = preview_override(repos, tmp_path / "plan.json")

    assert refused.returncode == 2, refused.stderr
    outcome = json.loads(refused.stderr)
    assert "url.*.rewrite" in outcome["error"]
    assert str(helper) not in outcome["error"]
    assert not marker.exists()


@pytest.mark.parametrize(
    ("key", "category"),
    [
        ("merge.danger.driver", "merge.*.driver"),
        ("credential.helper", "credential.*.helper"),
        ("include.path", "include.path"),
    ],
)
def test_preview_refuses_other_executable_local_configuration_by_key_only(
    tmp_path: Path, key: str, category: str
) -> None:
    repos = make_repositories(tmp_path)
    secret_value = str(tmp_path / "must-not-be-recorded")
    git(repos["source"], "config", key, secret_value)

    refused = preview_override(repos, tmp_path / "plan.json")

    assert refused.returncode == 2, refused.stderr
    outcome = json.loads(refused.stderr)
    assert category in outcome["error"]
    assert secret_value not in outcome["error"]


def test_preview_refuses_worktree_include_without_reading_its_target(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    source = repos["source"]
    missing_secret_target = tmp_path / "secret-config-that-must-not-be-read"
    git(source, "config", "extensions.worktreeConfig", "true")
    git(source, "config", "--worktree", "include.path", str(missing_secret_target))

    refused = preview_override(repos, tmp_path / "plan.json")

    assert refused.returncode == 2, refused.stderr
    outcome = json.loads(refused.stderr)
    assert "include.path" in outcome["error"]
    assert str(missing_secret_target) not in outcome["error"]


def test_owned_operational_failure_keeps_raw_evidence_and_replays_read_only(
    tmp_path: Path,
) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    branch = "codex/owned-failure"
    arguments = apply_args(repos, plan, output, evidence, branch)
    objects = repos["source"] / ".git" / "objects"
    original_mode = objects.stat().st_mode
    objects.chmod(0o500)
    try:
        failed = run_cli(*arguments)
    finally:
        objects.chmod(original_mode)

    assert failed.returncode == 2, failed.stderr
    outcome = json.loads(failed.stderr)
    assert outcome["status"] == "operational_failure"
    result_path = evidence / "result.json"
    receipt_bytes = result_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    assert receipt["status"] == "operational_failure"
    assert receipt["recovery"]["automatic_action"] == "none"
    assert not (evidence / "attempt.lock").exists()
    for command in receipt["command_records"]:
        for stream in ("stdout", "stderr"):
            raw = Path(command[stream]["raw_path"]).read_bytes()
            assert len(raw) == command[stream]["bytes"]
            assert hashlib.sha256(raw).hexdigest() == command[stream]["sha256"]
    before = source_fingerprint(repos["source"])

    repeated = run_cli(*arguments)

    assert repeated.returncode == 2, repeated.stderr
    assert json.loads(repeated.stderr)["repeated"] is True
    assert result_path.read_bytes() == receipt_bytes
    assert source_fingerprint(repos["source"]) == before


def test_owned_timeout_finalizes_and_repeat_does_not_retry(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    source = repos["source"]
    target = repos["target"]
    blocked_object = source / ".git" / "objects" / target[:2] / target[2:]
    blocked_object.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(blocked_object)
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    branch = "codex/owned-timeout"
    arguments = (*apply_args(repos, plan, output, evidence, branch), "--subprocess-timeout", "1")
    try:
        timed_out = run_cli(*arguments, descendants_ignore_sigterm=True, timeout=30)
        assert timed_out.returncode == 4, timed_out.stderr
        outcome = json.loads(timed_out.stderr)
        assert outcome["status"] == "timeout"
        result_path = evidence / "result.json"
        receipt_bytes = result_path.read_bytes()
        receipt = json.loads(receipt_bytes)
        assert receipt["status"] == "timeout"
        assert receipt["diagnostic"]["process_group_settled"] is True
        assert not fifo_has_blocked_reader(blocked_object)
        repeated = run_cli(*arguments, timeout=30)
        assert repeated.returncode == 4, repeated.stderr
        assert json.loads(repeated.stderr)["repeated"] is True
        assert result_path.read_bytes() == receipt_bytes
    finally:
        if blocked_object.exists():
            blocked_object.unlink()


def test_evidence_persistence_failure_retains_owned_lock_and_paths(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    (evidence / "raw").mkdir(parents=True)
    branch = "codex/evidence-failure"

    result = run_cli(*apply_args(repos, plan, output, evidence, branch))

    assert result.returncode == 2, result.stderr
    outcome = json.loads(result.stderr)
    assert outcome["status"] == "evidence_error"
    assert outcome["attempt_owned"] is True
    assert outcome["result_path"] == str(evidence / "result.json")
    assert outcome["raw_evidence_path"] == str(evidence / "raw")
    assert outcome["lock_path"] == str(evidence / "attempt.lock")
    assert (evidence / "attempt.lock").exists()
    assert not (evidence / "result.json").exists()


@pytest.mark.parametrize("interrupt_signal", [signal.SIGTERM, signal.SIGINT])
def test_catchable_signal_during_owned_apply_settles_git_and_finalizes_receipt(
    tmp_path: Path, interrupt_signal: signal.Signals
) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    source = repos["source"]
    target = repos["target"]
    assert git(source, "cat-file", "-e", target, check=False).returncode != 0
    blocked_object = source / ".git" / "objects" / target[:2] / target[2:]
    blocked_object.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(blocked_object)
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    arguments = apply_args(repos, plan, output, evidence, "codex/interrupted")
    process = subprocess.Popen(
        [sys.executable, "-m", "tools.fork_maintenance", *arguments],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    fifo_writer: int | None = None
    try:
        for _ in range(500):
            if (evidence / "attempt.lock").exists():
                try:
                    fifo_writer = os.open(blocked_object, os.O_WRONLY | os.O_NONBLOCK)
                except OSError as exc:
                    assert exc.errno == errno.ENXIO
                else:
                    break  # a verified owned Git reader is blocked on this fixture FIFO
            threading.Event().wait(0.02)
        else:
            pytest.fail("owned blocked Git fetch did not reach explicit process readiness")

        os.kill(process.pid, interrupt_signal)
        os.kill(process.pid, interrupt_signal)  # repeated catchable signal must coalesce
        stdout, stderr = process.communicate(timeout=HANG_GUARD)
        assert process.returncode == 4, (stdout, stderr)
        outcome = json.loads(stderr)
        assert outcome["status"] == "interrupted"
        receipt = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
        assert receipt["status"] == "interrupted"
        assert receipt["diagnostic"]["signal"] == signal.Signals(interrupt_signal).name
        assert receipt["diagnostic"]["process_group_settled"] is True
        assert not (evidence / "attempt.lock").exists()
        interrupted = next(
            record for record in receipt["command_records"] if record.get("timed_out") is False
            and record.get("process_group_settled") is True
            and "fetch" in record["argv"]
        )
        for probe in (os.kill, os.killpg):
            with pytest.raises(ProcessLookupError):
                probe(interrupted["pid"], 0)
        assert fifo_writer is not None
        os.close(fifo_writer)
        fifo_writer = None
        assert not fifo_has_blocked_reader(blocked_object)
    finally:
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(HANG_GUARD)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        if fifo_writer is not None:
            os.close(fifo_writer)
        if blocked_object.exists():
            blocked_object.unlink()


def test_exact_sha_override_freezes_fetchable_non_tip_commit(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    work = repos["upstream_work"]
    non_tip = commit_file(work, "upstream.txt", "non-tip target\n", "non-tip target")
    commit_file(work, "later.txt", "later tip\n", "later tip")
    git(work, "tag", "-a", "annotated", "-m", "annotated fixture tag", repos["base"])
    git(work, "push", "publish", "main", "refs/tags/annotated")
    tag_object = git(work, "rev-parse", "refs/tags/annotated").stdout.strip()
    assert tag_object != repos["base"]
    assert non_tip not in git(work, "ls-remote", str(repos["upstream"])).stdout  # SHA-only
    plan_path = tmp_path / "plan.json"
    reason = ("--override-reason", "exact fixture commit")
    invalid: dict[str, tuple[str, tuple[str, ...]]] = {
        "absent": ("deadbeef" * 5, reason),
        "source-only": (repos["candidate"], reason),
        "annotated-tag-object": (tag_object, reason),
        "missing-reason": (non_tip, ()),
        "empty-reason": (non_tip, ("--override-reason", "")),
        "blank-reason": (non_tip, ("--override-reason", "   ")),
    }
    before = source_fingerprint(repos["source"])

    for name, (sha, reason_args) in invalid.items():
        result = preview_sha(repos, plan_path, sha, *reason_args)

        assert result.returncode == 2, (name, result.stdout, result.stderr)
        assert json.loads(result.stderr)["status"] == "refused", name
        assert not plan_path.exists(), name
        assert source_fingerprint(repos["source"]) == before, name

    valid = preview_sha(repos, plan_path, non_tip, *reason)

    assert valid.returncode == 0, valid.stderr
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["selection"]["target_commit"] == non_tip
    assert plan["selection"]["override"] == {"reason": "exact fixture commit", "sha": non_tip}
    assert source_fingerprint(repos["source"]) == before
    commit_file(work, "later.txt", "advanced tip\n", "advance tip")  # tips move after planning
    git(work, "push", "publish", "main")
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    branch = "codex/exact-sha"

    def forged(mutate: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
        copy = json.loads(json.dumps(plan))
        mutate(copy["selection"])
        return resign(copy)

    tampered = {
        "override-sha-mismatch": forged(lambda s: s["override"].update(sha=repos["target"])),
        "target-mismatch": forged(lambda s: s.update(target_commit=repos["target"])),
        "missing-reason": forged(lambda s: s["override"].pop("reason")),
        "blank-reason": forged(lambda s: s["override"].update(reason=" ")),
        "missing-override": forged(lambda s: s.update(override=None)),
    }
    for name, forged_plan in tampered.items():
        forged_path = tmp_path / f"{name}.json"
        forged_path.write_text(json.dumps(forged_plan), encoding="utf-8")

        result = run_cli(*apply_args(repos, forged_path, output, tmp_path / name, branch))

        assert result.returncode == 2, (name, result.stdout, result.stderr)
        assert json.loads(result.stderr)["status"] == "refused", name
        assert attempt_absent(repos, output, branch), name
        assert not (tmp_path / name).exists(), name
        assert source_fingerprint(repos["source"]) == before, name

    applied = run_cli(*apply_args(repos, plan_path, output, evidence, branch))

    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["status"] == "applied"
    assert git(output, "rev-parse", "HEAD^").stdout.strip() == non_tip
    assert (output / "upstream.txt").read_text(encoding="utf-8") == "non-tip target\n"
    assert not (output / "later.txt").exists()
    receipt = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
    assert receipt["acquisition"]["target_commit"] == non_tip


def test_primed_rerere_resolution_is_not_replayed_by_apply(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    source = repos["source"]
    repos["candidate"] = commit_file(source, "shared.txt", "fork version\n", "fork conflict")
    repos["target"] = commit_file(
        repos["upstream_work"], "shared.txt", "upstream version\n", "upstream conflict"
    )
    git(repos["upstream_work"], "push", "publish", "main")
    git(source, "config", "rerere.enabled", "true")
    git(source, "config", "rerere.autoUpdate", "true")
    git(source, "fetch", "origin")
    resolution = b"recorded rerere resolution\n"
    assert git(source, "merge", "--no-edit", "origin/main", check=False).returncode != 0
    (source / "shared.txt").write_bytes(resolution)
    git(source, "add", "shared.txt")
    git(source, "commit", "--no-edit")
    postimages = list((source / ".git" / "rr-cache").glob("*/postimage"))
    assert [path.read_bytes() for path in postimages] == [resolution]
    # Positive control: plain Git in this fixture replays and stages the recorded resolution.
    git(source, "reset", "--hard", repos["candidate"])
    git(source, "merge", "--no-edit", "origin/main", check=False)
    assert (source / "shared.txt").read_bytes() == resolution
    assert git(source, "diff", "--name-only", "--diff-filter=U").stdout == ""
    git(source, "reset", "--hard", repos["candidate"])
    assert git(source, "rev-parse", "HEAD").stdout.strip() == repos["candidate"]
    assert git(source, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""
    plan = tmp_path / "plan.json"
    preview = preview_override(repos, plan)
    assert preview.returncode == 0, preview.stderr
    output = tmp_path / "attempt"

    result = run_cli(*apply_args(repos, plan, output, tmp_path / "evidence", "codex/rerere"))

    assert result.returncode == 3, result.stderr
    outcome = json.loads(result.stdout)
    assert (outcome["status"], outcome["conflicted_paths"]) == ("conflict", ["shared.txt"])
    conflicted = (output / "shared.txt").read_bytes()
    assert b"<<<<<<<" in conflicted and conflicted != resolution
    assert git(output, "diff", "--name-only", "--diff-filter=U").stdout == "shared.txt\n"
    assert [path.read_bytes() for path in postimages] == [resolution]


def test_inherited_git_environment_cannot_redirect_or_run_helpers(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    source = repos["source"]
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    git(foreign, "init", "-b", "main")
    commit_file(foreign, "foreign.txt", "foreign state\n", "foreign")
    (foreign / "untracked.txt").write_text("foreign untracked\n", encoding="utf-8")
    markers = tmp_path / "markers"
    markers.mkdir()
    helpers: dict[str, str] = {}
    for kind, tail in (("diff", "exit 0"), ("textconv", 'cat "$1"'), ("fsmonitor", "exit 1")):
        helper = tmp_path / f"{kind}-helper"
        helper.write_text(
            f"#!/bin/sh\nprintf ran >> '{markers / kind}'\n{tail}\n", encoding="utf-8"
        )
        helper.chmod(0o755)
        helpers[kind] = str(helper)
    attributes = tmp_path / "attributes"
    attributes.write_text("* diff=marker\n", encoding="utf-8")
    config = tmp_path / "injected.gitconfig"
    config.write_text(
        f"[diff]\n\texternal = {helpers['diff']}\n[core]\n\tattributesFile = {attributes}\n",
        encoding="utf-8",
    )
    configured = {
        **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_SYSTEM": str(config),
        "GIT_CONFIG_NOSYSTEM": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": helpers["fsmonitor"],
        "GIT_CONFIG_PARAMETERS": f"'diff.marker.textconv={helpers['textconv']}'",
    }
    # Positive control: plain Git under this configuration really runs every planted helper.
    probe = tmp_path / "probe"
    probe.mkdir()
    git(probe, "init", "-b", "main")
    commit_file(probe, "probe.txt", "one\n", "probe")
    (probe / "probe.txt").write_text("two\n", encoding="utf-8")
    for command in (("diff",), ("diff", "--no-ext-diff"), ("status",)):
        subprocess.run(
            [GIT, "-C", str(probe), *command],
            env=configured,
            capture_output=True,
            check=False,
            timeout=60,
        )
    assert sorted(path.name for path in markers.iterdir()) == ["diff", "fsmonitor", "textconv"]
    for marker in list(markers.iterdir()):
        marker.unlink()
    env = {
        **configured,
        "GIT_DIR": str(foreign / ".git"),
        "GIT_WORK_TREE": str(foreign),
        "GIT_INDEX_FILE": str(tmp_path / "redirected-index"),
    }

    def foreign_state() -> tuple[dict[str, str], str]:
        # Fixture Git runs with its own fixed environment, so observing cannot run helpers.
        return source_fingerprint(foreign), git(foreign, "ls-files", "--stage").stdout

    foreign_before = foreign_state()
    config_bytes = config.read_bytes()
    before = source_fingerprint(source)
    assert list(markers.iterdir()) == []
    plan_path = tmp_path / "plan.json"
    output = tmp_path / "attempt"

    preview = preview_sha(
        repos, plan_path, repos["target"], "--override-reason", "injected environment", env=env
    )
    assert preview.returncode == 0, preview.stderr
    applied = run_cli(
        *apply_args(repos, plan_path, output, tmp_path / "evidence", "codex/env"), env=env
    )

    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["status"] == "applied"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["source_repository"]["worktree"] == str(source.resolve())
    assert plan["source_repository"]["git_common_dir"] == str((source / ".git").resolve())
    assert plan["candidate"] == {"commit": repos["candidate"], "fork_base": repos["base"]}
    assert plan["capability_manifest"] == [
        {"classification": "pending", "path": "fork.txt", "status": "A"}
    ]
    assert (output / "fork.txt").read_text(encoding="utf-8") == "fork delta\n"
    assert (output / "upstream.txt").read_text(encoding="utf-8") == "upstream target\n"
    assert git(output, "rev-parse", "HEAD^").stdout.strip() == repos["target"]
    assert list(markers.iterdir()) == []
    assert foreign_state() == foreign_before
    assert not (tmp_path / "redirected-index").exists()
    assert config.read_bytes() == config_bytes
    after = source_fingerprint(source)
    assert (after["head"], after["status"], after["fetch_head"]) == (
        before["head"],
        before["status"],
        before["fetch_head"],
    )


OWNER_SCRIPT = """
import os, sys
lock = sys.argv[1]
os.makedirs(os.path.dirname(lock), exist_ok=True)
descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(descriptor, b"owner=fixture\\n")
os.close(descriptor)
print("ready", flush=True)
sys.stdin.read()
os.unlink(lock)
print("released", flush=True)
"""


def test_competing_apply_refuses_while_a_live_owner_holds_the_attempt_lock(
    tmp_path: Path,
) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    branch = "codex/owned"
    lock = evidence / "attempt.lock"
    arguments = apply_args(repos, plan, output, evidence, branch)
    owner = subprocess.Popen(
        [sys.executable, "-I", "-c", OWNER_SCRIPT, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert owner.stdout is not None
        ready, _, _ = select.select([owner.stdout], [], [], HANG_GUARD)  # explicit readiness
        assert ready and owner.stdout.readline() == "ready\n", owner.poll()
        held = lock.read_bytes()
        held_identity = (lock.stat().st_ino, lock.stat().st_mtime_ns)
        before = source_fingerprint(repos["source"])

        competing = run_cli(*arguments)

        assert competing.returncode == 2, competing.stderr
        refusal = json.loads(competing.stderr)
        assert refusal["status"] == "refused" and "lock" in refusal["error"]
        assert competing.stdout == ""
        assert owner.poll() is None  # the existing owner is preserved, not displaced
        assert lock.read_bytes() == held
        assert (lock.stat().st_ino, lock.stat().st_mtime_ns) == held_identity
        assert [path.name for path in evidence.iterdir()] == ["attempt.lock"]
        assert attempt_absent(repos, output, branch)
        assert source_fingerprint(repos["source"]) == before
        released, errors = owner.communicate(timeout=HANG_GUARD)
        assert (owner.returncode, released) == (0, "released\n"), errors
        assert not lock.exists()
    finally:
        if owner.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(HANG_GUARD)
        for stream in (owner.stdin, owner.stdout, owner.stderr):
            if stream is not None:
                stream.close()

    original = run_cli(*arguments)

    assert original.returncode == 0, original.stderr
    assert json.loads(original.stdout)["status"] == "applied"
    assert (evidence / "result.json").exists()
    assert not lock.exists()


def test_apply_refuses_occupied_destinations_and_dirty_replay_without_mutation(
    tmp_path: Path,
) -> None:
    repos = make_repositories(tmp_path)
    source = repos["source"]
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "foreign.txt").write_bytes(b"foreign destination bytes\n")
    git(source, "branch", "codex/existing", repos["base"])
    cases = [
        ("foreign-destination", occupied, "codex/fresh"),
        ("existing-branch", tmp_path / "fresh", "codex/existing"),
    ]
    before = source_fingerprint(source)

    for name, output, branch in cases:
        evidence = tmp_path / f"{name}-evidence"

        result = run_cli(*apply_args(repos, plan, output, evidence, branch))

        assert result.returncode == 2, (name, result.stdout, result.stderr)
        assert json.loads(result.stderr)["status"] == "refused", name
        assert not evidence.exists(), name
        assert source_fingerprint(source) == before, name
    assert [path.name for path in occupied.iterdir()] == ["foreign.txt"]
    assert (occupied / "foreign.txt").read_bytes() == b"foreign destination bytes\n"
    assert not (tmp_path / "fresh").exists()
    assert git(source, "rev-parse", "refs/heads/codex/existing").stdout.strip() == repos["base"]
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    arguments = apply_args(repos, plan, output, evidence, "codex/dirty")
    assert run_cli(*arguments).returncode == 0
    receipt = (evidence / "result.json").read_bytes()
    head = git(output, "rev-parse", "HEAD").stdout
    (output / "fork.txt").write_bytes(b"dirty tracked edit\n")
    (output / "scratch.txt").write_bytes(b"dirty untracked bytes\n")

    replay = run_cli(*arguments)

    assert replay.returncode == 2, replay.stderr
    assert json.loads(replay.stderr)["status"] == "refused"
    assert (output / "fork.txt").read_bytes() == b"dirty tracked edit\n"
    assert (output / "scratch.txt").read_bytes() == b"dirty untracked bytes\n"
    assert (evidence / "result.json").read_bytes() == receipt
    assert sorted(path.name for path in evidence.iterdir()) == ["raw", "result.json"]
    assert git(output, "rev-parse", "HEAD").stdout == head


METADATA_LIMIT = 8 * 1024 * 1024  # the CLI's per-response byte bound


def raw_body(body: bytes) -> Callable[..., None]:
    def respond(handler: Any, server: MetadataServer) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    return respond


def oversized_releases(handler: Any, server: MetadataServer) -> None:
    # Valid JSON that would select v1.1.0 if the byte bound were not enforced.
    entry = json.dumps([release_entry("v1.1.0", 1)]).encode()
    raw_body(entry[:-1] + b" " * METADATA_LIMIT + b"]")(handler, server)


@pytest.mark.parametrize(
    ("releases", "fragment"),
    [
        (raw_body(b"[{not json"), "HTTP request failed"),
        (raw_body(b'{"tag_name": "v1.1.0"}'), "must be an array"),
        (
            raw_body(b'[{"tag_name": "v1.1.0", "draft": false, "prerelease": false}]'),
            "published_at",
        ),
        (oversized_releases, "exceeds"),
    ],
    ids=["invalid-json", "not-array", "missing-published-at", "oversized"],
)
def test_preview_refuses_malformed_or_oversized_live_metadata(
    tmp_path: Path, metadata_server: Any, releases: Callable[..., None], fragment: str
) -> None:
    repos = make_repositories(tmp_path)
    server = metadata_server({**LIVE_ROUTES, "/releases": releases})
    plan_path = tmp_path / "plan.json"
    before = source_fingerprint(repos["source"])

    result = preview_live(repos, server, plan_path)

    assert result.returncode == 2, result.stderr
    outcome = json.loads(result.stderr)
    assert (outcome["status"], fragment in outcome["error"]) == ("refused", True), outcome
    assert "/releases?page=1" in server.requests
    assert not plan_path.exists()
    assert source_fingerprint(repos["source"]) == before


@pytest.mark.parametrize("flag", ["--subprocess-timeout", "--attempt-timeout"])
def test_apply_rejects_nonfinite_or_nonpositive_timeouts_before_plan_access(
    tmp_path: Path, flag: str
) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    plan_bytes = plan.read_bytes()
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    branch = "codex/bounds"
    before = source_fingerprint(repos["source"])

    # The absent plan proves ordering: reading any plan first would fail differently.
    for plan_path in (plan, tmp_path / "absent-plan.json"):
        for value in ("inf", "nan", "0", "-1"):
            arguments = apply_args(repos, plan_path, output, evidence, branch)

            result = run_cli(*arguments, f"{flag}={value}")

            assert result.returncode == 2, (plan_path, value, result.stderr)
            outcome = json.loads(result.stderr)
            assert (outcome["status"], flag in outcome["error"]) == ("refused", True), value
            assert attempt_absent(repos, output, branch), value
            assert not evidence.exists(), value
            assert source_fingerprint(repos["source"]) == before, value
    assert plan.read_bytes() == plan_bytes


def test_invalid_override_preserves_index_bytes(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    source = repos["source"]
    tracked = source / "fork.txt"
    index = source / ".git" / "index"
    def digest() -> str:
        return hashlib.sha256(index.read_bytes()).hexdigest()
    # Control: unchanged bytes with changed stat data really exercise index refresh.
    first = digest()
    stat = tracked.stat()
    os.utime(tracked, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    ordinary = git(source, "status", "--porcelain=v1")
    assert ordinary.stdout == ""
    assert digest() != first, "fixture did not exercise ordinary Git index refresh"
    before = digest()
    stat = tracked.stat()
    os.utime(tracked, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    plan = tmp_path / "invalid-plan.json"
    result = run_cli(
        "preview", "--source-repo", str(source), "--candidate", repos["candidate"],
        "--upstream-repository", "Graphify-Labs/graphify", "--upstream-url", str(repos["upstream"]),
        "--override-sha", "0" * 40, "--override-reason", "unavailable fixture target",
        "--output-plan", str(plan),
    )
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stderr)["status"] == "refused"
    assert not plan.exists()
    assert digest() == before, "invalid target preflight changed source index bytes"


def load_engine() -> Any:
    """Import the engine by path for precisely labelled lower-level lifecycle evidence."""
    spec = importlib.util.spec_from_file_location(
        "fork_maintenance_under_test", ROOT / "tools" / "fork_maintenance.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unobservable_state_is_unknown_and_repeat_refuses_for_uncertainty(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    # A FIFO where FETCH_HEAD belongs must never block state observation or be hashed.
    fetch_head = repos["source"] / ".git" / "FETCH_HEAD"
    if fetch_head.exists():
        fetch_head.unlink()
    os.mkfifo(fetch_head)
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    branch = "codex/unknown-state"
    arguments = apply_args(repos, plan, output, evidence, branch)
    try:
        first = run_cli(*arguments, timeout=HANG_GUARD)
        assert first.returncode == 2, first.stderr
        assert json.loads(first.stderr)["status"] == "operational_failure"
        receipt_bytes = (evidence / "result.json").read_bytes()
        state = json.loads(receipt_bytes)["observed_partial_state"]
        assert state["state"] == "unknown"
        assert "special file" in state["observation_error"]
        assert not (evidence / "attempt.lock").exists()
        assert attempt_absent(repos, output, branch)
        # Ordinary source again: the recorded unknown still proves neither equality nor drift.
        fetch_head.unlink()
        repeated = run_cli(*arguments, timeout=HANG_GUARD)
        assert repeated.returncode == 2, repeated.stderr
        refusal = json.loads(repeated.stderr)
        assert refusal["state_uncertain"] is True
        assert refusal["recorded_status"] == "operational_failure"
        assert "uncertainty" in refusal["error"] and "drift" not in refusal["error"]
        assert (evidence / "result.json").read_bytes() == receipt_bytes
        assert attempt_absent(repos, output, branch)
    finally:
        if fetch_head.exists() or fetch_head.is_symlink():
            fetch_head.unlink()


@pytest.mark.parametrize(
    "signal_marker", [None, "timed_out = True", "terminal_failure: BaseException | None = unexpected"]
)
def test_git_timeout_starts_one_shared_shutdown_allowance_and_proves_drained_eof(
    tmp_path: Path, signal_marker: str | None,
) -> None:
    # Lower-level evidence on the public GitRunner class with a real blocked Git child.
    engine = load_engine()
    blocked = tmp_path / "blocked-input"
    os.mkfifo(blocked)  # hash-object blocks opening it until its group is settled
    home = tmp_path / "home"
    home.mkdir()
    runner = engine.GitRunner(
        subprocess_timeout=0.5, deadline=time.monotonic() + HANG_GUARD, home=home
    )
    deliveries: list[int] = []
    previous: Any = None
    if signal_marker is not None:
        controller = engine.CatchableSignalController()
        engine.SIGNAL_CONTROLLER = controller
        previous = signal.signal(signal.SIGTERM, lambda signum, _frame: controller.handle(signum))
        sys.settrace(
            signal_trace_at(
                engine.GitRunner.run,
                signal_marker,
                deliveries,
                require_clear=True,
            )
        )
    try:
        with pytest.raises(engine.MaintenanceError) as caught:
            runner.run(None, "hash-object", str(blocked))
    finally:
        if signal_marker is not None:
            sys.settrace(None)
            signal.signal(signal.SIGTERM, previous)
            engine.SIGNAL_CONTROLLER = None
    after = time.monotonic()
    assert caught.value.status == "timeout"
    record = runner.records[-1]
    assert record["timed_out"] is True
    assert record["interrupted"] is (signal_marker is not None)
    assert deliveries == ([signal.SIGTERM] if signal_marker is not None else [])
    assert record["process_group_settled"] is True
    assert record["stdout_eof"] is True and record["stderr_eof"] is True
    assert record["raw_complete"] is True
    first = runner.shutdown_deadline
    assert first is not None, "terminal transition did not start the shutdown allowance"
    assert first <= after + engine.SHUTDOWN_ALLOWANCE_SECONDS
    assert runner.begin_shutdown() == first  # later transitions never extend it
    with pytest.raises(engine.MaintenanceError, match="mutation is forbidden"):
        runner.run(None, "--version", mutating=True)
    for probe in (os.kill, os.killpg):
        with pytest.raises(ProcessLookupError):
            probe(record["pid"], 0)


def test_state_hashing_refuses_a_symlinked_index_instead_of_following_it(tmp_path: Path) -> None:
    # Lower-level evidence: repository_index_sha256 feeds replay equality directly.
    engine = load_engine()
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-b", "main", str(repo))
    commit_file(repo, "file.txt", "content\n", "initial")
    index = repo / ".git" / "index"
    outside = tmp_path / "outside-index"
    outside.write_bytes(index.read_bytes())
    index.unlink()
    index.symlink_to(outside)
    home = tmp_path / "home"
    home.mkdir()
    runner = engine.GitRunner(
        subprocess_timeout=HANG_GUARD, deadline=time.monotonic() + HANG_GUARD, home=home
    )
    with pytest.raises(engine.MaintenanceError, match="state is unknown"):
        engine.repository_index_sha256(runner, repo)


@pytest.mark.parametrize(
    "signal_marker", [None, "timed_out = True", "terminal_failure: BaseException | None = unexpected"]
)
def test_http_worker_timeout_keeps_one_record_with_drained_original_bytes(
    tmp_path: Path, metadata_server: Any, signal_marker: str | None
) -> None:
    # Coverage (not new RED): lower-level http_json with a real stalled loopback server.
    engine = load_engine()
    server = metadata_server({"*": stall_headers})
    records: list[dict[str, Any]] = []
    deliveries: list[int] = []
    previous: Any = None
    if signal_marker is not None:
        controller = engine.CatchableSignalController()
        engine.SIGNAL_CONTROLLER = controller
        previous = signal.signal(signal.SIGTERM, lambda signum, _frame: controller.handle(signum))
        sys.settrace(
            signal_trace_at(
                engine.http_json,
                signal_marker,
                deliveries,
                require_clear=True,
            )
        )
    try:
        with pytest.raises(engine.MaintenanceError) as caught:
            engine.http_json(f"{server.url}/stall", 0.5, time.monotonic() + HANG_GUARD, records)
    finally:
        if signal_marker is not None:
            sys.settrace(None)
            signal.signal(signal.SIGTERM, previous)
            engine.SIGNAL_CONTROLLER = None
    assert caught.value.status == "timeout"
    assert server.requests
    assert len(records) == 1, "one worker must produce exactly one command record"
    record = records[0]
    assert record["interrupted"] is (signal_marker is not None)
    assert deliveries == ([signal.SIGTERM] if signal_marker is not None else [])
    assert record["timed_out"] is True and record["process_group_settled"] is True
    assert record["stdout_eof"] is True and record["raw_complete"] is True
    for stream in ("stdout", "stderr"):
        raw = record[f"_{stream}_raw"]
        assert record[stream]["bytes"] == len(raw)
        assert record[stream]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert caught.value.details["worker_settled"] is True


@pytest.mark.parametrize(
    ("origin", "admitted"),
    [
        ("{token}@example.invalid:owner/repo.git", False),
        ("https://[{token}/owner/repo.git", False),
        ("git@example.invalid:owner/repo.git", True),
    ],
)
def test_scp_and_malformed_origins_are_structured_admission_outcomes(
    tmp_path: Path, origin: str, admitted: bool
) -> None:
    repos = make_repositories(tmp_path)
    synthetic = "SYNTHETIC_DO_NOT_PUBLISH"
    git(repos["source"], "remote", "set-url", "origin", origin.format(token=synthetic))
    plan = tmp_path / "plan.json"

    result = preview_override(repos, plan)

    if admitted:  # control: the conventional SSH git identity in SCP form stays supported
        assert result.returncode == 0, result.stderr
        assert plan.exists()
        return
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stderr)["status"] == "refused"
    assert synthetic not in result.stdout + result.stderr
    assert not plan.exists()
    assert not list(tmp_path.glob(".plan.json.preview-evidence-*"))


def redirect_to(path: str) -> Callable[..., None]:
    def route(handler: Any, server: MetadataServer) -> None:
        handler.send_response(302)
        handler.send_header("Location", f"{server.url}{path}")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    return route


def test_live_metadata_records_observed_final_location_after_redirect(
    tmp_path: Path, metadata_server: Any
) -> None:
    repos = make_repositories(tmp_path)
    routes = {**LIVE_ROUTES, "/releases": redirect_to("/moved"), "/moved": LIVE_ROUTES["/releases"]}
    server = metadata_server(routes)
    plan_path = tmp_path / "plan.json"

    result = preview_live(repos, server, plan_path)

    assert result.returncode == 0, result.stderr
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    workers = [
        record for record in plan["command_evidence"]
        if record.get("origin") == "http_worker_envelope"
    ]
    first = next(record for record in workers if record["argv"][-2].endswith("/releases?page=1"))
    assert first["redirected"] is True
    assert first["response_final_url"] == f"{server.url}/moved"
    direct = next(record for record in workers if record["argv"][-2].endswith("/older"))
    assert direct["redirected"] is False
    assert direct["response_final_url"] == f"{server.url}/older"


def test_pypi_provenance_is_bound_per_consulted_version_response(
    tmp_path: Path, metadata_server: Any
) -> None:
    repos = make_repositories(tmp_path)
    routes = {
        **LIVE_ROUTES,
        "/pypi/graphifyy/1.3.0/json": redirect_to("/gone/1.3.0"),
        "/pypi/graphifyy/1.1.0/json": redirect_to("/mirror/1.1.0"),
        "/mirror/1.1.0": pypi_file(yanked=False),
    }
    server = metadata_server(routes)
    plan_path = tmp_path / "plan.json"

    result = preview_live(repos, server, plan_path)

    assert result.returncode == 0, result.stderr
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    observed = {
        item["version"]: item for item in plan["evidence_bindings"]["release_observations"]
    }
    # The requested base alone never establishes response provenance after a redirect.
    assert observed["1.1.0"]["pypi_response_final_url"] == f"{server.url}/mirror/1.1.0"
    assert observed["1.1.0"]["pypi_redirected"] is True
    assert observed["1.2.0"]["pypi_response_final_url"] == (
        f"{server.url}/pypi/graphifyy/1.2.0/json"
    )
    assert observed["1.2.0"]["pypi_redirected"] is False
    # A 404 reached through a redirect records the observed error location.
    assert observed["1.3.0"]["pypi_response_final_url"] == f"{server.url}/gone/1.3.0"
    for item in observed.values():
        assert item["pypi_provenance"] == "controlled_http"
    workers = [
        record for record in plan["command_evidence"]
        if record.get("origin") == "http_worker_envelope"
    ]
    gone = next(record for record in workers if record["argv"][-2].endswith("/1.3.0/json"))
    assert (gone["response_final_url"], gone["redirected"]) == (
        f"{server.url}/gone/1.3.0",
        True,
    )


def test_incomplete_drain_records_observed_group_settlement_separately(tmp_path: Path) -> None:
    # Labelled deterministic seam control on terminate_and_drain with real processes: the
    # owned group is settled while an escaped session descendant still holds stdout.
    engine = load_engine()
    ready = tmp_path / "escaped.pid"
    escaped_code = (
        "import pathlib,sys,time;"
        f"pathlib.Path({str(ready)!r}).write_text(str(__import__('os').getpid()));"
        "time.sleep(60)"
    )
    leader_code = (
        "import subprocess,sys,time;"
        "sys.stdout.write('captured-before-escape');sys.stdout.flush();"
        f"subprocess.Popen([sys.executable,'-c',{escaped_code!r}],start_new_session=True);"
        "time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    escaped = 0
    try:
        guard = time.monotonic() + HANG_GUARD
        while not ready.exists() or not ready.read_text():  # handshake: descendant is live
            assert time.monotonic() < guard, "escaped descendant never started"
            time.sleep(0.01)
        escaped = int(ready.read_text())
        stdout, _, drained, settled = engine.terminate_and_drain(
            process, time.monotonic() + 1.0, b"", b""
        )
        assert drained is False, "EOF cannot be claimed while a descendant holds the pipe"
        assert settled is True, "the owned group was observed settled independently of EOF"
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
        assert stdout in (b"", b"captured-before-escape")
    finally:
        if escaped:
            with contextlib.suppress(ProcessLookupError):
                os.kill(escaped, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=HANG_GUARD)


def test_success_publication_evidence_error_keeps_original_terminal_cause(
    tmp_path: Path,
) -> None:
    # Public CLI: a failed applied-receipt publication is not republished as a new failure.
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    (evidence / "raw").mkdir(parents=True)
    marker = evidence / "raw" / "preexisting"
    marker.write_bytes(b"prior evidence")

    result = run_cli(*apply_args(repos, plan, output, evidence, "codex/evidence-cause"))

    assert result.returncode == 2, result.stderr
    outcome = json.loads(result.stderr)
    assert outcome["status"] == "evidence_error"
    assert outcome["terminal_status"] == "applied"
    assert outcome["error"].count("evidence persistence failed") == 1
    assert outcome["lock_path"] == str(evidence / "attempt.lock")
    assert (evidence / "attempt.lock").exists()
    assert not (evidence / "result.json").exists()
    assert sorted(path.name for path in (evidence / "raw").iterdir()) == ["preexisting"]
    assert marker.read_bytes() == b"prior evidence"


def test_terminal_coalescing_absorbs_every_later_catchable_signal() -> None:
    # Labelled deterministic seam control on the public controller class: once terminal
    # publication coalesces, a later signal (for example during scratch cleanup) cannot
    # raise and contradict or re-run an already published outcome.
    engine = load_engine()
    controller = engine.CatchableSignalController()
    with controller.protect():
        controller.handle(signal.SIGTERM)  # deferred during publication
        controller.coalesce_terminal()
    controller.handle(signal.SIGINT)
    controller.handle(signal.SIGTERM)
    controller.deliver()
    fresh = engine.CatchableSignalController()
    with pytest.raises(engine.CaughtSignal):  # primary work stays interruptible
        fresh.handle(signal.SIGTERM)


@pytest.mark.parametrize("conflicting", [False, True])
def test_rebase_completion_starts_the_one_terminal_shutdown_allowance(
    tmp_path: Path, conflicting: bool
) -> None:
    # Public CLI: success/conflict completion is the first terminal transition, so the
    # receipt binds the one shared allowance separately from the primary deadline.
    repos = make_repositories(tmp_path)
    if conflicting:
        source = repos["source"]
        repos["candidate"] = commit_file(source, "shared.txt", "fork version\n", "fork")
        repos["target"] = commit_file(
            repos["upstream_work"], "shared.txt", "upstream version\n", "upstream"
        )
        git(repos["upstream_work"], "push", "publish", "main")
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    evidence = tmp_path / "evidence"
    result = run_cli(
        *apply_args(repos, plan, tmp_path / "attempt", evidence, "codex/terminal-allowance")
    )

    assert result.returncode == (3 if conflicting else 0), result.stderr
    receipt = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
    assert receipt["status"] == ("conflict" if conflicting else "applied")
    timing = receipt["timing"]
    assert timing["shutdown_allowance_seconds"] == 10.0
    started = timing["shutdown_deadline_monotonic"] - timing["shutdown_allowance_seconds"]
    assert started <= timing["primary_execution_deadline_monotonic"]
    assert timing["terminal_transition"] == "rebase_completed"


def test_expired_shutdown_allowance_blocks_terminal_receipt_publication(tmp_path: Path) -> None:
    # Labelled deterministic seam control: the allowance expired after the final raw
    # stream (here: no further streams), so serialization/publication must not proceed.
    engine = load_engine()
    home = tmp_path / "home"
    home.mkdir()
    runner = engine.GitRunner(
        subprocess_timeout=HANG_GUARD, deadline=time.monotonic() + HANG_GUARD, home=home
    )
    runner.shutdown_deadline = time.monotonic() - 1.0  # the one allowance already expired
    lock = tmp_path / "attempt.lock"
    lock.write_bytes(b"owned")
    result_path = tmp_path / "result.json"
    with pytest.raises(engine.MaintenanceError) as caught:
        engine.publish_terminal_result(
            result_path=result_path,
            raw_directory=tmp_path / "raw",
            result={"status": "applied", "recovery": None},
            runner=runner,
            lock=lock,
        )
    assert caught.value.status == "evidence_error"
    assert caught.value.details["terminal_status"] == "applied"
    assert caught.value.details["lock_path"] == str(lock)
    assert not result_path.exists(), "no complete receipt may be published after expiry"
    assert lock.read_bytes() == b"owned"


def test_first_signal_during_owned_error_conversion_still_finalizes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Labelled deterministic main/acquire_target seam: the owned operation has begun, and
    # OSError text conversion invokes the real handler installed by main. This exercises
    # the instruction-window guarantee without relying on probabilistic signal timing.
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    engine = load_engine()

    class SignalDuringStringConversion(OSError):
        def __str__(self) -> str:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
            return "injected owned acquisition failure"

    def fail_owned_acquisition(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise SignalDuringStringConversion()

    monkeypatch.setattr(engine, "acquire_target", fail_owned_acquisition)
    direct_rc = engine.main(apply_args(repos, plan, output, evidence, "codex/conversion-signal"))
    captured = capsys.readouterr()

    assert direct_rc == 2, captured.err
    outcome = json.loads(captured.err)
    assert outcome["status"] == "operational_failure"
    receipt_path = evidence / "result.json"
    assert receipt_path.exists(), "an owned first signal must not bypass finalization"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "operational_failure"
    assert receipt["error"] == "apply operation failed: injected owned acquisition failure"
    assert not (evidence / "attempt.lock").exists()


def test_http_parent_io_error_still_records_launched_worker_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Labelled deterministic parent-I/O seam around a real launched worker. An ordinary
    # communicate error is otherwise not reliably inducible from a public HTTP fixture.
    engine = load_engine()
    real_launch = engine.launch_owned

    class OneFaultingCommunicate:
        def __init__(self, process: subprocess.Popen[bytes]) -> None:
            self.process = process
            self.pid = process.pid
            self.faulted = False

        @property
        def returncode(self) -> int | None:
            return self.process.returncode

        @property
        def stdout(self) -> Any:
            return self.process.stdout

        @property
        def stderr(self) -> Any:
            return self.process.stderr

        def communicate(self, *args: Any, **kwargs: Any) -> tuple[bytes, bytes]:
            if not self.faulted:
                self.faulted = True
                raise OSError("injected parent pipe failure")
            return self.process.communicate(*args, **kwargs)

        def wait(self, *args: Any, **kwargs: Any) -> int:
            return self.process.wait(*args, **kwargs)

    def launch_with_parent_fault(argv: list[str], **options: Any) -> OneFaultingCommunicate:
        return OneFaultingCommunicate(real_launch(argv, **options))

    monkeypatch.setattr(engine, "launch_owned", launch_with_parent_fault)
    records: list[dict[str, Any]] = []
    with pytest.raises(OSError, match="injected parent pipe failure"):
        engine.http_json(
            "http://127.0.0.1:1/unreachable",
            HANG_GUARD,
            time.monotonic() + HANG_GUARD,
            records,
        )

    assert len(records) == 1, "every launched worker must append exactly one record"
    record = records[0]
    assert record["process_group_settled"] is True
    assert record["stdout_eof"] is True and record["stderr_eof"] is True
    assert record["raw_complete"] is True
    for stream in ("stdout", "stderr"):
        raw = record[f"_{stream}_raw"]
        assert record[stream]["bytes"] == len(raw)
        assert record[stream]["sha256"] == hashlib.sha256(raw).hexdigest()


def test_owned_whole_attempt_timeout_replays_unchanged_and_refuses_real_drift(
    tmp_path: Path,
) -> None:
    # Public CLI/process control: unlike the subprocess-timeout control above, this lets
    # the one whole-attempt budget expire while an owned Git child is handshaken on a FIFO.
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    source = repos["source"]
    target = repos["target"]
    blocked_object = source / ".git" / "objects" / target[:2] / target[2:]
    blocked_object.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(blocked_object)
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    arguments = (
        *apply_args(repos, plan, output, evidence, "codex/whole-attempt-timeout"),
        "--attempt-timeout",
        "2",
        "--subprocess-timeout",
        str(HANG_GUARD),
    )
    try:
        timed_out = run_cli(*arguments, timeout=30)
        assert timed_out.returncode == 4, timed_out.stderr
        first = json.loads(timed_out.stderr)
        assert first["status"] == "timeout"
        receipt_path = evidence / "result.json"
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes)
        assert receipt["diagnostic"]["bound"] == "whole-attempt"
        assert "observation_error" not in receipt["observed_partial_state"]
        blocked_object.unlink()

        repeated = run_cli(*arguments, timeout=HANG_GUARD)
        assert repeated.returncode == 4, repeated.stderr
        assert json.loads(repeated.stderr)["repeated"] is True
        assert receipt_path.read_bytes() == receipt_bytes

        (source / "genuine-drift.txt").write_text("drift\n", encoding="utf-8")
        drifted = run_cli(*arguments, timeout=HANG_GUARD)
        assert drifted.returncode == 2, drifted.stderr
        assert "drift" in json.loads(drifted.stderr)["error"]
        assert receipt_path.read_bytes() == receipt_bytes
    finally:
        if blocked_object.exists():
            blocked_object.unlink()


def test_http_error_envelope_keeps_one_complete_raw_record(
    metadata_server: Any,
) -> None:
    # Actual loopback worker control: malformed response JSON becomes an error envelope,
    # and that envelope remains byte-faithful in exactly one command record.
    engine = load_engine()
    server = metadata_server({"*": raw_body(b"{not-json")})
    records: list[dict[str, Any]] = []

    with pytest.raises(engine.MaintenanceError, match="HTTP request failed"):
        engine.http_json(
            f"{server.url}/broken", HANG_GUARD, time.monotonic() + HANG_GUARD, records
        )

    assert len(records) == 1
    record = records[0]
    assert record["direct_rc"] == 0
    assert record["stdout_eof"] is True and record["raw_complete"] is True
    envelope_bytes = record["_stdout_raw"]
    assert json.loads(envelope_bytes)["status"] == "refused"
    assert record["stdout"]["bytes"] == len(envelope_bytes)
    assert record["stdout"]["sha256"] == hashlib.sha256(envelope_bytes).hexdigest()


def test_http_late_return_after_timeout_keeps_final_bytes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Labelled deterministic late-return seam: the real worker has completed, but the
    # first parent observation reports a timeout snapshot before returning final bytes.
    engine = load_engine()
    real_launch = engine.launch_owned

    class LateReturn:
        def __init__(self, process: subprocess.Popen[bytes]) -> None:
            self.process = process
            self.pid = process.pid
            self.first = True
            self.final: tuple[bytes, bytes] | None = None

        @property
        def returncode(self) -> int | None:
            return self.process.returncode

        @property
        def stdout(self) -> Any:
            return self.process.stdout

        @property
        def stderr(self) -> Any:
            return self.process.stderr

        def communicate(self, *args: Any, **kwargs: Any) -> tuple[bytes, bytes]:
            if self.first:
                self.first = False
                self.final = self.process.communicate(*args, **kwargs)
                stdout, stderr = self.final
                raise subprocess.TimeoutExpired(
                    "http-worker", kwargs.get("timeout", 0), output=stdout[:1], stderr=stderr[:1]
                )
            assert self.final is not None
            return self.final

        def wait(self, *args: Any, **kwargs: Any) -> int:
            return self.process.wait(*args, **kwargs)

    def launch_late(argv: list[str], **options: Any) -> LateReturn:
        return LateReturn(real_launch(argv, **options))

    monkeypatch.setattr(engine, "launch_owned", launch_late)
    records: list[dict[str, Any]] = []
    with pytest.raises(engine.MaintenanceError) as caught:
        engine.http_json(
            "http://127.0.0.1:1/unreachable",
            HANG_GUARD,
            time.monotonic() + HANG_GUARD,
            records,
        )

    assert caught.value.status == "timeout"
    assert len(records) == 1
    record = records[0]
    raw = record["_stdout_raw"]
    assert raw.startswith(b"{") and b'"error"' in raw
    assert record["stdout"]["bytes"] == len(raw)
    assert record["stdout"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert record["stdout_eof"] is True and record["raw_complete"] is True


def test_first_signal_inside_owned_apply_exception_handler_preserves_original_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Labelled deterministic apply-handler trace seam: the first catchable signal lands
    # after the ordinary owned failure has entered its handler, before finalization begins.
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    engine = load_engine()

    def fail_owned_acquisition(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise OSError("injected owned acquisition failure")

    monkeypatch.setattr(engine, "acquire_target", fail_owned_acquisition)
    source_lines, first_line = inspect.getsourcelines(engine.apply_plan)
    handler_line = max(
        first_line + offset
        for offset, line in enumerate(source_lines)
        if line.strip() == "failure = exc"
    )
    delivered = False

    def trace(frame: Any, event: str, _arg: Any) -> Any:
        nonlocal delivered
        if (
            not delivered
            and frame.f_code is engine.apply_plan.__code__
            and event == "line"
            and frame.f_lineno == handler_line
        ):
            delivered = True
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
        return trace

    sys.settrace(trace)
    try:
        direct_rc = engine.main(apply_args(repos, plan, output, evidence, "codex/handler-signal"))
    finally:
        sys.settrace(None)
    captured = capsys.readouterr()

    assert delivered is True
    assert direct_rc == 2, captured.err
    outcome = json.loads(captured.err)
    assert outcome["status"] == "operational_failure"
    assert outcome["error"] == "apply operation failed: injected owned acquisition failure"
    receipt_path = evidence / "result.json"
    assert receipt_path.exists(), "the first handler signal must not bypass owned finalization"
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["error"] == outcome["error"]
    assert not (evidence / "attempt.lock").exists()


def test_post_link_ordinary_publication_failure_reports_observed_visibility_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Labelled post-link seam: complete receipt bytes become visible, then an ordinary
    # failure occurs before directory durability and lock release can be claimed.
    engine = load_engine()
    home = tmp_path / "home"
    home.mkdir()
    runner = engine.GitRunner(
        subprocess_timeout=HANG_GUARD, deadline=time.monotonic() + HANG_GUARD, home=home
    )
    lock = tmp_path / "attempt.lock"
    lock.write_bytes(b"owned")
    result_path = tmp_path / "result.json"
    real_write = engine.write_integrity_bound_result
    writes = 0

    def write_then_fail(
        path: Path, result: dict[str, Any], *, deadline: float | None = None
    ) -> None:
        nonlocal writes
        writes += 1
        real_write(path, result, deadline=deadline)
        raise TypeError("injected post-link publication failure")

    monkeypatch.setattr(engine, "write_integrity_bound_result", write_then_fail)
    with pytest.raises(engine.MaintenanceError) as caught:
        engine.publish_terminal_result(
            result_path=result_path,
            raw_directory=tmp_path / "raw",
            result={"status": "applied", "recovery": None},
            runner=runner,
            lock=lock,
        )

    assert writes == 1
    assert caught.value.status == "evidence_error"
    assert caught.value.details["terminal_status"] == "applied"
    assert caught.value.details["receipt_visibility"] == "present"
    assert caught.value.details["lock_visibility"] == "present"
    assert result_path.exists() and lock.exists()


@pytest.mark.parametrize("owner", ["git", "http"])
def test_first_signal_during_launched_process_recording_appends_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    # Labelled record-construction seam over actual Git/HTTP launches. The handler count
    # proves one delivery; the record count proves deferred delivery follows one append.
    engine = load_engine()
    controller = engine.CatchableSignalController()
    engine.SIGNAL_CONTROLLER = controller
    real_bounded_stream = engine.bounded_stream
    real_launch = engine.launch_owned
    deliveries = 0

    if owner == "http":
        def launch_success(_argv: list[str], **options: Any) -> Any:
            payload = json.dumps(
                {"value": [[], {}, hashlib.sha256(b"[]").hexdigest()], "final_url": "safe"}
            )
            return real_launch(
                [sys.executable, "-I", "-c", "import sys;sys.stdout.write(sys.argv[1])", payload],
                **options,
            )

        monkeypatch.setattr(engine, "launch_owned", launch_success)

    def interrupt_recording(value: bytes) -> dict[str, Any]:
        nonlocal deliveries
        if deliveries == 0:
            deliveries += 1
            controller.handle(signal.SIGTERM)
        return real_bounded_stream(value)

    monkeypatch.setattr(engine, "bounded_stream", interrupt_recording)
    records: list[dict[str, Any]]
    try:
        if owner == "git":
            home = tmp_path / "home"
            home.mkdir()
            runner = engine.GitRunner(
                subprocess_timeout=HANG_GUARD,
                deadline=time.monotonic() + HANG_GUARD,
                home=home,
            )
            records = runner.records
            with pytest.raises(engine.MaintenanceError, match="interrupted by SIGTERM"):
                runner.run(None, "--version")
        else:
            records = []
            with pytest.raises(engine.CaughtSignal):
                engine.http_json(
                    "http://127.0.0.1:1/unreachable",
                    HANG_GUARD,
                    time.monotonic() + HANG_GUARD,
                    records,
                )
    finally:
        engine.SIGNAL_CONTROLLER = None

    assert deliveries == 1
    assert len(records) == 1
    assert records[0]["stdout_eof"] is True
    assert records[0]["stderr_eof"] is True


def signal_trace_at(
    code: Any,
    marker: str,
    counter: list[int],
    ready: Callable[[Any], bool] | None = None,
    require_clear: bool = False,
) -> Any:
    # Labelled deterministic trace seam: deliver one catchable signal through the
    # installed handler when the named source line of the traced function is reached.
    lines, first = inspect.getsourcelines(code)
    target = {first + offset for offset, line in enumerate(lines) if line.strip() == marker}
    assert target, marker

    def trace(frame: Any, event: str, _arg: Any) -> Any:
        if (
            not counter
            and frame.f_code is code.__code__
            and event == "line"
            and frame.f_lineno in target
            and (ready is None or ready(frame))
        ):
            if require_clear:
                controller = frame.f_globals["SIGNAL_CONTROLLER"]
                assert controller is not None
                assert controller.raised is False and controller.pending is None
            counter.append(signal.SIGTERM)
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
        return trace

    return trace


@pytest.mark.parametrize("owner", ["git", "http"])
@pytest.mark.parametrize(
    "signal_marker",
    ["unexpected = exc", "terminal_failure: BaseException | None = unexpected"],
)
def test_first_signal_in_launched_process_ordinary_handler_records_once_with_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str, signal_marker: str
) -> None:
    # The first signal lands in the ordinary handler or after its cause is captured.
    engine = load_engine()
    controller = engine.CatchableSignalController()
    engine.SIGNAL_CONTROLLER = controller
    previous = signal.signal(signal.SIGTERM, lambda signum, _frame: controller.handle(signum))
    real_launch = engine.launch_owned
    launches: list[Any] = []

    def launch_failing_primary(argv: list[str], **options: Any) -> Any:
        process = real_launch(argv, **options)
        launches.append(process)
        real_communicate = process.communicate
        calls = 0

        def communicate(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("injected primary failure")
            return real_communicate(*args, **kwargs)

        process.communicate = communicate
        return process

    monkeypatch.setattr(engine, "launch_owned", launch_failing_primary)
    deliveries: list[int] = []
    records: list[dict[str, Any]]
    function = engine.GitRunner.run if owner == "git" else engine.http_json
    sys.settrace(signal_trace_at(function, signal_marker, deliveries, require_clear=True))
    try:
        if owner == "git":
            home = tmp_path / "home"
            home.mkdir()
            runner = engine.GitRunner(
                subprocess_timeout=HANG_GUARD, deadline=time.monotonic() + HANG_GUARD, home=home
            )
            records = runner.records
            with pytest.raises(RuntimeError, match="injected primary failure"):
                runner.run(None, "--version")
        else:
            records = []
            with pytest.raises(RuntimeError, match="injected primary failure"):
                engine.http_json(
                    "http://127.0.0.1:1/unreachable",
                    HANG_GUARD,
                    time.monotonic() + HANG_GUARD,
                    records,
                )
    finally:
        sys.settrace(None)
        signal.signal(signal.SIGTERM, previous)
        engine.SIGNAL_CONTROLLER = None

    assert deliveries == [signal.SIGTERM]
    assert len(launches) == 1
    assert len(records) == 1
    assert records[0]["pid"] == launches[0].pid
    assert records[0]["process_group_settled"] is True
    assert records[0]["stdout_eof"] is True and records[0]["stderr_eof"] is True
    assert launches[0].returncode is not None
    assert records[0]["interrupted"] is True


def test_deferred_http_interruption_is_reflected_in_its_single_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Labelled record-construction seam: the signal is deferred during recording and
    # delivered afterwards; the one record must agree with the raised interruption.
    engine = load_engine()
    controller = engine.CatchableSignalController()
    engine.SIGNAL_CONTROLLER = controller
    real_bounded_stream = engine.bounded_stream
    real_launch = engine.launch_owned
    deliveries = 0

    def launch_success_envelope(_argv: list[str], **options: Any) -> Any:
        payload = json.dumps(
            {"value": [[], {}, hashlib.sha256(b"[]").hexdigest()], "final_url": "safe"}
        )
        return real_launch(
            [sys.executable, "-I", "-c", "import sys;sys.stdout.write(sys.argv[1])", payload],
            **options,
        )

    def interrupt_recording(value: bytes) -> dict[str, Any]:
        nonlocal deliveries
        if deliveries == 0:
            deliveries += 1
            controller.handle(signal.SIGTERM)
        return real_bounded_stream(value)

    monkeypatch.setattr(engine, "bounded_stream", interrupt_recording)
    monkeypatch.setattr(engine, "launch_owned", launch_success_envelope)
    records: list[dict[str, Any]] = []
    try:
        with pytest.raises(engine.CaughtSignal):
            engine.http_json(
                "http://127.0.0.1:1/unreachable", HANG_GUARD, time.monotonic() + HANG_GUARD, records
            )
    finally:
        engine.SIGNAL_CONTROLLER = None

    assert deliveries == 1
    assert len(records) == 1
    assert records[0]["interrupted"] is True
    assert isinstance(records[0]["_stdout_raw"], bytes)


def test_first_signal_between_apply_handler_and_finalization_publishes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Labelled trace seam: the first signal lands after the ordinary handler exits and
    # before owned finalization starts; the original ordinary cause is still published.
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    engine = load_engine()

    def fail_owned_acquisition(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise OSError("injected owned acquisition failure")

    monkeypatch.setattr(engine, "acquire_target", fail_owned_acquisition)
    real_publish = engine.publish_terminal_result
    publications = 0

    def counted_publish(**kwargs: Any) -> None:
        nonlocal publications
        publications += 1
        real_publish(**kwargs)

    monkeypatch.setattr(engine, "publish_terminal_result", counted_publish)
    deliveries: list[int] = []
    sys.settrace(signal_trace_at(engine.apply_plan, "assert failure is not None", deliveries))
    try:
        direct_rc = engine.main(apply_args(repos, plan, output, evidence, "codex/gap-signal"))
    finally:
        sys.settrace(None)
    captured = capsys.readouterr()

    assert deliveries == [signal.SIGTERM]
    assert direct_rc == 2, captured.err
    outcome = json.loads(captured.err)
    assert outcome["status"] == "operational_failure"
    assert outcome["error"] == "apply operation failed: injected owned acquisition failure"
    assert publications == 1
    receipt = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
    assert receipt["error"] == outcome["error"]
    assert not (evidence / "attempt.lock").exists()


@pytest.mark.parametrize("error", [TypeError, ValueError])
def test_ordinary_observation_error_is_unknown_state_with_two_coalesced_deliveries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: type[Exception],
) -> None:
    # Labelled injected-handler seam: two deliveries through the installed handler during
    # owned observation coalesce; the ordinary observation error becomes unknown state.
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    engine = load_engine()

    def fail_owned_acquisition(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise OSError("injected owned acquisition failure")

    deliveries = 0
    deadlines: list[float] = []

    def observe_with_signals(runner: Any, *_args: Any) -> dict[str, Any]:
        nonlocal deliveries
        deadlines.append(runner.begin_shutdown())
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        for _ in range(2):
            handler(signal.SIGTERM, None)
            deliveries += 1
        raise error("injected observation failure")

    monkeypatch.setattr(engine, "acquire_target", fail_owned_acquisition)
    monkeypatch.setattr(engine, "observed_attempt_state", observe_with_signals)
    real_publish = engine.publish_terminal_result
    publications = 0

    def counted_publish(**kwargs: Any) -> None:
        nonlocal publications
        publications += 1
        real_publish(**kwargs)

    monkeypatch.setattr(engine, "publish_terminal_result", counted_publish)
    direct_rc = engine.main(apply_args(repos, plan, output, evidence, "codex/observe-error"))
    captured = capsys.readouterr()

    assert deliveries == 2
    assert len(deadlines) == 1
    assert direct_rc == 2, captured.err
    outcome = json.loads(captured.err)
    assert outcome["status"] == "operational_failure"
    assert outcome["error"] == "apply operation failed: injected owned acquisition failure"
    assert publications == 1
    receipt_path = evidence / "result.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "operational_failure"
    assert receipt["observed_partial_state"]["state"] == "unknown"
    assert "injected observation failure" in receipt["observed_partial_state"]["observation_error"]
    assert receipt["timing"]["shutdown_deadline_monotonic"] == deadlines[0]
    before = receipt_path.read_bytes()
    assert not (evidence / "attempt.lock").exists()
    assert receipt_path.read_bytes() == before


def test_two_installed_handler_deliveries_inside_publication_keep_cause_and_publish_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Labelled injected-handler seam: two deliveries through the installed SIGTERM handler
    # occur inside the actual terminal publication call; the ordinary cause is retained.
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    engine = load_engine()
    acquisitions = 0

    def fail_owned_acquisition(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal acquisitions
        acquisitions += 1
        raise OSError("injected owned acquisition failure")

    monkeypatch.setattr(engine, "acquire_target", fail_owned_acquisition)
    real_publish = engine.publish_terminal_result
    publications = 0
    deliveries = 0

    def publish_with_signals(**kwargs: Any) -> None:
        nonlocal publications, deliveries
        publications += 1
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        for _ in range(2):
            handler(signal.SIGTERM, None)
            deliveries += 1
        real_publish(**kwargs)

    monkeypatch.setattr(engine, "publish_terminal_result", publish_with_signals)
    direct_rc = engine.main(apply_args(repos, plan, output, evidence, "codex/publish-signal"))
    captured = capsys.readouterr()

    assert deliveries == 2
    assert direct_rc == 2, captured.err
    outcome = json.loads(captured.err)
    assert outcome["status"] == "operational_failure"
    assert outcome["error"] == "apply operation failed: injected owned acquisition failure"
    assert publications == 1
    assert acquisitions == 1
    receipt_path = evidence / "result.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["error"] == outcome["error"]
    assert isinstance(receipt["timing"]["shutdown_deadline_monotonic"], float)
    assert not (evidence / "attempt.lock").exists()
    before = receipt_path.read_bytes()
    source_refs = git(repos["source"], "for-each-ref").stdout
    # Observed subsequent activity: a later delivery must not trigger more publication
    # or primary work, and the receipt and source refs stay byte-identical.
    retained = signal.getsignal(signal.SIGTERM)
    if callable(retained):
        retained(signal.SIGTERM, None)
    assert publications == 1
    assert acquisitions == 1
    assert receipt_path.read_bytes() == before
    assert git(repos["source"], "for-each-ref").stdout == source_refs


def test_git_runner_uses_owned_home_as_cwd_with_discovery_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Labelled runner seam: every real Git launch is rooted in its owned empty home."""
    engine = load_engine()
    home = tmp_path / "ambient" / "owned-home"
    home.mkdir(parents=True)
    real_launch = engine.launch_owned
    launches: list[dict[str, Any]] = []

    def capture(argv: list[str], **options: Any) -> Any:
        launches.append(options.copy())
        return real_launch(argv, **options)

    monkeypatch.setattr(engine, "launch_owned", capture)
    runner = engine.GitRunner(
        subprocess_timeout=HANG_GUARD, deadline=time.monotonic() + HANG_GUARD, home=home
    )
    result = runner.run(None, "--version")

    assert result.returncode == 0
    assert launches[0]["cwd"] == home.resolve()
    assert launches[0]["env"]["GIT_CEILING_DIRECTORIES"] == str(home.resolve().parent)
    assert runner.records[0]["cwd"] == str(home.resolve())


def test_owned_home_ceiling_blocks_real_ambient_include_and_url_rewrite(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    decoy_work = tmp_path / "decoy-work"
    decoy_bare = tmp_path / "decoy.git"
    decoy_work.mkdir()
    git(decoy_work, "init", "-b", "main")
    decoy = commit_file(decoy_work, "decoy.txt", "decoy\n", "decoy")
    git(tmp_path, "clone", "--bare", str(decoy_work), str(decoy_bare))
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    git(ambient, "init", "-b", "main")
    included = tmp_path / "ambient-include.config"
    included.write_text(
        f"[url \"{decoy_bare}\"]\n\tinsteadOf = {repos['upstream']}\n",
        encoding="utf-8",
    )
    git(ambient, "config", "include.path", str(included))
    home = ambient / "nested" / "owned-home"
    home.mkdir(parents=True)
    argv = [GIT, "ls-remote", str(repos["upstream"]), "refs/heads/main"]
    direct = subprocess.run(
        argv, cwd=home, env=fixture_git_env(), capture_output=True, text=True, check=True
    )
    assert direct.stdout.split()[0] == decoy, "positive control did not execute ambient rewrite"

    engine = load_engine()
    runner = engine.GitRunner(
        subprocess_timeout=HANG_GUARD, deadline=time.monotonic() + HANG_GUARD, home=home
    )
    isolated = runner.run(None, "ls-remote", str(repos["upstream"]), "refs/heads/main")
    assert isolated.stdout.split()[0] == repos["target"]


def test_preview_refuses_alternate_refs_command_and_submodule_layout(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    marker = tmp_path / "alternate-invoked"
    git(repos["source"], "config", "core.alternateRefsCommand", f"touch {marker}")

    alternate = preview_override(repos, plan)

    assert alternate.returncode == 2, alternate.stderr
    assert "alternateRefsCommand" in alternate.stderr
    assert not marker.exists() and not plan.exists()

    git(repos["source"], "config", "--unset", "core.alternateRefsCommand")
    (repos["source"] / ".gitmodules").mkdir()
    submodule = preview_override(repos, plan)
    assert submodule.returncode == 2, submodule.stderr
    assert "submodule" in submodule.stderr.lower()
    assert not plan.exists()


@pytest.mark.parametrize(
    ("side", "layout"),
    [("candidate", "gitlink"), ("candidate", "gitmodules"),
     ("target", "gitlink"), ("target", "gitmodules")],
)
def test_preview_refuses_candidate_and_target_submodule_trees(
    tmp_path: Path, side: str, layout: str
) -> None:
    repos = make_repositories(tmp_path)
    repo = repos["source"] if side == "candidate" else repos["upstream_work"]
    if layout == "gitlink":
        git(repo, "update-index", "--add", "--cacheinfo", "160000", repos["base"], "nested")
        git(repo, "commit", "-m", f"{side} gitlink")
    else:
        commit_file(repo, ".gitmodules", "[submodule \"nested\"]\n", f"{side} gitmodules")
    changed = git(repo, "rev-parse", "HEAD").stdout.strip()
    repos[side] = changed
    if side == "target":
        git(repo, "push", "publish", "main")
    plan = tmp_path / f"{side}-{layout}.json"
    result = preview_override(repos, plan)
    assert result.returncode == 2, result.stderr
    assert "submodule" in result.stderr.lower()
    assert not plan.exists()


def test_sensitive_pagination_destination_is_never_requested_or_published(
    tmp_path: Path, metadata_server: Any
) -> None:
    repos = make_repositories(tmp_path)
    token = "SYNTHETIC_PAGINATION_TOKEN_NOT_SECRET"

    def first(handler: Any, server: MetadataServer) -> None:
        send_json_links(
            handler,
            [release_entry("v1.3.0", 3)],
            [f'<{server.url}/older?token={token}>; rel="next"'],
        )

    server = metadata_server({"/releases": first, "*": missing})
    plan = tmp_path / "plan.json"
    result = preview_live(repos, server, plan)

    assert result.returncode == 2, result.stderr
    assert server.requests == ["/releases?page=1"]
    assert token not in result.stdout + result.stderr
    assert not plan.exists()


def test_replay_with_retained_lock_refuses_without_rewriting_receipt(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    arguments = apply_args(repos, plan, output, evidence, "codex/retained-lock")
    assert run_cli(*arguments).returncode == 0
    receipt = evidence / "result.json"
    before = receipt.read_bytes()
    (evidence / "attempt.lock").write_bytes(b"retained ownership")

    replay = run_cli(*arguments)

    assert replay.returncode == 2, replay.stderr
    outcome = json.loads(replay.stderr)
    assert outcome["status"] == "refused"
    assert outcome["result_path"] == str(receipt)
    assert outcome["lock_path"] == str(evidence / "attempt.lock")
    assert receipt.read_bytes() == before


def test_dangling_lock_entry_conservatively_refuses_ownership(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output = tmp_path / "attempt"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    lock = evidence / "attempt.lock"
    lock.symlink_to(evidence / "missing-owner-record")
    before = source_fingerprint(repos["source"])

    result = run_cli(*apply_args(repos, plan, output, evidence, "codex/dangling-lock"))

    assert result.returncode == 2, result.stderr
    outcome = json.loads(result.stderr)
    assert outcome["lock_path"] == str(lock)
    assert "lock" in outcome["error"]
    assert lock.is_symlink() and not output.exists()
    assert source_fingerprint(repos["source"]) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [("draft", None), ("draft", "false"), ("prerelease", 0)],
)
def test_malformed_release_eligibility_refuses_newest_unknown_entry(
    tmp_path: Path, field: str, value: Any
) -> None:
    repos = make_repositories(tmp_path)
    releases = [release_entry("v1.3.0", 3), release_entry("v1.1.0", 1)]
    releases[0][field] = value
    release_fixture = tmp_path / "releases.json"
    pypi_fixture = tmp_path / "pypi.json"
    release_fixture.write_text(json.dumps({"pages": [releases]}), encoding="utf-8")
    pypi_fixture.write_text(
        json.dumps({"1.1.0": {"urls": [{"url": "https://files.invalid/a", "yanked": False}]}}),
        encoding="utf-8",
    )
    plan = tmp_path / "plan.json"
    result = run_cli(
        "preview", "--source-repo", str(repos["source"]), "--candidate", repos["candidate"],
        "--upstream-repository", "Graphify-Labs/graphify", "--upstream-url", str(repos["upstream"]),
        "--github-releases-fixture", str(release_fixture), "--pypi-fixture", str(pypi_fixture),
        "--output-plan", str(plan),
    )

    assert result.returncode == 2, result.stderr
    assert "eligibility" in result.stderr
    assert not plan.exists()


@pytest.mark.parametrize("owner", ["git", "http"])
@pytest.mark.parametrize("late_signal", [False, True])
@pytest.mark.parametrize("permission_denied", [False, True])
def test_normal_leader_exit_with_same_group_survivor_is_non_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    owner: str, late_signal: bool, permission_denied: bool,
) -> None:
    """Labelled launch seam: a real same-group child outlives a normally exiting leader."""
    engine = load_engine()
    real_launch = engine.launch_owned
    child_pid = tmp_path / f"{owner}-child.pid"
    child_code = (
        "import os,pathlib,time;"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()));"
        "time.sleep(60)"
    )
    leader_code = (
        "import pathlib,subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"p=pathlib.Path({str(child_pid)!r});"
        "deadline=time.monotonic()+5;"
        "exec('while not p.exists():\\n assert time.monotonic()<deadline\\n time.sleep(0.001)')"
    )

    def launch_survivor(_argv: list[str], **options: Any) -> Any:
        return real_launch([sys.executable, "-c", leader_code], **options)

    monkeypatch.setattr(engine, "launch_owned", launch_survivor)
    if permission_denied:
        real_killpg = engine.os.killpg

        def denied_group_signal(group: int, signum: int) -> None:
            if signum in (signal.SIGTERM, signal.SIGKILL):
                raise PermissionError(errno.EPERM, "controlled group denial")
            real_killpg(group, signum)

        monkeypatch.setattr(engine.os, "killpg", denied_group_signal)
    records: list[dict[str, Any]]
    deliveries: list[int] = []
    previous: Any = None
    if late_signal:
        controller = engine.CatchableSignalController()
        engine.SIGNAL_CONTROLLER = controller
        previous = signal.signal(signal.SIGTERM, lambda signum, _frame: controller.handle(signum))
        function = engine.GitRunner.run if owner == "git" else engine.http_json
        sys.settrace(
            signal_trace_at(
                function,
                "terminal_failure: BaseException | None = unexpected",
                deliveries,
                require_clear=True,
            )
        )
    try:
        if owner == "git":
            home = tmp_path / "home"
            home.mkdir()
            runner = engine.GitRunner(
                subprocess_timeout=HANG_GUARD,
                deadline=time.monotonic() + HANG_GUARD,
                home=home,
            )
            records = runner.records
            with pytest.raises(engine.MaintenanceError, match="process group remained"):
                runner.run(None, "--version")
        else:
            records = []
            with pytest.raises(engine.MaintenanceError, match="process group remained"):
                engine.http_json(
                    "http://127.0.0.1:1/safe",
                    HANG_GUARD,
                    time.monotonic() + HANG_GUARD,
                    records,
                )
        assert child_pid.exists(), "the real descendant must have started"
        assert len(records) == 1
        assert records[0]["process_group_absent_after_primary"] is False
        assert records[0]["direct_rc"] == 0
        assert records[0]["stdout_eof"] is True and records[0]["stderr_eof"] is True
        assert records[0]["interrupted"] is late_signal
        assert deliveries == ([signal.SIGTERM] if late_signal else [])
        assert records[0]["process_group_observed"] is True
        if permission_denied:
            assert records[0]["process_group_settled"] is False
            assert records[0]["raw_complete"] is False
            assert engine.process_group_absent(records[0]["process_group"]) is not True
        elif not late_signal:
            # Require utility-owned cleanup before the fixture's finally kills a child.
            assert records[0]["process_group_settled"] is True, (
                records[0], engine.process_group_absent(records[0]["process_group"])
            )
            assert engine.process_group_absent(records[0]["process_group"]) is True
        elif records[0]["process_group_settled"]:
            assert engine.process_group_absent(records[0]["process_group"]) is True
        else:
            assert records[0]["raw_complete"] is False
    finally:
        if late_signal:
            sys.settrace(None)
            signal.signal(signal.SIGTERM, previous)
            engine.SIGNAL_CONTROLLER = None
        if child_pid.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(child_pid.read_text()), signal.SIGKILL)


@pytest.mark.parametrize("owner", ["git", "http"])
def test_first_signal_in_record_phase_before_protection_records_once_with_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    # Labelled trace seam: the first signal lands on the record-phase transition, before
    # record construction enters signal protection; the spanning boundary must own it.
    engine = load_engine()
    controller = engine.CatchableSignalController()
    engine.SIGNAL_CONTROLLER = controller
    previous = signal.signal(signal.SIGTERM, lambda signum, _frame: controller.handle(signum))
    real_launch = engine.launch_owned
    launches: list[Any] = []

    def launch_failing_primary(argv: list[str], **options: Any) -> Any:
        process = real_launch(argv, **options)
        launches.append(process)
        real_communicate = process.communicate
        calls = 0

        def communicate(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("injected primary failure")
            return real_communicate(*args, **kwargs)

        process.communicate = communicate
        return process

    monkeypatch.setattr(engine, "launch_owned", launch_failing_primary)
    deliveries: list[int] = []
    records: list[dict[str, Any]]
    function = engine.GitRunner.run if owner == "git" else engine.http_json
    sys.settrace(signal_trace_at(function, 'phase = "record"', deliveries))
    try:
        if owner == "git":
            home = tmp_path / "home"
            home.mkdir()
            runner = engine.GitRunner(
                subprocess_timeout=HANG_GUARD, deadline=time.monotonic() + HANG_GUARD, home=home
            )
            records = runner.records
            with pytest.raises(RuntimeError, match="injected primary failure"):
                runner.run(None, "--version")
        else:
            records = []
            with pytest.raises(RuntimeError, match="injected primary failure"):
                engine.http_json(
                    "http://127.0.0.1:1/unreachable",
                    HANG_GUARD,
                    time.monotonic() + HANG_GUARD,
                    records,
                )
    finally:
        sys.settrace(None)
        signal.signal(signal.SIGTERM, previous)
        engine.SIGNAL_CONTROLLER = None

    assert deliveries == [signal.SIGTERM]
    assert len(launches) == 1
    assert len(records) == 1
    assert records[0]["pid"] == launches[0].pid
    assert records[0]["process_group_settled"] is True
    assert records[0]["stdout_eof"] is True and records[0]["stderr_eof"] is True
    assert isinstance(records[0]["_stdout_raw"], bytes)
    assert launches[0].returncode is not None
    if owner == "http":
        assert records[0]["interrupted"] is True


def test_metadata_port_zero_is_refused_before_worker_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct admission contrast: TCP port zero is never an omitted/default port."""
    engine = load_engine()
    launches = 0

    def forbidden_launch(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal launches
        launches += 1
        raise AssertionError("port-zero metadata reached the worker launch")

    monkeypatch.setattr(engine, "launch_owned", forbidden_launch)
    with pytest.raises(engine.MaintenanceError, match="port zero"):
        engine.http_json(
            "http://127.0.0.1:0/metadata", HANG_GUARD, time.monotonic() + HANG_GUARD, []
        )
    assert launches == 0


def test_source_snapshot_refuses_common_modules_before_status(tmp_path: Path) -> None:
    """A replay/state snapshot cannot traverse an initialized submodule layout."""
    repos = make_repositories(tmp_path)
    source = repos["source"]
    (source / ".git" / "modules").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    engine = load_engine()
    runner = engine.GitRunner(
        subprocess_timeout=HANG_GUARD, deadline=time.monotonic() + HANG_GUARD, home=home
    )

    with pytest.raises(engine.MaintenanceError, match="submodule"):
        engine.snapshot_source_checkout(runner, source)

    assert all("status" not in record["argv"] for record in runner.records)


@pytest.mark.parametrize("owner", ["git", "http"])
def test_late_signal_preserves_captured_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    """A signal after the record cannot replace an already captured ordinary failure."""
    engine = load_engine()

    class SignalOnPostRecordDelivery(engine.CatchableSignalController):
        def __init__(self) -> None:
            super().__init__()
            self.deliveries = 0
            self.signals = 0

        def deliver(self) -> None:
            self.deliveries += 1
            if self.deliveries == 2:
                self.signals += 1
                self.raised = True
                raise engine.CaughtSignal(signal.SIGTERM)

    controller = SignalOnPostRecordDelivery()
    engine.SIGNAL_CONTROLLER = controller
    try:
        if owner == "git":
            home = tmp_path / "home"
            home.mkdir()
            runner = engine.GitRunner(
                subprocess_timeout=HANG_GUARD,
                deadline=time.monotonic() + HANG_GUARD,
                home=home,
            )
            with pytest.raises(engine.MaintenanceError, match="direct rc") as caught:
                runner.run(None, "definitely-not-a-command")
            records = runner.records
        else:
            real_launch = engine.launch_owned

            def launch_error_envelope(_argv: list[str], **options: Any) -> Any:
                # Use json.dumps to avoid any dependence on shell quoting.
                payload = json.dumps({"error": "captured HTTP failure", "status": "refused"})
                return real_launch(
                    [sys.executable, "-I", "-c", "import sys;sys.stdout.write(sys.argv[1])", payload],
                    **options,
                )

            monkeypatch.setattr(engine, "launch_owned", launch_error_envelope)
            records = []
            with pytest.raises(engine.MaintenanceError, match="captured HTTP failure") as caught:
                engine.http_json(
                    "http://127.0.0.1:1/safe",
                    HANG_GUARD,
                    time.monotonic() + HANG_GUARD,
                    records,
                )
        assert caught.value.status == "refused"
        assert controller.deliveries >= 2
        assert controller.signals == 1
        assert len(records) == 1
        assert records[0].get("interrupted") is True
    finally:
        engine.SIGNAL_CONTROLLER = None


@pytest.mark.parametrize("owner", ["git", "http"])
def test_installed_signal_during_failure_handoff_keeps_captured_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    """Installed-handler seam at the post-record classification/raise boundary."""
    engine = load_engine()
    controller = engine.CatchableSignalController()
    engine.SIGNAL_CONTROLLER = controller
    previous = signal.signal(signal.SIGTERM, lambda signum, _frame: controller.handle(signum))
    deliveries: list[int] = []
    function = engine.GitRunner.run if owner == "git" else engine.http_json
    real_launch = engine.launch_owned
    launches: list[Any] = []

    if owner == "http":
        def launch_error(_argv: list[str], **options: Any) -> Any:
            payload = json.dumps({"error": "captured HTTP failure", "status": "refused"})
            process = real_launch(
                [sys.executable, "-I", "-c", "import sys;sys.stdout.write(sys.argv[1])", payload],
                **options,
            )
            launches.append(process)
            return process

        monkeypatch.setattr(engine, "launch_owned", launch_error)
    else:
        def count_launch(argv: list[str], **options: Any) -> Any:
            process = real_launch(argv, **options)
            launches.append(process)
            return process

        monkeypatch.setattr(engine, "launch_owned", count_launch)
    sys.settrace(signal_trace_at(function, "if terminal_failure is not None:", deliveries))
    try:
        if owner == "git":
            home = tmp_path / "home"
            home.mkdir()
            runner = engine.GitRunner(
                subprocess_timeout=HANG_GUARD,
                deadline=time.monotonic() + HANG_GUARD,
                home=home,
            )
            records = runner.records
            with pytest.raises(engine.MaintenanceError, match="direct rc"):
                runner.run(None, "definitely-not-a-command")
        else:
            records = []
            with pytest.raises(engine.MaintenanceError, match="captured HTTP failure"):
                engine.http_json(
                    "http://127.0.0.1:1/safe",
                    HANG_GUARD,
                    time.monotonic() + HANG_GUARD,
                    records,
                )
    finally:
        sys.settrace(None)
        signal.signal(signal.SIGTERM, previous)
        engine.SIGNAL_CONTROLLER = None
    assert deliveries == [signal.SIGTERM]
    assert len(launches) == 1
    assert len(records) == 1 and records[0]["interrupted"] is True
    assert records[0]["stdout_eof"] is True and records[0]["stderr_eof"] is True
    assert records[0]["process_group_settled"] is True
    assert records[0]["direct_rc"] is not None
    if owner == "git":
        assert records[0]["direct_rc"] != 0
        assert records[0]["_stderr_raw"]
    else:
        assert b"captured HTTP failure" in records[0]["_stdout_raw"]


@pytest.mark.parametrize("owner", ["git", "http"])
@pytest.mark.parametrize(
    "marker",
    [
        "drained = True",
        "primary_captured = True",
        "while record is None:",
        "terminal_failure: BaseException | None = unexpected",
        "with signal_protection():",
    ],
)
def test_first_signal_across_post_record_failure_transitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str, marker: str
) -> None:
    """The first signal reaches the late handoff after one complete owned record."""
    original = signal_trace_at

    def at_seam(function: Any, requested: str, deliveries: list[int]) -> Any:
        assert requested == "if terminal_failure is not None:"
        actual_marker = (
            "drained = True  # a returned communicate() observed EOF on both pipes"
            if marker == "drained = True" and owner == "http"
            else marker
        )
        return original(
            function,
            actual_marker,
            deliveries,
            ready=(
                (lambda frame: frame.f_locals.get("process").returncode is not None)
                if marker == "drained = True"
                else (lambda frame: frame.f_locals.get("drained") is True)
                if marker == "primary_captured = True"
                else (lambda frame: frame.f_locals.get("record") is not None)
            ),
            require_clear=True,
        )

    monkeypatch.setattr(sys.modules[__name__], "signal_trace_at", at_seam)
    test_installed_signal_during_failure_handoff_keeps_captured_cause(
        tmp_path, monkeypatch, owner
    )


@pytest.mark.parametrize("owner", ["git", "http"])
def test_first_signal_during_primary_does_not_promote_termination_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    """A real child terminated by the first signal has no earlier worker failure."""
    engine = load_engine()
    controller = engine.CatchableSignalController()
    engine.SIGNAL_CONTROLLER = controller
    previous = signal.signal(signal.SIGTERM, lambda signum, _frame: controller.handle(signum))
    real_launch = engine.launch_owned
    launches: list[Any] = []

    def sleeping_child(_argv: list[str], **options: Any) -> Any:
        process = real_launch(
            [sys.executable, "-I", "-c", "import time;time.sleep(60)"], **options
        )
        launches.append(process)
        return process

    monkeypatch.setattr(engine, "launch_owned", sleeping_child)
    marker = (
        "primary_output = process.communicate(timeout=timeout)"
        if owner == "git"
        else "primary_output = process.communicate("
    )
    function = engine.GitRunner.run if owner == "git" else engine.http_json
    deliveries: list[int] = []
    sys.settrace(signal_trace_at(function, marker, deliveries, require_clear=True))
    try:
        if owner == "git":
            home = tmp_path / "home"
            home.mkdir()
            runner = engine.GitRunner(
                subprocess_timeout=HANG_GUARD,
                deadline=time.monotonic() + HANG_GUARD,
                home=home,
            )
            records = runner.records
            with pytest.raises(engine.MaintenanceError) as caught:
                runner.run(None, "--version")
            assert caught.value.status == "interrupted"
        else:
            records = []
            with pytest.raises(engine.CaughtSignal):
                engine.http_json(
                    "http://127.0.0.1:1/safe",
                    HANG_GUARD,
                    time.monotonic() + HANG_GUARD,
                    records,
                )
    finally:
        sys.settrace(None)
        signal.signal(signal.SIGTERM, previous)
        engine.SIGNAL_CONTROLLER = None
    assert deliveries == [signal.SIGTERM]
    assert len(launches) == len(records) == 1
    assert records[0]["direct_rc"] == -signal.SIGTERM
    assert records[0]["interrupted"] is True
    assert records[0]["process_group_settled"] is True
    assert records[0]["stdout_eof"] is True and records[0]["stderr_eof"] is True
    assert engine.process_group_absent(records[0]["process_group"]) is True


@pytest.mark.parametrize("owner", ["git", "http"])
@pytest.mark.parametrize("late_seam", ["classification", "return"])
def test_first_signal_after_captured_success_is_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str, late_seam: str
) -> None:
    engine = load_engine()
    controller = engine.CatchableSignalController()
    engine.SIGNAL_CONTROLLER = controller
    previous = signal.signal(signal.SIGTERM, lambda signum, _frame: controller.handle(signum))
    launches: list[Any] = []
    real_launch = engine.launch_owned
    if owner == "http":
        payload = json.dumps({"value": [{"ok": True}, {}, "a" * 64]})

        def valid_worker(_argv: list[str], **options: Any) -> Any:
            process = real_launch(
                [sys.executable, "-I", "-c", "import sys;sys.stdout.write(sys.argv[1])", payload],
                **options,
            )
            launches.append(process)
            return process

        monkeypatch.setattr(engine, "launch_owned", valid_worker)
    else:
        def count_launch(argv: list[str], **options: Any) -> Any:
            process = real_launch(argv, **options)
            launches.append(process)
            return process

        monkeypatch.setattr(engine, "launch_owned", count_launch)
    function = engine.GitRunner.run if owner == "git" else engine.http_json
    marker = (
        "terminal_failure: BaseException | None = unexpected"
        if late_seam == "classification"
        else "return result" if owner == "git" else "return successful_result"
    )
    deliveries: list[int] = []
    sys.settrace(signal_trace_at(function, marker, deliveries, require_clear=True))
    try:
        if owner == "git":
            home = tmp_path / "home"
            home.mkdir()
            runner = engine.GitRunner(
                subprocess_timeout=HANG_GUARD,
                deadline=time.monotonic() + HANG_GUARD,
                home=home,
            )
            records = runner.records
            with pytest.raises(engine.MaintenanceError) as caught:
                runner.run(None, "--version")
            assert caught.value.status == "interrupted"
        else:
            records = []
            with pytest.raises(engine.CaughtSignal):
                engine.http_json(
                    "http://127.0.0.1:1/safe",
                    HANG_GUARD,
                    time.monotonic() + HANG_GUARD,
                    records,
                )
    finally:
        sys.settrace(None)
        signal.signal(signal.SIGTERM, previous)
        engine.SIGNAL_CONTROLLER = None
    assert deliveries == [signal.SIGTERM]
    assert len(launches) == len(records) == 1
    assert records[0]["direct_rc"] == 0
    assert records[0]["interrupted"] is True
    assert records[0]["raw_complete"] is True


@pytest.mark.parametrize(
    "payload,expected_status,expected_error",
    [
        (json.dumps({"status": "timeout"}), "timeout", "timed out"),
        ("not-json", "refused", "invalid JSON"),
    ],
)
def test_late_signal_preserves_captured_http_envelope_failure(
    monkeypatch: pytest.MonkeyPatch, payload: str, expected_status: str, expected_error: str
) -> None:
    engine = load_engine()
    controller = engine.CatchableSignalController()
    engine.SIGNAL_CONTROLLER = controller
    previous = signal.signal(signal.SIGTERM, lambda signum, _frame: controller.handle(signum))
    real_launch = engine.launch_owned
    launches: list[Any] = []

    def captured_worker(_argv: list[str], **options: Any) -> Any:
        process = real_launch(
            [sys.executable, "-I", "-c", "import sys;sys.stdout.write(sys.argv[1])", payload],
            **options,
        )
        launches.append(process)
        return process

    monkeypatch.setattr(engine, "launch_owned", captured_worker)
    deliveries: list[int] = []
    sys.settrace(
        signal_trace_at(
            engine.http_json,
            "terminal_failure: BaseException | None = unexpected",
            deliveries,
            require_clear=True,
        )
    )
    records: list[dict[str, Any]] = []
    try:
        with pytest.raises(engine.MaintenanceError, match=expected_error) as caught:
            engine.http_json(
                "http://127.0.0.1:1/safe",
                HANG_GUARD,
                time.monotonic() + HANG_GUARD,
                records,
            )
        assert caught.value.status == expected_status
    finally:
        sys.settrace(None)
        signal.signal(signal.SIGTERM, previous)
        engine.SIGNAL_CONTROLLER = None
    assert deliveries == [signal.SIGTERM]
    assert len(launches) == len(records) == 1
    assert records[0]["_stdout_raw"] == payload.encode()
    assert records[0]["direct_rc"] == 0 and records[0]["interrupted"] is True
    assert records[0]["raw_complete"] is True


def test_late_signal_interrupts_completed_check_false_nonzero(
    tmp_path: Path
) -> None:
    """M2: an accepted nonzero probe cannot return into a success/conflict caller."""
    engine = load_engine()
    home = tmp_path / "home"
    home.mkdir()
    no_signal = engine.GitRunner(
        subprocess_timeout=HANG_GUARD,
        deadline=time.monotonic() + HANG_GUARD,
        home=home,
    )
    control = no_signal.run(None, "definitely-not-a-command", check=False)
    assert control.returncode != 0
    assert len(no_signal.records) == 1

    controller = engine.CatchableSignalController()
    engine.SIGNAL_CONTROLLER = controller
    previous = signal.signal(signal.SIGTERM, lambda signum, _frame: controller.handle(signum))
    runner = engine.GitRunner(
        subprocess_timeout=HANG_GUARD,
        deadline=time.monotonic() + HANG_GUARD,
        home=home,
    )
    deliveries: list[int] = []
    sys.settrace(
        signal_trace_at(
            engine.GitRunner.run,
            "terminal_failure: BaseException | None = unexpected",
            deliveries,
            require_clear=True,
        )
    )
    try:
        with pytest.raises(engine.MaintenanceError) as caught:
            runner.run(None, "definitely-not-a-command", check=False)
        assert caught.value.status == "interrupted"
    finally:
        sys.settrace(None)
        signal.signal(signal.SIGTERM, previous)
        engine.SIGNAL_CONTROLLER = None
    assert deliveries == [signal.SIGTERM]
    assert len(runner.records) == 1
    assert runner.records[0]["direct_rc"] == control.returncode
    assert runner.records[0]["interrupted"] is True


@pytest.mark.parametrize("owner", ["git", "http"])
@pytest.mark.parametrize("observed", [False, None])
@pytest.mark.parametrize("late_signal", [False, True])
def test_first_signal_after_group_observation_preserves_non_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    owner: str, observed: bool | None, late_signal: bool,
) -> None:
    """A stored survivor or unknown observation is prior evidence, even after cleanup."""
    engine = load_engine()
    real_observe = engine.process_group_absent
    observations = 0

    def first_observation(group: int) -> bool | None:
        nonlocal observations
        observations += 1
        return observed if observations == 1 else real_observe(group)

    monkeypatch.setattr(engine, "process_group_absent", first_observation)
    real_launch = engine.launch_owned
    launches: list[Any] = []
    if owner == "http":
        payload = json.dumps({"value": [{"ok": True}, {}, "a" * 64]})

        def valid_worker(_argv: list[str], **options: Any) -> Any:
            process = real_launch(
                [sys.executable, "-I", "-c", "import sys;sys.stdout.write(sys.argv[1])", payload],
                **options,
            )
            launches.append(process)
            return process

        monkeypatch.setattr(engine, "launch_owned", valid_worker)
    else:
        def count_launch(argv: list[str], **options: Any) -> Any:
            process = real_launch(argv, **options)
            launches.append(process)
            return process

        monkeypatch.setattr(engine, "launch_owned", count_launch)
    controller = engine.CatchableSignalController()
    previous: Any = None
    deliveries: list[int] = []
    if late_signal:
        engine.SIGNAL_CONTROLLER = controller
        previous = signal.signal(signal.SIGTERM, lambda signum, _frame: controller.handle(signum))
        function = engine.GitRunner.run if owner == "git" else engine.http_json
        sys.settrace(
            signal_trace_at(function, "settled = group_observation is True", deliveries,
                            require_clear=True)
        )
    try:
        if owner == "git":
            home = tmp_path / "home"
            home.mkdir()
            runner = engine.GitRunner(
                subprocess_timeout=HANG_GUARD,
                deadline=time.monotonic() + HANG_GUARD,
                home=home,
            )
            records = runner.records
            with pytest.raises(engine.MaintenanceError, match="process group remained"):
                runner.run(None, "--version")
        else:
            records = []
            with pytest.raises(engine.MaintenanceError, match="process group remained"):
                engine.http_json(
                    "http://127.0.0.1:1/safe", HANG_GUARD,
                    time.monotonic() + HANG_GUARD, records,
                )
    finally:
        if late_signal:
            sys.settrace(None)
            signal.signal(signal.SIGTERM, previous)
            engine.SIGNAL_CONTROLLER = None
    assert deliveries == ([signal.SIGTERM] if late_signal else [])
    assert observations >= 1
    assert len(launches) == len(records) == 1
    assert records[0]["process_group_absent_after_primary"] is observed
    assert records[0]["interrupted"] is late_signal
    assert records[0]["raw_complete"] is records[0]["process_group_settled"]


# Retained caller controls use the repository-local fixtures and public entry point.
def evidence_fingerprint(root: Path) -> dict:
    """Bind receipt/lock bytes plus identity without reading unrelated files."""
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            result[str(path.relative_to(root))] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
            )
    return result


def test_actual_failed_publication_public_replay_preserves_ownership(
    tmp_path, monkeypatch, capsys
):
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    preview = preview_override(repos, plan)
    assert preview.returncode == 0, preview.stderr
    output, evidence = tmp_path / "output", tmp_path / "evidence"
    argv = apply_args(repos, plan, output, evidence, "codex/caller-post-link")
    engine = load_engine()
    real_write = engine.write_integrity_bound_result
    publications = []

    def real_link_then_fail(path, result, *, deadline=None):
        # Labelled seam: the real exclusive writer exposes exact receipt bytes first.
        real_write(path, result, deadline=deadline)
        publications.append(path)
        raise TypeError("caller post-link publication failure")

    monkeypatch.setattr(engine, "write_integrity_bound_result", real_link_then_fail)
    direct_rc = engine.main(list(argv))
    captured = capsys.readouterr()
    first = json.loads(captured.err)
    assert direct_rc != 0 and first["status"] == "evidence_error"
    assert first["terminal_status"] == "applied"
    assert first["receipt_visibility"] == first["lock_visibility"] == "present"
    assert publications == [evidence / "result.json"]
    assert (evidence / "result.json").is_file() and (evidence / "attempt.lock").is_file()
    before = evidence_fingerprint(evidence)
    source_before = source_fingerprint(repos["source"])
    output_before = source_fingerprint(output)

    replay = run_cli(*argv)

    assert replay.returncode == 2, (replay.stdout, replay.stderr)
    second = json.loads(replay.stderr)
    assert second["status"] == "refused"
    assert second["lock_path"] == str(evidence / "attempt.lock")
    assert second["result_path"] == str(evidence / "result.json")
    assert evidence_fingerprint(evidence) == before
    assert source_fingerprint(repos["source"]) == source_before
    assert source_fingerprint(output) == output_before


@pytest.mark.parametrize("mechanism", ["redirect", "pagination"])
@pytest.mark.parametrize("component", ["token", "userinfo"])
def test_rejected_destination_never_requested_or_published(
    tmp_path, metadata_server, mechanism, component
):
    repos = make_repositories(tmp_path)
    synthetic = "CALLER_SYNTHETIC_VALUE_NOT_A_CREDENTIAL"

    def first(handler, server):
        if component == "token":
            destination = server.url + "/rejected?token=" + synthetic
        else:
            destination = server.url.replace("http://", "http://" + synthetic + "@")
            destination += "/rejected"
        if mechanism == "redirect":
            handler.send_response(302)
            handler.send_header("Location", destination)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        else:
            send_json_links(
                handler, [release_entry("v1.3.0", 3)],
                [f'<{destination}>; rel="next"'],
            )

    server = metadata_server({"/releases": first, "*": missing})
    plan = tmp_path / "plan.json"
    result = preview_live(repos, server, plan)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert server.requests == ["/releases?page=1"]
    assert synthetic not in result.stdout + result.stderr
    assert not plan.exists()
    for directory in tmp_path.glob(".plan.json.preview-evidence-*"):
        for artifact in directory.rglob("*"):
            if artifact.is_file():
                assert synthetic.encode() not in artifact.read_bytes(), str(artifact)


@pytest.mark.parametrize("late_signal", [False, True])
def test_completed_check_false_conflict_handoff_through_main(tmp_path, capsys, late_signal):
    repos = make_repositories(tmp_path)
    repos["candidate"] = commit_file(
        repos["source"], "shared.txt", "fork conflict\n", "fork conflict"
    )
    repos["target"] = commit_file(
        repos["upstream_work"], "shared.txt", "upstream conflict\n", "upstream conflict"
    )
    git(repos["upstream_work"], "push", "publish", "main")
    plan = tmp_path / "plan.json"
    preview = preview_override(repos, plan)
    assert preview.returncode == 0, preview.stderr
    output, evidence = tmp_path / "output", tmp_path / "evidence"
    argv = apply_args(repos, plan, output, evidence, "codex/caller-late-conflict")
    engine = load_engine()
    lines, first_line = inspect.getsourcelines(engine.GitRunner.run)
    return_lines = {
        first_line + offset for offset, line in enumerate(lines)
        if line.strip() == "return result"
    }
    assert len(return_lines) == 1
    deliveries = []

    def trace(frame, event, _arg):
        if (
            late_signal and not deliveries and event == "line"
            and frame.f_code is engine.GitRunner.run.__code__
            and frame.f_lineno in return_lines
            and frame.f_locals["arguments"][0] == "rebase"
        ):
            # The actual rebase has already exited nonzero under check=False.
            assert frame.f_locals["check"] is False
            assert frame.f_locals["result"].returncode != 0
            deliveries.append(signal.SIGTERM)
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
        return trace

    sys.settrace(trace)
    try:
        rc = engine.main(list(argv))
    finally:
        sys.settrace(None)
    captured = capsys.readouterr()
    result = json.loads(captured.err if late_signal else captured.out)
    assert rc == (4 if late_signal else 3), (captured.out, captured.err)
    expected = "interrupted" if late_signal else "conflict"
    assert result["status"] == expected
    assert deliveries == ([signal.SIGTERM] if late_signal else [])
    receipt = json.loads((evidence / "result.json").read_text())
    assert receipt["status"] == expected
    assert not (evidence / "attempt.lock").exists()
    rebases = [
        item for item in receipt["command_records"]
        if "rebase" in item.get("argv", []) and "--onto" in item["argv"]
    ]
    assert len(rebases) == 1
    assert rebases[0]["direct_rc"] != 0
    assert rebases[0]["interrupted"] is late_signal
    assert rebases[0]["raw_complete"] is True
    assert git(output, "diff", "--name-only", "--diff-filter=U").stdout == "shared.txt\n"


def test_initialized_submodule_filter_is_refused_across_preview_apply_and_replay(
    tmp_path: Path,
) -> None:
    """Direct nested status runs a same-size clean filter; public paths refuse it."""
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    valid = preview_override(repos, plan)
    assert valid.returncode == 0, valid.stderr
    output, evidence = tmp_path / "output", tmp_path / "evidence"
    replay_args = apply_args(repos, plan, output, evidence, "codex/submodule-replay")
    first = run_cli(*replay_args)
    assert first.returncode == 0, (first.stdout, first.stderr)
    assert (evidence / "result.json").is_file()

    child = tmp_path / "child"
    child.mkdir()
    git(child, "init", "-b", "main")
    commit_file(child, ".gitattributes", "data.txt filter=probe\n", "attributes")
    commit_file(child, "data.txt", "original\n", "data")
    git(
        repos["source"], "-c", "protocol.file.allow=always",
        "submodule", "add", str(child), "nested",
    )
    git(repos["source"], "commit", "-am", "add initialized submodule")
    nested = repos["source"] / "nested"
    marker = tmp_path / "nested-filter-invoked"
    git(
        nested, "config", "filter.probe.clean",
        f"printf invoked > {shlex.quote(str(marker))}; cat",
    )
    original = (nested / "data.txt").read_bytes()
    changed = b"different"
    assert len(original) == len(changed)
    (nested / "data.txt").write_bytes(changed)
    control = git(nested, "status", "--porcelain=v1", "--untracked-files=all", check=False)
    assert control.returncode == 0, control.stderr
    assert marker.is_file(), "direct nested Git status must invoke the clean filter"
    marker.unlink()
    source = repos["source"]
    source_index = (source / ".git" / "index").read_bytes()
    source_head = (source / ".git" / "HEAD").read_bytes()
    source_refs = git(source, "for-each-ref", "--format=%(refname) %(objectname)").stdout
    source_objects = git(source, "count-objects", "-v").stdout
    source_fetch = (source / ".git" / "FETCH_HEAD")
    fetch_before = source_fetch.read_bytes() if source_fetch.exists() else None
    evidence_before = evidence_fingerprint(evidence)
    output_before = source_fingerprint(output)
    plan_before = plan.read_bytes()

    preview_plan = tmp_path / "unsupported-plan.json"
    preview = preview_override(repos, preview_plan)
    fresh = run_cli(*apply_args(
        repos, plan, tmp_path / "fresh-output", tmp_path / "fresh-evidence",
        "codex/submodule-fresh",
    ))
    replay = run_cli(*replay_args)

    for refusal in (preview, fresh, replay):
        assert refusal.returncode == 2, (refusal.stdout, refusal.stderr)
        assert "submodule" in refusal.stdout.lower() + refusal.stderr.lower()
    assert not preview_plan.exists()
    assert not (tmp_path / "fresh-output").exists()
    assert not (tmp_path / "fresh-evidence").exists()
    assert not marker.exists(), "none of the refused paths may run the nested helper"
    assert (source / ".git" / "index").read_bytes() == source_index
    assert (source / ".git" / "HEAD").read_bytes() == source_head
    assert git(source, "for-each-ref", "--format=%(refname) %(objectname)").stdout == source_refs
    assert git(source, "count-objects", "-v").stdout == source_objects
    assert (source_fetch.read_bytes() if source_fetch.exists() else None) == fetch_before
    assert evidence_fingerprint(evidence) == evidence_before
    assert source_fingerprint(output) == output_before
    assert plan.read_bytes() == plan_before


@pytest.mark.parametrize(
    "key", [
        "apiKey", "accessToken", "clientSecret", "apikey", "accesstoken", "clientsecret",
        "api.key", "api%2Ekey", "access.token", "client.secret",
        "api:key", "api/key", "access+token", "client%7Esecret", "client%257Esecret",
    ]
)
@pytest.mark.parametrize("route_kind", ["initial", "redirect", "pagination"])
def test_credential_alias_destination_is_refused_before_http_launch(
    tmp_path: Path, metadata_server: Any, key: str, route_kind: str
) -> None:
    repos = make_repositories(tmp_path)
    synthetic = "SYNTHETIC_CREDENTIAL_ALIAS_NOT_SECRET"

    def first(handler: Any, server: MetadataServer) -> None:
        destination = f"{server.url}/rejected?{key}={synthetic}"
        if route_kind == "redirect":
            handler.send_response(302)
            handler.send_header("Location", destination)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        else:
            send_json_links(handler, [release_entry("v1.3.0", 3)],
                            [f'<{destination}>; rel="next"'])

    server = metadata_server({"/releases": first, "/safe": lambda h, s: send_json(h, []),
                              "*": missing})
    plan = tmp_path / "plan.json"
    initial_url = f"{server.url}/rejected?{key}={synthetic}" if route_kind == "initial" else None
    result = preview_live(repos, server, plan, initial_url=initial_url)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert server.requests == ([] if route_kind == "initial" else ["/releases?page=1"])
    assert synthetic not in result.stdout + result.stderr
    assert not plan.exists()
    for directory in tmp_path.glob(".plan.json.preview-evidence-*"):
        for artifact in directory.rglob("*"):
            if artifact.is_file():
                assert synthetic.encode() not in artifact.read_bytes()


@pytest.mark.parametrize("route_kind", ["initial", "redirect", "pagination"])
def test_benign_query_keys_reach_public_preview(
    tmp_path: Path, metadata_server: Any, route_kind: str
) -> None:
    repos = make_repositories(tmp_path)
    routes = dict(LIVE_ROUTES)
    if route_kind == "redirect":
        routes["/releases"] = redirect_to("/moved?page=2&per_page=100&secretary=desk")
        routes["/moved"] = LIVE_ROUTES["/releases"]
    elif route_kind == "pagination":
        routes["/releases"] = lambda handler, server: send_json(
            handler, [release_entry("v1.3.0", 3), release_entry("v1.2.0", 2)],
            link=f"{server.url}/older?page=2&per_page=100&secretary=desk",
        )
    server = metadata_server(routes)
    plan = tmp_path / "plan.json"
    initial = (
        f"{server.url}/releases?page=1&per_page=100&secretary=desk"
        if route_kind == "initial" else None
    )
    result = preview_live(repos, server, plan, initial_url=initial)
    assert result.returncode == 0, (result.stdout, result.stderr)
    outcome = json.loads(result.stdout)
    assert outcome["status"] == "planned"
    assert outcome["plan_sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()
    assert server.requests[0].startswith("/releases?page=1")
    if route_kind != "initial":
        assert any("secretary=desk" in request for request in server.requests)


def test_mise_initial_endpoint_refuses_delimited_credential_key(
    tmp_path: Path, metadata_server: Any
) -> None:
    repos = make_repositories(tmp_path)
    server = metadata_server({"*": missing})
    plan = tmp_path / "plan.json"
    marker = "SYNTHETIC_MISE_VALUE_NOT_SECRET"
    command = [
        "mise", "run", "fork-maintenance", "--", "preview",
        "--source-repo", str(repos["source"]), "--candidate", repos["candidate"],
        "--upstream-repository", "Graphify-Labs/graphify",
        "--upstream-url", str(repos["upstream"]),
        "--github-releases-url", f"{server.url}/rejected?api:key={marker}",
        "--pypi-base-url", server.url, "--output-plan", str(plan),
    ]
    result = subprocess.run(
        command, cwd=ROOT, env={**os.environ, "no_proxy": "127.0.0.1"},
        capture_output=True, text=True, timeout=HANG_GUARD,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert server.requests == [] and not plan.exists()
    assert marker not in result.stdout + result.stderr
    assert not list(tmp_path.glob(".plan.json.preview-evidence-*"))


def test_ordinary_query_metadata_is_allowed_and_preserves_raw_response(
    metadata_server: Any,
) -> None:
    raw = b'[ {"ordinary": true} ]'

    def ordinary(handler: Any, server: MetadataServer) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    server = metadata_server({"/ordinary": ordinary, "*": missing})
    engine = load_engine()
    records: list[dict[str, Any]] = []
    parsed, _headers, digest = engine.http_json(
        f"{server.url}/ordinary?page=2&sort=recent", HANG_GUARD,
        time.monotonic() + HANG_GUARD, records,
    )
    assert server.requests == ["/ordinary?page=2&sort=recent"]
    assert len(records) == 1
    assert parsed == [{"ordinary": True}]
    assert digest == hashlib.sha256(raw).hexdigest()
    assert records[0]["response_sha256"] == digest


@pytest.mark.parametrize("hidden", [
    "1;apiKey=SYNTHETIC_HIDDEN_ALIAS",
    "1%3BapiKey%3DSYNTHETIC_HIDDEN_ALIAS",
    "1%26apiKey%3DSYNTHETIC_HIDDEN_ALIAS",
    "1%253BapiKey%253DSYNTHETIC_HIDDEN_ALIAS",
    "1%2526apiKey%253DSYNTHETIC_HIDDEN_ALIAS",
])
@pytest.mark.parametrize("route_kind", ["initial", "redirect", "pagination"])
def test_hidden_query_delimiter_refused_on_every_public_route(
    tmp_path: Path, metadata_server: Any, hidden: str, route_kind: str
) -> None:
    repos = make_repositories(tmp_path)
    destination = f"/rejected?page={hidden}"
    routes = dict(LIVE_ROUTES)
    if route_kind == "redirect":
        routes["/releases"] = redirect_to(destination)
    elif route_kind == "pagination":
        routes["/releases"] = lambda handler, server: send_json(
            handler, [], link=server.url + destination,
        )
    server = metadata_server(routes)
    plan = tmp_path / "plan.json"
    result = preview_live(
        repos, server, plan,
        initial_url=server.url + destination if route_kind == "initial" else None,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert server.requests == ([] if route_kind == "initial" else ["/releases?page=1"])
    assert "SYNTHETIC_HIDDEN_ALIAS" not in result.stdout + result.stderr
    assert not plan.exists()
    assert not list(tmp_path.glob(".plan.json.preview-evidence-*"))


def test_encoded_ordinary_query_value_remains_admitted_in_public_preview(
    tmp_path: Path, metadata_server: Any
) -> None:
    repos = make_repositories(tmp_path)
    server = metadata_server(dict(LIVE_ROUTES))
    plan = tmp_path / "plan.json"
    result = preview_live(
        repos, server, plan,
        initial_url=f"{server.url}/releases?page=1&note=release%20notes&secretary=desk",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert plan.exists()
    assert server.requests[0].endswith("note=release%20notes&secretary=desk")


@pytest.mark.parametrize("failure", ["http503", "timeout"])
def test_public_preview_http_failure_retains_bounded_raw_and_structured_evidence(
    tmp_path: Path, metadata_server: Any, failure: str
) -> None:
    repos = make_repositories(tmp_path)
    before = source_fingerprint(repos["source"])

    def fail(handler: Any, server: MetadataServer) -> None:
        if failure == "timeout":
            handler.send_response(200)
            handler.send_header("Content-Length", "999")
            handler.end_headers()
            handler.wfile.write(b'[ {"partial": true')
            handler.wfile.flush()
            server.stop.wait()
        else:
            send_json(handler, {"failure": "synthetic"}, status=503)

    server = metadata_server({"/releases": fail, "*": missing})
    plan = tmp_path / "plan.json"
    # The 503 arm verifies response evidence, not process-start latency. Keep
    # the deliberately short budget only for the separate timeout arm.
    network_timeout = "0.3" if failure == "timeout" else "2"
    result = preview_live(
        repos, server, plan, "--network-timeout", network_timeout, "--attempt-timeout", "30",
    )
    assert result.returncode == (4 if failure == "timeout" else 2), (result.stdout, result.stderr)
    outcome = json.loads(result.stderr)
    assert outcome["status"] == ("timeout" if failure == "timeout" else "refused")
    assert "HTTP" in outcome["error"]
    assert not plan.exists() and source_fingerprint(repos["source"]) == before
    directory = Path(outcome["command_evidence"])
    records_path = Path(outcome["command_records_path"])
    assert records_path.parent == directory
    assert hashlib.sha256(records_path.read_bytes()).hexdigest() == outcome["command_records_sha256"]
    records = json.loads(records_path.read_text())["command_records"]
    http = [record for record in records if record.get("origin") == "http_worker_envelope"]
    assert len(http) == 1
    record = http[0]
    assert record["direct_rc"] is not None
    assert record["stdout_eof"] and record["stderr_eof"]
    assert record["process_group_settled"] and record["raw_complete"]
    assert isinstance(record["process_group_observed"], bool)
    if failure == "http503":
        assert record["process_group_observed"]
    for stream in ("stdout", "stderr"):
        raw = Path(record[stream]["raw_path"]).read_bytes()
        assert len(raw) == record[stream]["bytes"]
        assert hashlib.sha256(raw).hexdigest() == record[stream]["sha256"]
    if failure == "http503":
        assert record["stdout"]["bytes"] > 0


def test_public_http_failure_cleanup_reuses_worker_shutdown_deadline(
    tmp_path: Path, metadata_server: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    repos = make_repositories(tmp_path)
    source_before = source_fingerprint(repos["source"])
    server = metadata_server({"/releases": lambda handler, _server: send_json(
        handler, {"failure": "synthetic"}, status=503,
    ), "*": missing})
    engine = load_engine()
    clock = [100.0]
    engine.time = types.SimpleNamespace(
        monotonic=lambda: clock[0], time_ns=time.time_ns, sleep=time.sleep,
    )
    real_cleanup = engine.TerminalScratch.cleanup
    observed: list[tuple[float, str]] = []

    def expire_at_cleanup(scratch: Any) -> None:
        if not scratch.attempted:
            observed.append((scratch.deadline(), scratch.path))
            clock[0] = scratch.deadline() + 1.0
        real_cleanup(scratch)

    monkeypatch.setattr(engine.TerminalScratch, "cleanup", expire_at_cleanup)
    plan = tmp_path / "plan.json"
    argv = [
        "preview", "--source-repo", str(repos["source"]),
        "--candidate", repos["candidate"],
        "--upstream-repository", "Graphify-Labs/graphify",
        "--upstream-url", str(repos["upstream"]),
        "--github-releases-url", f"{server.url}/releases?page=1",
        "--pypi-base-url", server.url,
        "--network-timeout", "2", "--attempt-timeout", "300",
        "--output-plan", str(plan),
    ]
    try:
        direct_rc = engine.main(argv)
        captured = capsys.readouterr()
        outcome = json.loads(captured.err)
        assert direct_rc == 2 and not captured.out
        assert outcome["status"] == "evidence_error"
        assert outcome["primary_status"] == "refused"
        assert "HTTP request failed with status 503" in outcome["primary_error"]
        assert observed and observed[0][0] == outcome["shutdown_deadline_monotonic"]
        assert observed[0][0] == 110.0
        assert not plan.exists()
        assert source_fingerprint(repos["source"]) == source_before
    finally:
        for _deadline, path in observed:
            shutil.rmtree(path, ignore_errors=True)


def test_public_preview_retains_nonempty_partial_worker_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    # The external worker is synthetic; preview, Git, subprocess ownership,
    # settlement, failure publication and CLI main are the product paths.
    repos = make_repositories(tmp_path)
    before = source_fingerprint(repos["source"])
    worker = tmp_path / "synthetic-http-worker"
    ready = tmp_path / "worker-wrote-partial-streams"
    worker.write_text(
        f"#!{sys.executable}\n"
        "import os,time\n"
        "os.write(1,b'HTTP-PARTIAL\\x00\\xff\\r\\n')\n"
        "os.write(2,b'HTTP-DIAGNOSTIC\\x00\\xfe\\r\\n')\n"
        f"open({str(ready)!r}, 'wb').close()\n"
        "time.sleep(20)\n",
        encoding="utf-8",
    )
    worker.chmod(0o755)
    engine = load_engine()

    class WorkerProxy:
        executable = str(worker)

        def __getattr__(self, name: str) -> Any:
            return getattr(sys, name)

    engine.sys = WorkerProxy()
    launch_owned = engine.launch_owned

    def launch_after_partial_streams(argv: list[str], **options: Any) -> subprocess.Popen[bytes]:
        process = launch_owned(argv, **options)
        if argv[0] == str(worker):
            startup_deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < startup_deadline:
                time.sleep(0.01)
            if not ready.exists():
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=2)
                pytest.fail("synthetic HTTP worker did not write partial streams")
        return process

    monkeypatch.setattr(engine, "launch_owned", launch_after_partial_streams)
    plan = tmp_path / "plan.json"
    direct_rc = engine.main([
        "preview", "--source-repo", str(repos["source"]),
        "--candidate", repos["candidate"],
        "--upstream-repository", "Graphify-Labs/graphify",
        "--upstream-url", str(repos["upstream"]),
        "--github-releases-url", "http://127.0.0.1:9/stall",
        "--pypi-base-url", "http://127.0.0.1:9",
        "--network-timeout", "8", "--attempt-timeout", "30",
        "--output-plan", str(plan),
    ])
    captured = capsys.readouterr()
    assert direct_rc == 4 and not captured.out
    outcome = json.loads(captured.err)
    assert outcome["status"] == "timeout" and not plan.exists()
    records_path = Path(outcome["command_records_path"])
    assert hashlib.sha256(records_path.read_bytes()).hexdigest() == outcome["command_records_sha256"]
    records = json.loads(records_path.read_text())["command_records"]
    http = [record for record in records if record.get("origin") == "http_worker_envelope"]
    assert len(http) == 1
    record = http[0]
    assert record["raw_complete"] and record["stdout_eof"] and record["stderr_eof"]
    assert record["process_group_settled"] and record["direct_rc"] is not None
    for stream, expected in (
        ("stdout", b"HTTP-PARTIAL\x00\xff\r\n"),
        ("stderr", b"HTTP-DIAGNOSTIC\x00\xfe\r\n"),
    ):
        raw = Path(record[stream]["raw_path"]).read_bytes()
        assert raw == expected
        assert record[stream]["bytes"] == len(raw)
        assert record[stream]["sha256"] == hashlib.sha256(raw).hexdigest()
    with pytest.raises(ProcessLookupError):
        os.killpg(record["process_group"], 0)
    assert source_fingerprint(repos["source"]) == before


def public_preview_argv(repos: dict[str, Any], server: MetadataServer, plan: Path) -> list[str]:
    return [
        "preview", "--source-repo", str(repos["source"]),
        "--candidate", repos["candidate"],
        "--upstream-repository", "Graphify-Labs/graphify",
        "--upstream-url", str(repos["upstream"]),
        "--github-releases-url", f"{server.url}/releases?page=1",
        "--pypi-base-url", server.url,
        "--network-timeout", "2", "--attempt-timeout", "30",
        "--output-plan", str(plan),
    ]


@pytest.mark.parametrize("interrupted", [False, True])
def test_preview_source_admission_handoff_retains_safe_evidence(
    tmp_path: Path, metadata_server: Any, capsys: Any, interrupted: bool,
) -> None:
    repos = make_repositories(tmp_path)
    before = source_fingerprint(repos["source"])
    server = metadata_server(dict(LIVE_ROUTES))
    engine = load_engine()
    plan = tmp_path / "plan.json"
    admissions: list[int] = []
    original_streams: list[tuple[bytes, bytes]] = []

    def trace(frame: Any, event: str, value: Any) -> Any:
        if (frame.f_code is engine.validate_source.__code__ and event == "return"
                and isinstance(value, dict)):
            records = frame.f_locals["runner"].records
            admissions.append(len(records))
            original_streams.extend((r["_stdout_raw"], r["_stderr_raw"]) for r in records)
            if interrupted:
                os.kill(os.getpid(), signal.SIGTERM)
        return trace

    sys.settrace(trace)
    try:
        rc = engine.main(public_preview_argv(repos, server, plan))
    finally:
        sys.settrace(None)
    captured = capsys.readouterr()
    outcome = json.loads(captured.err if interrupted else captured.out)
    assert len(admissions) == 1 and admissions[0] > 0
    assert rc == (4 if interrupted else 0)
    assert outcome["status"] == ("interrupted" if interrupted else "planned")
    records = assert_public_preview_records(outcome)
    for record, original in zip(records, original_streams, strict=False):
        assert Path(record["stdout"]["raw_path"]).read_bytes() == original[0]
        assert Path(record["stderr"]["raw_path"]).read_bytes() == original[1]
    if interrupted:
        assert server.requests == [] and not plan.exists()
        assert len(records) == admissions[0]
    assert source_fingerprint(repos["source"]) == before


def test_preview_source_admission_credential_refusal_with_signal_is_private(
    tmp_path: Path, metadata_server: Any, capsys: Any,
) -> None:
    repos = make_repositories(tmp_path)
    marker = "SYNTHETIC_SOURCE_ORIGIN_CREDENTIAL"
    git(repos["source"], "remote", "set-url", "origin", f"https://{marker}@example.invalid/repo")
    before = source_fingerprint(repos["source"])
    server = metadata_server(dict(LIVE_ROUTES))
    engine = load_engine()
    plan = tmp_path / "plan.json"
    injected: list[str] = []

    def trace(frame: Any, event: str, value: Any) -> Any:
        if (not injected and frame.f_code is engine.validate_source.__code__
                and event == "exception" and isinstance(value[1], engine.MaintenanceError)
                and "credential-bearing" in str(value[1])):
            injected.append("unsafe-origin")
            os.kill(os.getpid(), signal.SIGTERM)
        return trace

    sys.settrace(trace)
    try:
        rc = engine.main(public_preview_argv(repos, server, plan))
    finally:
        sys.settrace(None)
    captured = capsys.readouterr()
    assert injected == ["unsafe-origin"]
    assert rc == 2 and json.loads(captured.err)["status"] == "refused"
    assert marker not in captured.out + captured.err
    assert server.requests == [] and not plan.exists()
    assert not list(tmp_path.glob(".plan.json.preview-evidence-*"))
    assert source_fingerprint(repos["source"]) == before


@pytest.mark.parametrize("http_failure", [False, True])
def test_preview_existing_plan_is_preserved_and_never_reported_absent(
    tmp_path: Path, metadata_server: Any, capsys: Any, http_failure: bool,
) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    prepared = preview_override(repos, plan)
    assert prepared.returncode == 0, prepared.stderr
    original = plan.read_bytes()
    before = source_fingerprint(repos["source"])
    server = metadata_server({"/releases": lambda handler, _server: send_json(
        handler, {"failure": "synthetic"}, status=503,
    ), "*": missing})
    engine = load_engine()
    argv = public_preview_argv(repos, server, plan)
    if not http_failure:
        index = argv.index("--github-releases-url")
        del argv[index:index + 2]
        argv += ["--override-sha", repos["target"], "--override-reason", "existing-plan control"]
    rc = engine.main(argv)
    captured = capsys.readouterr()
    outcome = json.loads(captured.err)
    assert rc == 2 and not captured.out
    assert outcome["plan_visibility"] == "unknown"
    assert outcome["plan_preexisting"] is True
    assert outcome["plan_published_by_attempt"] is False
    assert plan.read_bytes() == original
    assert_public_preview_records(outcome)
    if http_failure:
        assert outcome["error"] == "HTTP request failed with status 503"
        assert server.requests == ["/releases?page=1"]
    else:
        assert server.requests == []
    assert source_fingerprint(repos["source"]) == before


@pytest.mark.parametrize("primary_timeout", [False, True])
@pytest.mark.parametrize("late_return", [False, True])
def test_preview_failure_delivery_rechecks_same_deadline_after_write(
    tmp_path: Path, metadata_server: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: Any, primary_timeout: bool, late_return: bool,
) -> None:
    repos = make_repositories(tmp_path)
    before = source_fingerprint(repos["source"])
    routes = dict(LIVE_ROUTES) if primary_timeout else {
        "/releases": lambda handler, _server: send_json(handler, {}, status=503), "*": missing,
    }
    server = metadata_server(routes)
    engine = load_engine()
    clock = [100.0]
    engine.time = types.SimpleNamespace(
        monotonic=lambda: clock[0], time_ns=time.time_ns, sleep=time.sleep,
    )
    if primary_timeout:
        real_derive = engine.derive_delta

        def expire_primary(runner: Any, *args: Any) -> Any:
            result = real_derive(runner, *args)
            runner.execution_deadline = 99.0
            return result

        monkeypatch.setattr(engine, "derive_delta", expire_primary)
    writes: list[str] = []

    class ReturningSink(io.StringIO):
        def write(self, value: str) -> int:
            count = super().write(value)
            if value.startswith("{"):
                writes.append(value)
                if late_return and len(writes) == 1:
                    clock[0] = 111.0  # labelled transport late-return seam; deadline110
            return count

    sink = ReturningSink()
    plan = tmp_path / "plan.json"
    with contextlib.redirect_stderr(sink):
        rc = engine.main(public_preview_argv(repos, server, plan))
    assert not capsys.readouterr().out
    outcomes = [json.loads(line) for line in sink.getvalue().splitlines()]
    expected_status = "timeout" if primary_timeout else "refused"
    first = outcomes[0]
    assert first["status"] == expected_status
    assert first["shutdown_deadline_monotonic"] == 110.0
    assert_public_preview_records(first)
    if late_return:
        assert rc == 2 and len(outcomes) == 2
        final = outcomes[-1]
        assert final["status"] == "evidence_error"
        assert final["primary_status"] == expected_status
        assert final["primary_error"] == first["error"]
        assert final["terminal_delivery"] == "late_return"
        assert final["shutdown_deadline_monotonic"] == 110.0
        assert final["command_records_sha256"] == first["command_records_sha256"]
    else:
        assert rc == (4 if primary_timeout else 2) and len(outcomes) == 1
    assert not plan.exists() and source_fingerprint(repos["source"]) == before



@pytest.mark.parametrize("stage", [
    "failure_first", "failure_second", "failure_flush", "initial_refusal",
    "success_stdout", "success_expired", "success_late_diagnostic",
])
@pytest.mark.parametrize("broken", [False, True])
def test_public_main_classifies_terminal_transport_errors(
    tmp_path: Path, metadata_server: Any, stage: str, broken: bool,
) -> None:
    repos = make_repositories(tmp_path)
    before = source_fingerprint(repos["source"])
    server = metadata_server({"/releases": lambda handler, _server: send_json(
        handler, {"failure": "synthetic"}, status=503,
    ), "*": missing})
    plan = tmp_path / "plan.json"
    argv = public_preview_argv(repos, server, plan)
    if stage.startswith("success"):
        index = argv.index("--github-releases-url")
        del argv[index:index + 2]
        argv += ["--override-sha", repos["target"], "--override-reason", "transport control"]
    elif stage == "initial_refusal":
        argv[argv.index("--pypi-base-url") + 1] = "http://127.0.0.1:0"
    facts_path = tmp_path / "transport-facts.json"
    # Public main runs uncaught in a real child. Only clock/stream boundaries are
    # controlled; EPIPE itself comes from an actual OS pipe with its reader closed.
    child = r"""
import contextlib, errno, hashlib, json, os, signal, sys, time, types
from pathlib import Path
from tools import fork_maintenance as engine
argv, stage, broken, facts_name = json.loads(sys.argv[1])
clock = [100.0]
engine.time = types.SimpleNamespace(
    monotonic=lambda: clock[0], time_ns=time.time_ns, sleep=time.sleep,
)
facts = {"json_writes": {"stdout": 0, "stderr": 0}, "epipe_count": 0,
         "deadlines": [], "original_streams": [], "persist_calls": 0}
handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
real_begin = engine.GitRunner.begin_shutdown
def begin(runner):
    deadline = real_begin(runner)
    facts["deadlines"].append(deadline)
    return deadline
engine.GitRunner.begin_shutdown = begin
real_persist = engine.persist_command_evidence
def persist(records, directory, deadline):
    facts["persist_calls"] += 1
    facts["original_streams"].extend(
        [{"bytes": len(r[k]), "sha256": hashlib.sha256(r[k]).hexdigest()}
         for k in ("_stdout_raw", "_stderr_raw")] for r in records
    )
    return real_persist(records, directory, deadline)
engine.persist_command_evidence = persist
def epipe():
    rd, wr = os.pipe()
    os.close(rd)
    try:
        os.write(wr, b"transport probe")
    except OSError as exc:
        assert exc.errno == errno.EPIPE
        facts["epipe_count"] += 1
        raise
    finally:
        os.close(wr)
class Sink:
    def __init__(self, original, channel):
        self.original, self.channel = original, channel
    def write(self, value):
        if value.startswith("{"):
            facts["json_writes"][self.channel] += 1
            count = facts["json_writes"][self.channel]
            fails = (
                self.channel == "stdout" and stage == "success_stdout"
                or self.channel == "stderr" and (
                    stage in {"failure_first", "initial_refusal", "success_expired",
                              "success_late_diagnostic"}
                    or stage == "failure_second" and count == 2
                )
            )
            if broken and fails:
                epipe()
            if stage == "failure_second" and count == 1 and self.channel == "stderr":
                clock[0] = 111.0
            if stage == "success_late_diagnostic" and self.channel == "stdout":
                clock[0] = 111.0
        return self.original.write(value)
    def flush(self):
        if broken and stage == "failure_flush" and self.channel == "stderr":
            epipe()
        return self.original.flush()
def trace(frame, event, value):
    if (stage == "success_expired" and frame.f_code is engine.preview.__code__
            and event == "return" and isinstance(value, engine.TerminalResponse)):
        clock[0] = value.deadline + 1
    return trace
sys.settrace(trace)
try:
    with contextlib.redirect_stdout(Sink(sys.stdout, "stdout")), \
            contextlib.redirect_stderr(Sink(sys.stderr, "stderr")):
        rc = engine.main(argv)
    facts["main_returned_rc"] = rc
finally:
    sys.settrace(None)
    facts["handlers_restored"] = all(signal.getsignal(s) == h for s, h in handlers.items())
    Path(facts_name).write_text(json.dumps(facts))
sys.exit(rc)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", child, json.dumps([argv, stage, broken, str(facts_path)])],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    facts = json.loads(facts_path.read_text())
    expected_rc = 2 if broken or not stage.startswith("success") else (
        0 if stage == "success_stdout" else 4
    )
    assert result.returncode == expected_rc, result.stderr
    assert facts["main_returned_rc"] == expected_rc
    assert facts["handlers_restored"] and facts["epipe_count"] == int(broken)
    assert "Traceback" not in result.stdout + result.stderr
    assert facts["json_writes"]["stderr"] == (2 if stage == "failure_second" else (
        0 if stage == "success_stdout" else 1
    ))
    stdout = [json.loads(line) for line in result.stdout.splitlines()]
    stderr = [json.loads(line) for line in result.stderr.splitlines()]
    if stage == "failure_second":
        assert len(stderr) == (1 if broken else 2)
        assert stderr[0]["error"] == "HTTP request failed with status 503"
        if not broken:
            assert stderr[-1]["terminal_delivery"] == "late_return"
            assert stderr[-1]["primary_error"] == stderr[0]["error"]
    if stage == "success_stdout":
        assert len(stdout) == int(not broken) and not stderr
    assert plan.exists() == stage.startswith("success")
    metadata = list(tmp_path.glob(".plan.json.preview-evidence-*/command-records.json"))
    if stage == "initial_refusal":
        assert not metadata and facts["persist_calls"] == 0 and server.requests == []
    else:
        assert facts["persist_calls"] == 1 and len(metadata) == 1
        records = assert_public_preview_records({
            "command_records_path": str(metadata[0]),
            "command_records_sha256": hashlib.sha256(metadata[0].read_bytes()).hexdigest(),
        })
        for record, pair in zip(records, facts["original_streams"], strict=True):
            for stream, original in zip(("stdout", "stderr"), pair, strict=True):
                assert record[stream]["sha256"] == original["sha256"]
                assert record[stream]["bytes"] == original["bytes"]
        assert len(set(facts["deadlines"])) == 1 and facts["deadlines"][0] == 110.0
        assert server.requests == (["/releases?page=1"] if stage.startswith("failure") else [])
    assert source_fingerprint(repos["source"]) == before



@pytest.mark.parametrize("channel", ["stdout", "stderr"])
@pytest.mark.parametrize("broken", [False, True])
def test_native_cli_transport_error_keeps_classified_process_exit(
    tmp_path: Path, channel: str, broken: bool,
) -> None:
    repos = make_repositories(tmp_path)
    before = source_fingerprint(repos["source"])
    plan = tmp_path / "plan.json"
    argv = [
        sys.executable, "-B", "-m", "tools.fork_maintenance", "preview",
        "--source-repo", str(repos["source"]), "--candidate", repos["candidate"],
        "--upstream-repository", "Graphify-Labs/graphify",
        "--upstream-url", str(repos["upstream"]), "--override-sha", repos["target"],
        "--override-reason", "native pipe control", "--output-plan", str(plan),
    ]
    if channel == "stderr":
        argv += ["--attempt-timeout", "0"]
    write_fd: int | None = None
    if broken:
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
    try:
        process = subprocess.Popen(
            argv, cwd=ROOT,
            stdout=write_fd if broken and channel == "stdout" else subprocess.PIPE,
            stderr=write_fd if broken and channel == "stderr" else subprocess.PIPE,
        )
    finally:
        if write_fd is not None:
            os.close(write_fd)
    stdout, stderr = process.communicate(timeout=30)
    expected = 2 if broken or channel == "stderr" else 0
    assert process.returncode == expected, stderr
    assert b"Traceback" not in (stdout or b"") + (stderr or b"")
    assert b"Exception ignored" not in (stdout or b"") + (stderr or b"")
    assert plan.exists() == (channel == "stdout")
    if channel == "stdout":
        metadata, = tmp_path.glob(".plan.json.preview-evidence-*/command-records.json")
        assert_public_preview_records({
            "command_records_path": str(metadata),
            "command_records_sha256": hashlib.sha256(metadata.read_bytes()).hexdigest(),
        })
    else:
        assert not list(tmp_path.glob(".plan.json.preview-evidence-*"))
    assert source_fingerprint(repos["source"]) == before



@pytest.mark.parametrize("argv, channel, healthy_rc", [
    (["--help"], "stdout", 0), (["preview"], "stderr", 2),
])
@pytest.mark.parametrize("broken", [False, True])
def test_native_argument_output_classifies_closed_transport(
    argv: list[str], channel: str, healthy_rc: int, broken: bool,
) -> None:
    write_fd: int | None = None
    if broken:
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
    try:
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "tools.fork_maintenance", *argv], cwd=ROOT,
            stdout=write_fd if broken and channel == "stdout" else subprocess.PIPE,
            stderr=write_fd if broken and channel == "stderr" else subprocess.PIPE,
        )
    finally:
        if write_fd is not None:
            os.close(write_fd)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == (2 if broken else healthy_rc), stderr
    assert b"Traceback" not in (stdout or b"") + (stderr or b"")
    assert b"Exception ignored" not in (stdout or b"") + (stderr or b"")


@pytest.mark.parametrize("argv, channel, healthy_rc", [
    (["--help"], "stdout", 0), (["preview"], "stderr", 2),
])
@pytest.mark.parametrize("closed", [False, True])
def test_native_argument_output_classifies_closed_python_stream(
    argv: list[str], channel: str, healthy_rc: int, closed: bool,
) -> None:
    child = """
import runpy, sys
channel, argv, closed = __import__('json').loads(sys.argv[1])
sys.argv = ['tools.fork_maintenance', *argv]
if closed:
    getattr(sys, channel).close()
runpy.run_module('tools.fork_maintenance', run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", child, json.dumps([channel, argv, closed])],
        cwd=ROOT, capture_output=True, timeout=10,
    )
    assert result.returncode == (2 if closed else healthy_rc)
    assert b"Traceback" not in result.stdout + result.stderr
    assert b"lost sys.stderr" not in result.stdout + result.stderr


@pytest.mark.parametrize("argv, channel", [
    (["--help"], "stdout"), (["preview"], "stderr"),
])
def test_imported_argument_output_leaves_closed_caller_stream_unchanged(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], channel: str,
) -> None:
    engine = load_engine()
    stream = io.StringIO()
    stream.close()
    monkeypatch.setattr(sys, channel, stream)
    handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    assert engine.main(argv) == 2
    assert getattr(sys, channel) is stream and stream.closed
    assert all(signal.getsignal(s) is h for s, h in handlers.items())


def assert_public_preview_records(outcome: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(outcome["command_records_path"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == outcome["command_records_sha256"]
    records = json.loads(path.read_text())["command_records"]
    assert records
    for record in records:
        assert record["direct_rc"] is not None
        assert record["stdout_eof"] and record["stderr_eof"]
        assert record["process_group_settled"] and record["raw_complete"]
        with pytest.raises(ProcessLookupError):
            os.killpg(record["process_group"], 0)
        for stream in ("stdout", "stderr"):
            raw = Path(record[stream]["raw_path"]).read_bytes()
            assert len(raw) == record[stream]["bytes"]
            assert hashlib.sha256(raw).hexdigest() == record[stream]["sha256"]
    return records


@pytest.mark.parametrize("bad_base", [
    "http://127.0.0.1:0", "https://127.0.0.1:0",
    "{base}?api:key=SYNTHETIC_REJECTED_ALIAS", "{base}#fragment",
])
def test_public_preview_rejects_unsafe_pypi_base_before_github_request(
    tmp_path: Path, metadata_server: Any, bad_base: str
) -> None:
    repos = make_repositories(tmp_path)
    before = source_fingerprint(repos["source"])
    server = metadata_server(dict(LIVE_ROUTES))
    plan = tmp_path / "plan.json"
    argv = public_preview_argv(repos, server, plan)
    argv[argv.index("--pypi-base-url") + 1] = bad_base.format(base=server.url)
    result = run_cli(*argv)
    assert result.returncode == 2 and not result.stdout
    assert json.loads(result.stderr)["status"] == "refused"
    assert "SYNTHETIC_REJECTED_ALIAS" not in result.stdout + result.stderr
    assert server.requests == []
    assert not plan.exists() and source_fingerprint(repos["source"]) == before
    assert not list(tmp_path.glob(".plan.json.preview-evidence-*"))


@pytest.mark.parametrize("when", ["before_protection", "inside_protection"])
def test_public_preview_http_failure_signal_keeps_first_cause_and_raw_evidence(
    tmp_path: Path, metadata_server: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: Any, when: str,
) -> None:
    repos = make_repositories(tmp_path)
    before = source_fingerprint(repos["source"])
    server = metadata_server({"/releases": lambda handler, _server: send_json(
        handler, {"failure": "synthetic"}, status=503,
    ), "*": missing})
    engine = load_engine()
    plan = tmp_path / "plan.json"
    injected: list[str] = []
    if when == "inside_protection":
        real_persist = engine.persist_command_evidence

        def signal_during_persist(records: Any, directory: Path, deadline: float) -> None:
            injected.append("inside")
            os.kill(os.getpid(), signal.SIGTERM)
            real_persist(records, directory, deadline)

        monkeypatch.setattr(engine, "persist_command_evidence", signal_during_persist)
    else:
        marker = next(
            index for index, line in enumerate(inspect.getsource(engine.preview).splitlines(),
                                             engine.preview.__code__.co_firstlineno)
            if "if primary is not None and (" in line
        )

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            if (not injected and frame.f_code is engine.preview.__code__
                    and event == "line" and frame.f_lineno == marker):
                injected.append("before")
                assert engine.SIGNAL_CONTROLLER.deferred == 0
                os.kill(os.getpid(), signal.SIGTERM)
            return trace

        sys.settrace(trace)
    try:
        rc = engine.main(public_preview_argv(repos, server, plan))
    finally:
        sys.settrace(None)
    captured = capsys.readouterr()
    outcome = json.loads(captured.err)
    assert injected == (["before"] if when == "before_protection" else ["inside"])
    assert rc == 2 and not captured.out and not plan.exists()
    assert outcome["status"] == "refused"
    assert outcome["error"] == "HTTP request failed with status 503"
    assert len([r for r in assert_public_preview_records(outcome)
                if r.get("origin") == "http_worker_envelope"]) == 1
    assert source_fingerprint(repos["source"]) == before


def test_public_preview_unsafe_redirect_signal_keeps_admission_refusal_private(
    tmp_path: Path, metadata_server: Any, capsys: Any
) -> None:
    repos = make_repositories(tmp_path)
    before = source_fingerprint(repos["source"])
    secret = "SYNTHETIC_REJECTED_ALIAS"
    server = metadata_server({
        "/releases": redirect_to(f"/rejected?apiKey={secret}"),
        "*": missing,
    })
    engine = load_engine()
    marker = next(
        index for index, line in enumerate(inspect.getsource(engine.preview).splitlines(),
                                         engine.preview.__code__.co_firstlineno)
        if line.strip() == "primary = as_primary(failure)"
    )
    injected: list[str] = []

    def trace(frame: Any, event: str, _arg: Any) -> Any:
        if (not injected and frame.f_code is engine.preview.__code__
                and event == "line" and frame.f_lineno == marker):
            injected.append("handler_entry")
            os.kill(os.getpid(), signal.SIGTERM)
        return trace

    plan = tmp_path / "plan.json"
    sys.settrace(trace)
    try:
        rc = engine.main(public_preview_argv(repos, server, plan))
    finally:
        sys.settrace(None)
    captured = capsys.readouterr()
    outcome = json.loads(captured.err)
    assert injected == ["handler_entry"]
    assert rc == 2 and not captured.out and outcome["status"] == "refused"
    assert "credential-bearing" in outcome["error"]
    assert secret not in captured.err and server.requests == ["/releases?page=1"]
    assert not plan.exists() and not list(tmp_path.glob(".plan.json.preview-evidence-*"))
    assert source_fingerprint(repos["source"]) == before


@pytest.mark.parametrize("when", ["evidence", "published", "return_line"])
def test_public_preview_success_signal_reports_evidence_and_plan_visibility(
    tmp_path: Path, metadata_server: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: Any, when: str,
) -> None:
    repos = make_repositories(tmp_path)
    before = source_fingerprint(repos["source"])
    server = metadata_server(dict(LIVE_ROUTES))
    engine = load_engine()
    plan = tmp_path / "plan.json"
    injected: list[str] = []
    if when == "evidence":
        real_persist = engine.persist_command_evidence

        def signal_during_persist(records: Any, directory: Path, deadline: float) -> None:
            injected.append("evidence")
            os.kill(os.getpid(), signal.SIGTERM)
            real_persist(records, directory, deadline)

        monkeypatch.setattr(engine, "persist_command_evidence", signal_during_persist)
    elif when == "published":
        real_write = engine.exclusive_write_bytes

        def signal_after_write(path: Path, content: bytes) -> None:
            real_write(path, content)
            if path == plan:
                injected.append("published")
                os.kill(os.getpid(), signal.SIGTERM)

        monkeypatch.setattr(engine, "exclusive_write_bytes", signal_after_write)
    else:
        marker = next(
            index for index, line in enumerate(inspect.getsource(engine.preview).splitlines(),
                                             engine.preview.__code__.co_firstlineno)
            if line.strip() == "return response"
        )

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            if (not injected and frame.f_code is engine.preview.__code__
                    and event == "line" and frame.f_lineno == marker):
                injected.append("return_line")
                os.kill(os.getpid(), signal.SIGTERM)
            return trace

        sys.settrace(trace)
    try:
        rc = engine.main(public_preview_argv(repos, server, plan))
    finally:
        sys.settrace(None)
    captured = capsys.readouterr()
    outcome = json.loads(captured.err)
    assert injected == [when]
    assert rc == 4 and not captured.out and outcome["status"] == "interrupted"
    assert outcome["plan"] == str(plan)
    if when == "evidence":
        assert not plan.exists() and outcome["plan_visibility"] == "absent"
    else:
        assert plan.exists() and outcome["plan_visibility"] == "published"
        assert hashlib.sha256(plan.read_bytes()).hexdigest() == outcome["plan_sha256"]
    assert_public_preview_records(outcome)
    assert source_fingerprint(repos["source"]) == before


@pytest.mark.parametrize("handler_signal", [False, True])
def test_public_preview_parent_http_io_failure_retains_worker_record(
    tmp_path: Path, metadata_server: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any,
    handler_signal: bool,
) -> None:
    repos = make_repositories(tmp_path)
    before = source_fingerprint(repos["source"])
    server = metadata_server({"/releases": lambda handler, _server: send_json(
        handler, {"failure": "synthetic"}, status=503,
    ), "*": missing})
    engine = load_engine()
    real_launch = engine.launch_owned

    class ParentFault:
        def __init__(self, process: subprocess.Popen[bytes]) -> None:
            self.process = process
            self.faulted = False

        def __getattr__(self, name: str) -> Any:
            return getattr(self.process, name)

        def communicate(self, *args: Any, **kwargs: Any) -> tuple[bytes, bytes]:
            result = self.process.communicate(*args, **kwargs)
            if not self.faulted:
                self.faulted = True
                raise OSError("injected parent pipe failure after worker completion")
            return result

    def launch_with_fault(argv: list[str], **options: Any) -> Any:
        process = real_launch(argv, **options)
        return ParentFault(process) if argv[0] == sys.executable else process

    monkeypatch.setattr(engine, "launch_owned", launch_with_fault)
    plan = tmp_path / "plan.json"
    injected: list[str] = []
    if handler_signal:
        marker = next(
            index for index, line in enumerate(inspect.getsource(engine.preview).splitlines(),
                                             engine.preview.__code__.co_firstlineno)
            if line.strip() == "primary = as_primary(failure)"
        )

        def trace(frame: Any, event: str, _arg: Any) -> Any:
            if (not injected and frame.f_code is engine.preview.__code__
                    and event == "line" and frame.f_lineno == marker):
                injected.append("handler_entry")
                os.kill(os.getpid(), signal.SIGTERM)
            return trace

        sys.settrace(trace)
    try:
        rc = engine.main(public_preview_argv(repos, server, plan))
    finally:
        sys.settrace(None)
    captured = capsys.readouterr()
    outcome = json.loads(captured.err)
    assert injected == (["handler_entry"] if handler_signal else [])
    assert rc == 2 and not captured.out and not plan.exists()
    assert outcome["status"] == "refused"
    assert outcome["error"] == (
        "filesystem operation failed: injected parent pipe failure after worker completion"
    )
    records = assert_public_preview_records(outcome)
    assert len([r for r in records if r.get("origin") == "http_worker_envelope"]) == 1
    assert source_fingerprint(repos["source"]) == before


@pytest.mark.parametrize("late_signal", [False, True])
def test_public_preview_expired_primary_preserves_raw_and_one_shutdown_allowance(
    tmp_path: Path, metadata_server: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: Any, late_signal: bool,
) -> None:
    repos = make_repositories(tmp_path)
    before = source_fingerprint(repos["source"])
    server = metadata_server(dict(LIVE_ROUTES))
    engine = load_engine()
    real_derive = engine.derive_delta
    real_shutdown = engine.GitRunner.begin_shutdown
    shutdowns: list[float] = []

    def expire_after_primary(runner: Any, *args: Any) -> Any:
        result = real_derive(runner, *args)
        runner.execution_deadline = time.monotonic() - 1.0
        return result

    def observe_shutdown(runner: Any) -> float:
        deadline = real_shutdown(runner)
        shutdowns.append(deadline)
        return deadline

    monkeypatch.setattr(engine, "derive_delta", expire_after_primary)
    monkeypatch.setattr(engine.GitRunner, "begin_shutdown", observe_shutdown)
    if late_signal:
        real_persist = engine.persist_command_evidence

        def signal_during_persist(records: Any, directory: Path, deadline: float) -> None:
            os.kill(os.getpid(), signal.SIGTERM)
            real_persist(records, directory, deadline)

        monkeypatch.setattr(engine, "persist_command_evidence", signal_during_persist)
    plan = tmp_path / "plan.json"
    rc = engine.main(public_preview_argv(repos, server, plan))
    captured = capsys.readouterr()
    outcome = json.loads(captured.err)
    assert rc == 4 and not captured.out and not plan.exists()
    assert outcome["status"] == "timeout" and "preview execution" in outcome["error"]
    assert shutdowns and len(set(shutdowns)) == 1
    assert outcome["shutdown_deadline_monotonic"] == shutdowns[0]
    assert_public_preview_records(outcome)
    assert source_fingerprint(repos["source"]) == before


def test_public_preview_postlink_failure_reports_visible_plan_and_raw_evidence(
    tmp_path: Path, metadata_server: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    repos = make_repositories(tmp_path)
    before = source_fingerprint(repos["source"])
    server = metadata_server(dict(LIVE_ROUTES))
    engine = load_engine()
    real_write = engine.exclusive_write_bytes
    plan = tmp_path / "plan.json"

    def fail_after_link(path: Path, content: bytes) -> None:
        real_write(path, content)
        if path == plan:
            raise OSError("injected post-link failure")

    monkeypatch.setattr(engine, "exclusive_write_bytes", fail_after_link)
    rc = engine.main(public_preview_argv(repos, server, plan))
    captured = capsys.readouterr()
    outcome = json.loads(captured.err)
    assert rc == 2 and not captured.out and outcome["status"] == "refused"
    assert "injected post-link failure" in outcome["error"]
    assert outcome["plan"] == str(plan) and outcome["plan_visibility"] == "unknown"
    assert plan.exists() and hashlib.sha256(plan.read_bytes()).hexdigest() == outcome["plan_sha256"]
    assert_public_preview_records(outcome)
    assert source_fingerprint(repos["source"]) == before


def test_public_http_failure_response_encoding_cannot_extend_shutdown(
    tmp_path: Path, metadata_server: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    repos = make_repositories(tmp_path)
    server = metadata_server({"/releases": lambda handler, _server: send_json(
        handler, {"failure": "synthetic"}, status=503,
    ), "*": missing})
    engine = load_engine()
    clock = [100.0]
    engine.time = types.SimpleNamespace(
        monotonic=lambda: clock[0], time_ns=time.time_ns, sleep=time.sleep,
    )
    encoded_failure: list[str] = []
    original_dumps = engine.json.dumps

    def expire_during_response(value: Any, *args: Any, **kwargs: Any) -> str:
        encoded = original_dumps(value, *args, **kwargs)
        if isinstance(value, dict) and "command_records_path" in value and value.get("status") == "refused":
            encoded_failure.append("prepared")
            clock[0] = 111.0
        return encoded

    monkeypatch.setattr(engine.json, "dumps", expire_during_response)
    plan = tmp_path / "plan.json"
    direct_rc = engine.main([
        "preview", "--source-repo", str(repos["source"]),
        "--candidate", repos["candidate"],
        "--upstream-repository", "Graphify-Labs/graphify",
        "--upstream-url", str(repos["upstream"]),
        "--github-releases-url", f"{server.url}/releases?page=1",
        "--pypi-base-url", server.url,
        "--network-timeout", "2", "--attempt-timeout", "300",
        "--output-plan", str(plan),
    ])
    captured = capsys.readouterr()
    assert direct_rc == 2 and not captured.out
    outcome = json.loads(captured.err)
    assert encoded_failure == ["prepared"]
    assert outcome["status"] == "evidence_error"
    assert outcome["primary_status"] == "refused"
    assert "HTTP request failed with status 503" in outcome["primary_error"]
    assert not plan.exists()


def test_late_signal_during_http_failure_finalization_preserves_first_cause_and_evidence(
    tmp_path: Path, metadata_server: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    repos = make_repositories(tmp_path)
    server = metadata_server({"/releases": lambda handler, _server: send_json(
        handler, {"failure": "synthetic"}, status=503,
    ), "*": missing})
    engine = load_engine()
    original_persist = engine.persist_command_evidence
    injected: list[str] = []

    def persist_with_signal(records: Any, directory: Path, deadline: float) -> None:
        injected.append("SIGTERM")
        engine.SIGNAL_CONTROLLER.handle(signal.SIGTERM)
        original_persist(records, directory, deadline)

    monkeypatch.setattr(engine, "persist_command_evidence", persist_with_signal)
    plan = tmp_path / "plan.json"
    direct_rc = engine.main([
        "preview", "--source-repo", str(repos["source"]),
        "--candidate", repos["candidate"],
        "--upstream-repository", "Graphify-Labs/graphify",
        "--upstream-url", str(repos["upstream"]),
        "--github-releases-url", f"{server.url}/releases?page=1",
        "--pypi-base-url", server.url,
        "--network-timeout", "2", "--attempt-timeout", "30",
        "--output-plan", str(plan),
    ])
    captured = capsys.readouterr()
    outcome = json.loads(captured.err)
    assert injected == ["SIGTERM"]
    assert direct_rc == 2 and not captured.out and not plan.exists()
    assert outcome["status"] == "refused"
    assert outcome["error"] == "HTTP request failed with status 503"
    records_path = Path(outcome["command_records_path"])
    assert hashlib.sha256(records_path.read_bytes()).hexdigest() == outcome["command_records_sha256"]
    records = json.loads(records_path.read_text())["command_records"]
    assert len([record for record in records if record.get("origin") == "http_worker_envelope"]) == 1


def test_public_replay_rejects_copied_source_identity_without_mutation(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output, evidence = tmp_path / "output", tmp_path / "evidence"
    argv = apply_args(repos, plan, output, evidence, "codex/copied-source")
    applied = run_cli(*argv)
    assert applied.returncode == 0, (applied.stdout, applied.stderr)
    original = run_cli(*argv)
    assert original.returncode == 0 and json.loads(original.stdout)["status"] == "replayed"
    copied = tmp_path / "source-copy"
    shutil.copytree(repos["source"], copied)
    before = {str(path): evidence_fingerprint(path) for path in
              (repos["source"], copied, output, evidence)}
    plan_before = plan.read_bytes()
    changed = list(argv)
    changed[changed.index("--source-repo") + 1] = str(copied)

    invalid = run_cli(*changed)

    assert invalid.returncode == 2, (invalid.stdout, invalid.stderr)
    assert "identity" in invalid.stderr.lower() or "source" in invalid.stderr.lower()
    assert plan.read_bytes() == plan_before
    assert {str(path): evidence_fingerprint(path) for path in
            (repos["source"], copied, output, evidence)} == before
    alias = list(argv)
    alias[alias.index("--source-repo") + 1] = str(repos["source"].parent / "." / repos["source"].name)
    assert run_cli(*alias).returncode == 0


@pytest.mark.parametrize("phase", ["prelink", "postlink"])
@pytest.mark.parametrize("terminal_kind", ["applied", "conflict", "ordinary_failure"])
def test_owned_apply_serialization_deadline_preserves_publication_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
    phase: str, terminal_kind: str,
) -> None:
    repos = make_repositories(tmp_path)
    if terminal_kind == "conflict":
        repos["candidate"] = commit_file(
            repos["source"], "shared.txt", "fork conflict\n", "fork conflict"
        )
        repos["target"] = commit_file(
            repos["upstream_work"], "shared.txt", "upstream conflict\n", "upstream conflict"
        )
        git(repos["upstream_work"], "push", "publish", "main")
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output, evidence = tmp_path / "output", tmp_path / "evidence"
    result_path = evidence / "result.json"
    engine = load_engine()
    if terminal_kind == "ordinary_failure":
        def fail_owned_acquisition(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise OSError("injected owned acquisition failure")

        monkeypatch.setattr(engine, "acquire_target", fail_owned_acquisition)
    clock = [100.0]
    engine.time = types.SimpleNamespace(
        monotonic=lambda: clock[0], time_ns=time.time_ns, sleep=time.sleep
    )
    if phase == "prelink":
        real_serialize = engine.json_file_bytes

        def expire_during_serialization(value: Any) -> bytes:
            content = real_serialize(value)
            clock[0] = 111.0
            return content

        monkeypatch.setattr(engine, "json_file_bytes", expire_during_serialization)
    else:
        real_write = engine.exclusive_write_bytes

        def expire_after_real_link(path: Path, content: bytes) -> None:
            real_write(path, content)
            if path == result_path:
                clock[0] = 111.0

        monkeypatch.setattr(engine, "exclusive_write_bytes", expire_after_real_link)

    direct_rc = engine.main(list(apply_args(
        repos, plan, output, evidence, f"codex/{phase}-deadline",
    )))
    captured = capsys.readouterr()
    outcome = json.loads(captured.err)
    assert direct_rc != 0, (captured.out, captured.err)
    assert outcome["status"] == "evidence_error"
    expected_status = "operational_failure" if terminal_kind == "ordinary_failure" else terminal_kind
    assert outcome["terminal_status"] == expected_status
    assert (evidence / "attempt.lock").is_file()
    assert result_path.is_file() is (phase == "postlink")
    if phase == "postlink":
        assert json.loads(result_path.read_text())["status"] == expected_status
    if terminal_kind != "ordinary_failure":
        assert output.is_dir()


def test_public_preview_serialization_expiry_never_publishes_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    engine = load_engine()
    clock = [100.0]
    engine.time = types.SimpleNamespace(
        monotonic=lambda: clock[0], time_ns=time.time_ns, sleep=time.sleep
    )
    real_serialize = engine.json_file_bytes

    def expire_during_serialization(value: Any) -> bytes:
        content = real_serialize(value)
        clock[0] = 111.0
        return content

    monkeypatch.setattr(engine, "json_file_bytes", expire_during_serialization)
    argv = [
        "preview", "--source-repo", str(repos["source"]),
        "--candidate", repos["candidate"],
        "--upstream-repository", "Graphify-Labs/graphify",
        "--upstream-url", str(repos["upstream"]),
        "--override-sha", repos["target"],
        "--override-reason", "bounded fixture target",
        "--output-plan", str(plan),
    ]

    direct_rc = engine.main(argv)
    captured = capsys.readouterr()

    assert direct_rc != 0, (captured.out, captured.err)
    assert not plan.exists()
    assert "timeout" in captured.err.lower() or "deadline" in captured.err.lower()


@pytest.mark.parametrize("seam", ["final_digest", "public_response"])
def test_preview_final_completion_is_bounded_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, seam: str
) -> None:
    repos = make_repositories(tmp_path)
    positive_plan = tmp_path / "positive.json"
    positive = preview_override(repos, positive_plan)
    assert positive.returncode == 0, positive.stderr
    assert json.loads(positive.stdout)["plan_sha256"] == hashlib.sha256(
        positive_plan.read_bytes()
    ).hexdigest()
    source_before = source_fingerprint(repos["source"])
    engine = load_engine()
    clock = [100.0]
    engine.time = types.SimpleNamespace(
        monotonic=lambda: clock[0], time_ns=time.time_ns, sleep=time.sleep
    )
    calls = []
    if seam == "final_digest":
        real_hash = engine.sha256_bytes

        def expire_on_final_digest(value: bytes) -> str:
            digest = real_hash(value)
            if value.startswith(b"{\n") and b'"plan_id"' in value:
                calls.append(seam)
                clock[0] = 111.0
            return digest

        monkeypatch.setattr(engine, "sha256_bytes", expire_on_final_digest)
    else:
        real_dumps = engine.json.dumps

        def expire_on_public_response(value: Any, *args: Any, **kwargs: Any) -> str:
            encoded = real_dumps(value, *args, **kwargs)
            if isinstance(value, engine.TerminalResponse) and value["status"] == "planned":
                calls.append(seam)
                clock[0] = 111.0
            return encoded

        monkeypatch.setattr(engine.json, "dumps", expire_on_public_response)
    plan = tmp_path / f"{seam}.json"
    argv = [
        "preview", "--source-repo", str(repos["source"]),
        "--candidate", repos["candidate"],
        "--upstream-repository", "Graphify-Labs/graphify",
        "--upstream-url", str(repos["upstream"]),
        "--override-sha", repos["target"],
        "--override-reason", "bounded fixture target",
        "--output-plan", str(plan),
    ]
    direct_rc = engine.main(argv)
    captured = capsys.readouterr()
    assert calls == [seam]
    assert direct_rc == 2 and not captured.out
    outcome = json.loads(captured.err)
    assert outcome["status"] == "evidence_error" and outcome["primary_status"] == "timeout"
    assert outcome["plan_visibility"] == "absent"
    assert not plan.exists()
    assert source_fingerprint(repos["source"]) == source_before


def test_preview_real_link_returning_after_deadline_keeps_visible_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    repos = make_repositories(tmp_path)
    engine = load_engine()
    clock = [100.0]
    engine.time = types.SimpleNamespace(
        monotonic=lambda: clock[0], time_ns=time.time_ns, sleep=time.sleep
    )
    plan = tmp_path / "late-visible.json"
    real_write = engine.exclusive_write_bytes
    links = []

    def link_then_expire(path: Path, content: bytes) -> None:
        real_write(path, content)
        if path == plan:
            links.append(path)
            clock[0] = 111.0

    monkeypatch.setattr(engine, "exclusive_write_bytes", link_then_expire)
    argv = [
        "preview", "--source-repo", str(repos["source"]),
        "--candidate", repos["candidate"],
        "--upstream-repository", "Graphify-Labs/graphify",
        "--upstream-url", str(repos["upstream"]),
        "--override-sha", repos["target"],
        "--override-reason", "bounded fixture target",
        "--output-plan", str(plan),
    ]
    direct_rc = engine.main(argv)
    captured = capsys.readouterr()
    outcome = json.loads(captured.err)
    assert links == [plan] and direct_rc == 2 and not captured.out
    assert outcome["status"] == "evidence_error" and outcome["primary_status"] == "timeout"
    assert outcome["terminal_status"] == "planned"
    assert outcome["plan"] == str(plan) and outcome["plan_visibility"] == "published"
    assert "plan publication reached its deadline" in outcome["primary_error"]
    assert json.loads(plan.read_bytes())["schema_version"] == engine.SCHEMA_VERSION


@pytest.mark.parametrize("seam", ["public_response", "scratch_cleanup"])
@pytest.mark.parametrize("terminal_kind", ["applied", "conflict", "ordinary_failure"])
def test_owned_terminal_completion_precedes_receipt_and_lock_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
    seam: str, terminal_kind: str,
) -> None:
    repos = make_repositories(tmp_path)
    if terminal_kind == "conflict":
        repos["candidate"] = commit_file(
            repos["source"], "shared.txt", "fork conflict\n", "fork conflict"
        )
        repos["target"] = commit_file(
            repos["upstream_work"], "shared.txt", "upstream conflict\n", "upstream conflict"
        )
        git(repos["upstream_work"], "push", "publish", "main")
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output, evidence = tmp_path / "output", tmp_path / "evidence"
    engine = load_engine()
    if terminal_kind == "ordinary_failure":
        def fail_acquisition(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise OSError("injected owned acquisition failure")

        monkeypatch.setattr(engine, "acquire_target", fail_acquisition)
    clock = [100.0]
    engine.time = types.SimpleNamespace(
        monotonic=lambda: clock[0], time_ns=time.time_ns, sleep=time.sleep
    )
    calls = []
    if seam == "public_response":
        real_rmtree = engine.shutil.rmtree

        def refuse_expired_cleanup(path: Any, *args: Any, **kwargs: Any) -> None:
            if Path(path).name.startswith("graphify-fork-apply-"):
                pytest.fail("expired response started fresh scratch cleanup")
            real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(engine.shutil, "rmtree", refuse_expired_cleanup)
        real_dumps = engine.json.dumps

        def expire_on_public_response(value: Any, *args: Any, **kwargs: Any) -> str:
            encoded = real_dumps(value, *args, **kwargs)
            if isinstance(value, engine.TerminalResponse):
                calls.append(seam)
                clock[0] = 111.0
            return encoded

        monkeypatch.setattr(engine.json, "dumps", expire_on_public_response)
    else:
        real_rmtree = engine.shutil.rmtree

        def expire_after_cleanup(path: Any, *args: Any, **kwargs: Any) -> None:
            real_rmtree(path, *args, **kwargs)
            if Path(path).name.startswith("graphify-fork-apply-"):
                calls.append(seam)
                clock[0] = 111.0

        monkeypatch.setattr(engine.shutil, "rmtree", expire_after_cleanup)
    direct_rc = engine.main(list(apply_args(
        repos, plan, output, evidence, f"codex/{seam}-{terminal_kind}",
    )))
    captured = capsys.readouterr()
    outcome = json.loads(captured.err)
    assert calls == [seam]
    assert direct_rc == 2 and not captured.out
    assert outcome["status"] == "evidence_error"
    assert outcome["terminal_status"] == (
        "operational_failure" if terminal_kind == "ordinary_failure" else terminal_kind
    )
    assert (evidence / "attempt.lock").is_file()
    assert not (evidence / "result.json").exists()
    assert output.is_dir() is (terminal_kind != "ordinary_failure")


def test_owned_scratch_cleanup_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output, evidence = tmp_path / "output", tmp_path / "evidence"
    engine = load_engine()
    real_rmtree = engine.shutil.rmtree
    attempts = []

    def fail_once(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name.startswith("graphify-fork-apply-"):
            attempts.append(str(path))
            raise OSError("injected scratch cleanup failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(engine.shutil, "rmtree", fail_once)
    direct_rc = engine.main(list(apply_args(
        repos, plan, output, evidence, "codex/cleanup-failure",
    )))
    captured = capsys.readouterr()
    outcome = json.loads(captured.err)
    assert direct_rc == 2 and not captured.out
    assert len(attempts) == 1
    assert outcome["status"] == "evidence_error" and outcome["terminal_status"] == "applied"
    assert "scratch cleanup failure" in outcome["error"]
    assert output.is_dir() and (evidence / "attempt.lock").is_file()
    assert not (evidence / "result.json").exists()
    real_rmtree(attempts[0])


def test_replay_response_completion_is_bounded_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output, evidence = tmp_path / "output", tmp_path / "evidence"
    argv = apply_args(repos, plan, output, evidence, "codex/read-only-completion")
    first = run_cli(*argv)
    assert first.returncode == 0, first.stderr
    source_before = source_fingerprint(repos["source"])
    output_before = source_fingerprint(output)
    evidence_before = evidence_fingerprint(evidence)
    engine = load_engine()
    clock = [100.0]
    engine.time = types.SimpleNamespace(
        monotonic=lambda: clock[0], time_ns=time.time_ns, sleep=time.sleep
    )
    real_dumps = engine.json.dumps
    calls = []

    def expire_on_replay(value: Any, *args: Any, **kwargs: Any) -> str:
        encoded = real_dumps(value, *args, **kwargs)
        if isinstance(value, engine.TerminalResponse) and value.get("status") == "replayed":
            calls.append("replay")
            clock[0] = 111.0
        return encoded

    monkeypatch.setattr(engine.json, "dumps", expire_on_replay)
    direct_rc = engine.main(list(argv))
    captured = capsys.readouterr()
    assert calls == ["replay"] and direct_rc == 4 and not captured.out, (
        direct_rc, captured.out, captured.err
    )
    assert json.loads(captured.err)["status"] == "timeout"
    assert source_fingerprint(repos["source"]) == source_before
    assert source_fingerprint(output) == output_before
    assert evidence_fingerprint(evidence) == evidence_before
    assert not (evidence / "attempt.lock").exists()


@pytest.mark.parametrize("scope", ["local", "worktree"])
def test_promisor_configuration_refuses_preview_first_apply_and_replay(
    tmp_path: Path, scope: str
) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output, evidence = tmp_path / "output", tmp_path / "evidence"
    argv = apply_args(repos, plan, output, evidence, f"codex/promisor-{scope}")
    assert run_cli(*argv).returncode == 0
    assert run_cli(*argv).returncode == 0  # same-source positive replay
    source = repos["source"]
    if scope == "worktree":
        git(source, "config", "extensions.worktreeConfig", "true")
        git(source, "config", "--worktree", "remote.origin.promisor", "true")
    else:
        git(source, "config", "remote.origin.promisor", "true")
    before = {str(path): evidence_fingerprint(path) for path in (source, output, evidence)}
    plan_bytes = plan.read_bytes()
    refused_preview = preview_override(repos, tmp_path / "refused-plan.json")
    refused_first = run_cli(*apply_args(
        repos, plan, tmp_path / "fresh-output", tmp_path / "fresh-evidence",
        f"codex/promisor-fresh-{scope}",
    ))
    refused_replay = run_cli(*argv)
    for result in (refused_preview, refused_first, refused_replay):
        assert result.returncode == 2, (result.stdout, result.stderr)
        assert "promisor" in result.stderr.lower() or "partial" in result.stderr.lower()
    assert not (tmp_path / "refused-plan.json").exists()
    assert not (tmp_path / "fresh-output").exists()
    assert not (tmp_path / "fresh-evidence").exists()
    assert plan.read_bytes() == plan_bytes
    assert {str(path): evidence_fingerprint(path) for path in (source, output, evidence)} == before


def test_output_worktree_promisor_configuration_refuses_replay(tmp_path: Path) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output, evidence = tmp_path / "output", tmp_path / "evidence"
    argv = apply_args(repos, plan, output, evidence, "codex/output-promisor")
    assert run_cli(*argv).returncode == 0
    git(repos["source"], "config", "extensions.worktreeConfig", "true")
    git(output, "config", "--worktree", "remote.origin.promisor", "true")
    before = {str(path): evidence_fingerprint(path) for path in
              (repos["source"], output, evidence)}
    result = run_cli(*argv)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "promisor" in result.stderr.lower() or "partial" in result.stderr.lower()
    assert {str(path): evidence_fingerprint(path) for path in
            (repos["source"], output, evidence)} == before


def test_filtered_clone_positive_lazy_fetch_and_preview_preflight_refusal(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    candidate = commit_file(upstream, "file.txt", "fixture contents\n", "fixture")
    git(upstream, "config", "uploadpack.allowFilter", "true")
    positive, treatment = tmp_path / "positive", tmp_path / "treatment"
    for clone in (positive, treatment):
        git(tmp_path, "clone", "--filter=tree:0", "--no-checkout",
            f"file://{upstream}", str(clone))

    def object_files(repo: Path) -> dict[str, str]:
        root = repo / ".git" / "objects"
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*") if path.is_file()}

    positive_before = object_files(positive)
    control = git(positive, "status", "--porcelain=v1", "--untracked-files=all")
    assert control.returncode == 0
    assert object_files(positive) != positive_before, "plain Git must lazily fetch objects"
    before = evidence_fingerprint(treatment)
    plan = tmp_path / "invalid-plan.json"
    refused = run_cli(
        "preview", "--source-repo", str(treatment), "--candidate", candidate,
        "--upstream-repository", "Graphify-Labs/graphify",
        "--upstream-url", f"file://{upstream}",
        "--override-sha", "deadbeef" * 5,
        "--override-reason", "invalid disposable fixture target",
        "--output-plan", str(plan),
        "--subprocess-timeout", "5", "--attempt-timeout", "15",
    )
    assert refused.returncode == 2, (refused.stdout, refused.stderr)
    assert "promisor" in refused.stderr.lower() or "partial" in refused.stderr.lower()
    assert not plan.exists()
    assert evidence_fingerprint(treatment) == before


@pytest.mark.parametrize("expired", [False, True])
def test_preview_final_delta_cpu_respects_primary_deadline(
    tmp_path: Path, capsys: Any, expired: bool
) -> None:
    repos = make_repositories(tmp_path)
    source_before = source_fingerprint(repos["source"])
    plan = tmp_path / "plan.json"
    engine = load_engine()
    clock = [100.0]
    engine.time = types.SimpleNamespace(
        monotonic=lambda: clock[0], time_ns=time.time_ns, sleep=time.sleep
    )
    lines, first_line = inspect.getsourcelines(engine.derive_delta)
    seam = {
        first_line + index for index, line in enumerate(lines)
        if line.strip() == 'pieces = diff.split("\\0")'
    }
    assert len(seam) == 1
    advances: list[int] = []

    def trace(frame: Any, event: str, _arg: Any) -> Any:
        if (expired and not advances and event == "line"
                and frame.f_code is engine.derive_delta.__code__ and frame.f_lineno in seam):
            clock[0] = 411.0  # execution deadline 400; past even a 10s allowance
            advances.append(frame.f_lineno)
        return trace

    argv = [
        "preview", "--source-repo", str(repos["source"]), "--candidate", repos["candidate"],
        "--upstream-repository", "Graphify-Labs/graphify",
        "--upstream-url", str(repos["upstream"]), "--override-sha", repos["target"],
        "--override-reason", "bounded fixture target", "--output-plan", str(plan),
        "--attempt-timeout", "300",
    ]
    sys.settrace(trace)
    try:
        direct_rc = engine.main(argv)
    finally:
        sys.settrace(None)
    captured = capsys.readouterr()
    assert source_fingerprint(repos["source"]) == source_before
    assert advances == ([next(iter(seam))] if expired else [])
    if expired:
        assert direct_rc == engine.EXIT_TIMEOUT and not plan.exists()
        assert captured.out == ""
        outcome = json.loads(captured.err)
        assert outcome["status"] == "timeout"
        assert outcome["plan_visibility"] == "absent"
        raw = Path(outcome["command_evidence"])
        assert raw.is_dir()
        records_path = Path(outcome["command_records_path"])
        records_bytes = records_path.read_bytes()
        assert hashlib.sha256(records_bytes).hexdigest() == outcome["command_records_sha256"]
        records = json.loads(records_bytes)["command_records"]
        assert len(records) == 20
        assert len(list(raw.glob("*.stdout"))) == len(list(raw.glob("*.stderr"))) == 20
        for record in records:
            assert record["direct_rc"] == 0
            assert record["process_group_observed"] is True
            assert record["process_group_absent_after_primary"] is True
            assert record["process_group_settled"] is True
            assert record["stdout_eof"] is record["stderr_eof"] is record["raw_complete"] is True
            with pytest.raises(ProcessLookupError):
                os.killpg(record["process_group"], 0)
            for stream in ("stdout", "stderr"):
                stream_path = Path(record[stream]["raw_path"])
                stream_bytes = stream_path.read_bytes()
                assert len(stream_bytes) == record[stream]["bytes"]
                assert hashlib.sha256(stream_bytes).hexdigest() == record[stream]["sha256"]
    else:
        assert direct_rc == 0 and captured.err == ""
        outcome = json.loads(captured.out)
        assert outcome["status"] == "planned"
        assert outcome["plan_sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()


@pytest.mark.parametrize("expired", [False, True])
def test_apply_rebase_completion_respects_primary_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, expired: bool
) -> None:
    repos = make_repositories(tmp_path)
    plan = tmp_path / "plan.json"
    assert preview_override(repos, plan).returncode == 0
    output, evidence = tmp_path / "output", tmp_path / "evidence"
    argv = apply_args(repos, plan, output, evidence, "codex/primary-transition")
    engine = load_engine()
    clock = [100.0]
    engine.time = types.SimpleNamespace(
        monotonic=lambda: clock[0], time_ns=time.time_ns, sleep=time.sleep
    )
    real_run = engine.GitRunner.run
    rebase_returns: list[int] = []

    def advance_after_rebase(self: Any, repo: Any, *command: str, **kwargs: Any) -> Any:
        result = real_run(self, repo, *command, **kwargs)
        if command and command[0] == "rebase":
            rebase_returns.append(result.returncode)
            if expired:
                clock[0] = 411.0
        return result

    monkeypatch.setattr(engine.GitRunner, "run", advance_after_rebase)
    direct_rc = engine.main(list(argv) + ["--attempt-timeout", "300"])
    captured = capsys.readouterr()
    assert rebase_returns == [0]
    receipt = json.loads((evidence / "result.json").read_text())
    assert not (evidence / "attempt.lock").exists()
    assert receipt["observed_partial_state"]["output_exists"] is True
    assert len(list((evidence / "raw").glob("*.stdout"))) == len(receipt["command_records"])
    assert len(list((evidence / "raw").glob("*.stderr"))) == len(receipt["command_records"])
    if expired:
        assert direct_rc == engine.EXIT_TIMEOUT and captured.out == ""
        assert json.loads(captured.err)["status"] == receipt["status"] == "timeout"
        assert "apply execution" in receipt["error"]
        before = evidence_fingerprint(evidence)
        replay = run_cli(*argv)
        assert replay.returncode == engine.EXIT_TIMEOUT
        assert json.loads(replay.stderr)["status"] == "timeout"
        assert evidence_fingerprint(evidence) == before
    else:
        assert direct_rc == 0 and captured.err == ""
        assert json.loads(captured.out)["status"] == receipt["status"] == "applied"
