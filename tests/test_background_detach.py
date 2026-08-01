"""The detachment contract — `@backgroundable` + `job_runner` + `check_job`.

Six primitives can legitimately run past the agent's ~600 s stream-watchdog. Each
carries a `background: bool = False` flag from ONE decorator rather than a
hand-copied branch, because the protocol has six moving parts that must agree:

    the published schema · the job_id · the args file · the runner's recursion
    guard · the result file · the read-back

Hand-copying that per tool is how four of them drift while the suite stays green.
These tests hold every decorated tool to the same contract, so adding a seventh
tool is one line plus one row in `MINIMAL_ARGS` — and forgetting the row is a
FAILURE, not a silent coverage hole.

The freeze-specific detachment tests stay in test_invariants.py (W1). Those cover
the tool whose background path actually shipped, BY NAME; these cover the
machinery every tool now shares.
"""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

# Importing the server is what POPULATES the registry — the @backgroundable
# decorations fire as the tool modules import. Without this the parametrized
# sweeps below would collect ZERO cases and the file would pass by covering
# nothing, which is the exact failure the roster lint exists to prevent.
import agent.mcp_server as _ms  # noqa: F401
from agent.skills.backgroundable import BACKGROUNDABLE, backgroundable


# One minimal, side-effect-free call per decorated tool. Every value is a plain
# JSON scalar/list — the point is to reach the spawn, never the tool body.
MINIMAL_ARGS: dict[str, dict] = {
    "freeze": {
        "env_name": "detach_env", "tools": ["samtools=1.21"]},
    "freeze_from_image": {
        "image": "quay.io/example:1", "tools": [{"name": "x", "evidence": "x --version"}],
        "name": "detach_img"},
    "build_env_from_authors_recipe": {
        "repo": "owner/repo", "tools": [{"name": "x", "evidence": "x --version"}],
        "name": "detach_recipe"},
    "install_conda_packages": {
        "env_name": "detach_conda", "packages": [{"spec": "samtools=1.21", "channel": "bioconda"}]},
    "run_step_in_container": {
        "freeze_request_key": "rk-detach", "command": "samtools --version",
        "pipeline_id": "pid-detach", "tool": "samtools"},
    "seal_workflow": {
        "pipeline_id": "pid-detach", "freeze_request_key": "rk-detach",
        "workflow_name": "detach_wf"},
}

# The slug each tool's `name_from` should pull out of MINIMAL_ARGS above.
EXPECTED_SLUG = {
    "freeze": "detach_env",                      # pipeline_name absent -> env_name
    "freeze_from_image": "detach_img",
    "build_env_from_authors_recipe": "detach_recipe",
    "install_conda_packages": "detach_conda",
    "run_step_in_container": "samtools",         # `tool` before pipeline_id
    "seal_workflow": "detach_wf",                # workflow_name before pipeline_id
}


def _tools():
    return sorted(BACKGROUNDABLE)


def test_every_decorated_tool_has_a_row_here():
    """The roster lint. A tool that gains the flag without gaining a row would
    be exercised by NONE of the parametrized tests below while the file still
    reads as if it covered everything."""
    assert set(MINIMAL_ARGS) == set(BACKGROUNDABLE), (
        f"MINIMAL_ARGS is out of sync with @backgroundable:\n"
        f"  decorated but unrowed: {sorted(set(BACKGROUNDABLE) - set(MINIMAL_ARGS))}\n"
        f"  rowed but undecorated: {sorted(set(MINIMAL_ARGS) - set(BACKGROUNDABLE))}")
    assert set(EXPECTED_SLUG) == set(BACKGROUNDABLE)


@pytest.mark.parametrize("name", _tools())
def test_flag_is_published_on_the_mcp_surface(name):
    """The decorator widens `__signature__`; FastMCP must actually publish it.
    An agent cannot pass a flag it never sees, so a silently-dropped parameter
    turns the whole feature into dead code that still passes its unit tests."""
    from agent.mcp_server import mcp

    tool = asyncio.run(mcp.get_tool(name))
    prop = tool.parameters["properties"].get("background")
    assert prop is not None, f"{name} does not publish `background`"
    assert prop["type"] == "boolean"
    assert prop["default"] is False
    assert "background=True" in (tool.description or ""), (
        f"{name}'s description does not explain the flag — the agent would see "
        "a boolean with no contract")


def test_widening_preserves_the_original_schema():
    """The alias types are the fragile part: `tools: StrList` is
    Annotated[list[str], BeforeValidator(...)], and a wrapper that lost the
    annotation would publish it as a bare object and break every caller."""
    from agent.mcp_server import mcp

    props = asyncio.run(mcp.get_tool("freeze")).parameters["properties"]
    assert props["tools"] == {"items": {"type": "string"}, "type": "array"}
    assert props["platform"] == {"default": "linux-64", "type": "string"}
    assert props["gated"] == {"default": False, "type": "boolean"}


@pytest.mark.parametrize("name", _tools())
def test_every_decorated_tool_is_importable_from_mcp_server(name):
    """`job_runner` resolves the tool with `getattr(agent.mcp_server, name)`. A
    tool decorated but missing from the back-compat re-exports would spawn fine
    and then fail INSIDE the detached subprocess, minutes later, where nobody is
    watching. Catch it here instead."""
    import agent.mcp_server as ms
    assert callable(getattr(ms, name, None)), (
        f"{name} is @backgroundable but not re-exported on agent.mcp_server — "
        "add it to the block at the bottom of agent/mcp_server.py")


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    """Capture the spawn instead of forking. Yields the recorded command."""
    import agent.mcp_server as ms
    rec: dict = {}

    def _stub_start(command, *, env_name="", job_id="", working_dir=""):
        rec["command"], rec["job_id"] = command, job_id
        return {"job_id": job_id, "log_path": str(tmp_path / f"{job_id}.log"),
                "state": "running", "pid": 4242}

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(ms._job_manager, "start", _stub_start)
    monkeypatch.setattr(ms._job_manager, "jobs_dir", jobs)
    rec["jobs_dir"] = jobs
    return rec


@pytest.mark.parametrize("name", _tools())
def test_background_true_detaches_without_running_the_tool(name, spawned):
    """The whole point: return a receipt IMMEDIATELY. If any tool body ran here
    it would touch docker/conda and this test would be slow or fail — none of
    these envs, images or drafts exist."""
    import agent.mcp_server as ms

    out = getattr(ms, name)(**MINIMAL_ARGS[name], background=True)
    assert out["background"] is True
    assert out["state"] == "running"
    assert out["tool"] == name
    assert out["job_id"] == spawned["job_id"]
    assert out["job_id"].startswith(f"{name}.{EXPECTED_SLUG[name]}."), (
        f"job_id {out['job_id']!r} is not readable in list_jobs — the "
        f"@backgroundable name_from for {name} did not resolve a slug")
    # The receipt must not read as a result: it says, in words, that nothing
    # has been produced, and it points at the ONE place the outcome will appear.
    assert "NOTHING has been produced" in out["note"]
    assert "check_job" in out["note"]
    assert f"agent.skills.job_runner {name} " in spawned["command"]


@pytest.mark.parametrize("name", _tools())
def test_background_never_reaches_the_args_file(name, spawned):
    """The parent-side half of the recursion guard. If `background: true` were
    written to the args file, the child would spawn another child and exit 0 —
    a job that always succeeds and never runs."""
    import agent.mcp_server as ms

    getattr(ms, name)(**MINIMAL_ARGS[name], background=True)
    args = json.loads((spawned["jobs_dir"] / f"{spawned['job_id']}.args.json").read_text())
    assert "background" not in args
    for k, v in MINIMAL_ARGS[name].items():
        assert args[k] == v, f"{name}: argument {k} did not survive the write"


def test_synchronous_path_is_untouched(monkeypatch):
    """background defaults to False and the wrapper must then be transparent —
    same call, same kwargs, same return object."""
    import agent.mcp_tools.env_tools as et
    seen = {}

    def _fake_install(env_name, packages):
        seen["env_name"], seen["packages"] = env_name, packages
        return {"success": True, "sentinel": "from the real body"}

    monkeypatch.setattr(et._ms._env_mgr, "install", _fake_install)
    out = et.install_conda_packages(env_name="e", packages=[{"spec": "s", "channel": "c"}])
    assert out["sentinel"] == "from the real body"
    assert seen["env_name"] == "e"


def test_unserializable_argument_refuses_and_names_the_parameter(spawned):
    """`json.dumps(default=str)` would SILENTLY stringify an argument, so the
    child would run a different call than the one the agent asked for. Refuse,
    and say which parameter — a refusal that says "the arguments" is a puzzle."""
    import agent.mcp_server as ms

    out = ms.install_conda_packages(
        env_name="e", packages=[{"spec": "s", "channel": "c"}],
        pipeline_id=object(), background=True)   # type: ignore[arg-type]
    assert out["success"] is False
    assert out["code"] == "background.unserializable_argument"
    assert out["parameter"] == "pipeline_id"
    assert "pipeline_id" in out["fix"]
    assert "job_id" not in out, "a refused spawn must not hand back a job to poll"


# --------------------------------------------------------------------------
# job_runner — the child half
# --------------------------------------------------------------------------

def _run_runner(tool, args, tmp_path, monkeypatch):
    from agent.skills import job_runner
    a, r = tmp_path / "a.json", tmp_path / "r.json"
    a.write_text(json.dumps(args))
    monkeypatch.setattr(sys, "argv", ["job_runner", tool, str(a), str(r)])
    rc = job_runner.main()
    return rc, (json.loads(r.read_text()) if r.exists() else None)


def test_runner_refuses_a_tool_that_was_never_decorated(tmp_path, monkeypatch):
    """The allowlist. The runner must never turn an arbitrary string off the
    args file into a callable — the set it resolves against is DERIVED from the
    decorations, so it cannot drift from what is actually backgroundable."""
    rc, result = _run_runner("os", {}, tmp_path, monkeypatch)
    assert rc == 1
    assert result["stage"] == "background_unknown_tool"
    assert "freeze" in result["known"], "the refusal should list what IS runnable"


def test_runner_refuses_a_decorated_tool_that_is_not_exported(tmp_path, monkeypatch):
    """Decorated but absent from mcp_server's re-exports — a wiring bug. It must
    be named as one, not reported as a caller error to be retried."""
    monkeypatch.setitem(BACKGROUNDABLE, "ghost_tool", ())
    rc, result = _run_runner("ghost_tool", {}, tmp_path, monkeypatch)
    assert rc == 1
    assert result["stage"] == "background_tool_not_exported"
    assert "re-exports" in result["error"]


def test_runner_writes_a_result_when_the_args_file_is_corrupt(tmp_path, monkeypatch):
    """Every subprocess outcome leaves a result — including the ones that happen
    before the tool is ever reached."""
    from agent.skills import job_runner
    a, r = tmp_path / "a.json", tmp_path / "r.json"
    a.write_text("{not json")
    monkeypatch.setattr(sys, "argv", ["job_runner", "freeze", str(a), str(r)])
    assert job_runner.main() == 1
    result = json.loads(r.read_text())
    assert result["stage"] == "background_args_read"
    assert result["tool"] == "freeze"


def test_runner_result_write_is_atomic(tmp_path):
    """A `check_job` landing mid-write must never read a truncated object and
    report it as the tool's outcome."""
    from agent.skills.job_runner import _write_result
    p = tmp_path / "r.json"
    _write_result(str(p), {"success": True, "deep": {"a": [1, 2, {"b": None}]}})
    assert json.loads(p.read_text())["deep"]["a"][2]["b"] is None
    assert not list(tmp_path.glob("*.tmp")), "the temp file must be renamed, not left behind"


# --------------------------------------------------------------------------
# check_job — the read-back
# --------------------------------------------------------------------------

@pytest.fixture
def jm(tmp_path):
    from agent.skills.job_manager import JobManager
    m = JobManager(_ms.config)
    m.jobs_dir = tmp_path / "jobs"
    m.jobs_dir.mkdir()
    return m


def _exited_job(jm, job_id, *, tool_job=True):
    (jm.jobs_dir / f"{job_id}.status.json").write_text(json.dumps({
        "state": "exited", "returncode": 0, "start_time": 0.0, "command": "x"}))
    if tool_job:
        jm.args_path(job_id).write_text("{}")


def test_check_inlines_the_tool_result_once_the_job_is_over(jm):
    """One read. Before this the caller polled check_job, saw 'exited', and then
    had to remember to open result_path — and a forgotten second call looks
    exactly like success while the draft never got its step."""
    _exited_job(jm, "freeze.x.aaaa")
    jm.result_path("freeze.x.aaaa").write_text(json.dumps(
        {"success": True, "request_key": "rk-1", "mode": "adopt"}))
    got = jm.check("freeze.x.aaaa")
    assert got["result"]["request_key"] == "rk-1"


def test_check_states_the_hole_when_a_tool_job_wrote_no_result(jm):
    """A detached tool that died before writing must not read as a clean exit.
    The hole is stated; it is never rounded up to a pass."""
    _exited_job(jm, "freeze.x.bbbb")
    got = jm.check("freeze.x.bbbb")
    assert got["result"] is None
    assert "without writing a result" in got["result_missing"]


def test_check_flags_an_unreadable_result_rather_than_swallowing_it(jm):
    _exited_job(jm, "freeze.x.cccc")
    jm.result_path("freeze.x.cccc").write_text("{truncated")
    got = jm.check("freeze.x.cccc")
    assert got["result"] is None
    assert "unreadable" in got["result_missing"]


def test_check_claims_nothing_for_a_plain_shell_job(jm):
    """`run_in_background` jobs never owed a result. Reporting `result: None`
    for them would invent a failure that did not happen."""
    _exited_job(jm, "plain.dddd", tool_job=False)
    got = jm.check("plain.dddd")
    assert "result" not in got and "result_missing" not in got


def test_check_does_not_inline_while_the_job_is_still_running(jm, monkeypatch):
    """A result read early would be a previous run's, or a partial write."""
    (jm.jobs_dir / "freeze.x.eeee.status.json").write_text(json.dumps({
        "state": "running", "pid": 999999, "start_time": 0.0, "command": "x"}))
    jm.args_path("freeze.x.eeee").write_text("{}")
    jm.result_path("freeze.x.eeee").write_text(json.dumps({"success": True}))
    monkeypatch.setattr(type(jm), "_is_pid_alive", staticmethod(lambda *a, **k: True))
    got = jm.check("freeze.x.eeee")
    assert got["state"] == "running"
    assert "result" not in got


# --------------------------------------------------------------------------
# import-time guards — a misuse of the decorator is a startup error
# --------------------------------------------------------------------------

def test_decorator_rejects_a_name_from_that_is_not_a_parameter():
    """A typo would otherwise degrade silently to an unreadable job_id, and
    nobody notices an ugly identifier until they are hunting a job at 3 a.m."""
    with pytest.raises(TypeError, match="no such parameter"):
        @backgroundable("env_nmae")
        def t(env_name: str) -> dict: ...


def test_decorator_rejects_a_tool_that_already_declares_background():
    with pytest.raises(TypeError, match="already declares"):
        @backgroundable()
        def t(background: bool = False) -> dict: ...


def test_decorator_rejects_var_args():
    """`**kwargs` would collapse into one nested key in bind().arguments and the
    child re-enters by keyword only."""
    with pytest.raises(TypeError, match="keyword signature"):
        @backgroundable()
        def t(a: str, **kw) -> dict: ...


def test_decorator_registers_and_appends_the_contract():
    @backgroundable("a")
    def a_probe_tool(a: str = "") -> dict:
        """Original text."""
        return {}

    assert BACKGROUNDABLE.pop("a_probe_tool") == ("a",)
    assert a_probe_tool.__doc__.startswith("Original text.")
    assert "background=True" in a_probe_tool.__doc__


# --------------------------------------------------------------------------
# preflight — the checks that must refuse IN-CALL, never in a detached job
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["freeze", "freeze_from_image",
                                  "build_env_from_authors_recipe"])
def test_a_dead_daemon_refuses_in_call_instead_of_spawning(name, spawned, monkeypatch):
    """Every image-producing tool checks docker in its body. Detaching would turn
    that instant, correctly-named refusal into a job that dies minutes later
    inside a log the agent has to go find — so the check is hoisted ahead of the
    spawn as well."""
    import agent.mcp_server as ms
    monkeypatch.setattr(ms, "_check_docker_available",
                        lambda: {"success": False, "code": "freeze.docker_unavailable"})
    out = getattr(ms, name)(**MINIMAL_ARGS[name], background=True)
    assert out["code"] == "freeze.docker_unavailable"
    assert "job_id" not in out, "nothing should have been detached"
    assert "job_id" not in spawned, "JobManager.start was called anyway"


@pytest.mark.parametrize("name", ["freeze", "freeze_from_image",
                                  "build_env_from_authors_recipe"])
def test_a_full_disk_refuses_in_call_instead_of_spawning(name, spawned, monkeypatch):
    """The A1 failsafe exists because four parallel freezes once piled buildkit
    intermediates to 92% capacity and wedged into a retry loop. Backgrounding is
    what makes four parallel freezes ordinary, so the guard has to hold on this
    path too — and it did NOT, for the two freeze variants, until it was hoisted
    here: only `freeze` itself carried it."""
    import agent.mcp_server as ms
    monkeypatch.setattr(ms, "_check_disk_failsafe",
                        lambda: {"success": False, "code": "freeze.disk_pressure"})
    out = getattr(ms, name)(**MINIMAL_ARGS[name], background=True)
    assert out["code"] == "freeze.disk_pressure"
    assert "job_id" not in spawned


def test_preflight_does_not_run_on_the_synchronous_path(monkeypatch):
    """The flag is opt-in and the wrapper must be transparent without it. A
    preflight that fired on every call would double every check in the body."""
    ran = []

    @backgroundable("a", preflight=lambda: ran.append(1) or {"success": False})
    def a_sync_probe(a: str = "") -> dict:
        """Doc."""
        return {"success": True, "ran_body": True}

    try:
        assert a_sync_probe(a="x")["ran_body"] is True
        assert ran == []
    finally:
        BACKGROUNDABLE.pop("a_sync_probe", None)
