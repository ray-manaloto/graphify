"""Deterministic, repository-local Graphify fork maintenance command."""

from __future__ import annotations

import argparse
from functools import partial
import hashlib
import json
import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO, TypeVar


SCHEMA_VERSION = 1
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9A-Za-z]+)*(?:[-+._][0-9A-Za-z]+)*$")
MAX_HTTP_BYTES = 8 * 1024 * 1024
MAX_STREAM_CHARS = 128 * 1024
DEFAULT_SUBPROCESS_TIMEOUT = 30.0
DEFAULT_ATTEMPT_TIMEOUT = 300.0
SHUTDOWN_ALLOWANCE_SECONDS = 10.0
COMPOSITE_GROUP_ENV = "GRAPHIFY_FORK_COMPOSITE_GROUP"


SETTLE_GRACE_SECONDS = 2.0
EXIT_REFUSED = 2
EXIT_CONFLICT = 3
EXIT_TIMEOUT = 4
OwnedT = TypeVar("OwnedT")


class CaughtSignal(BaseException):
    """A catchable termination request that must unwind owned subprocesses."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


class MaintenanceError(Exception):
    """A fail-closed error suitable for a structured CLI response."""

    def __init__(
        self, message: str, *, status: str = "refused", details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.details = details or {}
        self.terminal_deadline: float | None = None
        self.encoded: str | None = None

    def prepare_terminal(self, deadline: float) -> None:
        """Encode a failure before its owner's terminal allowance expires."""

        self.encoded = json.dumps(
            {**self.details, "status": self.status, "error": str(self)}, sort_keys=True
        )
        check_deadline(deadline, "terminal failure response encoding")
        self.terminal_deadline = deadline


class TerminalTransportError(Exception):
    """An output stream failed; do not diagnose recursively on that transport."""


def quiesce_cli_stream(stream: TextIO) -> None:
    """Keep interpreter shutdown from retrying a failed native standard stream."""

    try:
        descriptor = stream.fileno()
        if descriptor not in (1, 2):
            return
        sink = os.open(os.devnull, os.O_WRONLY)
        try:
            if sink != descriptor:
                os.dup2(sink, descriptor)
        finally:
            if sink != descriptor:
                os.close(sink)
    except (OSError, ValueError):
        # Null-sink setup is best effort; never diagnose over the failed stream.
        pass


def write_terminal(encoded: str, *, stream: TextIO, end: str = "\n") -> None:
    """Flush one terminal record or transfer control to the no-output fallback."""

    try:
        print(encoded, file=stream, flush=True, end=end)
    except (OSError, UnicodeError, ValueError) as exc:
        if __name__ == "__main__":
            quiesce_cli_stream(stream)
        raise TerminalTransportError("terminal output could not be delivered") from exc


class TerminalResponse(dict[str, Any]):
    """Carry the exact CLI bytes prepared before terminal publication."""

    def __init__(
        self, value: dict[str, Any], deadline: float, expiry_details: dict[str, Any]
    ) -> None:
        super().__init__(value)
        self.deadline = deadline
        self.expiry_details = expiry_details
        self.encoded: str | None = None

    def prepare(self) -> None:
        self.encoded = json.dumps(self, sort_keys=True)
        check_deadline(self.deadline, "terminal response encoding")


class CatchableSignalController:
    """Coalesce catchable signals across child handoff and terminal publication."""

    def __init__(self) -> None:
        self.deferred = 0
        self.pending: int | None = None
        # After the first raise the attempt is terminating; later signals only coalesce.
        self.raised = False

    def handle(self, signum: int) -> None:
        if self.deferred or self.raised:
            self.pending = self.pending or signum
            return
        self.raised = True
        raise CaughtSignal(signum)

    def hold(self) -> None:
        self.deferred += 1

    def release(self) -> None:
        self.deferred -= 1

    @contextmanager
    def protect(self) -> Any:
        self.hold()
        try:
            yield
        finally:
            self.release()

    def deliver(self) -> None:
        if self.deferred == 0 and self.pending is not None and not self.raised:
            signum, self.pending = self.pending, None
            self.raised = True
            raise CaughtSignal(signum)

    def coalesce_terminal(self) -> None:
        # Terminal publication was attempted: later signals only coalesce, so they can
        # neither re-run finalization nor contradict an already published outcome.
        self.pending = None
        self.raised = True


SIGNAL_CONTROLLER: CatchableSignalController | None = None


def signal_protection() -> Any:
    return SIGNAL_CONTROLLER.protect() if SIGNAL_CONTROLLER else nullcontext()


def acquire_owned(create: Callable[[], OwnedT]) -> OwnedT:
    """Create an owned resource with catchable signals held.

    On success the hold stays in place; the caller's first statement inside its
    settling try is release_owned(), so no signal can land between ownership and
    the handler that settles it. Handler-level deferral never alters child masks.
    """

    if SIGNAL_CONTROLLER:
        SIGNAL_CONTROLLER.hold()
    try:
        return create()
    except BaseException:
        if SIGNAL_CONTROLLER:
            SIGNAL_CONTROLLER.release()
        raise


def launch_owned(argv: list[str], **options: Any) -> subprocess.Popen[bytes]:
    return acquire_owned(lambda: subprocess.Popen[bytes](argv, **options))


def composite_worker_group() -> bool:
    """The supervised engine is the leader of its private composite group."""

    # The marker names this engine's direct parent, not an ambient boolean that
    # a standalone preview/apply could accidentally inherit from a shell.
    return (os.environ.get(COMPOSITE_GROUP_ENV) == str(os.getppid()) and
            os.getpid() == os.getpgrp())


def launch_owned_lock(lock: Path) -> int:
    return acquire_owned(lambda: os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))


def release_owned() -> None:
    """First statement of an owned child's settling try: deliver any deferred signal."""

    if SIGNAL_CONTROLLER:
        SIGNAL_CONTROLLER.release()
        SIGNAL_CONTROLLER.deliver()


def check_deadline(deadline: float, activity: str = "state observation") -> None:
    if time.monotonic() >= deadline:
        raise MaintenanceError(
            f"{activity} reached its deadline; state is unknown", status="timeout"
        )


def regular_file_sha256(path: Path, deadline: float) -> str | None:
    """Hash one regular file without following a final symlink or blocking on special files."""

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MaintenanceError("repository state is unreadable; state is unknown") from exc
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise MaintenanceError("repository state contains a special file; state is unknown")
        digest = hashlib.sha256()
        while chunk := handle.read(1024 * 1024):
            check_deadline(deadline)
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise MaintenanceError(f"{label} must be an exact lowercase 40-hex commit SHA")
    return value


def read_json_file(path: Path, label: str) -> Any:
    try:
        size = path.stat().st_size
        if size > MAX_HTTP_BYTES:
            raise MaintenanceError(f"{label} exceeds {MAX_HTTP_BYTES} bytes")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MaintenanceError(f"cannot read {label}: {exc}") from exc


def display_bytes(value: bytes) -> str:
    return value.decode("utf-8", errors="backslashreplace")


def bounded_stream(value: bytes) -> dict[str, Any]:
    text = display_bytes(value)
    return {
        "text": text[:MAX_STREAM_CHARS],
        "sha256": sha256_bytes(value),
        "bytes": len(value),
        "truncated": len(text) > MAX_STREAM_CHARS,
    }


class GitRunner:
    """Run Git with isolated configuration and process-group timeout settlement."""

    def __init__(
        self,
        *,
        subprocess_timeout: float,
        deadline: float,
        home: Path,
        committer: tuple[str, str] | None = None,
    ) -> None:
        self.subprocess_timeout = subprocess_timeout
        self.execution_deadline = deadline
        self.deadline = deadline
        self.shutdown_deadline: float | None = None
        self.mutation_closed = False
        self.records: list[dict[str, Any]] = []
        self.home = home.resolve()
        safe_path = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"
        git_executable = shutil.which("git", path=safe_path)
        if git_executable is None:
            raise MaintenanceError("Git executable was not found in the fixed system search path")
        self.git_executable = git_executable
        self.environment = {
            "PATH": safe_path,
            "HOME": str(home),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "false",
            "GIT_EDITOR": ":",
            "GIT_SEQUENCE_EDITOR": ":",
            "GIT_MERGE_AUTOEDIT": "no",
            "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
            "GIT_ALLOW_PROTOCOL": "file:https:ssh",
            # Repoless commands must not discover configuration in an ambient checkout.
            "GIT_CEILING_DIRECTORIES": str(self.home.parent),
        }
        if committer is not None:
            self.environment["GIT_COMMITTER_NAME"] = committer[0]
            self.environment["GIT_COMMITTER_EMAIL"] = committer[1]

    def run(
        self,
        repo: Path | None,
        *arguments: str,
        check: bool = True,
        mutating: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if mutating and (self.shutdown_deadline is not None or self.mutation_closed):
            raise MaintenanceError("mutation is forbidden after terminal transition")
        remaining = self.deadline - time.monotonic()
        # Name the bound actually in force: primary execution or the shutdown allowance.
        bound = "whole-attempt" if self.shutdown_deadline is None else "shutdown-allowance"
        if remaining <= 0:
            raise MaintenanceError(
                f"{bound} timeout expired before Git launch",
                status="timeout",
                details={"process_group_settled": True, "bound": bound},
            )
        timeout = min(self.subprocess_timeout, remaining)
        argv = [
            self.git_executable,
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "rebase.updateRefs=false",
            "-c",
            "rebase.autoStash=false",
            "-c",
            "rerere.enabled=false",
            "-c",
            "rerere.autoUpdate=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "tag.gpgSign=false",
            "-c",
            "fetch.writeCommitGraph=false",
            "-c",
            "gc.auto=0",
        ]
        if repo is not None:
            argv.extend(("-C", str(repo)))
        argv.extend(arguments)
        started = time.monotonic()
        shared_group = composite_worker_group()
        try:
            process = launch_owned(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.environment,
                cwd=self.home,
                start_new_session=not shared_group,
            )
        except OSError as exc:
            raise MaintenanceError(f"could not launch Git: {exc}") from exc
        timed_out = False
        settled = False
        group_observation: bool | None = None
        group_observation_performed = False
        group_survived_primary = False
        drained = False
        interrupted: CaughtSignal | None = None
        unexpected: BaseException | None = None
        stdout = stderr = b""
        primary_output: tuple[bytes, bytes] | None = None
        # One spanning ownership boundary from handoff through the single append: the
        # primary runs once, settlement is bounded by the shared allowance, and a first
        # signal landing in any ordinary handler or transition still reaches the record.
        phase = "primary"
        record: dict[str, Any] | None = None
        # A recorded primary outcome predates a late signal; termination after an early
        # signal does not turn its resulting rc or incomplete bytes into an earlier failure.
        primary_captured = False
        classified = False
        terminal_failure: BaseException | None = None
        result: subprocess.CompletedProcess[str] | None = None
        while True:
            try:
                while record is None:
                    try:
                        if phase == "primary":
                            # The acquisition hold defers signals until release_owned(), so the
                            # primary starts at most once and cannot be skipped by a signal.
                            phase = "primary-running"
                            try:
                                try:
                                    release_owned()
                                    primary_output = process.communicate(timeout=timeout)
                                    stdout, stderr = primary_output
                                    drained = True
                                    primary_captured = True
                                    group_observation, group_observation_performed = (
                                        (process.poll() is not None if shared_group else
                                         process_group_absent(process.pid)), True
                                    )
                                    settled = group_observation is True
                                    group_survived_primary = not settled
                                except subprocess.TimeoutExpired as timeout_exc:
                                    timed_out = True
                                    stdout, stderr = timeout_exc.output or b"", timeout_exc.stderr or b""
                            except CaughtSignal as exc:
                                # The returned tuple proves EOF even when the signal lands
                                # before the next statement acknowledges it.
                                if primary_output is not None:
                                    stdout, stderr = primary_output
                                    drained = primary_captured = True
                                if group_observation_performed:
                                    settled = group_observation is True
                                    group_survived_primary = not settled
                                context = exc.__context__
                                if isinstance(context, subprocess.TimeoutExpired):
                                    timed_out = True
                                    stdout, stderr = context.output or b"", context.stderr or b""
                                interrupted = exc
                            except BaseException as exc:
                                unexpected = exc
                            phase = "settle"
                        while (not drained or not settled) and phase == "settle":
                            try:
                                # First terminal transition: the one shared shutdown allowance starts here.
                                deadline = self.begin_shutdown()
                                if drained:
                                    settled = (settle_direct_worker(process, deadline) if shared_group
                                               else self._settle(process, deadline))
                                else:
                                    stdout, stderr, drained, settled = terminate_and_drain(
                                        process, deadline, stdout, stderr,
                                        shared_group=shared_group,
                                    )
                                break
                            except CaughtSignal as exc:
                                # At most one CaughtSignal is ever raised, so this settles once more.
                                interrupted = interrupted or exc
                            except BaseException as exc:
                                unexpected = unexpected or exc
                                settled = False
                                break
                        phase = "record"
                        with signal_protection():
                            record = {
                                "argv": argv,
                                "pid": process.pid,
                                "process_group": os.getpgrp() if shared_group else process.pid,
                                "group_ownership": "outer_composite" if shared_group else "worker",
                                "cwd": str(self.home),
                                "direct_rc": process.returncode,
                                "duration_ms": round((time.monotonic() - started) * 1000),
                                "mutating": mutating,
                                "process_group_settled": None if shared_group else settled,
                                "worker_settled": settled,
                                "process_group_absent_after_primary": (
                                    None if shared_group else group_observation),
                                "process_group_observed": (
                                    False if shared_group else group_observation_performed),
                                "worker_exited_after_primary": (
                                    group_observation if shared_group else None),
                                "stderr": bounded_stream(stderr),
                                "stdout": bounded_stream(stdout),
                                "timed_out": timed_out,
                                "interrupted": interrupted is not None,
                                "stdout_eof": drained,
                                "stderr_eof": drained,
                                "raw_complete": drained and settled and process.returncode is not None,
                                "raw_complete_scope": "worker" if shared_group else "group",
                                "_stdout_raw": stdout,
                                "_stderr_raw": stderr,
                            }
                            self.records.append(record)
                    except CaughtSignal as exc:
                        # The first signal landed on an ordinary handler or transition outside the
                        # local catches: keep only its direct ordinary context and finish the record.
                        interrupted = interrupted or exc
                        context = exc.__context__
                        if phase in {"primary-running", "settle"} and isinstance(context, Exception):
                            unexpected = unexpected or context
                            if phase == "settle":
                                # The settlement handler was interrupted: settle no further.
                                settled = False
                                phase = "record"
                        if phase == "primary-running":
                            phase = "settle"
                if not classified:
                    # Freeze captured evidence once before selecting any late interruption.
                    terminal_failure: BaseException | None = unexpected
                    with signal_protection():
                        direct_rc = process.returncode
                        if terminal_failure is None and timed_out:
                            limit = bound if timeout < self.subprocess_timeout else "subprocess"
                            terminal_failure = MaintenanceError(
                                f"Git command timed out after {timeout:.3f}s ({limit} bound)",
                                status="timeout",
                                details={
                                    "process_group_settled": settled,
                                    "timed_out_argv": argv,
                                    "bound": limit,
                                },
                            )
                        elif terminal_failure is None and group_survived_primary:
                            terminal_failure = MaintenanceError(
                                "Git process group remained present or could not be observed after leader completion",
                                details={
                                    "process_group_settled": settled,
                                    "group_observation": group_observation,
                                },
                            )
                        elif terminal_failure is None and direct_rc is None and interrupted is None:
                            terminal_failure = MaintenanceError(
                                "Git process did not reach a terminal return code"
                            )
                        elif terminal_failure is None and direct_rc is not None:
                            stdout_text = display_bytes(stdout)
                            stderr_text = display_bytes(stderr)
                            assert direct_rc is not None
                            result = subprocess.CompletedProcess(
                                argv, direct_rc, stdout_text, stderr_text
                            )
                            if check and result.returncode != 0 and primary_captured:
                                detail = stderr_text.strip() or stdout_text.strip() or "no diagnostic"
                                terminal_failure = MaintenanceError(
                                    f"Git command failed with direct rc {result.returncode}: {detail[:2000]}"
                                )
                        classified = True
                if SIGNAL_CONTROLLER:
                    SIGNAL_CONTROLLER.deliver()
                assert record is not None
                record["interrupted"] = interrupted is not None
                if terminal_failure is not None:
                    raise terminal_failure
                if interrupted is not None:
                    raise MaintenanceError(
                        f"interrupted by {signal.Signals(interrupted.signum).name}",
                        status="interrupted",
                        details={
                            "signal": signal.Signals(interrupted.signum).name,
                            "process_group_settled": settled,
                            "interrupted_argv": argv,
                        },
                    )
                assert result is not None
                return result
            except CaughtSignal as exc:
                interrupted = interrupted or exc
                if record is None:
                    if phase == "primary-running":
                        if primary_output is not None:
                            stdout, stderr = primary_output
                            drained = primary_captured = True
                        if group_observation_performed:
                            settled = group_observation is True
                            group_survived_primary = not settled
                    context = exc.__context__
                    if phase == "primary-running" and isinstance(context, subprocess.TimeoutExpired):
                        timed_out = True
                        stdout, stderr = context.output or b"", context.stderr or b""
                    elif phase in {"primary-running", "settle"} and isinstance(context, Exception):
                        unexpected = unexpected or context
                        if phase == "settle":
                            settled = False
                            phase = "record"
                    if phase == "primary-running":
                        phase = "settle"

    def begin_shutdown(self) -> float:
        """Start the single shutdown allowance once; later transitions never extend it."""
        if self.shutdown_deadline is None:
            self.shutdown_deadline = time.monotonic() + SHUTDOWN_ALLOWANCE_SECONDS
            self.deadline = self.shutdown_deadline
        return self.shutdown_deadline

    def end_mutation(self) -> None:
        """Terminal outcome decided: observation and publication only, never mutation."""
        self.mutation_closed = True

    @staticmethod
    def _settle(process: subprocess.Popen[Any], deadline: float) -> bool:
        """Terminate the whole process group, then confirm no member remains by deadline."""

        def grace(limit: float) -> float:
            return max(0.0, min(limit, deadline - time.monotonic()))

        group = process.pid

        def observe_absence() -> bool:
            # EPERM is uncertainty, not survival or absence. A terminating orphan
            # may be reaped shortly afterwards; only an observed ESRCH proves it.
            observation_deadline = time.monotonic() + grace(SETTLE_GRACE_SECONDS)
            while True:
                if process_group_absent(group) is True:
                    return True
                if time.monotonic() >= observation_deadline:
                    return False
                time.sleep(0.01)

        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                break
            except PermissionError:
                # A denied signal proves neither result; observe within the same bound.
                return observe_absence()
            try:
                process.wait(timeout=grace(SETTLE_GRACE_SECONDS / 2))
            except subprocess.TimeoutExpired:
                continue
        try:
            process.wait(timeout=grace(SETTLE_GRACE_SECONDS))
        except subprocess.TimeoutExpired:
            return False
        # Orphaned members are reaped by init asynchronously; poll the group briefly.
        settle_deadline = time.monotonic() + grace(SETTLE_GRACE_SECONDS)
        while time.monotonic() < settle_deadline:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                return True
            except PermissionError:
                return observe_absence()
            time.sleep(0.01)
        return False


def settle_direct_worker(process: subprocess.Popen[Any], deadline: float) -> bool:
    """Settle one composite worker; the outer supervisor owns its shared group."""

    for signum in (signal.SIGTERM, signal.SIGKILL):
        if process.poll() is not None:
            return True
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=max(0.0, min(SETTLE_GRACE_SECONDS / 2,
                                               deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            continue
        return True
    return process.poll() is not None


def terminate_and_drain(
    process: subprocess.Popen[bytes], deadline: float, stdout: bytes, stderr: bytes,
    *, shared_group: bool = False,
) -> tuple[bytes, bytes, bool, bool]:
    """Settle an owned group and drain its pipes within the shared shutdown deadline.

    Returns (stdout, stderr, drained, settled). Drained means both pipes reached
    actual EOF; otherwise every byte captured so far is kept and never replaced.
    """

    with signal_protection():
        settled = (settle_direct_worker(process, deadline) if shared_group
                   else GitRunner._settle(process, deadline))
        drain = max(0.0, min(SETTLE_GRACE_SECONDS, deadline - time.monotonic()))
        try:
            stdout, stderr = process.communicate(timeout=drain)
        except subprocess.TimeoutExpired as drain_exc:
            # A descendant escaped the process group and still holds a pipe. EOF stays
            # unproven, but the observed group settlement is reported as observed.
            return drain_exc.output or stdout, drain_exc.stderr or stderr, False, settled
        return stdout, stderr, True, settled


def process_group_absent(group: int) -> bool | None:
    """Observe an owned process group without treating permission uncertainty as absence."""

    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return None
    return False


UNSAFE_LOCAL_CONFIG = (
    (re.compile(r"^url\..+\.(insteadOf|pushInsteadOf)$", re.IGNORECASE), "url.*.rewrite"),
    (
        re.compile(r"^filter\..+\.(clean|smudge|process)$", re.IGNORECASE),
        "filter.*.executable",
    ),
    (re.compile(r"^merge\..+\.driver$", re.IGNORECASE), "merge.*.driver"),
    (re.compile(r"^credential(?:\..+)?\.helper$", re.IGNORECASE), "credential.*.helper"),
    (re.compile(r"^include\.path$", re.IGNORECASE), "include.path"),
    (re.compile(r"^includeIf\..+\.path$", re.IGNORECASE), "includeIf.*.path"),
    (re.compile(r"^core\.alternateRefsCommand$", re.IGNORECASE), "core.alternateRefsCommand"),
    (re.compile(r"^extensions\.partialClone$", re.IGNORECASE), "partial-clone/promisor"),
    (re.compile(r"^remote\..+\.promisor$", re.IGNORECASE), "partial-clone/promisor"),
    (re.compile(r"^remote\..+\.partialCloneFilter$", re.IGNORECASE), "partial-clone/promisor"),
)


def admit_local_config(runner: GitRunner, source: Path) -> None:
    """Reject executable local configuration while never reading or reporting values."""

    if not source.is_dir():
        raise MaintenanceError("source repository is not a directory")
    keys: list[str] = []
    scopes = ["--local"]
    for scope in scopes:
        result = runner.run(
            source,
            "config",
            scope,
            "--no-includes",
            "--name-only",
            "--get-regexp",
            ".*",
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise MaintenanceError(
                "could not inspect source local/worktree configuration keys "
                f"(direct rc {result.returncode})"
            )
        keys.extend(line for line in result.stdout.splitlines() if line)
    if any(key.lower() == "extensions.worktreeconfig" for key in keys):
        enabled = runner.run(
            source,
            "config",
            "--local",
            "--type=bool",
            "--get",
            "extensions.worktreeConfig",
            check=False,
        )
        if enabled.returncode not in {0, 1}:
            raise MaintenanceError("could not validate worktree configuration enablement")
        if enabled.returncode == 0 and enabled.stdout.strip() == "true":
            worktree = runner.run(
                source,
                "config",
                "--worktree",
                "--no-includes",
                "--name-only",
                "--get-regexp",
                ".*",
                check=False,
            )
            if worktree.returncode not in {0, 1}:
                raise MaintenanceError(
                    "could not inspect source worktree configuration keys "
                    f"(direct rc {worktree.returncode})"
                )
            keys.extend(line for line in worktree.stdout.splitlines() if line)
    unsafe = sorted(
        {
            category
            for key in keys
            for pattern, category in UNSAFE_LOCAL_CONFIG
            if pattern.match(key)
        }
    )
    if unsafe:
        # Key names identify the denied capability; values may contain secrets or executable text.
        raise MaintenanceError(
            "unsafe source-local executable configuration is present: " + ", ".join(unsafe)
        )


def validate_source(
    runner: GitRunner, source: Path, candidate: str,
    *, on_admitted: Callable[[dict[str, str]], None] | None = None,
) -> dict[str, str]:
    admit_local_config(runner, source)
    refuse_submodule_layout(runner, source)
    top = Path(runner.run(source, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top != source:
        raise MaintenanceError("--source-repo must name the repository worktree root")
    resolved = runner.run(source, "rev-parse", "--verify", f"{candidate}^{{commit}}").stdout.strip()
    if resolved != candidate:
        raise MaintenanceError("candidate does not resolve to the exact supplied commit")
    common_text = runner.run(source, "rev-parse", "--git-common-dir").stdout.strip()
    common = (source / common_text).resolve() if not Path(common_text).is_absolute() else Path(common_text)
    git_dir_text = runner.run(source, "rev-parse", "--git-dir").stdout.strip()
    git_dir = (source / git_dir_text).resolve() if not Path(git_dir_text).is_absolute() else Path(git_dir_text).resolve()
    origin = runner.run(source, "config", "--get", "remote.origin.url", check=False).stdout.strip()
    # Origin bytes are not eligible for persistence until their admission succeeds.
    # Publish that fact to the preview owner in the same protected transition;
    # subsequent safe observations remain interruptible and owned by that caller.
    with signal_protection():
        validate_repository_location(origin, "source origin")
        identity = {
            "worktree": str(source),
            "git_common_dir": str(common.resolve()),
            "git_worktree_dir": str(git_dir),
            "origin_url": origin,
        }
        if on_admitted is not None:
            on_admitted(identity)
    if SIGNAL_CONTROLLER:
        SIGNAL_CONTROLLER.deliver()
    identity.update({
        "head": runner.run(source, "rev-parse", "HEAD").stdout.strip(),
        "branch": runner.run(source, "branch", "--show-current").stdout.strip(),
        "status_sha256": sha256_bytes(
            runner.run(source, "status", "--porcelain=v1", "--untracked-files=all").stdout.encode()
        ),
    })
    return identity


def refuse_submodule_layout(runner: GitRunner, repo: Path) -> None:
    """Refuse nested Git layouts using filesystem existence and safe index plumbing."""

    if os.path.lexists(repo / ".gitmodules"):
        raise MaintenanceError("submodule layouts are unsupported: .gitmodules is present")
    common_text = runner.run(repo, "rev-parse", "--git-common-dir").stdout.strip()
    common = Path(common_text)
    if not common.is_absolute():
        common = repo / common
    if os.path.lexists(common / "modules"):
        raise MaintenanceError("submodule layouts are unsupported: common modules directory exists")
    index = runner.run(repo, "ls-files", "--stage", "-z").stdout
    for entry in index.split("\0"):
        if entry and entry.split(" ", 1)[0] == "160000":
            raise MaintenanceError("submodule layouts are unsupported: indexed gitlink is present")


SENSITIVE_QUERY_WORDS = frozenset(
    {"auth", "authorization", "credential", "key", "password", "passwd", "secret", "token"}
)
SENSITIVE_QUERY_COMPACT = frozenset({"apikey", "accesstoken", "clientsecret"})


def sensitive_query_key(key: str) -> bool:
    normalized = unicodedata.normalize("NFKC", key)
    segmented = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])",
        "_",
        normalized,
    )
    # parse_qsl has decoded one URL layer. A remaining percent sign leaves a
    # second decoding ambiguous, so refuse it before any request is launched.
    if "%" in segmented:
        return True
    words = [word.casefold() for word in re.findall(r"[^\W_]+", segmented)]
    return any(word in SENSITIVE_QUERY_WORDS for word in words) or (
        "".join(words) in SENSITIVE_QUERY_COMPACT
    )


def ambiguous_query_delimiter(query: str) -> bool:
    """Reject encoded separators that another query parser could reinterpret."""

    for component in query.split("&"):
        decoded = component
        if ";" in decoded:
            return True
        for _ in range(5):
            following = urllib.parse.unquote_plus(decoded)
            if following == decoded:
                break
            decoded = following
            if ";" in decoded or "&" in decoded:
                return True
        # Unresolved encoding after the bounded passes is ambiguous too.
        if "%" in decoded:
            return True
    return False


def parsed_admitted_url(
    value: str, label: str, *, schemes: set[str], allow_query: bool
) -> urllib.parse.SplitResult:
    if "\n" in value or "\r" in value or value.startswith("-") or value.startswith("ext::"):
        raise MaintenanceError(f"unsafe {label}")
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    except (ValueError, UnicodeError) as exc:
        raise MaintenanceError(f"malformed {label}") from exc
    if parsed.scheme.lower() not in schemes or not parsed.hostname:
        raise MaintenanceError(f"{label} uses an unsupported URL form")
    if parsed.password is not None or (parsed.username is not None and parsed.scheme != "ssh"):
        raise MaintenanceError(f"credential-bearing {label} is forbidden")
    if parsed.scheme == "ssh" and parsed.username not in {None, "git"}:
        raise MaintenanceError(f"credential-bearing {label} is forbidden")
    if parsed.fragment or (parsed.query and not allow_query):
        raise MaintenanceError(f"{label} contains unsupported URL components")
    if ambiguous_query_delimiter(parsed.query):
        raise MaintenanceError(f"credential-bearing {label} is forbidden")
    if any(sensitive_query_key(key) for key, _value in query):
        raise MaintenanceError(f"credential-bearing {label} is forbidden")
    return parsed


def parsed_admitted_metadata_url(
    value: str, label: str, *, allow_query: bool = True
) -> urllib.parse.SplitResult:
    parsed = parsed_admitted_url(value, label, schemes={"http", "https"}, allow_query=allow_query)
    if parsed.port == 0:
        raise MaintenanceError(f"{label} uses forbidden port zero")
    return parsed


def url_origin(parsed: urllib.parse.SplitResult) -> tuple[str, str, int | None]:
    scheme = parsed.scheme.lower()
    default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, (parsed.hostname or "").lower(), (
        default_port if parsed.port is None else parsed.port
    )


def admitted_metadata_destination(initial: str, candidate: str, label: str) -> str:
    """Resolve and admit an HTTP destination against the explicit endpoint's origin."""

    try:
        resolved = urllib.parse.urljoin(initial, candidate)
    except (ValueError, UnicodeError) as exc:
        raise MaintenanceError(f"malformed {label}") from exc
    first = parsed_admitted_metadata_url(initial, label)
    following = parsed_admitted_metadata_url(resolved, label)
    if url_origin(first) != url_origin(following):
        raise MaintenanceError(f"{label} changed origin")
    return resolved


def split_url(value: str, label: str) -> urllib.parse.SplitResult:
    """Structured URL admission never echoes a rejected value in its diagnostic."""
    try:
        return urllib.parse.urlsplit(value)
    except (ValueError, UnicodeError) as exc:
        raise MaintenanceError(f"malformed {label}") from exc


SCP_LOCATION = re.compile(r"^(?:(?P<user>[^@/]*)@)?(?P<host>[^@/:]+):")


def validate_repository_location(value: str, label: str) -> str:
    if not value:
        return value
    parsed = split_url(value, label)
    if parsed.scheme:
        if parsed.scheme == "file":
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise MaintenanceError(f"credential-bearing {label} is forbidden")
            return value
        parsed_admitted_url(value, label, schemes={"https", "ssh"}, allow_query=False)
        return value
    if "\n" in value or "\r" in value or value.startswith("-"):
        raise MaintenanceError(f"unsafe {label}")
    scp = SCP_LOCATION.match(value) if ":" in value.split("/", 1)[0] else None
    if scp is not None and scp.group("user") not in {None, "git"}:
        # Same identity rule as ssh:// URLs: only the conventional git user is admitted.
        raise MaintenanceError(f"credential-bearing {label} is forbidden")
    return value


def validate_upstream_url(value: str) -> str:
    if "\n" in value or "\r" in value or value.startswith("-") or value.startswith("ext::"):
        raise MaintenanceError("unsafe upstream URL")
    parsed = split_url(value, "upstream URL")
    if parsed.scheme in {"https", "ssh"}:
        parsed_admitted_url(value, "upstream URL", schemes={"https", "ssh"}, allow_query=False)
        return value
    if parsed.scheme == "file":
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise MaintenanceError("credential-bearing upstream URL is forbidden")
        return value
    path = Path(value)
    if path.is_absolute() and path.exists():
        return str(path.resolve())
    raise MaintenanceError("upstream URL must be HTTPS, SSH, file://, or an existing absolute path")


def parse_published_at(value: Any) -> datetime:
    if not isinstance(value, str):
        raise MaintenanceError("release published_at must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MaintenanceError(f"invalid release published_at: {value}") from exc
    if parsed.tzinfo is None:
        raise MaintenanceError("release published_at must include a timezone")
    return parsed


def version_from_tag(tag: str) -> str:
    version = tag[1:] if tag.startswith("v") else tag
    if not VERSION_RE.fullmatch(version):
        raise MaintenanceError(f"release tag does not map to a safe PyPI version: {tag}")
    return version


class AdmittedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect before urllib can issue the next request."""

    def __init__(self, initial: str) -> None:
        super().__init__()
        self.initial = initial

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        admitted = admitted_metadata_destination(self.initial, newurl, "metadata redirect")
        return super().redirect_request(req, fp, code, msg, headers, admitted)


def _http_json_request(url: str, timeout: float) -> tuple[Any, Mapping[str, str], str, str]:
    parsed_admitted_metadata_url(url, "metadata endpoint")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "graphify-fork-maintenance/1",
        },
    )
    try:
        opener = urllib.request.build_opener(AdmittedRedirectHandler(url))
        with opener.open(request, timeout=timeout) as response:
            content = response.read(MAX_HTTP_BYTES + 1)
            if len(content) > MAX_HTTP_BYTES:
                raise MaintenanceError(f"HTTP response exceeds {MAX_HTTP_BYTES} bytes")
            headers = dict(response.headers.items())
            links = response.headers.get_all("Link", [])
            if links:
                headers["Link"] = ",".join(links)
            # geturl() is the observed final location after any redirect.
            return json.loads(content), headers, sha256_bytes(content), response.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # The observed error location, which differs from url after a redirect.
            observed = exc.geturl()
            return None, {}, "missing", observed if isinstance(observed, str) else url
        raise MaintenanceError(f"HTTP request failed with status {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        status = "timeout" if isinstance(reason, TimeoutError) else "refused"
        raise MaintenanceError("HTTP request failed", status=status) from exc


def _http_json_worker(url: str, timeout: float) -> None:
    """One isolated urllib request; this worker never launches another process."""
    try:
        payload, headers, digest, final_url = _http_json_request(url, timeout)
        envelope = {"value": [payload, headers, digest], "final_url": final_url}
    except MaintenanceError as exc:
        envelope = {"error": str(exc), "status": exc.status}
    print(json.dumps(envelope))


def http_timeout_error(
    bound: str,
    process: subprocess.Popen[bytes],
    settled: bool,
    record: dict[str, Any],
    finished: float,
    started: float,
    request_deadline: float,
) -> MaintenanceError:
    return MaintenanceError(
        "HTTP request timed out",
        status="timeout",
        details={
            "bound": bound,
            "worker_pid": process.pid,
            "worker_direct_rc": process.returncode,
            "worker_settled": settled,
            "worker_output_complete": record["raw_complete"],
            "elapsed_ms": round((finished - started) * 1000),
            "deadline_overrun_ms": round(max(0.0, finished - request_deadline) * 1000),
        },
    )


def http_json(
    url: str, timeout: float, deadline: float, records: list[dict[str, Any]],
    begin_shutdown: Callable[[], float] | None = None,
) -> tuple[Any, Mapping[str, str], str]:
    parsed_admitted_metadata_url(url, "metadata endpoint")
    started = time.monotonic()
    request_deadline = min(deadline, started + timeout)
    bound = "whole-attempt" if deadline <= started + timeout else "network"
    if request_deadline <= started:
        raise MaintenanceError(
            "HTTP deadline already expired", status="timeout", details={"bound": bound}
        )
    # -I excludes caller Python path/startup injection. Loading by absolute path
    # avoids adding a public worker operation or depending on the current directory.
    argv = [
        sys.executable,
        "-I",
        "-c",
        "import runpy,sys; runpy.run_path(sys.argv[1])['_http_json_worker'](sys.argv[2],float(sys.argv[3]))",
        str(Path(__file__).resolve()),
        url,
        str(request_deadline - started),
    ]
    shared_group = composite_worker_group()
    try:
        process = launch_owned(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=not shared_group,
        )
    except OSError as exc:
        raise MaintenanceError(f"could not launch HTTP worker: {exc}") from exc
    stdout = stderr = b""
    primary_output: tuple[bytes, bytes] | None = None
    drained = False
    settled = False
    group_observation: bool | None = None
    group_observation_performed = False
    group_survived_primary = False
    timed_out = False
    interrupted: CaughtSignal | None = None
    unexpected: BaseException | None = None
    envelope: Any = None
    try:
        # One spanning ownership boundary from handoff through the single append, as in
        # GitRunner.run: ordinary-handler and transition signals still reach the record.
        phase = "primary"
        shutdown_deadline: float | None = None
        record: dict[str, Any] | None = None
        # Preserve the timing distinction between a completed worker response and
        # bytes/rc observed only after signal-triggered settlement.
        primary_captured = False
        classified = False
        terminal_failure: BaseException | None = None
        successful_result: tuple[Any, Mapping[str, str], str] | None = None
        while True:
            try:
                while record is None:
                    try:
                        if phase == "primary":
                            phase = "primary-running"
                            try:
                                try:
                                    release_owned()
                                    primary_output = process.communicate(
                                        timeout=max(0.0, request_deadline - time.monotonic())
                                    )
                                    stdout, stderr = primary_output
                                    drained = True  # a returned communicate() observed EOF on both pipes
                                    primary_captured = True
                                    group_observation, group_observation_performed = (
                                        (process.poll() is not None if shared_group else
                                         process_group_absent(process.pid)), True
                                    )
                                    settled = group_observation is True
                                    group_survived_primary = not settled
                                    timed_out = time.monotonic() >= request_deadline
                                except subprocess.TimeoutExpired as timeout_exc:
                                    timed_out = True
                                    stdout, stderr = timeout_exc.output or b"", timeout_exc.stderr or b""
                            except CaughtSignal as exc:
                                if primary_output is not None:
                                    stdout, stderr = primary_output
                                    drained = primary_captured = True
                                if group_observation_performed:
                                    settled = group_observation is True
                                    group_survived_primary = not settled
                                context = exc.__context__
                                if isinstance(context, subprocess.TimeoutExpired):
                                    timed_out = True
                                    stdout, stderr = context.output or b"", context.stderr or b""
                                interrupted = exc
                            except BaseException as exc:
                                unexpected = exc
                            phase = "settle"
                        # The worker's terminal transition starts this preview's one shutdown
                        # allowance; a failed preview publishes nothing afterwards.
                        while (not drained or not settled) and phase == "settle":
                            try:
                                shutdown_deadline = shutdown_deadline or (
                                    begin_shutdown() if begin_shutdown is not None
                                    else time.monotonic() + SHUTDOWN_ALLOWANCE_SECONDS
                                )
                                if drained:
                                    settled = (settle_direct_worker(process, shutdown_deadline)
                                               if shared_group else
                                               GitRunner._settle(process, shutdown_deadline))
                                else:
                                    stdout, stderr, drained, settled = terminate_and_drain(
                                        process, shutdown_deadline, stdout, stderr,
                                        shared_group=shared_group,
                                    )
                                break
                            except CaughtSignal as exc:
                                interrupted = interrupted or exc
                            except BaseException as exc:
                                unexpected = unexpected or exc
                                settled = False
                                break
                        phase = "record"
                        finished = time.monotonic()
                        # Exactly one record per worker, holding every captured original byte.
                        with signal_protection():
                            record = {
                                "argv": argv,
                                "pid": process.pid,
                                "process_group": os.getpgrp() if shared_group else process.pid,
                                "group_ownership": "outer_composite" if shared_group else "worker",
                                "direct_rc": process.returncode,
                                "duration_ms": round((finished - started) * 1000),
                                "mutating": False,
                                "origin": "http_worker_envelope",
                                "process_group_settled": None if shared_group else settled,
                                "worker_settled": settled,
                                "process_group_absent_after_primary": (
                                    None if shared_group else group_observation),
                                "process_group_observed": (
                                    False if shared_group else group_observation_performed),
                                "worker_exited_after_primary": (
                                    group_observation if shared_group else None),
                                "timed_out": timed_out,
                                "interrupted": interrupted is not None,
                                "stdout_eof": drained,
                                "stderr_eof": drained,
                                "raw_complete": drained and settled and process.returncode is not None,
                                "raw_complete_scope": "worker" if shared_group else "group",
                                "stdout": bounded_stream(stdout),
                                "stderr": bounded_stream(stderr),
                                "_stdout_raw": stdout,
                                "_stderr_raw": stderr,
                                "response_sha256": None,
                            }
                            records.append(record)
                    except CaughtSignal as exc:
                        interrupted = interrupted or exc
                        context = exc.__context__
                        if phase in {"primary-running", "settle"} and isinstance(context, Exception):
                            unexpected = unexpected or context
                            if phase == "settle":
                                settled = False
                                phase = "record"
                        if phase == "primary-running":
                            phase = "settle"
                if not classified:
                    # Decode complete captured output only once, before final delivery.
                    terminal_failure: BaseException | None = unexpected
                    with signal_protection():
                        if terminal_failure is None and timed_out:
                            terminal_failure = http_timeout_error(
                                bound, process, settled, record, finished, started, request_deadline
                            )
                        elif terminal_failure is None and group_survived_primary:
                            terminal_failure = MaintenanceError(
                                "HTTP worker process group remained present or could not be observed after leader completion",
                                details={
                                    "process_group_settled": settled,
                                    "group_observation": group_observation,
                                },
                            )
                        elif terminal_failure is None and interrupted is not None and not primary_captured:
                            # A termination rc or incomplete envelope after an early signal is not
                            # a previously captured worker failure.
                            pass
                        elif terminal_failure is None and process.returncode != 0:
                            terminal_failure = MaintenanceError(
                                "HTTP worker failed",
                                details={
                                    "worker_direct_rc": process.returncode,
                                    "stderr": bounded_stream(stderr),
                                },
                            )
                        elif terminal_failure is None:
                            try:
                                envelope = json.loads(stdout)
                            except (json.JSONDecodeError, UnicodeError):
                                terminal_failure = MaintenanceError("HTTP worker returned invalid JSON")
                            if terminal_failure is None and not isinstance(envelope, dict):
                                terminal_failure = MaintenanceError("HTTP worker returned an invalid envelope")
                            if terminal_failure is None and envelope.get("status") == "timeout":
                                record["timed_out"] = True
                                terminal_failure = http_timeout_error(
                                    bound, process, settled, record, finished, started, request_deadline
                                )
                            elif terminal_failure is None and "error" in envelope:
                                terminal_failure = MaintenanceError(str(envelope["error"]))
                            elif terminal_failure is None:
                                value = envelope.get("value")
                                if (
                                    not isinstance(value, list)
                                    or len(value) != 3
                                    or not isinstance(value[1], dict)
                                    or not isinstance(value[2], str)
                                ):
                                    terminal_failure = MaintenanceError(
                                        "HTTP worker returned an invalid result"
                                    )
                                else:
                                    record["response_sha256"] = value[2]
                                    final_url = envelope.get("final_url")
                                    record["response_final_url"] = (
                                        final_url if isinstance(final_url, str) else None
                                    )
                                    record["redirected"] = record["response_final_url"] != url
                                    successful_result = value[0], value[1], value[2]
                        if process.stdout is not None:
                            process.stdout.close()
                        if process.stderr is not None:
                            process.stderr.close()
                        pipes_closed = True
                        classified = True
                if SIGNAL_CONTROLLER:
                    SIGNAL_CONTROLLER.deliver()
                assert record is not None
                record["interrupted"] = interrupted is not None
                if terminal_failure is not None:
                    if begin_shutdown is not None:
                        begin_shutdown()
                    raise terminal_failure
                if interrupted is not None:
                    if begin_shutdown is not None:
                        begin_shutdown()
                    raise interrupted
                assert successful_result is not None
                return successful_result
            except CaughtSignal as exc:
                if exc is interrupted:
                    raise
                interrupted = interrupted or exc
                if record is None:
                    if phase == "primary-running":
                        if primary_output is not None:
                            stdout, stderr = primary_output
                            drained = primary_captured = True
                        if group_observation_performed:
                            settled = group_observation is True
                            group_survived_primary = not settled
                    context = exc.__context__
                    if phase == "primary-running" and isinstance(context, subprocess.TimeoutExpired):
                        timed_out = True
                        stdout, stderr = context.output or b"", context.stderr or b""
                    elif phase in {"primary-running", "settle"} and isinstance(context, Exception):
                        unexpected = unexpected or context
                        if phase == "settle":
                            settled = False
                            phase = "record"
                    if phase == "primary-running":
                        phase = "settle"
    finally:
        if not locals().get("pipes_closed", False):
            with signal_protection():
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()


def next_link(headers: Mapping[str, str]) -> str | None:
    link = headers.get("Link") or headers.get("link")
    if not link:
        return None
    found: list[str] = []
    for item in link.split(","):
        # A semicolon inside <URL> belongs to its query, not the Link parameters.
        # Keep that URL intact so the canonical destination admission sees it.
        trimmed = item.strip()
        closing = trimmed.find(">") if trimmed.startswith("<") else -1
        pieces = (
            [trimmed[: closing + 1]]
            + [piece.strip() for piece in trimmed[closing + 1 :].split(";") if piece.strip()]
            if closing >= 0 else [piece.strip() for piece in trimmed.split(";")]
        )
        relations: list[str] = []
        for parameter in pieces[1:]:
            match = re.fullmatch(r'rel\s*=\s*"([^"]*)"', parameter, re.IGNORECASE)
            if match:
                relations.extend(match.group(1).split())
        declares_next = any(relation.lower() == "next" for relation in relations)
        if not declares_next:
            if re.search(r"rel\s*=.*next", item, re.IGNORECASE):
                raise MaintenanceError("malformed GitHub next-link declaration")
            continue
        target = pieces[0]
        if not (target.startswith("<") and target.endswith(">") and len(target) > 2):
            raise MaintenanceError("malformed GitHub next-link target")
        found.append(target[1:-1])
    if len(found) > 1:
        raise MaintenanceError("multiple GitHub next-link declarations are ambiguous")
    return found[0] if found else None


def validate_pagination_url(initial: str, candidate: str) -> str:
    return admitted_metadata_destination(initial, candidate, "GitHub pagination next link")


def load_release_metadata(
    args: argparse.Namespace, runner: GitRunner
) -> tuple[list[Any], dict[str, Any], str, dict[str, Any]]:
    initial_url = args.github_releases_url
    initial_parsed = parsed_admitted_metadata_url(initial_url, "GitHub releases endpoint")
    pypi_parsed = parsed_admitted_metadata_url(
        args.pypi_base_url, "PyPI base URL", allow_query=False
    )
    if args.github_releases_fixture or args.pypi_fixture:
        if not (args.github_releases_fixture and args.pypi_fixture):
            raise MaintenanceError("both release and PyPI fixtures are required together")
        release_data = read_json_file(Path(args.github_releases_fixture), "release fixture")
        pypi_data = read_json_file(Path(args.pypi_fixture), "PyPI fixture")
        if not isinstance(release_data, dict) or not isinstance(release_data.get("pages"), list):
            raise MaintenanceError("release fixture must contain a pages array")
        if not isinstance(pypi_data, dict):
            raise MaintenanceError("PyPI fixture must be an object keyed by version")
        releases: list[Any] = []
        for page in release_data["pages"]:
            if not isinstance(page, list):
                raise MaintenanceError("each release fixture page must be an array")
            releases.extend(page)
        bindings = {
            "github": {
                "canonical_json_sha256": sha256_bytes(canonical_json(release_data)),
                "pages_observed": len(release_data["pages"]),
                "provenance": "recorded_fixture",
            },
            "pypi": {
                "canonical_json_sha256": sha256_bytes(canonical_json(pypi_data)),
                "provenance": "recorded_fixture",
            },
        }
        return releases, pypi_data, "recorded_fixture", bindings

    releases = []
    hashes: list[str] = []
    url = initial_url
    visited: set[str] = set()
    pages = 0
    first_record = len(runner.records)
    while url:
        if url in visited:
            raise MaintenanceError("GitHub release pagination cycle detected")
        visited.add(url)
        pages += 1
        if pages > args.max_pages:
            raise MaintenanceError(
                "GitHub release pagination exceeded --max-pages; refusing truncation"
            )
        payload, headers, digest = http_json(
            url, args.network_timeout, runner.deadline, runner.records,
            runner.begin_shutdown,
        )
        if not isinstance(payload, list):
            raise MaintenanceError("GitHub releases response must be an array")
        releases.extend(payload)
        hashes.append(digest)
        following = next_link(headers)
        url = validate_pagination_url(initial_url, following) if following is not None else ""
    # Official provenance requires every observed final location to be the requested one.
    redirected = any(record.get("redirected") for record in runner.records[first_record:])
    evidence_kind = (
        "official_github_live"
        if (
            not redirected
            and initial_parsed.scheme == "https"
            and initial_parsed.hostname == "api.github.com"
            and initial_parsed.path == f"/repos/{args.upstream_repository}/releases"
        )
        else "controlled_http"
    )
    github_provenance = (
        "official_live"
        if (
            not redirected
            and initial_parsed.scheme == "https"
            and initial_parsed.hostname == "api.github.com"
            and initial_parsed.path == f"/repos/{args.upstream_repository}/releases"
        )
        else "controlled_http"
    )
    pypi_provenance = (
        "official_live"
        if pypi_parsed.scheme == "https" and pypi_parsed.hostname == "pypi.org"
        else "controlled_http"
    )
    return (
        releases,
        {"live_pypi_base_url": args.pypi_base_url, "page_hashes": hashes},
        evidence_kind,
        {
            "github": {
                "page_response_sha256": hashes,
                "pages_observed": pages,
                "provenance": github_provenance,
            },
            "pypi": {"base_provenance": pypi_provenance},
        },
    )


def pypi_usable(
    version: str,
    pypi_data: dict[str, Any],
    evidence_kind: str,
    network_timeout: float,
    runner: GitRunner,
) -> tuple[bool, str, dict[str, Any]]:
    """Return usability, response digest and this version's own response provenance."""
    if evidence_kind == "recorded_fixture":
        payload = pypi_data.get(version)
        digest = sha256_bytes(canonical_json(payload))
        provenance: dict[str, Any] = {"pypi_provenance": "recorded_fixture"}
    else:
        base = pypi_data["live_pypi_base_url"].rstrip("/")
        payload, _, digest = http_json(
            f"{base}/pypi/graphifyy/{version}/json",
            network_timeout,
            runner.deadline,
            runner.records,
            runner.begin_shutdown,
        )
        # Classify only what this response observed; the requested base is not enough.
        final_url = runner.records[-1].get("response_final_url")
        parsed = urllib.parse.urlparse(final_url) if isinstance(final_url, str) else None
        provenance = {
            "pypi_response_final_url": final_url,
            "pypi_redirected": runner.records[-1].get("redirected"),
            "pypi_provenance": (
                "unknown"
                if parsed is None
                else "official_live"
                if parsed.scheme == "https" and parsed.hostname == "pypi.org"
                else "controlled_http"
            ),
        }
    if payload is None:
        return False, digest, provenance
    if not isinstance(payload, dict) or not isinstance(payload.get("urls"), list):
        raise MaintenanceError(f"PyPI metadata for {version} must contain a urls array")
    for item in payload["urls"]:
        if (
            isinstance(item, dict)
            and item.get("yanked") is False
            and isinstance(item.get("url"), str)
            and item["url"]
        ):
            return True, digest, provenance
    return False, digest, provenance


def select_release(
    args: argparse.Namespace, runner: GitRunner, upstream_url: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    releases, pypi_data, evidence_kind, release_bindings = load_release_metadata(args, runner)
    stable: list[tuple[datetime, dict[str, Any]]] = []
    for item in releases:
        if not isinstance(item, dict):
            raise MaintenanceError("each GitHub release must be an object")
        if type(item.get("draft")) is not bool or type(item.get("prerelease")) is not bool:
            raise MaintenanceError(
                "each GitHub release must have boolean draft and prerelease eligibility fields"
            )
        if item.get("draft") is False and item.get("prerelease") is False:
            stable.append((parse_published_at(item.get("published_at")), item))
    stable.sort(key=lambda pair: pair[0], reverse=True)
    for newer, older in zip(stable, stable[1:]):
        if newer[0] == older[0] and newer[1].get("tag_name") != older[1].get("tag_name"):
            raise MaintenanceError("stable releases share publication time; target order is ambiguous")
    observations: list[dict[str, Any]] = []
    for published, item in stable:
        tag = item.get("tag_name")
        if not isinstance(tag, str) or not tag:
            raise MaintenanceError("stable release is missing tag_name")
        version = version_from_tag(tag)
        usable, digest, provenance = pypi_usable(
            version, pypi_data, evidence_kind, args.network_timeout, runner
        )
        observations.append(
            {
                "tag": tag,
                "version": version,
                "pypi_sha256": digest,
                "usable": usable,
                **provenance,
            }
        )
        if not usable:
            continue
        target = resolve_tag(runner, upstream_url, tag)
        return (
            {
                "mode": "release",
                "evidence_kind": evidence_kind,
                "target_commit": target,
                "release": {
                    "tag": tag,
                    "version": version,
                    "published_at": published.isoformat().replace("+00:00", "Z"),
                },
                "override": None,
            },
            {
                "release_observations": observations,
                "release_source": pypi_data,
                "release_inputs": release_bindings,
            },
        )
    raise MaintenanceError("no qualifying stable GitHub release has a usable non-yanked PyPI file")


def resolve_tag(runner: GitRunner, upstream_url: str, tag: str) -> str:
    if tag.startswith("-") or "\n" in tag or "\r" in tag:
        raise MaintenanceError("unsafe release tag")
    result = runner.run(
        None,
        "ls-remote",
        "--exit-code",
        upstream_url,
        f"refs/tags/{tag}",
        f"refs/tags/{tag}^{{}}",
    )
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t", 1)
        if len(fields) != 2 or not SHA_RE.fullmatch(fields[0]):
            raise MaintenanceError("malformed git ls-remote output")
        refs[fields[1]] = fields[0]
    target = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
    if target is None:
        raise MaintenanceError("release tag did not resolve to an exact remote commit")
    return target


def derive_delta(
    runner: GitRunner,
    scratch: Path,
    source: Path,
    candidate: str,
    upstream_url: str,
    target: str,
) -> tuple[str, list[dict[str, str]]]:
    runner.run(None, "init", "--bare", str(scratch), mutating=True)
    fetch_args = ("fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules")
    # Establish upstream supply in an empty object database, before candidate
    # objects could satisfy a requested SHA locally. Only exact commits qualify.
    runner.run(scratch, *fetch_args, upstream_url, target, mutating=True)
    resolved = runner.run(scratch, "rev-parse", "--verify", f"{target}^{{commit}}").stdout.strip()
    if resolved != target:
        raise MaintenanceError("upstream target must be an exact commit, not a tag object")
    refuse_submodule_tree(runner, scratch, target, "target")
    runner.run(scratch, *fetch_args, str(source), candidate, mutating=True)
    base = runner.run(scratch, "merge-base", candidate, target).stdout.strip()
    require_sha(base, "fork base")
    refuse_submodule_tree(runner, scratch, candidate, "candidate")
    refuse_submodule_tree(runner, scratch, base, "fork base")
    diff = runner.run(
        scratch,
        "diff",
        "--no-ext-diff",
        "--no-renames",
        "--name-status",
        "-z",
        base,
        candidate,
    ).stdout
    pieces = diff.split("\0")
    if pieces and pieces[-1] == "":
        pieces.pop()
    if len(pieces) % 2:
        raise MaintenanceError("malformed NUL-delimited git diff output")
    manifest = []
    for index in range(0, len(pieces), 2):
        status, path = pieces[index], pieces[index + 1]
        if not status or not path:
            raise MaintenanceError("fork delta contains an empty status or path")
        manifest.append({"path": path, "status": status, "classification": "pending"})
    return base, manifest


def refuse_submodule_tree(runner: GitRunner, repo: Path, tree: str, label: str) -> None:
    """Reject gitlinks and root .gitmodules without reading either one's content."""

    listing = runner.run(repo, "ls-tree", "-r", "-z", tree).stdout
    for entry in listing.split("\0"):
        if not entry:
            continue
        metadata, separator, path = entry.partition("\t")
        if not separator:
            raise MaintenanceError(f"could not inspect {label} tree layout")
        mode = metadata.split(" ", 1)[0]
        if mode == "160000":
            raise MaintenanceError(f"submodule layouts are unsupported: {label} contains a gitlink")
        if path == ".gitmodules":
            raise MaintenanceError(
                f"submodule layouts are unsupported: {label} contains root .gitmodules"
            )


def exclusive_write_bytes(path: Path, content: bytes) -> None:
    """Publish complete bytes atomically without ever replacing an existing path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise MaintenanceError(f"refusing to overwrite existing file: {path}") from exc
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    else:
        os.unlink(temporary)


class TerminalScratch:
    """Clean owned scratch once, under the current shared deadline."""

    def __init__(self, prefix: str) -> None:
        self.path = tempfile.mkdtemp(prefix=prefix)
        self.deadline: Callable[[], float] | None = None
        self.attempted = False

    def cleanup(self) -> None:
        if self.attempted:
            return
        self.attempted = True
        if self.deadline is not None:
            check_deadline(self.deadline(), "scratch cleanup")
        shutil.rmtree(self.path)
        if self.deadline is not None:
            check_deadline(self.deadline(), "scratch cleanup")


@contextmanager
def terminal_scratch(prefix: str) -> Any:
    scratch = TerminalScratch(prefix)
    try:
        yield scratch
    finally:
        if sys.exc_info()[0] is None:
            scratch.cleanup()
        else:
            # A failed terminal response remains the primary cause. Cleanup is
            # attempted only if the same deadline still admits it.
            try:
                scratch.cleanup()
            except Exception:
                pass


def json_file_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def atomic_write_json(path: Path, value: Any, *, deadline: float | None = None) -> bytes:
    content = json_file_bytes(value)
    if deadline is not None:
        check_deadline(deadline, "JSON publication")
    exclusive_write_bytes(path, content)
    if deadline is not None:
        check_deadline(deadline, "JSON publication")
    return content


def persist_command_evidence(
    records: list[dict[str, Any]], directory: Path, deadline: float
) -> None:
    """Persist every raw command stream and replace private bytes with durable bindings."""

    directory.mkdir(parents=True, exist_ok=False)
    for index, record in enumerate(records):
        for stream in ("stdout", "stderr"):
            check_deadline(deadline, "evidence persistence")
            raw_key = f"_{stream}_raw"
            raw = record.get(raw_key)
            if not isinstance(raw, bytes):
                raise MaintenanceError("command evidence is missing a raw byte stream")
            path = directory / f"command-{index:04d}.{stream}"
            exclusive_write_bytes(path, raw)
            summary = record.get(stream)
            if not isinstance(summary, dict):
                raise MaintenanceError("command evidence stream summary is malformed")
            summary["raw_path"] = str(path)
            summary["origin"] = "command"
            record.pop(raw_key)
    # The final stream write is itself bounded work; no later phase runs after expiry.
    check_deadline(deadline, "evidence persistence")


def persist_command_record_metadata(
    records: list[dict[str, Any]], directory: Path, deadline: float
) -> tuple[Path, str]:
    """Bind settled command facts to retained raw streams under the terminal bound."""

    check_deadline(deadline, "command metadata encoding")
    content = json_file_bytes({"schema_version": SCHEMA_VERSION, "command_records": records})
    check_deadline(deadline, "command metadata encoding")
    digest = sha256_bytes(content)
    path = directory / "command-records.json"
    exclusive_write_bytes(path, content)
    check_deadline(deadline, "command metadata publication")
    return path, digest


def preview(args: argparse.Namespace) -> dict[str, Any]:
    candidate = require_sha(args.candidate, "candidate")
    if not REPOSITORY_RE.fullmatch(args.upstream_repository):
        raise MaintenanceError("upstream repository must have owner/name form")
    upstream_url = validate_upstream_url(args.upstream_url)
    if not args.override_sha:
        # Reject caller-supplied metadata destinations before source observation can
        # produce bytes that a failure path might otherwise retain.
        parsed_admitted_metadata_url(args.github_releases_url, "GitHub releases endpoint")
        parsed_admitted_metadata_url(args.pypi_base_url, "PyPI base URL", allow_query=False)
    source = Path(args.source_repo).resolve(strict=True)
    output = Path(args.output_plan).resolve(strict=False)
    if output == source or source in output.parents:
        raise MaintenanceError("preview plan must be written outside the source worktree")
    deadline = time.monotonic() + args.attempt_timeout
    with terminal_scratch("graphify-fork-preview-") as scratch:
        temporary = scratch.path
        temporary_path = Path(temporary)
        home = temporary_path / "home"
        home.mkdir()
        runner = GitRunner(
            subprocess_timeout=args.subprocess_timeout,
            deadline=deadline,
            home=home,
        )
        scratch.deadline = lambda: runner.deadline
        identity: dict[str, str] = {}
        source_admitted = False

        def admit_source_identity(observed: dict[str, str]) -> None:
            nonlocal identity, source_admitted
            protected = tuple(
                Path(observed[key]) for key in ("worktree", "git_common_dir", "git_worktree_dir")
            )
            if any(paths_overlap(output, item) for item in protected):
                raise MaintenanceError("preview plan must be outside protected repository paths")
            identity = observed
            source_admitted = True

        evidence_directory = output.parent / (
            f".{output.name}.preview-evidence-{os.getpid()}-{time.time_ns()}"
        )
        def as_primary(error: BaseException) -> MaintenanceError:
            if isinstance(error, MaintenanceError):
                return error
            if isinstance(error, CaughtSignal):
                name = signal.Signals(error.signum).name
                return MaintenanceError(
                    f"interrupted by {name}", status="interrupted", details={"signal": name}
                )
            if isinstance(error, (OSError, UnicodeError, subprocess.SubprocessError)):
                return MaintenanceError(f"filesystem operation failed: {error}")
            return MaintenanceError(f"preview operation failed: {error}")

        # Execution runs exactly once. This outer signal catch also owns the gap
        # between an ordinary exception and entry into protected finalization.
        executed = False
        primary: MaintenanceError | None = None
        selection: dict[str, Any] = {}
        bindings: dict[str, Any] = {}
        base = ""
        capability_manifest: list[dict[str, str]] = []
        while True:
            try:
                if not executed:
                    executed = True
                    try:
                        validate_source(
                            runner, source, candidate, on_admitted=admit_source_identity
                        )
                        if SIGNAL_CONTROLLER:
                            SIGNAL_CONTROLLER.deliver()
                        if args.override_sha:
                            selection = select_override(args)
                            bindings = {"override_validation": "owned_scratch_remote_commit"}
                        else:
                            if args.override_reason:
                                raise MaintenanceError("--override-reason requires --override-sha")
                            selection, bindings = select_release(args, runner, upstream_url)
                        base, capability_manifest = derive_delta(
                            runner, temporary_path / "objects.git", source, candidate,
                            upstream_url, selection["target_commit"],
                        )
                        check_deadline(runner.execution_deadline, "preview execution")
                    except Exception as failure:
                        primary = as_primary(failure)

                # Unsafe metadata may be present in an earlier HTTP envelope or
                # pagination header. It must never enter retained raw evidence.
                if primary is not None and (
                    not source_admitted
                    or "credential-bearing" in str(primary)
                    or not runner.records
                ):
                    raise primary

                with signal_protection():
                    shutdown_deadline = runner.begin_shutdown()
                    records_path: Path | None = None
                    records_sha256: str | None = None
                    visibility = "unknown"
                    plan_preexisting: bool | None = None
                    plan_published = False
                    plan_id: str | None = None
                    plan_sha256: str | None = None

                    def terminal_details() -> dict[str, Any]:
                        details: dict[str, Any] = {
                            "command_evidence": str(evidence_directory),
                            "command_records_path": str(
                                records_path or evidence_directory / "command-records.json"
                            ),
                            "plan": str(output),
                            "plan_visibility": visibility,
                            "plan_preexisting": plan_preexisting,
                            "plan_published_by_attempt": plan_published,
                            "primary_execution_deadline_monotonic": runner.execution_deadline,
                            "shutdown_deadline_monotonic": shutdown_deadline,
                        }
                        if records_sha256 is not None:
                            details["command_records_sha256"] = records_sha256
                        if plan_id is not None:
                            details["plan_id"] = plan_id
                        if plan_sha256 is not None:
                            details["plan_sha256"] = plan_sha256
                        return details

                    def prepared_failure(cause: MaintenanceError) -> MaintenanceError:
                        failure = MaintenanceError(
                            str(cause), status=cause.status,
                            details={**cause.details, **terminal_details()},
                        )
                        failure.prepare_terminal(shutdown_deadline)
                        return failure

                    # Prepare a conservative envelope before raw writes or cleanup.
                    # A later expiry can still report the original primary cause and
                    # retrievable paths without starting another allowance.
                    fallback = MaintenanceError(
                        "preview finalization exceeded its shutdown allowance",
                        status="evidence_error",
                        details={
                            "primary_status": primary.status if primary else "unknown",
                            "primary_error": str(primary) if primary else "terminal state is unknown",
                            "terminal_status": "evidence_error",
                            **{**terminal_details(), "plan_visibility": "unknown"},
                        },
                    )
                    try:
                        fallback.prepare_terminal(shutdown_deadline)
                    except MaintenanceError:
                        # The HTTP worker may already have spent the whole allowance.
                        # Keep its original deadline and an encoded fallback; main
                        # will report expiry without granting more time.
                        fallback.terminal_deadline = shutdown_deadline

                    def prepared_or_fallback(error: MaintenanceError) -> MaintenanceError:
                        try:
                            error.prepare_terminal(shutdown_deadline)
                        except MaintenanceError:
                            return fallback
                        return error

                    prepublication_timeout: MaintenanceError | None = None
                    try:
                        check_deadline(shutdown_deadline, "plan visibility observation")
                        try:
                            output.lstat()
                        except FileNotFoundError:
                            visibility = "absent"
                            plan_preexisting = False
                        except OSError:
                            # An unobservable or previously occupied destination is
                            # not a plan published by this attempt, nor proven absent.
                            pass
                        else:
                            plan_preexisting = True
                        check_deadline(shutdown_deadline, "plan visibility observation")
                        persist_command_evidence(
                            runner.records, evidence_directory, shutdown_deadline
                        )
                        records_path, records_sha256 = persist_command_record_metadata(
                            runner.records, evidence_directory, shutdown_deadline
                        )
                        scratch.cleanup()
                        if primary is not None:
                            raise prepared_failure(primary)
                        if SIGNAL_CONTROLLER and SIGNAL_CONTROLLER.pending is not None:
                            primary = as_primary(CaughtSignal(SIGNAL_CONTROLLER.pending))
                            raise prepared_failure(primary)

                        prepublication_timeout = MaintenanceError(
                            "preview final completion reached its deadline; state is unknown",
                            status="timeout",
                            details={
                                "primary_status": "timeout",
                                "primary_error": "preview final completion reached its deadline; state is unknown",
                                "terminal_status": "timeout",
                                **terminal_details(),
                            },
                        )
                        try:
                            prepublication_timeout.prepare_terminal(shutdown_deadline)
                        except MaintenanceError:
                            raise fallback

                        plan: dict[str, Any] = {
                            "schema_version": SCHEMA_VERSION,
                            "observed_at": utc_now(),
                            "source_repository": identity,
                            "candidate": {"commit": candidate, "fork_base": base},
                            "upstream_repository": {
                                "identity": args.upstream_repository, "url": upstream_url,
                            },
                            "selection": selection,
                            "inputs": {
                                "distribution": "graphifyy",
                                "source_repo": str(source),
                                "output_plan": str(output),
                            },
                            "capability_manifest": capability_manifest,
                            "evidence_bindings": bindings,
                            "command_evidence": runner.records,
                        }
                        plan["plan_id"] = sha256_bytes(canonical_json(plan))
                        plan["integrity"] = {
                            "algorithm": "sha256",
                            "sha256": sha256_bytes(canonical_json(plan)),
                        }
                        plan_bytes = json_file_bytes(plan)
                        plan_id = plan["plan_id"]
                        plan_sha256 = sha256_bytes(plan_bytes)
                        response = TerminalResponse(
                            {
                                "status": "planned", "plan": str(output),
                                "plan_id": plan_id, "plan_sha256": plan_sha256,
                                "command_evidence": str(evidence_directory),
                                "command_records_path": str(records_path),
                                "command_records_sha256": records_sha256,
                            },
                            shutdown_deadline,
                            {"terminal_status": "planned", **terminal_details()},
                        )
                        response.prepare()
                        post_publication_timeout = MaintenanceError(
                            "plan publication reached its deadline; state is unknown",
                            status="timeout",
                            details={
                                "primary_status": "timeout",
                                "primary_error": "plan publication reached its deadline; state is unknown",
                                "terminal_status": "planned",
                                **{
                                    **terminal_details(), "plan_visibility": "published",
                                    "plan_published_by_attempt": True,
                                },
                            },
                        )
                        post_publication_timeout.prepare_terminal(shutdown_deadline)
                        if SIGNAL_CONTROLLER and SIGNAL_CONTROLLER.pending is not None:
                            primary = as_primary(CaughtSignal(SIGNAL_CONTROLLER.pending))
                            raise prepared_failure(primary)
                        check_deadline(shutdown_deadline, "plan publication")
                        visibility = "unknown"
                        exclusive_write_bytes(output, plan_bytes)
                        plan_published = True
                        visibility = "published"
                        response.expiry_details.update(terminal_details())
                        try:
                            check_deadline(shutdown_deadline, "plan publication")
                        except MaintenanceError as expired:
                            raise post_publication_timeout from expired
                        if SIGNAL_CONTROLLER and SIGNAL_CONTROLLER.pending is not None:
                            primary = as_primary(CaughtSignal(SIGNAL_CONTROLLER.pending))
                            raise prepared_failure(primary)
                        return response
                    except MaintenanceError as terminal_exc:
                        if terminal_exc.terminal_deadline is not None:
                            raise
                        if (primary is None and terminal_exc.status == "timeout"
                                and visibility == "absent" and prepublication_timeout is not None):
                            raise prepublication_timeout from terminal_exc
                        original = primary or terminal_exc
                        raise prepared_or_fallback(MaintenanceError(
                            "preview evidence finalization failed" if primary else str(original),
                            status="evidence_error" if primary else original.status,
                            details={
                                "primary_status": original.status,
                                "primary_error": str(original),
                                "terminal_status": terminal_exc.status,
                                **terminal_details(),
                            },
                        )) from terminal_exc
                    except Exception as terminal_exc:
                        original = primary or as_primary(terminal_exc)
                        raise prepared_or_fallback(MaintenanceError(
                            "preview evidence finalization failed" if primary else str(original),
                            status="evidence_error" if primary else original.status,
                            details={
                                "primary_status": original.status,
                                "primary_error": str(original),
                                "terminal_status": "evidence_error",
                                **terminal_details(),
                            },
                        )) from terminal_exc
                    finally:
                        if SIGNAL_CONTROLLER:
                            # A signal can arrive at the final return line after the
                            # last explicit pending check. The plan may already be
                            # visible; report that fact rather than silently succeed.
                            try:
                                late_signal = SIGNAL_CONTROLLER.pending
                                if late_signal is not None and primary is None and sys.exc_info()[0] is None:
                                    primary = as_primary(CaughtSignal(late_signal))
                                    try:
                                        raise prepared_failure(primary)
                                    except MaintenanceError as late_failure:
                                        if late_failure.terminal_deadline is not None:
                                            raise
                                        raise prepared_or_fallback(MaintenanceError(
                                            "preview response finalization failed",
                                            status="evidence_error",
                                            details={
                                                "primary_status": primary.status,
                                                "primary_error": str(primary),
                                                "terminal_status": late_failure.status,
                                                **terminal_details(),
                                            },
                                        )) from late_failure
                            finally:
                                SIGNAL_CONTROLLER.coalesce_terminal()
            except CaughtSignal as caught:
                if primary is None:
                    context = caught.__context__
                    primary = as_primary(context if isinstance(context, BaseException) else caught)
                # A terminal signal never repeats primary work or an evidence write.
                continue


def select_override(args: argparse.Namespace) -> dict[str, Any]:
    target = require_sha(args.override_sha, "override SHA")
    if not args.override_reason or not args.override_reason.strip():
        raise MaintenanceError("exact SHA override requires a recorded non-empty reason")
    return {
        "mode": "override",
        "evidence_kind": "owned_scratch_remote_commit",
        "target_commit": target,
        "release": None,
        "override": {"sha": target, "reason": args.override_reason.strip()},
    }


def load_and_validate_plan(path: Path, expected_sha256: str) -> tuple[dict[str, Any], str]:
    if not DIGEST_RE.fullmatch(expected_sha256):
        raise MaintenanceError("--expected-plan-sha256 must be exactly 64 lowercase hex digits")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise MaintenanceError(f"cannot read frozen plan: {exc}") from exc
    if len(content) > MAX_HTTP_BYTES:
        raise MaintenanceError(f"frozen plan exceeds {MAX_HTTP_BYTES} bytes")
    observed_sha256 = sha256_bytes(content)
    if observed_sha256 != expected_sha256:
        raise MaintenanceError("frozen plan exact-byte SHA-256 does not match caller approval")
    try:
        payload = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MaintenanceError(f"cannot parse frozen plan: {exc}") from exc
    if not isinstance(payload, dict):
        raise MaintenanceError("frozen plan must be a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise MaintenanceError("unsupported frozen plan schema version")
    integrity = payload.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "sha256":
        raise MaintenanceError("frozen plan has no supported integrity binding")
    expected = integrity.get("sha256")
    if not isinstance(expected, str) or not DIGEST_RE.fullmatch(expected):
        raise MaintenanceError("frozen plan integrity digest is malformed")
    unsigned = dict(payload)
    unsigned.pop("integrity", None)
    if sha256_bytes(canonical_json(unsigned)) != expected:
        raise MaintenanceError("frozen plan integrity check failed")
    plan_id = payload.get("plan_id")
    id_input = dict(unsigned)
    id_input.pop("plan_id", None)
    if not isinstance(plan_id, str) or plan_id != sha256_bytes(canonical_json(id_input)):
        raise MaintenanceError("frozen plan identifier check failed")
    source = payload.get("source_repository")
    candidate = payload.get("candidate")
    upstream = payload.get("upstream_repository")
    selection = payload.get("selection")
    if not (
        isinstance(source, dict)
        and isinstance(candidate, dict)
        and isinstance(upstream, dict)
        and isinstance(selection, dict)
    ):
        raise MaintenanceError("frozen plan is missing required identity objects")
    require_sha(candidate.get("commit", ""), "planned candidate")
    require_sha(candidate.get("fork_base", ""), "planned fork base")
    require_sha(selection.get("target_commit", ""), "planned target")
    if selection.get("mode") not in {"release", "override"}:
        raise MaintenanceError("frozen plan selection mode is invalid")
    if selection["mode"] == "override":
        override = selection.get("override")
        if not isinstance(override, dict):
            raise MaintenanceError("frozen override requires its explicit SHA and reason")
        override_sha = require_sha(override.get("sha"), "frozen override SHA")
        if override_sha != selection["target_commit"]:
            raise MaintenanceError("frozen override SHA does not match the target commit")
        reason = override.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise MaintenanceError("frozen override requires a non-empty reason")
        if selection.get("release") is not None:
            raise MaintenanceError("frozen override conflicts with a release selector")
    manifest = payload.get("capability_manifest")
    if not isinstance(manifest, list):
        raise MaintenanceError("frozen plan capability manifest must be an array")
    for item in manifest:
        if (
            not isinstance(item, dict)
            or item.get("classification") != "pending"
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("status"), str)
        ):
            raise MaintenanceError("frozen plan capability manifest entry is invalid")
    return payload, observed_sha256


def validate_committer(name: str, email: str) -> tuple[str, str]:
    forbidden = ("\0", "\n", "\r", "<", ">")
    if not name or not name.strip() or any(token in name for token in forbidden):
        raise MaintenanceError("--committer-name must be nonempty and contain no delimiters")
    if (
        not email
        or not email.strip()
        or any(token in email for token in forbidden)
        or not re.fullmatch(r"[^\s@]+@[^\s@]+", email)
    ):
        raise MaintenanceError("--committer-email must be a delimiter-free email address")
    return name, email


def validate_effective_committer(
    runner: GitRunner, committer: tuple[str, str]
) -> dict[str, str]:
    result = runner.run(None, "var", "GIT_COMMITTER_IDENT")
    match = re.fullmatch(r"(.*) <([^<>]+)> .+", result.stdout.strip())
    if match is None or match.groups() != committer:
        raise MaintenanceError("effective Git committer identity did not match explicit inputs")
    return {"name": committer[0], "email": committer[1], "git_ident": result.stdout.strip()}


def snapshot_refs(runner: GitRunner, source: Path) -> dict[str, str]:
    result = runner.run(source, "for-each-ref", "--format=%(refname)%00%(objectname)")
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\0", 1)
        if len(fields) != 2 or not SHA_RE.fullmatch(fields[1]):
            raise MaintenanceError("malformed for-each-ref output")
        refs[fields[0]] = fields[1]
    return refs


def snapshot_source_checkout(runner: GitRunner, source: Path) -> dict[str, Any]:
    admit_local_config(runner, source)
    refuse_submodule_layout(runner, source)
    fetch_path_text = runner.run(source, "rev-parse", "--git-path", "FETCH_HEAD").stdout.strip()
    fetch_path = Path(fetch_path_text)
    if not fetch_path.is_absolute():
        fetch_path = source / fetch_path
    fetch_sha256 = regular_file_sha256(fetch_path, runner.deadline)
    return {
        "head": runner.run(source, "rev-parse", "HEAD").stdout.strip(),
        "status": runner.run(
            source, "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout,
        "fetch_head_sha256": fetch_sha256,
        "fetch_head_present": fetch_sha256 is not None,
        "index_sha256": repository_index_sha256(runner, source),
        "worktree_content_sha256": worktree_content_sha256(runner, source),
    }


def repository_index_sha256(runner: GitRunner, repo: Path) -> str | None:
    index_text = runner.run(repo, "rev-parse", "--git-path", "index").stdout.strip()
    index_path = Path(index_text)
    if not index_path.is_absolute():
        index_path = repo / index_path
    return regular_file_sha256(index_path, runner.deadline)


def worktree_content_sha256(runner: GitRunner, repo: Path) -> str:
    listed = runner.run(
        repo,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    ).stdout
    digest = hashlib.sha256()
    root = repo.resolve()
    for relative in sorted(set(path for path in listed.split("\0") if path)):
        check_deadline(runner.deadline)
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise MaintenanceError("repository state contains an unsafe path")
        path = repo / relative
        try:
            path.lstat()
        except FileNotFoundError:
            kind = b"missing"
            content_digest = b""
        except OSError as exc:
            raise MaintenanceError("repository state is unreadable") from exc
        else:
            resolved_parent = path.parent.resolve()
            if resolved_parent != root and root not in resolved_parent.parents:
                raise MaintenanceError("repository state escapes through a symlink")
            if path.is_symlink():
                kind = b"symlink"
                content_digest = hashlib.sha256(os.fsencode(os.readlink(path))).digest()
            elif path.is_file():
                kind = b"file"
                file_sha256 = regular_file_sha256(path, runner.deadline)
                if file_sha256 is None:
                    raise MaintenanceError("repository state changed while observed; state is unknown")
                content_digest = bytes.fromhex(file_sha256)
            elif path.is_dir():
                kind = b"directory"
                content_digest = b""
            else:
                raise MaintenanceError("repository state contains a special file")
        digest.update(os.fsencode(relative))
        digest.update(b"\0" + kind + b"\0" + content_digest)
    return digest.hexdigest()


def verify_remote_target(
    runner: GitRunner, upstream_url: str, selection: dict[str, Any]
) -> None:
    target = require_sha(selection.get("target_commit", ""), "planned target")
    if selection.get("mode") == "release":
        release = selection.get("release")
        if not isinstance(release, dict) or not isinstance(release.get("tag"), str):
            raise MaintenanceError("release plan is missing its frozen tag")
        if resolve_tag(runner, upstream_url, release["tag"]) != target:
            raise MaintenanceError("frozen release tag no longer resolves to the planned commit")
        return
    # An override can be an ancestor with no ref pointing directly to it.
    # apply_plan verifies upstream supply/type in owned scratch before acquisition.


def ensure_source_identity(
    runner: GitRunner, source: Path, candidate: str, planned: dict[str, Any]
) -> None:
    current = validate_source(runner, source, candidate)
    expected_keys = (
        "worktree",
        "git_common_dir",
        "git_worktree_dir",
        "origin_url",
        "head",
        "branch",
        "status_sha256",
    )
    for key in expected_keys:
        if current.get(key) != planned.get(key):
            raise MaintenanceError(f"source repository identity drifted since preview: {key}")


def validate_attempt_inputs(
    args: argparse.Namespace, plan: dict[str, Any], runner: GitRunner
) -> tuple[Path, str, Path, Path, str]:
    try:
        source = Path(args.source_repo).resolve(strict=True)
    except OSError as exc:
        raise MaintenanceError(f"cannot resolve source repository: {exc}") from exc
    upstream_url = validate_upstream_url(args.upstream_url)
    output = Path(args.output_worktree).resolve(strict=False)
    evidence = Path(args.evidence_dir).resolve(strict=False)
    branch = args.branch
    if not branch.startswith("codex/"):
        raise MaintenanceError("attempt branch must use the codex/ namespace")
    runner.run(None, "check-ref-format", "--branch", branch)
    admit_local_config(runner, source)
    common_text = runner.run(source, "rev-parse", "--git-common-dir").stdout.strip()
    git_dir_text = runner.run(source, "rev-parse", "--git-dir").stdout.strip()
    common = (source / common_text).resolve() if not Path(common_text).is_absolute() else Path(common_text).resolve()
    git_dir = (source / git_dir_text).resolve() if not Path(git_dir_text).is_absolute() else Path(git_dir_text).resolve()
    protected = (source, common, git_dir)
    if any(paths_overlap(output, item) or paths_overlap(evidence, item) for item in protected):
        raise MaintenanceError("output and evidence must be outside protected repository paths")
    if output == evidence or output in evidence.parents or evidence in output.parents:
        raise MaintenanceError("output worktree and evidence directory must not overlap")
    upstream = plan["upstream_repository"]
    if upstream_url != upstream.get("url"):
        raise MaintenanceError("explicit upstream URL does not match the frozen plan")
    return source, upstream_url, output, evidence, branch


def paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def read_result(path: Path) -> dict[str, Any]:
    result = read_json_file(path, "attempt result")
    if not isinstance(result, dict):
        raise MaintenanceError("attempt result must be a JSON object")
    integrity = result.get("integrity")
    if not isinstance(integrity, dict) or not isinstance(integrity.get("sha256"), str):
        raise MaintenanceError("attempt result has no integrity binding")
    unsigned = dict(result)
    unsigned.pop("integrity", None)
    if sha256_bytes(canonical_json(unsigned)) != integrity["sha256"]:
        raise MaintenanceError("attempt result integrity check failed")
    return result


def replay_result(
    runner: GitRunner,
    result: dict[str, Any],
    plan: dict[str, Any],
    output: Path,
    branch: str,
) -> dict[str, Any]:
    if result.get("status") != "applied" or result.get("plan_id") != plan["plan_id"]:
        raise MaintenanceError("existing evidence does not describe this successful plan")
    attempt = result.get("attempt")
    if not isinstance(attempt, dict):
        raise MaintenanceError("existing attempt result is malformed")
    if attempt.get("output_worktree") != str(output) or attempt.get("branch") != branch:
        raise MaintenanceError("existing attempt identity does not match explicit arguments")
    if not output.is_dir():
        raise MaintenanceError("successful replay worktree is missing")
    top = Path(runner.run(output, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top != output:
        raise MaintenanceError("replay output is not the recorded worktree root")
    if runner.run(output, "branch", "--show-current").stdout.strip() != branch:
        raise MaintenanceError("replay worktree is on a different branch")
    if runner.run(output, "status", "--porcelain=v1", "--untracked-files=all").stdout:
        raise MaintenanceError("replay worktree is dirty")
    commit = runner.run(output, "rev-parse", "HEAD").stdout.strip()
    if commit != result.get("result_commit"):
        raise MaintenanceError("replay branch no longer points to the recorded result")
    tree = runner.run(output, "rev-parse", "HEAD^{tree}").stdout.strip()
    if tree != result.get("result_tree"):
        raise MaintenanceError("replay delivered tree no longer matches the recorded result")
    target = plan["selection"]["target_commit"]
    ancestry = runner.run(output, "merge-base", "--is-ancestor", target, commit, check=False)
    if ancestry.returncode != 0:
        raise MaintenanceError("replay result no longer descends from the frozen target")
    return {
        "status": "replayed",
        "plan_id": plan["plan_id"],
        "result_commit": commit,
        "result_tree": tree,
    }


def replay_conflict(
    runner: GitRunner,
    result: dict[str, Any],
    plan: dict[str, Any],
    output: Path,
    branch: str,
) -> dict[str, Any]:
    if result.get("status") != "conflict" or result.get("plan_id") != plan["plan_id"]:
        raise MaintenanceError("existing evidence does not describe this conflicted plan")
    attempt = result.get("attempt")
    if not isinstance(attempt, dict):
        raise MaintenanceError("existing conflict attempt is malformed")
    if attempt.get("output_worktree") != str(output) or attempt.get("branch") != branch:
        raise MaintenanceError("existing conflict identity does not match explicit arguments")
    if not output.is_dir():
        raise MaintenanceError("conflicted worktree is missing")
    git_dir_text = runner.run(output, "rev-parse", "--git-dir").stdout.strip()
    git_dir = Path(git_dir_text)
    if not git_dir.is_absolute():
        git_dir = output / git_dir
    if not ((git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()):
        raise MaintenanceError("recorded conflict no longer has preserved rebase state")
    conflicts = sorted(
        path
        for path in runner.run(
            output, "diff", "--name-only", "--diff-filter=U", "-z"
        ).stdout.split("\0")
        if path
    )
    if conflicts != result.get("conflicted_paths"):
        raise MaintenanceError("recorded conflicted paths changed")
    recovery = result.get("recovery")
    if not isinstance(recovery, dict):
        raise MaintenanceError("existing conflict attempt has malformed recovery guidance")
    return {
        "status": "conflict",
        "repeated": True,
        "plan_id": plan["plan_id"],
        "conflicted_paths": conflicts,
        "recovery": recovery,
    }


def acquire_target(
    runner: GitRunner,
    source: Path,
    scratch: Path,
    upstream_url: str,
    target: str,
) -> dict[str, Any]:
    before_refs = snapshot_refs(runner, source)
    before_checkout = snapshot_source_checkout(runner, source)
    record_start = len(runner.records)
    runner.run(
        source,
        "fetch",
        "--no-tags",
        "--no-write-fetch-head",
        "--no-recurse-submodules",
        "--no-auto-maintenance",
        "--refmap=",
        str(scratch),
        target,
        mutating=True,
    )
    resolved = runner.run(source, "rev-parse", "--verify", f"{target}^{{commit}}").stdout.strip()
    if resolved != target:
        raise MaintenanceError("acquired object does not resolve to the frozen target")
    after_refs = snapshot_refs(runner, source)
    after_checkout = snapshot_source_checkout(runner, source)
    if after_refs != before_refs:
        raise MaintenanceError("verified acquisition changed a preexisting ref")
    if after_checkout != before_checkout:
        raise MaintenanceError("verified acquisition changed FETCH_HEAD or the source checkout")
    return {
        "status": "acquired",
        "target_commit": target,
        "validated_upstream_provenance": upstream_url,
        "transfer_repository": str(scratch),
        "refs_unchanged": True,
        "source_checkout_unchanged": True,
        "commands": runner.records[record_start:],
    }


def write_integrity_bound_result(
    path: Path, result: dict[str, Any], *, deadline: float | None = None
) -> None:
    result["integrity"] = {"algorithm": "sha256", "sha256": sha256_bytes(canonical_json(result))}
    if deadline is not None:
        check_deadline(deadline, "result integrity encoding")
    atomic_write_json(path, result, deadline=deadline)


def observed_attempt_state(
    runner: GitRunner, source: Path, output: Path, branch: str
) -> dict[str, Any]:
    """Capture bounded, replay-comparable state without repairing anything."""

    branch_ref = f"refs/heads/{branch}"
    branch_result = runner.run(source, "rev-parse", "--verify", branch_ref, check=False)
    state: dict[str, Any] = {
        "source": snapshot_source_checkout(runner, source),
        "branch_ref": branch_ref,
        "branch_commit": branch_result.stdout.strip() if branch_result.returncode == 0 else None,
        "output_exists": output.exists(),
    }
    if output.is_dir():
        admit_local_config(runner, output)
        refuse_submodule_layout(runner, output)
        head = runner.run(output, "rev-parse", "HEAD", check=False)
        git_dir_result = runner.run(output, "rev-parse", "--git-dir", check=False)
        conflicts = runner.run(
            output, "diff", "--name-only", "--diff-filter=U", "-z", check=False
        )
        git_dir_text = git_dir_result.stdout.strip()
        git_dir = Path(git_dir_text)
        if git_dir_text and not git_dir.is_absolute():
            git_dir = output / git_dir
        state["output"] = {
            "head": head.stdout.strip() if head.returncode == 0 else None,
            "status": runner.run(
                output, "status", "--porcelain=v1", "--untracked-files=all", check=False
            ).stdout,
            "conflicted_paths": sorted(path for path in conflicts.stdout.split("\0") if path),
            "rebase_state": bool(
                git_dir_text
                and ((git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists())
            ),
            "index_sha256": repository_index_sha256(runner, output),
            "worktree_content_sha256": worktree_content_sha256(runner, output),
            "rebase_metadata_sha256": rebase_metadata_sha256(git_dir, runner.deadline),
        }
    return state


def rebase_metadata_sha256(git_dir: Path, deadline: float) -> str | None:
    candidates = (git_dir / "rebase-merge", git_dir / "rebase-apply")
    if any(candidate.is_symlink() for candidate in candidates):
        raise MaintenanceError("rebase state is a symlink; state is unknown")
    root = next((candidate for candidate in candidates if candidate.is_dir()), None)
    if root is None:
        return None
    digest = hashlib.sha256()
    try:
        with os.scandir(root) as listing:
            entries = sorted(listing, key=lambda entry: entry.name)
        # Any nested directory is unsupported, so one flat listing covers the whole tree.
        for entry in entries:
            check_deadline(deadline)
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                raise MaintenanceError("rebase state contains unsupported filesystem entries")
            file_sha256 = regular_file_sha256(Path(entry.path), deadline)
            if file_sha256 is None:
                raise MaintenanceError("rebase state changed while observed; state is unknown")
            digest.update(os.fsencode(entry.name))
            digest.update(b"\0" + bytes.fromhex(file_sha256))
    except OSError as exc:
        raise MaintenanceError("rebase state is unreadable") from exc
    return digest.hexdigest()


def recovery_for(output: Path, branch: str) -> dict[str, Any]:
    return {
        "automatic_action": "none",
        "worktree": str(output),
        "branch": branch,
        "guidance": (
            "Inspect the preserved attempt and command evidence; this tool does not retry, "
            "resolve, reset, abort, continue, or remove it automatically."
        ),
    }


def publish_terminal_result(
    *,
    result_path: Path,
    raw_directory: Path,
    result: dict[str, Any],
    runner: GitRunner,
    lock: Path,
    response: TerminalResponse | None = None,
    scratch_cleanup: Callable[[], None] | None = None,
) -> None:
    try:
        protection = SIGNAL_CONTROLLER.protect() if SIGNAL_CONTROLLER else nullcontext()
        with protection:
            try:
                # Publication is a terminal boundary: it starts (or reuses) the one allowance.
                persist_command_evidence(
                    runner.records, raw_directory, runner.begin_shutdown()
                )
                result["command_records"] = runner.records
                if response is not None:
                    response.prepare()
                if scratch_cleanup is not None:
                    scratch_cleanup()
                # Deadline check and exclusive link are not atomic; the OS call is excluded.
                check_deadline(runner.begin_shutdown(), "evidence persistence")
                write_integrity_bound_result(
                    result_path, result, deadline=runner.begin_shutdown()
                )
                check_deadline(runner.begin_shutdown(), "result publication")
                lock.unlink()
                check_deadline(runner.begin_shutdown(), "ownership release")
            finally:
                if SIGNAL_CONTROLLER:
                    SIGNAL_CONTROLLER.coalesce_terminal()
    except Exception as exc:
        deadline = runner.shutdown_deadline

        def visibility(path: Path) -> str:
            try:
                if deadline is None:
                    return "unknown"
                check_deadline(deadline, "publication visibility observation")
                return "present" if os.path.lexists(path) else "absent"
            except Exception:
                return "unknown"

        receipt_visibility = visibility(result_path)
        lock_visibility = visibility(lock)
        raise MaintenanceError(
            f"evidence persistence failed: {exc}",
            status="evidence_error",
            details={
                "attempt_owned": True,
                "result_path": str(result_path),
                "raw_evidence_path": str(raw_directory),
                "lock_path": str(lock),
                "receipt_visibility": receipt_visibility,
                "lock_visibility": lock_visibility,
                "terminal_status": result.get("status"),
                "recovery": result.get("recovery"),
            },
        ) from exc


def apply_plan(args: argparse.Namespace) -> dict[str, Any]:
    try:
        plan_path = Path(args.plan).resolve(strict=True)
    except OSError as exc:
        raise MaintenanceError(f"cannot resolve frozen plan: {exc}") from exc
    plan, plan_file_sha256 = load_and_validate_plan(plan_path, args.expected_plan_sha256)
    committer = validate_committer(args.committer_name, args.committer_email)
    deadline = time.monotonic() + args.attempt_timeout
    with terminal_scratch("graphify-fork-apply-") as scratch_scope:
        temporary = scratch_scope.path
        home = Path(temporary) / "home"
        home.mkdir()
        runner = GitRunner(
            subprocess_timeout=args.subprocess_timeout,
            deadline=deadline,
            home=home,
            committer=committer,
        )
        scratch_scope.deadline = lambda: runner.deadline
        cleanup_scratch = scratch_scope.cleanup
        source, upstream_url, output, evidence, branch = validate_attempt_inputs(
            args, plan, runner
        )
        result_path = evidence / "result.json"
        lock = evidence / "attempt.lock"
        raw_directory = evidence / "raw"

        def terminal_response(value: dict[str, Any]) -> TerminalResponse:
            return TerminalResponse(
                value,
                runner.begin_shutdown(),
                {
                    "attempt_owned": True,
                    "terminal_status": value["status"],
                    "result_path": str(result_path),
                    "lock_path": str(lock),
                    "receipt_visibility": "unknown",
                    "lock_visibility": "unknown",
                    "recovery": value.get("recovery"),
                },
            )

        def read_only_response(value: dict[str, Any]) -> TerminalResponse:
            response = TerminalResponse(
                value,
                runner.begin_shutdown(),
                {"terminal_status": value["status"], "evidence": str(result_path)},
            )
            response.prepare()
            cleanup_scratch()
            check_deadline(response.deadline, "read-only replay completion")
            return response

        # A visible receipt plus retained ownership is incomplete publication, never replay.
        if os.path.lexists(lock):
            raise MaintenanceError(
                "attempt lock ownership remains present; completed publication is uncertain",
                details={"result_path": str(result_path), "lock_path": str(lock)},
            )

        # Terminal replay is deliberately before remote validation, scratch extraction,
        # acquisition, or creation of any new evidence.
        if result_path.exists():
            existing = read_result(result_path)
            attempt = existing.get("attempt")
            if not isinstance(attempt, dict):
                raise MaintenanceError("existing attempt result has a malformed attempt identity")
            if existing.get("plan_sha256") != plan_file_sha256:
                raise MaintenanceError("existing attempt result has a different exact-byte plan pin")
            if attempt.get("committer") != {"name": committer[0], "email": committer[1]}:
                raise MaintenanceError("existing attempt result has a different explicit committer")
            ensure_source_identity(
                runner, source, plan["candidate"]["commit"], plan["source_repository"]
            )
            if output.is_dir():
                admit_local_config(runner, output)
            recorded_state = existing.get("observed_partial_state")
            if not isinstance(recorded_state, dict):
                raise MaintenanceError("existing attempt has malformed observed state")
            if "observation_error" in recorded_state:
                # Unknown is neither equality nor drift: refuse read-only for uncertainty.
                raise MaintenanceError(
                    "existing attempt state was never observed; refusing replay under uncertainty",
                    details={
                        "state_uncertain": True,
                        "recorded_status": existing.get("status"),
                        "evidence": str(result_path),
                        "recovery": existing.get("recovery"),
                    },
                )
            current_state = observed_attempt_state(runner, source, output, branch)
            if current_state != recorded_state:
                raise MaintenanceError("existing attempt state drifted; refusing replay")
            status = existing.get("status")
            if status == "conflict":
                return read_only_response(replay_conflict(runner, existing, plan, output, branch))
            if status == "applied":
                return read_only_response(replay_result(runner, existing, plan, output, branch))
            if status not in {"operational_failure", "timeout", "interrupted"}:
                raise MaintenanceError("existing attempt result has an unsupported terminal status")
            recovery = existing.get("recovery")
            if not isinstance(recovery, dict):
                raise MaintenanceError("existing failed attempt has malformed recovery guidance")
            return read_only_response({
                "status": status,
                "repeated": True,
                "plan_id": plan["plan_id"],
                "error": existing.get("error", "recorded apply failure"),
                "recovery": recovery,
                "evidence": str(result_path),
            })

        if os.path.lexists(lock):
            raise MaintenanceError("another apply owns this exact attempt lock")
        effective_committer = validate_effective_committer(runner, committer)
        candidate = plan["candidate"]["commit"]
        ensure_source_identity(runner, source, candidate, plan["source_repository"])
        verify_remote_target(runner, upstream_url, plan["selection"])
        scratch = Path(temporary) / "objects.git"
        # Re-derive in owned scratch before source mutation. The same validated scratch is
        # later the source repository's only acquisition peer.
        base, manifest = derive_delta(
            runner,
            scratch,
            source,
            candidate,
            upstream_url,
            plan["selection"]["target_commit"],
        )
        if base != plan["candidate"]["fork_base"]:
            raise MaintenanceError("frozen fork base does not match the re-derived merge base")
        if manifest != plan["capability_manifest"]:
            raise MaintenanceError("frozen capability manifest does not match the re-derived delta")
        branch_ref = f"refs/heads/{branch}"
        branch_exists = (
            runner.run(source, "show-ref", "--verify", "--quiet", branch_ref, check=False).returncode
            == 0
        )
        if output.exists() or branch_exists:
            raise MaintenanceError("destination directory or attempt branch already exists")
        evidence.mkdir(parents=True, exist_ok=True)
        attempt_identity = {
            "branch": branch,
            "output_worktree": str(output),
            "evidence_dir": str(evidence),
            "committer": {"name": committer[0], "email": committer[1]},
        }
        ephemeral = {
            "scratch_root": temporary,
            "lifetime": "removed when this invocation returns",
            "note": "argv may name scratch paths as exact history; raw_path links are durable",
        }
        # Signals stay deferred from lock creation until the owning try below begins.
        try:
            descriptor = launch_owned_lock(lock)
        except FileExistsError as exc:
            raise MaintenanceError("another apply owns this exact attempt lock") from exc
        failure: BaseException | None = None
        held = False
        try:
            try:
                release_owned()
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(f"pid={os.getpid()}\nplan_id={plan['plan_id']}\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                refs_before = snapshot_refs(runner, source)
                source_before = snapshot_source_checkout(runner, source)
                target = plan["selection"]["target_commit"]
                acquisition = acquire_target(runner, source, scratch, upstream_url, target)
                mutation_start = len(runner.records)
                runner.run(
                    source,
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(output),
                    plan["candidate"]["commit"],
                    mutating=True,
                )
                rebase = runner.run(
                    output,
                    "rebase",
                    "--onto",
                    target,
                    plan["candidate"]["fork_base"],
                    branch,
                    check=False,
                    mutating=True,
                )
                # Rebase completion is the first terminal transition: mutation closes and the
                # one shutdown allowance now bounds terminal observation and publication.
                runner.end_mutation()
                check_deadline(runner.execution_deadline, "apply execution")
                terminal_timing = {
                    "terminal_transition": "rebase_completed",
                    "primary_execution_deadline_monotonic": runner.execution_deadline,
                    "shutdown_allowance_seconds": SHUTDOWN_ALLOWANCE_SECONDS,
                    "shutdown_deadline_monotonic": runner.begin_shutdown(),
                }
                if rebase.returncode != 0:
                    conflicts = sorted(
                        path
                        for path in runner.run(
                            output, "diff", "--name-only", "--diff-filter=U", "-z"
                        ).stdout.split("\0")
                        if path
                    )
                    if not conflicts:
                        raise MaintenanceError(
                            f"rebase stopped with direct rc {rebase.returncode} without unmerged paths"
                        )
                    refs_after_conflict = snapshot_refs(runner, source)
                    source_after_conflict = snapshot_source_checkout(runner, source)
                    for name, commit in refs_before.items():
                        if refs_after_conflict.get(name) != commit:
                            raise MaintenanceError(f"preexisting ref changed during conflict: {name}")
                    if set(refs_after_conflict).difference(refs_before) != {branch_ref}:
                        raise MaintenanceError("conflicted apply created an unexpected ref")
                    if source_after_conflict != source_before:
                        raise MaintenanceError("conflicted apply changed FETCH_HEAD or source checkout")
                    recovery = recovery_for(output, branch)
                    conflict_result: dict[str, Any] = {
                        "schema_version": SCHEMA_VERSION,
                        "status": "conflict",
                        "completed_at": utc_now(),
                        "plan_id": plan["plan_id"],
                        "plan_sha256": plan_file_sha256,
                        "attempt": attempt_identity,
                        "effective_committer": effective_committer,
                        "acquisition": acquisition,
                        "conflicted_paths": conflicts,
                        "preexisting_refs_unchanged": True,
                        "source_checkout_unchanged": True,
                        "rebase_direct_rc": rebase.returncode,
                        "recovery": recovery,
                        "timing": terminal_timing,
                        "mutations": runner.records[mutation_start:],
                        "observed_partial_state": observed_attempt_state(
                            runner, source, output, branch
                        ),
                        "ephemeral_scratch": ephemeral,
                    }
                    response = terminal_response({
                        "status": "conflict",
                        "repeated": False,
                        "plan_id": plan["plan_id"],
                        "conflicted_paths": conflicts,
                        "recovery": recovery,
                        "evidence": str(result_path),
                    })
                    publish_terminal_result(
                        result_path=result_path,
                        raw_directory=raw_directory,
                        result=conflict_result,
                        runner=runner,
                        lock=lock,
                        response=response,
                        scratch_cleanup=cleanup_scratch,
                    )
                    return response
                result_commit = runner.run(output, "rev-parse", "HEAD").stdout.strip()
                result_tree = runner.run(output, "rev-parse", "HEAD^{tree}").stdout.strip()
                ancestry = runner.run(
                    output, "merge-base", "--is-ancestor", target, result_commit, check=False
                )
                if ancestry.returncode != 0:
                    raise MaintenanceError("result does not descend from the frozen target")
                if runner.run(
                    output, "status", "--porcelain=v1", "--untracked-files=all"
                ).stdout:
                    raise MaintenanceError("successful output worktree is unexpectedly dirty")
                refs_after = snapshot_refs(runner, source)
                source_after = snapshot_source_checkout(runner, source)
                for name, commit in refs_before.items():
                    if refs_after.get(name) != commit:
                        raise MaintenanceError(f"preexisting ref changed during apply: {name}")
                if set(refs_after).difference(refs_before) != {branch_ref}:
                    raise MaintenanceError("apply created an unexpected ref")
                if source_after != source_before:
                    raise MaintenanceError("apply changed FETCH_HEAD or the source checkout")
                result: dict[str, Any] = {
                    "schema_version": SCHEMA_VERSION,
                    "status": "applied",
                    "completed_at": utc_now(),
                    "plan_id": plan["plan_id"],
                    "plan_sha256": plan_file_sha256,
                    "attempt": attempt_identity,
                    "effective_committer": effective_committer,
                    "acquisition": acquisition,
                    "ancestry_proved": True,
                    "preexisting_refs_unchanged": True,
                    "source_checkout_unchanged": True,
                    "result_commit": result_commit,
                    "result_tree": result_tree,
                    "timing": terminal_timing,
                    "mutations": runner.records[mutation_start:],
                    "observed_partial_state": observed_attempt_state(
                        runner, source, output, branch
                    ),
                    "ephemeral_scratch": ephemeral,
                }
                response = terminal_response({
                    "status": "applied",
                    "plan_id": plan["plan_id"],
                    "result_commit": result_commit,
                    "result_tree": result_tree,
                    "evidence": str(result_path),
                })
                publish_terminal_result(
                    result_path=result_path,
                    raw_directory=raw_directory,
                    result=result,
                    runner=runner,
                    lock=lock,
                    response=response,
                    scratch_cleanup=cleanup_scratch,
                )
                return response
            except Exception as exc:
                # Keep catchable signals deferred from the ordinary handler through the
                # transition into owned finalization, which releases this hold itself.
                if SIGNAL_CONTROLLER:
                    SIGNAL_CONTROLLER.hold()
                    held = True
                failure = exc
        except CaughtSignal as exc:
            # A signal raised while entering the ordinary handler keeps that direct
            # owned-operation context as the first cause. Fatal contexts are not converted.
            context = exc.__context__
            failure = failure or (context if isinstance(context, Exception) else exc)

        assert failure is not None

        # Owned finalization: repeated catchable signals coalesce until publication ends,
        # and observation shares the one shutdown allowance begun at the first transition.
        # Conversion and status construction are inside the same protected attempt, so a
        # signal raised by exception text conversion cannot escape the owned receipt path.
        while True:
            try:
                if held and SIGNAL_CONTROLLER:
                    held = False  # cleared first: a signal here is still deferred
                    SIGNAL_CONTROLLER.release()
                with signal_protection():
                    if isinstance(failure, CaughtSignal):
                        owned_failure = MaintenanceError(
                            f"interrupted by {signal.Signals(failure.signum).name}",
                            status="interrupted",
                            details={"signal": signal.Signals(failure.signum).name},
                        )
                    elif isinstance(failure, MaintenanceError):
                        if failure.status == "evidence_error":
                            raise failure  # keep the original publication cause and paths
                        owned_failure = failure
                    else:
                        owned_failure = MaintenanceError(f"apply operation failed: {failure}")
                    status = (
                        owned_failure.status
                        if owned_failure.status in {"timeout", "interrupted"}
                        else "operational_failure"
                    )
                    shutdown_deadline = runner.begin_shutdown()
                    try:
                        partial_state = observed_attempt_state(runner, source, output, branch)
                    except Exception as state_exc:  # noqa: BLE001 - truthful unknown state
                        partial_state = {"state": "unknown", "observation_error": str(state_exc)}
                    failed_result: dict[str, Any] = {
                        "schema_version": SCHEMA_VERSION,
                        "status": status,
                        "completed_at": utc_now(),
                        "plan_id": plan["plan_id"],
                        "plan_sha256": plan_file_sha256,
                        "attempt": attempt_identity,
                        "effective_committer": effective_committer,
                        "error": str(owned_failure),
                        "diagnostic": owned_failure.details,
                        "timing": {
                            "primary_execution_deadline_monotonic": runner.execution_deadline,
                            "shutdown_allowance_seconds": SHUTDOWN_ALLOWANCE_SECONDS,
                            "shutdown_deadline_monotonic": shutdown_deadline,
                        },
                        "observed_partial_state": partial_state,
                        "ephemeral_scratch": ephemeral,
                        "recovery": recovery_for(output, branch),
                    }
                    response = terminal_response({
                        "status": status,
                        "repeated": False,
                        "plan_id": plan["plan_id"],
                        "error": failed_result["error"],
                        "recovery": failed_result["recovery"],
                        "evidence": str(result_path),
                    })
                    publish_terminal_result(
                        result_path=result_path,
                        raw_directory=raw_directory,
                        result=failed_result,
                        runner=runner,
                        lock=lock,
                        response=response,
                        scratch_cleanup=cleanup_scratch,
                    )
                break
            except CaughtSignal:
                continue
        return response


def add_shared_limits(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--subprocess-timeout", type=float, default=DEFAULT_SUBPROCESS_TIMEOUT)
    parser.add_argument("--attempt-timeout", type=float, default=DEFAULT_ATTEMPT_TIMEOUT)


class TerminalArgumentParser(argparse.ArgumentParser):
    """Use the same no-retry output boundary for help and usage diagnostics."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Python 3.14 probes stdout while constructing colorized formatters.
        # A closed caller stream must reach our terminal refusal boundary.
        if sys.version_info >= (3, 14):
            kwargs.setdefault("color", False)
            kwargs.setdefault("formatter_class", partial(argparse.HelpFormatter, color=False))
        super().__init__(*args, **kwargs)

    def _print_message(self, message: str, file: Any = None) -> None:
        if message:
            write_terminal(message, stream=file if file is not None else sys.stderr, end="")


WORKFLOW_SCHEMA_VERSION = 1
PREVIEW_FIELDS = {
    "source_repo": "--source-repo", "candidate": "--candidate",
    "upstream_repository": "--upstream-repository", "upstream_url": "--upstream-url",
    "output_plan": "--output-plan", "override_sha": "--override-sha",
    "override_reason": "--override-reason", "github_releases_url": "--github-releases-url",
    "pypi_base_url": "--pypi-base-url", "github_releases_fixture": "--github-releases-fixture",
    "pypi_fixture": "--pypi-fixture",
}
APPLY_FIELDS = {
    "plan": "--plan", "committer_name": "--committer-name",
    "committer_email": "--committer-email", "source_repo": "--source-repo",
    "upstream_url": "--upstream-url", "output_worktree": "--output-worktree",
    "branch": "--branch", "evidence_dir": "--evidence-dir",
}


def workflow_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != WORKFLOW_SCHEMA_VERSION:
        raise MaintenanceError("unsupported workflow configuration")
    if set(value) - {"schema_version", "preview", "apply", "expected_plan_sha256", "qualification_sidecar"}:
        raise MaintenanceError("unknown workflow configuration field")
    apply = value.get("apply")
    if not isinstance(apply, dict) or set(apply) != set(APPLY_FIELDS):
        raise MaintenanceError("workflow apply inputs are incomplete or unknown")
    preview = value.get("preview")
    if preview is not None:
        if not isinstance(preview, dict) or set(preview) - set(PREVIEW_FIELDS):
            raise MaintenanceError("workflow preview inputs contain an unknown field")
        if not {
            "source_repo", "candidate", "upstream_repository", "upstream_url", "output_plan"
        }.issubset(preview):
            raise MaintenanceError("workflow preview inputs are incomplete")
    for part in (apply, preview or {}):
        if any(not isinstance(v, str) or not v for v in part.values()):
            raise MaintenanceError("workflow arguments must be nonempty strings")
    if preview:
        require_sha(preview["candidate"], "workflow candidate")
        if ("override_sha" in preview) != ("override_reason" in preview):
            raise MaintenanceError("workflow override requires exact SHA and reason together")
        if "override_sha" in preview:
            require_sha(preview["override_sha"], "workflow override")
            if not preview["override_reason"].strip():
                raise MaintenanceError("workflow override reason must be nonempty")
            if any(key in preview for key in (
                "github_releases_url", "github_releases_fixture", "pypi_fixture", "pypi_base_url"
            )):
                raise MaintenanceError("workflow override cannot include release-selection inputs")
        if ("github_releases_fixture" in preview) != ("pypi_fixture" in preview):
            raise MaintenanceError("workflow release fixtures must be supplied together")
        if Path(preview["output_plan"]).resolve() != Path(apply["plan"]).resolve():
            raise MaintenanceError("preview output and apply plan differ")
        if Path(preview["source_repo"]).resolve() != Path(apply["source_repo"]).resolve():
            raise MaintenanceError("preview and apply source repositories differ")
        if preview["upstream_url"] != apply["upstream_url"]:
            raise MaintenanceError("preview and apply upstream URLs differ")
    validate_upstream_url(apply["upstream_url"])
    if preview:
        validate_upstream_url(preview["upstream_url"])
        if "github_releases_url" in preview:
            parsed_admitted_metadata_url(preview["github_releases_url"], "GitHub releases endpoint")
        if "pypi_base_url" in preview:
            parsed_admitted_metadata_url(preview["pypi_base_url"], "PyPI base URL", allow_query=False)
        if not Path(preview["source_repo"]).is_absolute() or not Path(preview["output_plan"]).is_absolute():
            raise MaintenanceError("workflow preview paths must be absolute")
    for key in ("plan", "source_repo", "output_worktree", "evidence_dir"):
        if not Path(apply[key]).is_absolute():
            raise MaintenanceError(f"workflow {key} must be absolute")
    pin = value.get("expected_plan_sha256")
    if pin is not None and (not isinstance(pin, str) or not DIGEST_RE.fullmatch(pin)):
        raise MaintenanceError("workflow plan pin is invalid")
    sidecar = value.get("qualification_sidecar")
    if sidecar is not None and (not isinstance(sidecar, str) or not Path(sidecar).is_absolute()):
        raise MaintenanceError("qualification sidecar path must be absolute")
    return value


def workflow_fixture_bindings(config: dict[str, Any]) -> dict[str, dict[str, str]]:
    preview = config.get("preview") or {}
    bindings: dict[str, dict[str, str]] = {}
    for key in ("github_releases_fixture", "pypi_fixture"):
        if key not in preview:
            continue
        path = Path(preview[key]).resolve(strict=True)
        if not path.is_file() or path.stat().st_size > MAX_HTTP_BYTES:
            raise MaintenanceError(f"workflow {key} is not a bounded regular file")
        bindings[key] = {"path": str(path), "sha256": sha256_bytes(path.read_bytes())}
    return bindings


def workflow_admit_paths(config: dict[str, Any], index_path: Path,
                         config_path: Path, run_id: str,
                         currency_ordinal: int = 1) -> None:
    """Reject composite path aliases before publishing an index or stage directory."""
    apply = config["apply"]
    source = Path(apply["source_repo"]).resolve(strict=True)
    with terminal_scratch("graphify-fork-admit-") as scratch:
        home = Path(scratch.path) / "home"
        home.mkdir()
        runner = GitRunner(subprocess_timeout=DEFAULT_SUBPROCESS_TIMEOUT,
                           deadline=time.monotonic() + DEFAULT_ATTEMPT_TIMEOUT,
                           home=home)
        common_text = runner.run(source, "rev-parse", "--git-common-dir").stdout.strip()
        worktree_text = runner.run(source, "rev-parse", "--git-dir").stdout.strip()
    def git_path(value: str) -> Path:
        path = Path(value)
        return (source / path).resolve() if not path.is_absolute() else path.resolve()
    protected = (source, git_path(common_text), git_path(worktree_text))
    boundary_paths = {
        "workflow config": config_path.resolve(),
        "workflow index": index_path.resolve(),
        "frozen plan": Path(apply["plan"]).resolve(),
        "output worktree": Path(apply["output_worktree"]).resolve(),
        "apply evidence": Path(apply["evidence_dir"]).resolve(),
    }
    for stage in ("preview", "apply", "replay-failure", f"currency-{currency_ordinal}",
                  f"currency-{currency_ordinal}.json", f"currency-{currency_ordinal}-commands"):
        boundary_paths[f"{stage} evidence"] = (index_path.parent / f"{run_id}-{stage}").resolve()
    for label, path in boundary_paths.items():
        if any(paths_overlap(path, item) for item in protected):
            raise MaintenanceError(f"{label} overlaps source or Git metadata")
    entries = list(boundary_paths.items())
    for position, (left_label, left_path) in enumerate(entries):
        for right_label, right_path in entries[position + 1:]:
            if paths_overlap(left_path, right_path):
                raise MaintenanceError(f"{left_label} overlaps {right_label}")


def workflow_environment_identity() -> dict[str, Any]:
    """Bind the executable and policy inputs that can change deterministic replay."""
    root = Path(__file__).resolve().parent.parent
    git_path = shutil.which("git", path="/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin")
    if git_path is None:
        raise MaintenanceError("Git executable is unavailable for workflow identity")
    files = {
        "engine": Path(__file__).resolve(), "python": Path(sys.executable).resolve(),
        "git": Path(git_path).resolve(),
    }
    for name in ("pyproject.toml", "uv.lock", "mise.toml"):
        path = root / name
        if path.is_file():
            files[name] = path
    return {
        "files": {key: {"path": str(path), "sha256": sha256_bytes(path.read_bytes())}
                  for key, path in files.items()},
        "python_version": sys.version,
        "import_environment": {name: os.environ.get(name)
                               for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
                                            "UV_PROJECT_ENVIRONMENT")},
    }


def workflow_checkpoint_input(stage: str, index: dict[str, Any],
                              plan: dict[str, Any] | None = None) -> str:
    config = index["config"]
    fields: dict[str, Any] = {"stage": stage, "environment": index["environment_identity"]}
    if stage == "preview":
        fields["preview"] = config.get("preview")
        fields["fixture_bindings"] = index.get("preview_fixture_bindings", {})
    elif stage == "apply":
        if plan is None or not index.get("plan_sha256"):
            raise MaintenanceError("apply checkpoint requires exact frozen plan")
        fields["plan_sha256"] = index["plan_sha256"]
        fields["candidate"] = plan["candidate"]
        fields["selection"] = plan["selection"]
        fields["source_repository"] = plan["source_repository"]
        fields["apply"] = config["apply"]
    else:
        raise MaintenanceError("unknown workflow checkpoint stage")
    return sha256_bytes(canonical_json(fields))


def workflow_progress(index: dict[str, Any]) -> dict[str, Any]:
    stages = index.get("stages", {})
    apply = stages.get("apply", {}) if isinstance(stages, dict) else {}
    applied = isinstance(apply, dict) and apply.get("status") in {"applied", "replayed"}
    qualified = bool(index.get("qualification_sidecar_sha256"))
    invalidated = any(index.get(name) for name in (
        "observed_config_drift", "observed_environment_drift", "observed_checkpoint_drift",
        "target_invalidation", "currency_unknown", "terminal_stop"))
    return {
        "graphify": {"plan": "frozen" if index.get("plan_sha256") else "pending",
                     "apply": "applied" if applied else "pending",
                     "capability_qualification": "recorded" if qualified else "pending",
                     "checkpoint_reuse": ("invalidated" if invalidated else
                                          "requires_read_only_replay" if applied else "not_available"),
                     "invalidation": index.get("target_invalidation"),
                     "terminal_stop": index.get("terminal_stop")},
        "knowledge_base": {"status": "pending_external_receipt"},
        "dotfiles": {"status": "pending_external_receipt"},
        "aggregate": "partial" if applied else "pending",
    }


def workflow_observe_checkpoints(index: dict[str, Any]) -> None:
    expected_environment = index.get("environment_identity")
    if not isinstance(expected_environment, dict) or expected_environment != workflow_environment_identity():
        index["observed_environment_drift"] = True
        return
    if index.get("observed_config_drift"):
        return
    plan_path = Path(index["config"]["apply"]["plan"])
    stages = index["stages"]
    for stage in ("preview", "apply"):
        receipt = stages.get(stage)
        if receipt is None:
            continue
        if not isinstance(receipt, dict):
            index["observed_checkpoint_drift"] = f"{stage}_receipt_malformed"
            return
        if receipt.get("status") not in (
            {"planned"} if stage == "preview" else {"applied", "replayed"}
        ):
            continue
        checkpoint = receipt.get("checkpoint")
        if not isinstance(checkpoint, dict):
            index["observed_checkpoint_drift"] = f"{stage}_checkpoint_missing"
            return
        if not plan_path.is_file():
            index["observed_checkpoint_drift"] = f"{stage}_plan_missing"
            return
        if stage == "preview":
            input_sha = workflow_checkpoint_input(stage, index)
            output_sha = sha256_bytes(plan_path.read_bytes())
        else:
            pin = index.get("plan_sha256")
            if not isinstance(pin, str):
                index["observed_checkpoint_drift"] = "apply_plan_pin_missing"
                return
            try:
                plan, _ = load_and_validate_plan(plan_path, pin)
            except MaintenanceError:
                index["observed_checkpoint_drift"] = "apply_plan_drift"
                return
            input_sha = workflow_checkpoint_input(stage, index, plan)
            result_path = Path(index["config"]["apply"]["evidence_dir"]) / "result.json"
            if not result_path.is_file():
                index["observed_checkpoint_drift"] = "apply_result_missing"
                return
            output_sha = sha256_bytes(result_path.read_bytes())
        if checkpoint != {"input_sha256": input_sha, "output_sha256": output_sha}:
            index["observed_checkpoint_drift"] = f"{stage}_checkpoint_drift"
            return


def workflow_validate_plan_config(plan: dict[str, Any], config: dict[str, Any]) -> None:
    apply = config["apply"]
    if (plan["source_repository"].get("worktree") != str(Path(apply["source_repo"]).resolve()) or
            plan["upstream_repository"].get("url") != validate_upstream_url(apply["upstream_url"])):
        raise MaintenanceError("frozen plan source or upstream differs from workflow configuration")
    preview = config.get("preview")
    if preview:
        inputs = plan.get("inputs")
        if (plan["candidate"]["commit"] != preview["candidate"] or
                plan["upstream_repository"].get("identity") != preview["upstream_repository"] or
                not isinstance(inputs, dict) or
                inputs.get("output_plan") != str(Path(preview["output_plan"]).resolve())):
            raise MaintenanceError("frozen candidate or preview input differs from workflow configuration")
        selection = plan["selection"]
        if "override_sha" in preview:
            override = selection.get("override")
            if (selection["mode"] != "override" or not isinstance(override, dict) or
                    override.get("sha") != preview["override_sha"] or
                    override.get("reason") != preview["override_reason"]):
                raise MaintenanceError("frozen override differs from workflow configuration")
        elif selection["mode"] != "release":
            raise MaintenanceError("workflow release selection cannot silently become an override")
        if "github_releases_fixture" in preview:
            bindings = plan.get("evidence_bindings")
            release_inputs = bindings.get("release_inputs") if isinstance(bindings, dict) else None
            github_input = release_inputs.get("github") if isinstance(release_inputs, dict) else None
            pypi_input = release_inputs.get("pypi") if isinstance(release_inputs, dict) else None
            if (selection.get("evidence_kind") != "recorded_fixture" or
                    not isinstance(github_input, dict) or not isinstance(pypi_input, dict) or
                    github_input.get("canonical_json_sha256") != sha256_bytes(
                        canonical_json(read_json_file(
                            Path(preview["github_releases_fixture"]), "release fixture"
                        ))
                    ) or
                    pypi_input.get("canonical_json_sha256") != sha256_bytes(
                        canonical_json(read_json_file(
                            Path(preview["pypi_fixture"]), "PyPI fixture"
                        ))
                    )):
                raise MaintenanceError("frozen release evidence differs from workflow fixtures")
        elif selection["mode"] == "release" and selection.get("evidence_kind") == "recorded_fixture":
            raise MaintenanceError("recorded fixture selection requires frozen workflow fixtures")
    selection = plan["selection"]
    if selection["mode"] == "release":
        release = selection.get("release")
        if (not isinstance(release, dict) or
                not isinstance(release.get("tag"), str) or
                not isinstance(release.get("version"), str) or
                version_from_tag(release["tag"]) != release["version"]):
            raise MaintenanceError("frozen release tag and version are invalid")
        parse_published_at(release.get("published_at"))
        if selection.get("override") is not None:
            raise MaintenanceError("frozen release conflicts with an override")
        bindings = plan.get("evidence_bindings")
        observations = bindings.get("release_observations") if isinstance(bindings, dict) else None
        if (not isinstance(observations, list) or not observations or
                any(not isinstance(item, dict) for item in observations) or
                observations[-1].get("tag") != release["tag"] or
                observations[-1].get("usable") is not True or
                any(item.get("usable") is True for item in observations[:-1])):
            raise MaintenanceError("frozen release does not match first usable observation")


def workflow_arguments(operation: str, fields: dict[str, str]) -> list[str]:
    allowed = PREVIEW_FIELDS if operation == "preview" else APPLY_FIELDS
    arguments = [item for key, value in fields.items() if key in allowed
                 for item in (allowed[key], value)]
    if operation == "apply":
        arguments.extend(("--expected-plan-sha256", fields["expected_plan_sha256"]))
    return arguments


def workflow_project(index: dict[str, Any], observed_plan_exists: bool | None) -> dict[str, Any]:
    progress = workflow_progress(index)
    if index.get("observed_config_drift"):
        return {"status": "stopped", "next_action": "inspect_config_drift",
                "stopped_reason": "workflow_config_drift", "progress": progress}
    if index.get("observed_environment_drift"):
        return {"status": "stopped", "next_action": "inspect_environment_drift",
                "stopped_reason": "workflow_environment_drift", "progress": progress}
    if index.get("observed_checkpoint_drift"):
        return {"status": "stopped", "next_action": "inspect_checkpoint_drift",
                "stopped_reason": index["observed_checkpoint_drift"], "progress": progress}
    if index.get("terminal_stop"):
        return {"status": "stopped", "next_action": "inspect_target_limit",
                "stopped_reason": index["terminal_stop"], "progress": progress}
    if index.get("currency_unknown"):
        return {"status": "stopped", "next_action": "inspect_currency_unknown",
                "stopped_reason": "currency_unknown", "progress": progress}
    if index.get("target_invalidation"):
        return {"status": "stopped", "next_action": "qualify_new_target",
                "stopped_reason": "target_changed", "progress": progress}
    stages = index.get("stages", {})
    if not isinstance(stages, dict):
        raise MaintenanceError("workflow stages are malformed")
    if index.get("replay_failure"):
        return {"status": "stopped", "next_action": "inspect_replay_failure",
                "stopped_reason": "replay_refused", "progress": progress}
    for stage in ("preview", "apply"):
        receipt = stages.get(stage)
        if receipt is not None and (not isinstance(receipt, dict) or receipt.get("status") not in
                                    ({"planned"} if stage == "preview" else {"applied", "replayed"})):
            return {"status": "stopped", "next_action": "inspect_failed_stage",
                    "stopped_reason": stage, "progress": progress}
    if "apply" in stages:
        if index.get("qualification_sidecar_sha256"):
            return {"status": "stopped", "next_action": "await_fork_gates",
                    "stopped_reason": "downstream_qualification_not_automated",
                    "progress": progress}
        return {"status": "stopped", "next_action": "qualify_capabilities",
                "stopped_reason": "ticket788_sidecar_required", "progress": progress}
    if "preview" in stages or observed_plan_exists is True:
        return {"status": "stopped", "next_action": "supply_external_plan_pin",
                "stopped_reason": "exact_byte_plan_pin_required", "progress": progress}
    if observed_plan_exists is None:
        return {"status": "stopped", "next_action": "inspect_plan_visibility",
                "stopped_reason": "plan_visibility_unknown", "progress": progress}
    return {"status": "ready", "next_action": "preview", "stopped_reason": None,
            "progress": progress}


def workflow_load_index(path: Path, run_id: str, *, currency: bool = False,
                        allow_config_drift: bool = False) -> dict[str, Any]:
    try:
        content = path.read_bytes()
        index = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MaintenanceError(f"cannot read workflow index: {exc}") from exc
    if not isinstance(index, dict) or index.get("schema_version") != WORKFLOW_SCHEMA_VERSION:
        raise MaintenanceError("workflow index schema is invalid")
    unsigned = dict(index)
    observed_integrity = unsigned.pop("integrity", None)
    if (not isinstance(observed_integrity, dict) or
            observed_integrity.get("algorithm") != "sha256" or
            observed_integrity.get("sha256") != sha256_bytes(canonical_json(unsigned))):
        raise MaintenanceError("workflow index integrity check failed")
    if index.get("run_id") != run_id:
        raise MaintenanceError("workflow run identity mismatch")
    if (not isinstance(index.get("config_path"), str) or
            not Path(index["config_path"]).is_absolute() or
            not isinstance(index.get("stages"), dict) or
            not isinstance(index.get("target_attempts"), list)):
        raise MaintenanceError("workflow index structure is invalid")
    config = workflow_config(index.get("config"))
    if index.get("immutable_config_sha256") != sha256_bytes(canonical_json(config)):
        raise MaintenanceError("workflow configuration identity drift")
    config_path = Path(index["config_path"])
    try:
        current_config = config_path.read_bytes()
    except OSError as exc:
        if not allow_config_drift:
            raise MaintenanceError(f"workflow configuration unavailable: {exc}") from exc
        current_config = None
    if (current_config is None or sha256_bytes(current_config) != index.get("config_file_sha256")) and not allow_config_drift:
        raise MaintenanceError("workflow configuration file drift")
    if current_config is None or sha256_bytes(current_config) != index.get("config_file_sha256"):
        index["observed_config_drift"] = True
    if not currency and workflow_fixture_bindings(config) != index.get("preview_fixture_bindings", {}):
        raise MaintenanceError("workflow release fixture identity drift")
    observations = index.get("currency_observations", [])
    if not isinstance(observations, list):
        raise MaintenanceError("workflow currency observations are malformed")
    for reference in observations:
        if (not isinstance(reference, dict) or
                not isinstance(reference.get("path"), str) or
                not Path(reference["path"]).is_absolute() or
                not isinstance(reference.get("sha256"), str) or
                not DIGEST_RE.fullmatch(reference["sha256"])):
            raise MaintenanceError("workflow currency receipt reference is malformed")
        if sha256_bytes(workflow_regular_bytes(
            Path(reference["path"]), "workflow currency receipt"
        )) != reference["sha256"]:
            raise MaintenanceError("workflow currency receipt identity drift")
    return index


def workflow_index_bytes(index: dict[str, Any]) -> bytes:
    unsigned = dict(index)
    unsigned.pop("integrity", None)
    index["integrity"] = {"algorithm": "sha256", "sha256": sha256_bytes(canonical_json(unsigned))}
    return json_file_bytes(index)


def workflow_evidence_ref(value: Any) -> str:
    if (not isinstance(value, dict) or set(value) != {"path", "sha256"} or
            not isinstance(value["path"], str) or not Path(value["path"]).is_absolute() or
            not isinstance(value["sha256"], str) or not DIGEST_RE.fullmatch(value["sha256"])):
        raise MaintenanceError("ticket788 evidence reference must bind an absolute file and SHA-256")
    path = Path(value["path"])
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise MaintenanceError("ticket788 evidence reference is not a regular file")
        digest = hashlib.sha256()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise MaintenanceError("ticket788 evidence reference changed file type")
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise MaintenanceError(f"ticket788 evidence reference cannot be read: {exc}") from exc
    if digest.hexdigest() != value["sha256"]:
        raise MaintenanceError("ticket788 evidence reference digest drift")
    return value["sha256"]


def workflow_validate_sidecar(index: dict[str, Any], plan: dict[str, Any]) -> str:
    sidecar_name = index["config"].get("qualification_sidecar")
    if not sidecar_name:
        raise MaintenanceError("ticket788 qualification sidecar is not configured")
    sidecar_path = Path(sidecar_name)
    content = sidecar_path.read_bytes()
    sidecar = json.loads(content)
    if not isinstance(sidecar, dict) or set(sidecar) != {
        "schema_version", "ticket", "plan_sha256", "target_commit",
        "result_commit", "result_tree", "result_receipt_sha256",
        "capabilities", "evidence_refs"
    } or sidecar["schema_version"] != 1 or sidecar["ticket"] != 788:
        raise MaintenanceError("ticket788 sidecar schema is incomplete")
    result_path = Path(index["config"]["apply"]["evidence_dir"]) / "result.json"
    result = read_result(result_path)
    if (result.get("status") != "applied" or
            result.get("plan_id") != plan["plan_id"] or
            result.get("plan_sha256") != index["plan_sha256"] or
            result.get("ancestry_proved") is not True or
            sidecar["plan_sha256"] != index["plan_sha256"] or
            sidecar["target_commit"] != plan["selection"]["target_commit"] or
            sidecar["result_commit"] != result.get("result_commit") or
            sidecar["result_tree"] != result.get("result_tree") or
            sidecar["result_receipt_sha256"] != sha256_bytes(result_path.read_bytes())):
        raise MaintenanceError("ticket788 sidecar does not bind exact plan and output")
    refs = sidecar["evidence_refs"]
    if not isinstance(refs, list) or not refs:
        raise MaintenanceError("ticket788 sidecar evidence is incomplete")
    evidence_digests = {workflow_evidence_ref(ref) for ref in refs}
    if len(evidence_digests) != len(refs):
        raise MaintenanceError("ticket788 sidecar evidence is duplicated")
    items = sidecar["capabilities"]
    manifest = plan["capability_manifest"]
    if not isinstance(items, list) or len(items) != len(manifest):
        raise MaintenanceError("ticket788 sidecar capability coverage is incomplete")
    expected = {(item["path"], item["status"]) for item in manifest}
    if len(expected) != len(manifest):
        raise MaintenanceError("frozen capability manifest has duplicate identities")
    observed: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "path", "status", "classification", "rationale", "evidence_refs"
        }:
            raise MaintenanceError("ticket788 capability entry is malformed")
        identity = (item["path"], item["status"])
        if identity in observed or identity not in expected:
            raise MaintenanceError("ticket788 capability identity is missing or duplicated")
        observed.add(identity)
        if item["classification"] not in {"retained", "equivalent", "superseded", "user-excluded"}:
            raise MaintenanceError("ticket788 capability classification is unknown")
        if (not isinstance(item["rationale"], str) or not item["rationale"].strip() or
                not isinstance(item["evidence_refs"], list) or not item["evidence_refs"] or
                any(not isinstance(ref, str) or ref not in evidence_digests
                    for ref in item["evidence_refs"])):
            raise MaintenanceError("ticket788 capability has no evidence")
    if observed != expected:
        raise MaintenanceError("ticket788 sidecar evidence is incomplete")
    return sha256_bytes(content)


def workflow_save_index(path: Path, index: dict[str, Any], prior_sha: str) -> None:
    # This is the sole mutable workflow record. Immutable plans and receipts retain
    # their exclusive-publication semantics.
    if sha256_bytes(path.read_bytes()) != prior_sha:
        raise MaintenanceError("workflow index changed concurrently")
    payload = workflow_index_bytes(index)
    fd, temporary = tempfile.mkstemp(prefix=".workflow-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def workflow_supervise(command: list[str], timeout: float = DEFAULT_ATTEMPT_TIMEOUT + 30) -> dict[str, Any]:
    """Own the outer child group and boundedly retain its original pipe bytes."""
    limit = MAX_HTTP_BYTES
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    stream_eof = {"stdout": False, "stderr": False}
    overflow = False
    total = 0
    timed_out = False
    interrupted: str | None = None
    previous: dict[int, Any] = {}
    pending_signal: int | None = None
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal pending_signal
        # Do not unwind Popen before its owned handle can be assigned. The first
        # loop observation settles it under the same shutdown allowance.
        pending_signal = pending_signal or signum

    for signum in (signal.SIGINT, signal.SIGTERM):
        handler = signal.getsignal(signum)
        if handler is not signal.SIG_IGN:
            previous[signum] = handler
            signal.signal(signum, request_stop)

    def drain(until: float) -> None:
        nonlocal total, overflow
        assert selector is not None
        while selector.get_map() and time.monotonic() < until:
            events = selector.select(timeout=min(0.1, max(0.0, until - time.monotonic())))
            for key, _ in events:
                try:
                    content = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not content:
                    stream_eof[key.data] = True
                    selector.unregister(key.fileobj)
                    continue
                available = max(0, limit - total)
                streams[key.data].extend(content[:available])
                total += min(len(content), available)
                if len(content) > available:
                    overflow = True
            if overflow:
                return

    try:
        child_env = os.environ.copy()
        child_env[COMPOSITE_GROUP_ENV] = str(os.getpid())
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, env=child_env)
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        primary_deadline = time.monotonic() + timeout
        try:
            while True:
                if pending_signal is not None:
                    interrupted = signal.Signals(pending_signal).name
                    break
                drain(min(primary_deadline, time.monotonic() + 0.1))
                if overflow:
                    break
                if not selector.get_map() and process.poll() is not None:
                    break
                if time.monotonic() >= primary_deadline:
                    timed_out = True
                    break
                if not selector.get_map():
                    time.sleep(0.01)
        except (CaughtSignal, KeyboardInterrupt) as exc:
            interrupted = (signal.Signals(exc.signum).name if isinstance(exc, CaughtSignal)
                           else "KeyboardInterrupt")
        if pending_signal is not None:
            interrupted = signal.Signals(pending_signal).name
        group_after_primary = process_group_absent(process.pid)
        needs_settlement = timed_out or interrupted is not None or overflow or group_after_primary is not True
        if needs_settlement:
            for signum in previous:
                signal.signal(signum, signal.SIG_IGN)
            shutdown_deadline = time.monotonic() + SHUTDOWN_ALLOWANCE_SECONDS
            group_settled = GitRunner._settle(process, shutdown_deadline)
            drain(shutdown_deadline)
        else:
            group_settled = True
        eof = stream_eof["stdout"] and stream_eof["stderr"]
        absent_after_shutdown = process_group_absent(process.pid)
        return {
            "stdout": bytes(streams["stdout"]), "stderr": bytes(streams["stderr"]),
            "direct_rc": process.poll(), "timed_out": timed_out,
            "interrupted": interrupted, "capture_limit_exceeded": overflow,
            "stdout_eof": stream_eof["stdout"], "stderr_eof": stream_eof["stderr"],
            "group_absent_after_primary": group_after_primary,
            "process_group_settled": group_settled,
            "process_group_absent_after_shutdown": absent_after_shutdown,
            "descendant_ownership": ("outer_group_only" if needs_settlement else
                                     "cooperative_engine_exit"),
            "raw_complete": (not needs_settlement and eof and not overflow and group_settled and
                             absent_after_shutdown is True and process.poll() is not None),
            "shutdown_allowance_seconds": SHUTDOWN_ALLOWANCE_SECONDS if needs_settlement else 0,
        }
    except BaseException:
        # This also covers a failure while registering pipes after Popen returned.
        # Signals during launch only set pending_signal, so the handle is assigned
        # before any interruption path can unwind this scope.
        if process is not None:
            GitRunner._settle(process, time.monotonic() + SHUTDOWN_ALLOWANCE_SECONDS)
        raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if selector is not None:
            selector.close()
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def workflow_stage(operation: str, fields: dict[str, str], index: dict[str, Any],
                   index_path: Path, prior_sha: str) -> dict[str, Any]:
    command = [sys.executable, "-m", "tools.fork_maintenance", operation,
               *workflow_arguments(operation, fields)]
    stage_dir = index_path.parent / f"{index['run_id']}-{operation}"
    stage_dir.mkdir(mode=0o700, exist_ok=False)
    index["stages"][operation] = {"status": "in_progress", "direct_rc": None,
                                  "evidence_dir": str(stage_dir)}
    workflow_save_index(index_path, index, prior_sha)
    supervised = workflow_supervise(command)
    stdout_path, stderr_path = stage_dir / "stdout.raw", stage_dir / "stderr.raw"
    exclusive_write_bytes(stdout_path, supervised["stdout"])
    exclusive_write_bytes(stderr_path, supervised["stderr"])
    if supervised["timed_out"] and not supervised["raw_complete"]:
        outcome = {"status": "uncertain", "error": "outer stage timeout left incomplete evidence or owner"}
    elif supervised["timed_out"]:
        outcome = {"status": "timeout", "error": "outer workflow stage deadline elapsed",
                   "stage_timeout_seconds": DEFAULT_ATTEMPT_TIMEOUT + 30}
    elif supervised["interrupted"]:
        outcome = {"status": "interrupted", "error": supervised["interrupted"]}
    elif not supervised["raw_complete"] or supervised["group_absent_after_primary"] is not True:
        outcome = {"status": "uncertain", "error": "outer stage group or pipe settlement is uncertain"}
    else:
        try:
            outcome = json.loads((supervised["stdout"] if supervised["direct_rc"] == 0
                                  else supervised["stderr"]).decode())
        except (UnicodeError, json.JSONDecodeError):
            outcome = {"status": "unparseable"}
    return {"status": outcome.get("status"), "direct_rc": supervised["direct_rc"],
            "stdout_path": str(stdout_path), "stdout_sha256": sha256_bytes(supervised["stdout"]),
            "stderr_path": str(stderr_path), "stderr_sha256": sha256_bytes(supervised["stderr"]),
            "settlement": {key: value for key, value in supervised.items()
                           if key not in {"stdout", "stderr"}}, "outcome": outcome}


def workflow_replay_apply(index: dict[str, Any], index_path: Path,
                          pin: str, prior_sha: str) -> None:
    config = index["config"]["apply"]
    result_path = Path(config["evidence_dir"]) / "result.json"
    lock_path = Path(config["evidence_dir"]) / "attempt.lock"
    if os.path.lexists(lock_path) or not result_path.is_file():
        raise MaintenanceError("apply ownership or result is uncertain; refusing composite replay")
    command = [sys.executable, "-m", "tools.fork_maintenance", "apply",
               *workflow_arguments("apply", {**config, "expected_plan_sha256": pin})]
    supervised = workflow_supervise(command)
    if supervised["timed_out"] and not supervised["raw_complete"]:
        response = {"status": "uncertain", "error": "outer replay timeout left incomplete evidence or owner"}
    elif supervised["timed_out"]:
        response = {"status": "timeout", "error": "outer workflow replay deadline elapsed",
                    "stage_timeout_seconds": DEFAULT_ATTEMPT_TIMEOUT + 30}
    elif supervised["interrupted"]:
        response = {"status": "interrupted", "error": supervised["interrupted"]}
    elif not supervised["raw_complete"] or supervised["group_absent_after_primary"] is not True:
        response = {"status": "uncertain", "error": "outer replay group or pipe settlement is uncertain"}
    else:
        try:
            response = json.loads((supervised["stdout"] if supervised["direct_rc"] == 0
                                   else supervised["stderr"]).decode())
        except (UnicodeError, json.JSONDecodeError):
            response = {"status": "unparseable"}
    if (supervised["direct_rc"] == 0 and response.get("status") == "replayed" and
            supervised["raw_complete"] and supervised["group_absent_after_primary"] is True):
        return
    failure_dir = index_path.parent / f"{index['run_id']}-replay-failure"
    failure_dir.mkdir(mode=0o700, exist_ok=False)
    stdout_path, stderr_path = failure_dir / "stdout.raw", failure_dir / "stderr.raw"
    exclusive_write_bytes(stdout_path, supervised["stdout"])
    exclusive_write_bytes(stderr_path, supervised["stderr"])
    index["replay_failure"] = {
        "direct_rc": supervised["direct_rc"], "outcome": response,
        "stdout_path": str(stdout_path), "stdout_sha256": sha256_bytes(supervised["stdout"]),
        "stderr_path": str(stderr_path), "stderr_sha256": sha256_bytes(supervised["stderr"]),
        "settlement": {key: value for key, value in supervised.items()
                       if key not in {"stdout", "stderr"}},
    }
    workflow_save_index(index_path, index, prior_sha)
    raise MaintenanceError("apply replay refused; retained raw evidence requires inspection")


def workflow_regular_bytes(path: Path, label: str) -> bytes:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_HTTP_BYTES:
            raise MaintenanceError(f"{label} is not a bounded regular file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                             getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            observed = os.fstat(stream.fileno())
            if not stat.S_ISREG(observed.st_mode) or observed.st_size > MAX_HTTP_BYTES:
                raise MaintenanceError(f"{label} changed file type or size")
            content = stream.read(MAX_HTTP_BYTES + 1)
    except OSError as exc:
        raise MaintenanceError(f"{label} cannot be read: {exc}") from exc
    if len(content) > MAX_HTTP_BYTES:
        raise MaintenanceError(f"{label} exceeds byte limit")
    return content


def workflow_validate_currency_subject(boundary: str, identity: dict[str, str],
                                       index: dict[str, Any]) -> None:
    fields = {
        "publication": {"result_commit", "result_tree", "gate_receipt_path",
                        "gate_receipt_sha256", "review_receipt_path", "review_receipt_sha256"},
        "integration": {"published_ref", "published_commit", "publication_receipt_path",
                        "publication_receipt_sha256", "consumer_plan_path",
                        "consumer_plan_sha256"},
        "completion": {"consumer_result_commit", "consumer_result_path",
                       "consumer_result_sha256"},
    }
    if set(identity) != fields[boundary]:
        raise MaintenanceError("currency boundary subject identities are incomplete or unknown")
    if boundary == "publication":
        result = read_result(Path(index["config"]["apply"]["evidence_dir"]) / "result.json")
        if (identity["result_commit"] != result.get("result_commit") or
                identity["result_tree"] != result.get("result_tree")):
            raise MaintenanceError("publication subject differs from applied output")
        if identity["gate_receipt_path"] == identity["review_receipt_path"]:
            raise MaintenanceError("publication gate and review receipts must be distinct")
    if boundary == "integration":
        result = read_result(Path(index["config"]["apply"]["evidence_dir"]) / "result.json")
        if (identity["published_commit"] != result.get("result_commit") or
                not identity["published_ref"].startswith(("refs/heads/", "refs/tags/")) or
                any(char.isspace() for char in identity["published_ref"])):
            raise MaintenanceError("integration subject does not bind applied output and immutable ref")
    for key in ("published_commit", "consumer_result_commit"):
        if key in identity:
            require_sha(identity[key], f"currency {key}")
    for key in tuple(identity):
        if not key.endswith("_path"):
            continue
        digest_key = key.removesuffix("_path") + "_sha256"
        path = Path(identity[key])
        digest = identity[digest_key]
        if not path.is_absolute() or not DIGEST_RE.fullmatch(digest):
            raise MaintenanceError("currency external receipt requires absolute path and SHA-256")
        observed = sha256_bytes(workflow_regular_bytes(path, "currency external receipt"))
        if observed != digest:
            raise MaintenanceError("currency external receipt identity drift")


def workflow_currency(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Record a diagnostic boundary observation; never promote or rewrite a plan."""
    index_path = Path(args.evidence_index).resolve()
    index = workflow_load_index(index_path, args.run_id, currency=True)
    workflow_admit_paths(index["config"], index_path, Path(index["config_path"]),
                         args.run_id, len(index.get("currency_observations", [])) + 1)
    workflow_observe_checkpoints(index)
    if any(index.get(name) for name in (
        "observed_config_drift", "observed_environment_drift", "observed_checkpoint_drift"
    )):
        return {"run_id": args.run_id, **workflow_project(index, True)}, EXIT_REFUSED
    if index.get("terminal_stop") or index.get("currency_unknown") or index.get("replay_failure"):
        return {"run_id": args.run_id, **workflow_project(index, True)}, EXIT_REFUSED
    config = index["config"]
    preview_fields = config.get("preview")
    if not preview_fields or not index.get("plan_sha256"):
        raise MaintenanceError("currency requires a pinned workflow preview")
    plan, _ = load_and_validate_plan(Path(config["apply"]["plan"]), index["plan_sha256"])
    if (plan["candidate"]["commit"] != preview_fields["candidate"] or
            plan["selection"]["target_commit"] != index["target_attempts"][0]["target_commit"]):
        raise MaintenanceError("currency frozen candidate or initial target drift")
    if index["stages"].get("apply", {}).get("status") not in {"applied", "replayed"}:
        raise MaintenanceError("currency requires an applied output")
    if index.get("qualification_sidecar_sha256"):
        if workflow_validate_sidecar(index, plan) != index["qualification_sidecar_sha256"]:
            raise MaintenanceError("currency sidecar identity drift")
    elif config.get("qualification_sidecar"):
        raise MaintenanceError("currency requires qualified capability sidecar")
    subject_path = Path(args.subject)
    if not subject_path.is_absolute() or not DIGEST_RE.fullmatch(args.expected_subject_sha256):
        raise MaintenanceError("currency requires absolute subject and exact-byte SHA-256 pin")
    subject_bytes = workflow_regular_bytes(subject_path, "currency boundary subject")
    if sha256_bytes(subject_bytes) != args.expected_subject_sha256:
        raise MaintenanceError("currency boundary subject identity drift")
    subject = json.loads(subject_bytes)
    if (not isinstance(subject, dict) or set(subject) != {
            "schema_version", "boundary", "run_id", "plan_sha256", "target_commit", "identity"
    } or subject.get("schema_version") != 1 or subject.get("boundary") != args.boundary or
            subject.get("run_id") != args.run_id or
            subject.get("plan_sha256") != index["plan_sha256"] or
            subject.get("target_commit") != index["target_attempts"][-1]["target_commit"] or
            not isinstance(subject.get("identity"), dict) or not subject["identity"] or
            any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                for k, v in subject["identity"].items())):
        raise MaintenanceError("currency boundary subject is incomplete or mismatched")
    workflow_validate_currency_subject(args.boundary, subject["identity"], index)
    lock = Path(str(index_path) + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise MaintenanceError("workflow ownership lock remains present") from exc
    os.close(fd)
    try:
        prior_sha = sha256_bytes(index_path.read_bytes())
        # The saved result bytes alone cannot establish the current checkout.
        workflow_replay_apply(index, index_path, index["plan_sha256"], prior_sha)
        ordinal = len(index.get("currency_observations", [])) + 1
        observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        observation: dict[str, Any] = {
            "schema_version": 1, "run_id": args.run_id, "boundary": args.boundary,
            "observed_at": observed_at, "subject_path": str(subject_path),
            "subject_sha256": args.expected_subject_sha256, "subject_identity": subject["identity"],
            "plan_sha256": index["plan_sha256"],
            "comparison_target": index["target_attempts"][-1]["target_commit"],
            "promotion_authorized": False,
        }
        runner: GitRunner | None = None
        try:
            if plan["selection"]["mode"] == "override":
                observation["selection"] = plan["selection"]
                observation["release_bindings"] = {"mode": "fixed_override"}
            else:
                with terminal_scratch("graphify-fork-currency-") as scratch:
                    home = Path(scratch.path) / "home"
                    home.mkdir()
                    runner = GitRunner(subprocess_timeout=DEFAULT_SUBPROCESS_TIMEOUT,
                                       deadline=time.monotonic() + DEFAULT_ATTEMPT_TIMEOUT,
                                       home=home)
                    currency_args = argparse.Namespace(**preview_fields)
                    currency_args.github_releases_url = preview_fields.get(
                        "github_releases_url",
                        "https://api.github.com/repos/Graphify-Labs/graphify/releases?per_page=100&page=1")
                    currency_args.pypi_base_url = preview_fields.get("pypi_base_url", "https://pypi.org")
                    currency_args.github_releases_fixture = preview_fields.get("github_releases_fixture")
                    currency_args.pypi_fixture = preview_fields.get("pypi_fixture")
                    currency_args.network_timeout = 15.0
                    currency_args.max_pages = 20
                    selection, bindings = select_release(currency_args, runner,
                                                         config["apply"]["upstream_url"])
                    observation["selection"] = selection
                    observation["release_bindings"] = bindings
                    observation["fixture_bindings"] = workflow_fixture_bindings(config)
        except (MaintenanceError, OSError, UnicodeError, json.JSONDecodeError,
                subprocess.SubprocessError) as exc:
            observation["status"] = "unknown"
            observation["error"] = str(exc)
        if runner is not None:
            command_dir = index_path.parent / f"{args.run_id}-currency-{ordinal}-commands"
            persist_command_evidence(runner.records, command_dir, time.monotonic() + 30)
            observation["command_records"] = runner.records
        if observation.get("status") != "unknown":
            target = observation["selection"]["target_commit"]
            comparison = observation["comparison_target"]
            compared_release = index["target_attempts"][-1].get(
                "release", plan["selection"].get("release"))
            observation["comparison_release"] = compared_release
            observation["status"] = (
                "current" if target == comparison and
                observation["selection"].get("release") == compared_release else "changed"
            )
        observation_path = index_path.parent / f"{args.run_id}-currency-{ordinal}.json"
        exclusive_write_bytes(observation_path, json_file_bytes(observation))
        reference = {"path": str(observation_path),
                     "sha256": sha256_bytes(observation_path.read_bytes()),
                     "boundary": args.boundary, "status": observation["status"],
                     "subject_sha256": args.expected_subject_sha256}
        index.setdefault("currency_observations", []).append(reference)
        if observation["status"] == "unknown":
            index["currency_unknown"] = reference
        if observation["status"] == "changed":
            target = observation["selection"]["target_commit"]
            index["target_invalidation"] = {
                "observation": reference, "invalidates": ["plan", "apply", "qualification_sidecar"],
                "approval_pin_required": True,
            }
            if len(index["target_attempts"]) >= 3:
                index["terminal_stop"] = "target_attempt_limit_exhausted"
            else:
                index["target_attempts"].append({"target_commit": target,
                                                  "release": observation["selection"].get("release"),
                                                  "required_by": reference,
                                                  "plan_sha256": None})
        workflow_save_index(index_path, index, prior_sha)
        code = 0 if observation["status"] == "current" and not index.get("target_invalidation") else EXIT_REFUSED
        return {"run_id": args.run_id, "currency": reference,
                **workflow_project(index, True), "target_attempts": index["target_attempts"]}, code
    finally:
        os.unlink(lock)


def workflow_execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    index_path = Path(args.evidence_index).resolve()
    if args.operation == "run":
        config_path = Path(args.workflow_config).resolve(strict=True)
        config_bytes = config_path.read_bytes()
        config = workflow_config(json.loads(config_bytes))
        run_id = sha256_bytes(canonical_json({"config": config, "path": str(config_path)}))
        workflow_admit_paths(config, index_path, config_path, run_id)
        if os.path.lexists(index_path):
            raise MaintenanceError("workflow index already exists; use resume")
        plan_path = Path(config["apply"]["plan"])
        pin = config.get("expected_plan_sha256")
        if plan_path.exists() and pin:
            planned, _ = load_and_validate_plan(plan_path, pin)
            workflow_validate_plan_config(planned, config)
        if not plan_path.exists() and "preview" not in config:
            raise MaintenanceError("plan absent and preview inputs unavailable")
        index = {"schema_version": WORKFLOW_SCHEMA_VERSION, "run_id": run_id,
                 "config": config, "config_path": str(config_path),
                 "config_file_sha256": sha256_bytes(config_bytes),
                 "immutable_config_sha256": sha256_bytes(canonical_json(config)),
                 "environment_identity": workflow_environment_identity(),
                 "preview_fixture_bindings": workflow_fixture_bindings(config),
                 "stages": {}, "target_attempts": []}
        exclusive_write_bytes(index_path, workflow_index_bytes(index))
    else:
        run_id = args.run_id
        index = workflow_load_index(index_path, run_id, currency=True, allow_config_drift=True)
        config = index["config"]
        workflow_admit_paths(config, index_path, Path(index["config_path"]), run_id)
        workflow_observe_checkpoints(index)
        if any(index.get(name) for name in (
            "observed_config_drift", "observed_environment_drift", "observed_checkpoint_drift"
        )):
            code = 0 if args.operation == "status" else EXIT_REFUSED
            return {"run_id": run_id, **workflow_project(index, True),
                    "stages": index["stages"], "target_attempts": index["target_attempts"]}, code
        if args.operation == "resume" and not (
            index.get("terminal_stop") or index.get("target_invalidation") or
            index.get("currency_unknown")
        ) and workflow_fixture_bindings(config) != index.get("preview_fixture_bindings", {}):
            raise MaintenanceError("workflow release fixture identity drift")
    if args.operation == "status":
        plan_path = Path(config["apply"]["plan"])
        observed = plan_path.exists()
        try:
            observed_fixtures = workflow_fixture_bindings(config)
            fixtures_current = observed_fixtures == index.get("preview_fixture_bindings", {})
            if not fixtures_current and index.get("currency_observations"):
                latest = index["currency_observations"][-1]
                latest_receipt = json.loads(workflow_regular_bytes(
                    Path(latest["path"]), "currency observation"))
                fixtures_current = (
                    latest_receipt.get("status") == "current" and
                    latest_receipt.get("fixture_bindings") == observed_fixtures
                )
        except (MaintenanceError, OSError):
            fixtures_current = False
        if not fixtures_current and not (
            index.get("target_invalidation") or index.get("currency_unknown") or
            index.get("terminal_stop")
        ):
            return {"run_id": run_id, "status": "stopped",
                    "next_action": "recheck_currency", "stopped_reason": "release_fixture_drift",
                    "stages": index["stages"], "target_attempts": index["target_attempts"],
                    "progress": workflow_progress(index)}, 0
        if index.get("plan_sha256") and (
            not observed or sha256_bytes(plan_path.read_bytes()) != index["plan_sha256"]
        ):
            return {"run_id": run_id, "status": "stopped",
                    "next_action": "inspect_plan_drift", "stopped_reason": "plan_pin_drift",
                    "stages": index["stages"], "target_attempts": index["target_attempts"],
                    "progress": workflow_progress(index)}, 0
        sidecar_name = config.get("qualification_sidecar")
        if index.get("qualification_sidecar_sha256") and (
            not sidecar_name or not Path(sidecar_name).exists() or
            sha256_bytes(Path(sidecar_name).read_bytes()) != index["qualification_sidecar_sha256"]
        ):
            return {"run_id": run_id, "status": "stopped",
                    "next_action": "inspect_sidecar_drift", "stopped_reason": "sidecar_drift",
                    "stages": index["stages"], "target_attempts": index["target_attempts"],
                    "progress": workflow_progress(index)}, 0
        if index.get("qualification_sidecar_sha256"):
            try:
                frozen_plan, _ = load_and_validate_plan(plan_path, index["plan_sha256"])
                workflow_validate_sidecar(index, frozen_plan)
            except MaintenanceError:
                return {"run_id": run_id, "status": "stopped",
                        "next_action": "inspect_sidecar_drift", "stopped_reason": "sidecar_evidence_drift",
                        "stages": index["stages"], "target_attempts": index["target_attempts"],
                        "progress": workflow_progress(index)}, 0
        if os.path.lexists(Path(config["apply"]["evidence_dir"]) / "attempt.lock"):
            return {"run_id": run_id, "status": "stopped",
                    "next_action": "inspect_retained_lock", "stopped_reason": "apply_ownership_uncertain",
                    "stages": index["stages"], "target_attempts": index["target_attempts"],
                    "progress": workflow_progress(index)}, 0
        return {"run_id": run_id, **workflow_project(index, observed), "stages": index["stages"],
                "target_attempts": index["target_attempts"]}, 0
    lock = Path(str(index_path) + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise MaintenanceError("workflow ownership lock remains present") from exc
    os.close(fd)
    try:
        prior_sha = sha256_bytes(index_path.read_bytes())
        stages = index["stages"]
        plan_path = Path(config["apply"]["plan"])
        if any(stage in stages and (
            stages[stage].get("direct_rc") != 0 or
            stages[stage].get("status") not in (
                {"planned"} if stage == "preview" else {"applied", "replayed"}
            )
        ) for stage in ("preview", "apply")):
            return {"run_id": run_id, **workflow_project(index, plan_path.exists())}, EXIT_REFUSED
        if index.get("replay_failure"):
            return {"run_id": run_id, **workflow_project(index, plan_path.exists())}, EXIT_REFUSED
        if index.get("terminal_stop") or index.get("target_invalidation") or index.get("currency_unknown"):
            return {"run_id": run_id, **workflow_project(index, plan_path.exists())}, EXIT_REFUSED
        if not plan_path.exists() and "preview" not in stages:
            if "preview" not in config:
                raise MaintenanceError("plan absent and preview inputs unavailable")
            receipt = workflow_stage("preview", config["preview"], index, index_path,
                                     prior_sha)
            if receipt["direct_rc"] == 0 and receipt["status"] == "planned" and plan_path.is_file():
                receipt["checkpoint"] = {
                    "input_sha256": workflow_checkpoint_input("preview", index),
                    "output_sha256": sha256_bytes(plan_path.read_bytes()),
                }
            stages["preview"] = receipt
            workflow_save_index(index_path, index, sha256_bytes(index_path.read_bytes()))
            prior_sha = sha256_bytes(index_path.read_bytes())
            if receipt["direct_rc"] != 0 or receipt["status"] != "planned":
                return {"run_id": run_id, **workflow_project(index, plan_path.exists())}, EXIT_REFUSED
            if workflow_fixture_bindings(config) != index["preview_fixture_bindings"]:
                raise MaintenanceError("workflow release fixture drifted during preview")
        pin = getattr(args, "expected_plan_sha256", None) or config.get("expected_plan_sha256")
        if (getattr(args, "expected_plan_sha256", None) and config.get("expected_plan_sha256")
                and args.expected_plan_sha256 != config["expected_plan_sha256"]):
            raise MaintenanceError("resume pin differs from immutable workflow configuration")
        if not pin:
            return {"run_id": run_id, **workflow_project(index, plan_path.exists())}, EXIT_REFUSED
        if not DIGEST_RE.fullmatch(pin):
            raise MaintenanceError("external plan pin is invalid")
        plan, observed_pin = load_and_validate_plan(plan_path, pin)
        workflow_validate_plan_config(plan, config)
        if index.get("plan_sha256") not in (None, observed_pin):
            raise MaintenanceError("workflow plan pin changed")
        index["plan_sha256"] = observed_pin
        if not index["target_attempts"]:
            index["target_attempts"].append({"target_commit": plan["selection"]["target_commit"],
                                             "release": plan["selection"].get("release"),
                                             "plan_sha256": observed_pin})
        if "apply" not in stages:
            receipt = workflow_stage("apply", {**config["apply"],
                "expected_plan_sha256": observed_pin}, index, index_path, prior_sha)
            result_path = Path(config["apply"]["evidence_dir"]) / "result.json"
            if (receipt["direct_rc"] == 0 and receipt["status"] in {"applied", "replayed"}
                    and result_path.is_file()):
                receipt["checkpoint"] = {
                    "input_sha256": workflow_checkpoint_input("apply", index, plan),
                    "output_sha256": sha256_bytes(result_path.read_bytes()),
                }
            stages["apply"] = receipt
            workflow_save_index(index_path, index, sha256_bytes(index_path.read_bytes()))
            if receipt["direct_rc"] != 0 or receipt["status"] not in {"applied", "replayed"}:
                return {"run_id": run_id, **workflow_project(index, plan_path.exists())}, EXIT_REFUSED
            prior_sha = sha256_bytes(index_path.read_bytes())
        else:
            workflow_replay_apply(index, index_path, observed_pin, prior_sha)
        if index.get("qualification_sidecar_sha256"):
            current = workflow_validate_sidecar(index, plan)
            if current != index["qualification_sidecar_sha256"]:
                raise MaintenanceError("ticket788 sidecar identity drift")
        elif config.get("qualification_sidecar") and Path(config["qualification_sidecar"]).exists():
            index["qualification_sidecar_sha256"] = workflow_validate_sidecar(index, plan)
            workflow_save_index(index_path, index, prior_sha)
        return {"run_id": run_id, **workflow_project(index, plan_path.exists())}, EXIT_REFUSED
    finally:
        os.unlink(lock)


def build_parser() -> argparse.ArgumentParser:
    parser = TerminalArgumentParser(
        description="Preview, apply, or inspect a bounded Graphify fork-maintenance workflow."
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    preview_parser = subparsers.add_parser(
        "preview", help="write a frozen, non-mutating plan"
    )
    preview_parser.add_argument("--source-repo", required=True)
    preview_parser.add_argument("--candidate", required=True)
    preview_parser.add_argument("--upstream-repository", required=True)
    preview_parser.add_argument("--upstream-url", required=True)
    preview_parser.add_argument("--output-plan", required=True)
    selection = preview_parser.add_mutually_exclusive_group()
    selection.add_argument("--override-sha")
    selection.add_argument(
        "--github-releases-url",
        default=("https://api.github.com/repos/Graphify-Labs/graphify/releases?per_page=100&page=1"),
    )
    preview_parser.add_argument("--override-reason")
    preview_parser.add_argument("--pypi-base-url", default="https://pypi.org")
    preview_parser.add_argument("--github-releases-fixture")
    preview_parser.add_argument("--pypi-fixture")
    preview_parser.add_argument("--network-timeout", type=float, default=15.0)
    preview_parser.add_argument("--max-pages", type=int, default=20)
    add_shared_limits(preview_parser)
    apply_parser = subparsers.add_parser("apply", help="apply an existing frozen plan")
    apply_parser.add_argument("--plan", required=True)
    apply_parser.add_argument("--expected-plan-sha256", required=True)
    apply_parser.add_argument("--committer-name", required=True)
    apply_parser.add_argument("--committer-email", required=True)
    apply_parser.add_argument("--source-repo", required=True)
    apply_parser.add_argument("--upstream-url", required=True)
    apply_parser.add_argument("--output-worktree", required=True)
    apply_parser.add_argument("--branch", required=True)
    apply_parser.add_argument("--evidence-dir", required=True)
    add_shared_limits(apply_parser)
    run_parser = subparsers.add_parser("run", help="start a bounded fork-maintenance workflow")
    run_parser.add_argument("--workflow-config", required=True)
    run_parser.add_argument("--evidence-index", required=True)
    resume_parser = subparsers.add_parser("resume", help="continue a stopped workflow")
    resume_parser.add_argument("--evidence-index", required=True)
    resume_parser.add_argument("--run-id", required=True)
    resume_parser.add_argument("--expected-plan-sha256")
    status_parser = subparsers.add_parser("status", help="read a workflow projection")
    status_parser.add_argument("--evidence-index", required=True)
    status_parser.add_argument("--run-id", required=True)
    status_parser.add_argument("--json", action="store_true")
    currency_parser = subparsers.add_parser(
        "currency", help="record a bounded diagnostic boundary currency observation")
    currency_parser.add_argument("--evidence-index", required=True)
    currency_parser.add_argument("--run-id", required=True)
    currency_parser.add_argument("--boundary", choices=("publication", "integration", "completion"),
                                 required=True)
    currency_parser.add_argument("--subject", required=True)
    currency_parser.add_argument("--expected-subject-sha256", required=True)
    return parser


def positive_number(value: float, label: str) -> None:
    if not math.isfinite(value) or value <= 0.0:
        raise MaintenanceError(f"{label} must be finite and positive")


def main(argv: Sequence[str] | None = None) -> int:
    global SIGNAL_CONTROLLER
    try:
        args = build_parser().parse_args(argv)
    except TerminalTransportError:
        return EXIT_REFUSED
    if args.operation in {"run", "resume", "status", "currency"}:
        try:
            outcome, code = workflow_currency(args) if args.operation == "currency" else workflow_execute(args)
        except (MaintenanceError, OSError, UnicodeError, json.JSONDecodeError,
                subprocess.SubprocessError) as exc:
            outcome = {"status": "refused", "error": str(exc)}
            code = EXIT_REFUSED
        try:
            write_terminal(json.dumps(outcome, sort_keys=True),
                           stream=sys.stdout if code == 0 else sys.stderr)
        except TerminalTransportError:
            return EXIT_REFUSED
        return code
    installed: dict[int, Any] = {}
    controller = CatchableSignalController()
    SIGNAL_CONTROLLER = controller

    def request_stop(signum: int, _frame: Any) -> None:
        controller.handle(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(signum)
        if previous is not signal.SIG_IGN:
            installed[signum] = previous
            signal.signal(signum, request_stop)
    try:
        try:
            positive_number(args.subprocess_timeout, "--subprocess-timeout")
            positive_number(args.attempt_timeout, "--attempt-timeout")
            if args.operation == "preview":
                positive_number(args.network_timeout, "--network-timeout")
            result = preview(args) if args.operation == "preview" else apply_plan(args)
        except CaughtSignal as exc:
            context = exc.__context__
            if isinstance(context, MaintenanceError):
                outcome = {**context.details, "status": context.status, "error": str(context)}
                write_terminal(json.dumps(outcome, sort_keys=True), stream=sys.stderr)
                return (
                    EXIT_TIMEOUT
                    if context.status in {"timeout", "interrupted"}
                    else EXIT_REFUSED
                )
            if isinstance(context, (OSError, UnicodeError, subprocess.SubprocessError)):
                outcome = {
                    "status": "refused",
                    "error": f"filesystem operation failed: {context}",
                }
                write_terminal(json.dumps(outcome, sort_keys=True), stream=sys.stderr)
                return EXIT_REFUSED
            if isinstance(context, Exception):
                raise context
            outcome = {
                "status": "interrupted",
                "signal": signal.Signals(exc.signum).name,
                "error": f"interrupted by {signal.Signals(exc.signum).name}",
            }
            write_terminal(json.dumps(outcome, sort_keys=True), stream=sys.stderr)
            return EXIT_TIMEOUT
        except MaintenanceError as exc:
            if exc.terminal_deadline is not None:
                try:
                    check_deadline(exc.terminal_deadline, "terminal failure response delivery")
                except MaintenanceError as terminal_exc:
                    outcome = {
                        **exc.details,
                        "status": "evidence_error",
                        "primary_status": exc.details.get("primary_status", exc.status),
                        "primary_error": exc.details.get("primary_error", str(exc)),
                        "error": str(terminal_exc),
                    }
                    write_terminal(json.dumps(outcome, sort_keys=True), stream=sys.stderr)
                    return EXIT_REFUSED
                assert exc.encoded is not None
                write_terminal(exc.encoded, stream=sys.stderr)
                try:
                    check_deadline(exc.terminal_deadline, "terminal failure output delivery")
                except MaintenanceError as terminal_exc:
                    # The original bytes may already have reached the caller. Report
                    # late-return uncertainty once, without retrying delivery or
                    # extending the owner's deadline, and retain its original cause.
                    outcome = {
                        **exc.details,
                        "status": "evidence_error",
                        "primary_status": exc.details.get("primary_status", exc.status),
                        "primary_error": exc.details.get("primary_error", str(exc)),
                        "terminal_delivery": "late_return",
                        "error": str(terminal_exc),
                    }
                    write_terminal(json.dumps(outcome, sort_keys=True), stream=sys.stderr)
                    return EXIT_REFUSED
                return EXIT_TIMEOUT if exc.status in {"timeout", "interrupted"} else EXIT_REFUSED
            outcome = {**exc.details, "status": exc.status, "error": str(exc)}
            write_terminal(json.dumps(outcome, sort_keys=True), stream=sys.stderr)
            return EXIT_TIMEOUT if exc.status in {"timeout", "interrupted"} else EXIT_REFUSED
        except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
            outcome = {"status": "refused", "error": f"filesystem operation failed: {exc}"}
            write_terminal(json.dumps(outcome, sort_keys=True), stream=sys.stderr)
            return EXIT_REFUSED
        if isinstance(result, TerminalResponse):
            try:
                check_deadline(result.deadline, "terminal response delivery")
            except MaintenanceError as exc:
                owned = bool(result.expiry_details.get("attempt_owned"))
                outcome = {
                    **result.expiry_details,
                    "status": "evidence_error" if owned else "timeout",
                    "error": str(exc),
                }
                write_terminal(json.dumps(outcome, sort_keys=True), stream=sys.stderr)
                return EXIT_REFUSED if owned else EXIT_TIMEOUT
            assert result.encoded is not None
            encoded = result.encoded
        else:
            encoded = json.dumps(result, sort_keys=True)
        status = result.get("status")
        if status in {"operational_failure", "timeout", "interrupted"}:
            write_terminal(encoded, stream=sys.stderr)
            return EXIT_TIMEOUT if status in {"timeout", "interrupted"} else EXIT_REFUSED
        write_terminal(encoded, stream=sys.stdout)
        if isinstance(result, TerminalResponse):
            try:
                check_deadline(result.deadline, "terminal output delivery")
            except MaintenanceError as exc:
                owned = bool(result.expiry_details.get("attempt_owned"))
                outcome = {
                    **result.expiry_details,
                    "status": "evidence_error" if owned else "timeout",
                    "error": str(exc),
                }
                write_terminal(json.dumps(outcome, sort_keys=True), stream=sys.stderr)
                return EXIT_REFUSED if owned else EXIT_TIMEOUT
        return EXIT_CONFLICT if status == "conflict" else 0
    except TerminalTransportError:
        # Finalization already owns any durable evidence or publication. A failed
        # recipient cannot receive another diagnostic; never retry or rerun work.
        return EXIT_REFUSED
    finally:
        for signum, previous in installed.items():
            signal.signal(signum, previous)
        SIGNAL_CONTROLLER = None


if __name__ == "__main__":
    raise SystemExit(main())
