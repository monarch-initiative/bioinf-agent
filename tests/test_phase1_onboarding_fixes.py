"""Layout-independent defects from the second cold-start drive.

Each of these was found by a reader who had never seen the repo, and each one
reports something the system does not know:

  * a backup of the command-and-control file escaped the ignore rule whose own
    comment says NEVER COMMIT;
  * a detached container step that ran clean finished as a failed job, because
    the job runner read a verdict off a key no tool is required to write.

The configuration-menu half of that drive (a save that lost every staged edit
when its destination directory did not exist; three surfaces announcing `valid`
for a path holding no file) is tested in tests/test_config_menu.py, which is
where scripts/configure.py's behaviour already lives.

What remains here shares a shape rather than a subsystem: each turns an absence
into a confident claim.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# --- CS65: the backup of a secret is a secret ---------------------------------

def test_projects_access_backups_are_ignored():
    """`scripts/configure.py` writes `<path>.bak` on every save. The ignore rule
    was anchored and exact (`/projects_access.yaml`), so the backup — same
    hostnames, same usernames, same paths — was a tracked file waiting to be
    committed by any `git add -A`."""
    for name in ("projects_access.yaml", "projects_access.yaml.bak"):
        res = subprocess.run(["git", "check-ignore", "-q", name],
                             cwd=ROOT, capture_output=True)
        assert res.returncode == 0, (
            f"{name} is NOT ignored by .gitignore — it carries real cluster "
            f"hostnames, usernames and paths")


# --- CS18: a verdict manufactured from a field nobody wrote --------------------

def test_call_verdict_has_three_answers():
    from agent.skills.outcomes import broke, call_verdict, degraded, loop, proven

    assert call_verdict(proven("x.ok", success=True)) is True
    assert call_verdict(degraded("x.partial")) is True, (
        "degraded proceeded — it states reduced assurance, not failure")
    assert call_verdict(broke("x.dead")) is False
    assert call_verdict(loop("x.retry")) is False, (
        "loop hands back for a retry — the work is not done")

    assert call_verdict({"success": True}) is True
    assert call_verdict({"success": False}) is False

    assert call_verdict({"returncode": 0, "stdout": "fine"}) is None, (
        "a tool that states no verdict must read as UNSTATED, never as failed")
    assert call_verdict(None) is None
    assert call_verdict({"success": "yes"}) is None, (
        "a non-bool is not a statement")


def test_outcome_beats_a_stale_success_key():
    """`outcome` is the contracted field and wins. A boundary that re-wraps an
    inner result can carry a `success` from the inner call."""
    from agent.skills.outcomes import call_verdict, refused

    assert call_verdict(refused("x.no", success=True)) is False


def test_run_step_in_container_states_its_outcome():
    """Its return spreads `run_in_container`'s dict, which carries no verdict, so
    the backgrounded form exited 1 for a step that ran clean — a red job for
    work that succeeded, on the one primitive `validated == shipped` requires."""
    import ast

    from agent.skills.outcomes import HELPER_NAMES

    src = (ROOT / "agent" / "mcp_tools" / "run_tools.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "run_step_in_container")

    # The success path is the one that was silently red; both branches must carry
    # an outcome class, which is the field call_verdict reads first.
    tagged = {r.value.func.id for r in ast.walk(fn)
              if isinstance(r, ast.Return) and isinstance(r.value, ast.Call)
              and isinstance(r.value.func, ast.Name) and r.value.func.id in HELPER_NAMES}
    assert "proven" in tagged and "broke" in tagged, (
        f"the terminal returns of run_step_in_container must be outcome-tagged "
        f"(found {sorted(tagged)})")

    payloads = [n for n in ast.walk(fn) if isinstance(n, ast.Dict)]
    keys = {k.value for d in payloads for k in d.keys if isinstance(k, ast.Constant)}
    assert "success" in keys, "the returned payload must also state `success`"


def test_job_runner_does_not_read_success_bare():
    """The exit code is the job's badge, and it was computed as
    `0 if result.get("success") else 1` — which makes `absent` and `False`
    indistinguishable. It must go through the three-state reader."""
    src = (ROOT / "agent" / "skills" / "job_runner.py").read_text()
    tail = src[src.index("_write_result(result_path, result)"):]
    # Comments may quote the old expression while explaining why it went.
    tail = "\n".join(ln for ln in tail.splitlines() if not ln.lstrip().startswith("#"))
    assert 'result.get("success")' not in tail, (
        "job_runner's exit code must not branch on a bare `success` lookup — "
        "use outcomes.call_verdict, which distinguishes unstated from failed")
    assert "call_verdict" in tail


@pytest.mark.parametrize("payload,expected", [
    ({"success": True}, "succeeded"),
    ({"success": False}, "failed"),
    ({"returncode": 0, "stdout": "ok"}, "unstated"),
])
def test_check_job_labels_the_tool_outcome(tmp_path, payload, expected):
    """check_job is the ONE place a detached outcome is read, so the label lives
    there — computed from the result, never written into it."""
    import json

    from agent.skills.job_manager import JobManager

    jm = JobManager({"paths": {"conda_envs_prefix": str(tmp_path),
                               "jobs_dir": str(tmp_path)}})
    status = {"state": "exited"}
    jm.args_path("j1").parent.mkdir(parents=True, exist_ok=True)
    jm.args_path("j1").write_text("{}")
    jm.result_path("j1").write_text(json.dumps(payload))
    jm._inline_tool_result("j1", status)
    assert status["tool_outcome"] == expected
    assert "tool_outcome" not in status["result"], (
        "the label is computed at the read seam — the tool's own return is "
        "carried verbatim")
