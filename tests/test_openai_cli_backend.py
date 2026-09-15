"""Tests for the openai-cli backend (_call_openai_cli): argv contract only.

No network, no Codex binary: shutil.which and subprocess.run are monkeypatched.
"""
import json

import pytest

from graphify import llm


class _Captured:
    def __init__(self):
        self.args = None
        self.kwargs = None
        self.calls = []


def _override_names(args):
    """Pull the `<name>`s out of any `-c mcp_servers.<name>.enabled=false` pairs."""
    names = []
    it = iter(args)
    for tok in it:
        if tok == "-c":
            val = next(it, "")
            if val.startswith("mcp_servers.") and val.endswith(".enabled=false"):
                names.append(val[len("mcp_servers."):-len(".enabled=false")])
    return names


def _reset_mcp_cache(monkeypatch):
    """The in-process memoization is module-global state -- without this, one
    test's cached verdict for (codex_cmd, cwd[, names]) leaks into the next
    test that happens to share the same key (e.g. the default `servers`),
    silently skipping that test's own fake subprocess calls."""
    monkeypatch.setattr(llm, "_CODEX_MCP_NAMES_CACHE", {})
    monkeypatch.setattr(llm, "_CODEX_MCP_RESOLVE_CACHE", {})


def _arm(monkeypatch, response=None, servers=("graphify", "docs"), unresolvable=(), probe_raises=False):
    """Fake a Codex CLI: `mcp list --json` (no `-c`) returns `servers`; the SAME
    subcommand WITH `-c mcp_servers.<name>.enabled=false` overrides is the
    resolvability probe `_codex_resolvable_disable_args` adds -- it fails (rc 1)
    if any named override is in `unresolvable`, or raises if `probe_raises`.
    `exec` writes the -o file the way codex exec does.
    """
    _reset_mcp_cache(monkeypatch)
    cap = _Captured()
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)

    class P:
        def __init__(self, stdout="", returncode=0):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(args, **kwargs):
        cap.calls.append(list(args))
        if "mcp" in args and "list" in args:
            overrides = _override_names(args)
            if overrides:
                if probe_raises:
                    raise TimeoutError("codex mcp list probe hung")
                if any(n in unresolvable for n in overrides):
                    return P("", returncode=1)
            return P(json.dumps([{"name": n, "enabled": True} for n in servers]))
        cap.args = list(args)
        cap.kwargs = kwargs
        # write the -o file the way codex exec does
        out_idx = args.index("-o") + 1
        payload = response if response is not None else {"nodes": [{"id": "f", "type": "function"}], "edges": []}
        with open(args[out_idx], "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return P()

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", fake_run)
    return cap


def test_argv_contract_defaults(monkeypatch):
    monkeypatch.delenv("GRAPHIFY_OPENAI_CLI_MODEL", raising=False)
    monkeypatch.delenv("GRAPHIFY_OPENAI_CLI_EFFORT", raising=False)
    cap = _arm(monkeypatch)
    llm._call_openai_cli("def f(): pass", max_tokens=64)
    a = cap.args
    assert a[0] == "/usr/bin/codex" and a[1] == "exec"
    assert "--skip-git-repo-check" in a and "--json" in a
    assert a[a.index("--sandbox") + 1] == "read-only"
    assert a[a.index("--model") + 1] == "gpt-5.6-sol"          # default model
    assert "model_reasoning_effort=ultra" in a                  # default effort
    assert a[-1] == "-"                                         # prompt via stdin
    assert cap.kwargs.get("input")                              # not argv (MAX_ARG_STRLEN)


def test_every_configured_mcp_server_is_disabled_per_call(monkeypatch):
    """A blanket `mcp_servers={}` is merged away by Codex; per-server `enabled` works."""
    cap = _arm(monkeypatch, servers=("graphify", "docs"))
    llm._call_openai_cli("def f(): pass", max_tokens=64)
    a = cap.args
    assert "mcp_servers.graphify.enabled=false" in a
    assert "mcp_servers.docs.enabled=false" in a
    assert "mcp_servers={}" not in a
    # the server list came from Codex itself, no hardcoded names
    assert any("mcp" in c and "list" in c and "--json" in c for c in cap.calls)


def test_no_configured_servers_adds_no_overrides(monkeypatch):
    cap = _arm(monkeypatch, servers=())
    llm._call_openai_cli("def f(): pass", max_tokens=64)
    assert not [x for x in cap.args if str(x).startswith("mcp_servers.")]


def test_unresolvable_server_does_not_veto_the_others(monkeypatch):
    """A plugin-provided (or wrong-cwd) name Codex cannot resolve here must not
    take the whole extraction down, and must not silence overrides for names
    that DO resolve -- a mixed list, not all-or-nothing."""
    cap = _arm(monkeypatch, servers=("graphify", "exa"), unresolvable=("exa",))
    llm._call_openai_cli("def f(): pass", max_tokens=64)
    a = cap.args
    assert "mcp_servers.graphify.enabled=false" in a
    assert not any(str(x).startswith("mcp_servers.exa") for x in a)
    # the combined probe failed, so the fallback had to probe each name alone
    probe_calls = [c for c in cap.calls if "mcp" in c and "list" in c and _override_names(c)]
    assert len(probe_calls) >= 2


def test_all_unresolvable_still_runs_the_extraction(monkeypatch):
    """Nothing resolves here (e.g. every server is plugin-provided) -- the run
    must still happen, just with no MCP servers disabled."""
    cap = _arm(monkeypatch, servers=("exa", "other-plugin"), unresolvable=("exa", "other-plugin"))
    result = llm._call_openai_cli("def f(): pass", max_tokens=64)
    assert not [x for x in cap.args if str(x).startswith("mcp_servers.")]
    assert result.get("nodes")  # codex exec still ran and produced output


def test_single_unresolvable_server_skips_the_redundant_reprobe(monkeypatch):
    """With exactly one candidate, the combined probe already answers for it --
    no second identical probe is needed."""
    cap = _arm(monkeypatch, servers=("exa",), unresolvable=("exa",))
    llm._call_openai_cli("def f(): pass", max_tokens=64)
    assert not [x for x in cap.args if str(x).startswith("mcp_servers.")]
    probe_calls = [c for c in cap.calls if "mcp" in c and "list" in c and _override_names(c)]
    assert len(probe_calls) == 1


def test_validation_probe_exception_excludes_rather_than_raises(monkeypatch):
    """Every failure mode of the resolvability probe itself -- here, a raised
    exception, e.g. a timeout -- must return [] for the affected names and never
    propagate out of the CLI call."""
    cap = _arm(monkeypatch, servers=("graphify", "docs"), probe_raises=True)
    llm._call_openai_cli("def f(): pass", max_tokens=64)  # must not raise
    assert not [x for x in cap.args if str(x).startswith("mcp_servers.")]


def test_repeated_call_same_cwd_pays_no_further_probe_cost(monkeypatch):
    """The SECOND `_call_openai_cli` in the same process and working directory
    must not re-list or re-probe -- the listing + fallback probes from the
    first call cost 4 subprocess spawns here (list, combined-fail, 2 singles);
    the second call must add zero more of those, only its own exec call."""
    cap = _arm(monkeypatch, servers=("graphify", "exa"), unresolvable=("exa",))
    llm._call_openai_cli("def f(): pass", max_tokens=64)
    first_mcp_calls = [c for c in cap.calls if "mcp" in c and "list" in c]
    assert len(first_mcp_calls) >= 3  # sanity: the first call really did probe

    cap.calls.clear()  # isolate what the SECOND call does
    llm._call_openai_cli("def f(): pass", max_tokens=64)
    assert not [c for c in cap.calls if "mcp" in c and "list" in c]
    assert cap.args is not None  # the exec call itself still ran


def test_different_cwd_is_not_served_the_first_cwds_answer(monkeypatch):
    """P2: the SAME override for the SAME name resolves differently by working
    directory. A cache blind to cwd would let the second cwd's call inherit the
    first cwd's verdict; this pins that it can't, at the two helpers directly
    (the seam the memoization is keyed on)."""
    _reset_mcp_cache(monkeypatch)
    calls = []

    class P:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def fake_run(args, **kwargs):
        cwd = kwargs.get("cwd")
        calls.append((list(args), cwd))
        # "graphify" only resolves from /repo-with-graphify.
        return P(0 if cwd == "/repo-with-graphify" else 1)

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", fake_run)

    names = ["graphify"]
    resolved_here = llm._codex_resolvable_disable_args("/usr/bin/codex", "/repo-with-graphify", names)
    resolved_elsewhere = llm._codex_resolvable_disable_args("/usr/bin/codex", "/tmp", names)
    assert "mcp_servers.graphify.enabled=false" in resolved_here
    assert "mcp_servers.graphify.enabled=false" not in resolved_elsewhere
    # both cwds were actually probed -- neither was served from the other's cache
    assert len(calls) == 2


class _P:
    """A bare rc-only fake process, for tests below that fake `subprocess.run`
    directly rather than through `_arm` -- they need to key behaviour off the
    exact SET of names in an override, which `_arm`'s single `unresolvable`
    membership test cannot express."""

    def __init__(self, returncode):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def test_survivor_set_that_codex_rejects_yields_no_overrides(monkeypatch):
    """Round 3: per-name resolvability does not prove the ASSEMBLED list is
    accepted. 'a' and 'b' each resolve alone; 'c' resolves nowhere; but the
    survivor set {'a','b'} together is rejected by this fake (a case P3 shows
    does not happen on this machine today, but the code must still guard
    against it). The function must return [], never the unvalidated survivor
    list -- the exact catastrophic failure this function exists to prevent,
    reached by a longer route."""
    _reset_mcp_cache(monkeypatch)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        overrides = set(_override_names(args))
        if overrides == {"a", "b"}:
            return _P(1)  # the assembled survivor SET is rejected
        if overrides in ({"a"}, {"b"}):
            return _P(0)  # each survives alone
        return _P(1)      # the full {"a","b","c"} combined probe, or anything with "c"

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", fake_run)

    result = llm._codex_resolvable_disable_args("/usr/bin/codex", "/repo", ["a", "b", "c"])
    assert result == []
    # the survivor set really was probed AS A SET, not assumed from its parts
    assert any(set(_override_names(c)) == {"a", "b"} for c in calls)


def test_survivor_set_that_codex_accepts_is_returned(monkeypatch):
    """Mirror of the above: when the assembled survivor set IS accepted, it is
    returned -- the new guard does not needlessly zero out a good answer."""
    _reset_mcp_cache(monkeypatch)

    def fake_run(args, **kwargs):
        overrides = set(_override_names(args))
        if "c" in overrides:
            return _P(1)  # "c" never resolves, alone or combined
        return _P(0)      # {"a","b"} together, or "a"/"b" alone, all resolve

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", fake_run)

    result = llm._codex_resolvable_disable_args("/usr/bin/codex", "/repo", ["a", "b", "c"])
    assert "mcp_servers.a.enabled=false" in result
    assert "mcp_servers.b.enabled=false" in result
    assert not any(str(x).startswith("mcp_servers.c") for x in result)


def test_success_path_gains_no_extra_probe(monkeypatch):
    """When the combined probe over ALL names passes immediately, the new
    survivor-set validation must not run at all -- that list was already
    validated as a whole by construction, and re-probing it would double the
    cost of the already-good case."""
    _reset_mcp_cache(monkeypatch)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return _P(0)

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", fake_run)

    result = llm._codex_resolvable_disable_args("/usr/bin/codex", "/repo", ["a", "b"])
    assert "mcp_servers.a.enabled=false" in result
    assert "mcp_servers.b.enabled=false" in result
    assert len(calls) == 1  # exactly the one combined probe -- nothing more


def test_empty_survivor_set_costs_no_extra_probe(monkeypatch):
    """Nothing survived the per-name fallback pass -- there is nothing to
    validate, so no additional subprocess call is made beyond the combined
    probe and the per-name fallback probes themselves."""
    _reset_mcp_cache(monkeypatch)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return _P(1)  # everything fails, always

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", fake_run)

    result = llm._codex_resolvable_disable_args("/usr/bin/codex", "/repo", ["a", "b"])
    assert result == []
    # combined probe (1) + per-name "a" (1) + per-name "b" (1) = 3; no 4th
    assert len(calls) == 3


def test_argv_env_overrides(monkeypatch):
    monkeypatch.setenv("GRAPHIFY_OPENAI_CLI_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("GRAPHIFY_OPENAI_CLI_EFFORT", "high")
    cap = _arm(monkeypatch)
    llm._call_openai_cli("def f(): pass", max_tokens=64)
    a = cap.args
    assert a[a.index("--model") + 1] == "gpt-5.6-luna"
    assert "model_reasoning_effort=high" in a
    assert "model_reasoning_effort=ultra" not in a


def test_missing_binary_raises(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="Codex CLI not found"):
        llm._call_openai_cli("x", max_tokens=16)
