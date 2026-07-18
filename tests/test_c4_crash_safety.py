"""C4 — crash safety: every primitive returns a TAGGED outcome on hostile input,
never an uncaught exception.

Under full-auto an autonomous agent branches on the `outcome`/`code` of every
call. An unhandled exception (ValueError, KeyError, AttributeError, TypeError, …)
is a dead end — the agent gets a traceback it cannot interpret and is stuck. So
every agent-facing MCP tool MUST, on hostile input (missing env, bad path,
nonexistent project/pipeline, malformed args), come back with a dict carrying an
outcome tag — refused/broke/etc. — not raise.

This harness drives the whole tool surface with hostile inputs chosen to hit each
tool's guard layer, and asserts a tagged dict returns. A SAFETY NET neutralises
every external boundary (subprocess / Popen / urllib) and scopes the pipeline-
state singleton to a tmp dir, so a tool with a MISSING guard that reaches the
boundary fails fast in-sandbox (still a finding if it then crashes) and nothing
escapes to the real system.

The battery is the living C4 checklist: a new tool without an entry here is
uncovered; add one when you add a primitive.
"""
from __future__ import annotations

import subprocess

import pytest

from agent.skills.outcomes import OUTCOME_CLASSES
from agent.skills.pipeline_state import PipelineState

# Hostile sentinels — deliberately nonexistent references.
BAD_ENV = "bioinf_c4_nonexistent_env_zzz"
BAD_PID = "c4_nonexistent_pipeline_zzz"
BAD_PROJ = "c4_nonexistent_project_zzz"
BAD_PATH = "/c4/nonexistent/path/zzz.dat"
BAD_KEY = "c4_nonexistent_freeze_key_zzz"
BAD_JOB = "c4_nonexistent_job_zzz"


class _FakePopen:
    """A Popen that never spawns anything — the safety net for background/service
    tools whose env guard might be missing. `pid` is a high, almost-certainly-
    unused POSITIVE integer (a real OS pid is always positive), so a psutil
    resource-monitor thread hits NoSuchProcess (which it handles) rather than a
    ValueError that only a negative sentinel would trigger — i.e. we simulate a
    process that vanished instantly, the realistic worst case."""
    def __init__(self, *a, **k):
        self.pid = 2 ** 31 - 2
        self.returncode = 127
    def poll(self): return 127
    def wait(self, timeout=None): return 127
    def communicate(self, *a, **k): return ("", "c4-sandbox: external calls disabled")
    def terminate(self): pass
    def kill(self): pass
    def send_signal(self, *a): pass


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch, tmp_path):
    """Isolate state + neutralise every external boundary so a hostile call can
    never touch the real system, and a missing guard fails fast rather than
    running something."""
    from agent import mcp_server as m

    # 1) scope the pipeline-state singleton to tmp (no draft leaks into the repo).
    tmp_drafts = tmp_path / "pipeline_drafts"; tmp_drafts.mkdir()
    cfg = {**m.config, "paths": {**m.config.get("paths", {}),
                                 "drafts_dir": str(tmp_drafts),
                                 "pipelines_dir": str(tmp_path / "env_reports")}}
    monkeypatch.setattr(m, "_pipeline_state", PipelineState(cfg))

    # scope the job manager's on-disk writes to tmp (a spawn-path test writes a
    # status + log file; keep them out of the real data/jobs tree).
    tmp_jobs = tmp_path / "jobs"; tmp_jobs.mkdir()
    monkeypatch.setattr(m._job_manager, "jobs_dir", tmp_jobs)

    # 2) neutralise external process + network boundaries.
    def _dead_run(*a, **k):
        return subprocess.CompletedProcess(a[0] if a else "", 127, "", "c4-sandbox: disabled")
    monkeypatch.setattr(subprocess, "run", _dead_run)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    import urllib.request
    def _dead_url(*a, **k):
        raise urllib.error.URLError("c4-sandbox: network disabled")
    monkeypatch.setattr(urllib.request, "urlopen", _dead_url)
    import requests
    def _dead_req(*a, **k):
        raise requests.RequestException("c4-sandbox: network disabled")
    for verb in ("get", "post", "head", "put"):
        monkeypatch.setattr(requests, verb, _dead_req)
    yield


# ---------------------------------------------------------------------------
# The battery. Each entry: (label, callable-name, module, kwargs, require_tag).
# require_tag=True  → hostile input to a guarded tool: MUST come back with an
#                     outcome tag (refused/broke/…).
# require_tag=False → benign/read-only tool with no meaningful hostile input:
#                     MUST still return a dict and not crash (tag optional).
# ---------------------------------------------------------------------------
def _battery():
    from agent.mcp_tools import (bridge_tools as B, data_tools as D, env_tools as E,
                                 freeze_tools as F, intent_tools as I, jobs_tools as J,
                                 observability_tools as O, plan_tools as P, run_tools as R,
                                 sealed_tools as ST, service_tools as S, workflow_tools as W)
    return [
        # -- bridge (auth must refuse before any ssh) -----------------------
        ("upload", B.upload, dict(project_name=BAD_PROJ, compute_env_name=BAD_ENV,
                                  local_path=BAD_PATH, remote_abs_path="/remote/x"), True),
        ("download", B.download, dict(project_name=BAD_PROJ, compute_env_name=BAD_ENV,
                                      remote_abs_path="/remote/x", local_path=BAD_PATH), True),
        ("cluster_module_avail", B.cluster_module_avail,
         dict(project_name=BAD_PROJ, compute_env_name=BAD_ENV, pattern="gcc"), True),
        ("globus_task_status", B.globus_task_status,
         dict(project_name=BAD_PROJ, compute_env_name=BAD_ENV, task_id="not-a-uuid"), True),
        ("cluster_job_status", B.cluster_job_status,
         dict(project_name=BAD_PROJ, compute_env_name=BAD_ENV, job_id="not-a-digit;rm"), True),
        ("submit_workflow_job", B.submit_workflow_job,
         dict(project_name=BAD_PROJ, compute_env_name=BAD_ENV, workflow_dir="/wf",
              workflow_name="wf", tool_name="t", command="c", inputs={}, outputs={},
              apptainer_sif="s", apptainer_module="m", nextflow_module="n", slurm={}), True),
        ("stage_apptainer_image", B.stage_apptainer_image,
         dict(project_name=BAD_PROJ, compute_env_name=BAD_ENV, freeze_request_key=BAD_KEY), True),
        ("run_step_on_cluster", B.run_step_on_cluster,
         dict(pipeline_id=BAD_PID, freeze_request_key=BAD_KEY, project_name=BAD_PROJ,
              compute_env_name=BAD_ENV, workflow_name="wf", tool_name="t", command="c",
              inputs={}, outputs={}, download_local_dir="/tmp", apptainer_module="m",
              nextflow_module="n", slurm={}), True),
        # -- data -----------------------------------------------------------
        ("download_reference_database", D.download_reference_database,
         dict(name="", url="", local_path=""), True),
        ("acquire_reference_via_recipe", D.acquire_reference_via_recipe,
         dict(name="", recipe_path="", compute_env=""), True),
        ("list_available_resources", D.list_available_resources, dict(resource_type="bogus"), False),
        ("download_resource", D.download_resource, dict(resource_type="bogus", resource_id="bogus"), True),
        ("add_core_test_data", D.add_core_test_data, dict(accession="", assay_type="bogus_assay"), True),
        ("add_core_pod5_data", D.add_core_pod5_data, dict(accession="", sample="", source_url=""), True),
        ("add_phenopacket", D.add_phenopacket, dict(source_url="not://a-url"), True),
        ("phenopacket_to_vcf", D.phenopacket_to_vcf, dict(phenopacket_id=BAD_PID, output_vcf=BAD_PATH), True),
        # select_test_data is a QUERY: a no-match returns available alternatives +
        # score (interpretable), not a gated action, so no outcome tag is required.
        ("select_test_data", D.select_test_data, dict(assay_type="nonexistent_assay_zzz"), False),
        # install_pipeline_brief is PURE INFO (returns the protocol brief) — no
        # hostile input, just must not crash.
        ("install_pipeline_brief", D.install_pipeline_brief, dict(name=""), False),
        # -- intent (the front door) ----------------------------------------
        # interpret_request is a pure validation/routing QUERY: malformed JSON (or an
        # invalid intent) comes back as {ok: False, error: …} — interpretable, no crash,
        # no side effects. Its verdicts (decline/ask/investigate/proceed) are a DIFFERENT
        # axis from the outcome-tag vocabulary, so no tag is required. Like resolve_tool.
        ("interpret_request", I.interpret_request, dict(intent_json="not valid json{{"), False),
        # -- plan (the composition front door) ------------------------------
        # plan_request is a pure validation/gating QUERY, exactly like interpret_request:
        # malformed JSON (or an invalid plan) comes back as {ok: False, error: …} —
        # interpretable, no crash, no side effects. Its verdict (plan_ready) is a
        # DIFFERENT axis from the outcome-tag vocabulary, so no tag is required. It
        # dispatches NOTHING, so there is no action to gate.
        ("plan_request", P.plan_request, dict(plan_json="not valid json{{"), False),
        # -- sealed-step reader (RUN_STEP-of-a-sealed-workflow) --------------
        # describe_sealed_step is a pure typed-read QUERY, the third advisory reader
        # (sibling of interpret_request/plan_request): a missing workflow / bad step
        # comes back as {ok: False, error, available_*} — interpretable, no crash, no
        # side effects, dispatches nothing. Off the outcome-tag axis, so no tag required.
        ("describe_sealed_step", ST.describe_sealed_step,
         dict(workflow_name="no_such_workflow_zzz", step=1), False),
        # -- env / install (missing env must refuse before subprocess) ------
        # search_package / resolve_tool are QUERIES: an unknown package returns a
        # not-found / no-decision dict (interpretable), tag optional.
        ("search_package", E.search_package, dict(package_name=""), False),
        ("resolve_tool", E.resolve_tool, dict(tool=""), False),
        ("create_conda_env", E.create_conda_env, dict(env_name=""), True),
        ("install_conda_packages", E.install_conda_packages,
         dict(env_name=BAD_ENV, packages=[{"spec": "x"}]), True),
        ("install_git_repo", E.install_git_repo,
         dict(env_name=BAD_ENV, repo_url="not://url", tool_name="t"), True),
        ("synth_fetch", E.synth_fetch, dict(repo_url="not://url"), True),
        ("synth_build", E.synth_build, dict(env_name=BAD_ENV, repo_url="not://url", tool_name="t"), True),
        ("install_release_binary", E.install_release_binary,
         dict(env_name=BAD_ENV, tool_name="t", url="not://url"), True),
        ("install_perl_package", E.install_perl_package, dict(env_name=BAD_ENV, module="Mod"), True),
        ("install_cargo_tool", E.install_cargo_tool, dict(env_name=BAD_ENV, crate="c"), True),
        ("install_go_tool", E.install_go_tool, dict(env_name=BAD_ENV, package="p"), True),
        ("install_jar_tool", E.install_jar_tool, dict(env_name=BAD_ENV, tool_name="t", jar_url="not://url"), True),
        ("install_r_package", E.install_r_package, dict(env_name=BAD_ENV, name="p", source="cran"), True),
        ("install_pip_package", E.install_pip_package, dict(env_name=BAD_ENV, name="p"), True),
        ("run_install_command", E.run_install_command, dict(env_name=BAD_ENV, command="echo hi"), True),
        # -- freeze (gated+no-license is a clean early guard, never reaches docker)
        ("freeze", F.freeze, dict(env_name=BAD_ENV, tools=["tool"], gated=True), True),
        # freeze_from_image / build_env_from_authors_recipe: empty tools is a clean early
        # guard that refuses before any docker/git — the hostile-input fast path.
        ("freeze_from_image", F.freeze_from_image,
         dict(image="", tools=[], name=""), True),
        ("build_env_from_authors_recipe", F.build_env_from_authors_recipe,
         dict(repo="", tools=[], name=""), True),
        ("verify_env_recipe", F.verify_env_recipe, dict(recipe_path=BAD_PATH), True),
        ("generate_user_guide", F.generate_user_guide, dict(pipeline_id=BAD_PID), True),
        # -- jobs -----------------------------------------------------------
        ("run_in_background", J.run_in_background, dict(command="", env_name=BAD_ENV), True),
        # spawn path: a command that exits instantly (the realistic race) must not
        # crash start() when it reads the child's process group (getpgid) — returns
        # a {state: running} record the caller polls via check_job.
        ("run_in_background_spawn", J.run_in_background, dict(command="true", env_name=""), False),
        ("check_job", J.check_job, dict(job_id=BAD_JOB), True),
        ("cancel_job", J.cancel_job, dict(job_id=BAD_JOB), True),
        ("list_jobs", J.list_jobs, dict(), False),
        # -- observability --------------------------------------------------
        ("snapshot_project", O.snapshot_project, dict(project_name=BAD_PROJ), True),
        ("agent_status", O.agent_status, dict(), False),
        # -- run ------------------------------------------------------------
        ("run_pipeline_step", R.run_pipeline_step, dict(env_name=BAD_ENV, command="c", pipeline_id=BAD_PID), True),
        ("run_step_in_container", R.run_step_in_container,
         dict(freeze_request_key=BAD_KEY, command="c", pipeline_id=BAD_PID), True),
        ("verify_installation", R.verify_installation,
         dict(env_name=BAD_ENV, package_name="p", check_command="p --version"), True),
        ("run_in_env", R.run_in_env, dict(env_name=BAD_ENV, command="echo hi"), True),
        ("validate_output", R.validate_output, dict(file_path=BAD_PATH, expected_type="bam"), True),
        # malformed BATCH entry (None in files[]) must not crash the batch.
        ("validate_output_malformed", R.validate_output, dict(files=[None, {}]), False),
        # -- service (missing env must refuse before spawning) --------------
        ("start_service", S.start_service,
         dict(env_name=BAD_ENV, service_name="svc", start_command="start", health_check_command="hc"), True),
        ("stop_service", S.stop_service, dict(env_name=BAD_ENV, service_name="svc"), True),
        # check_gpu / check_service_health are PROBES returning {available/healthy,
        # …} status (interpretable), not gated actions — must not crash, tag optional.
        ("check_gpu", S.check_gpu, dict(), False),
        ("check_service_health", S.check_service_health, dict(env_name=BAD_ENV, health_check_command="hc"), False),
        ("verify_service_dependency", S.verify_service_dependency,
         dict(pipeline_id=BAD_PID, service_name="svc", env_name=BAD_ENV), True),
        # -- workflow -------------------------------------------------------
        ("seal_workflow", W.seal_workflow, dict(pipeline_id=BAD_PID, freeze_request_key=BAD_KEY), True),
        ("write_pipeline_provenance", W.write_pipeline_provenance,
         dict(pipeline="p", conda_env_path=BAD_PATH, pipeline_spec_path=BAD_PATH,
              output_files=[], output_dir=BAD_PATH, sample_key="s"), True),
        ("list_installed_pipelines", W.list_installed_pipelines, dict(), False),
        ("fetch_r_package_deps", W.fetch_r_package_deps, dict(github_repo="not/a/real/repo"), True),
        ("start_pipeline", W.start_pipeline, dict(pipeline_name="", description=""), False),
        # discard is IDEMPOTENT: a nonexistent draft returns {existed: False}
        # (interpretable), not an error — must not crash, tag optional.
        ("discard_pipeline_draft", W.discard_pipeline_draft, dict(pipeline_id=BAD_PID), False),
        ("show_pipeline_draft", W.show_pipeline_draft, dict(pipeline_id=BAD_PID), True),
        ("patch_pipeline", W.patch_pipeline, dict(pipeline_id=BAD_PID, patches={"notes": "x"}), True),
        ("stage_authored_artifact", W.stage_authored_artifact,
         dict(pipeline_id=BAD_PID, path=BAD_PATH, role="r", description="d"), True),
        ("mark_step_validated", W.mark_step_validated, dict(pipeline_id=BAD_PID, step=1), True),
        # stringly-typed step must refuse, not TypeError on the range comparison.
        ("mark_step_validated_badstep", W.mark_step_validated,
         dict(pipeline_id=BAD_PID, step="notanint"), True),
    ]


_BATTERY = _battery()


@pytest.mark.parametrize("label,fn,kwargs,require_tag", _BATTERY, ids=[b[0] for b in _BATTERY])
def test_primitive_never_crashes_on_hostile_input(label, fn, kwargs, require_tag):
    """The C4 property: a hostile call comes back as an interpretable dict — with
    an outcome tag when the input is something the tool should reject — never an
    uncaught exception."""
    try:
        result = fn(**kwargs)
    except Exception as e:                       # noqa: BLE001 — that's the point
        import traceback
        pytest.fail(f"{label} raised {type(e).__name__} on hostile input "
                    f"(an agent can't interpret this): {e}\n{traceback.format_exc()}")

    assert isinstance(result, dict), \
        f"{label} returned a non-dict ({type(result).__name__}) — an agent can't branch on it"
    if require_tag:
        assert result.get("outcome") in OUTCOME_CLASSES, \
            f"{label} returned a dict with no outcome tag on hostile input: keys={list(result)[:12]}"


def test_search_package_survives_non_json_200(monkeypatch):
    """Targeted C4: a registry that returns HTTP 200 with a NON-JSON body (a
    rate-limit / captive-portal / proxy error page) must not crash the search.
    The battery can't reach this line (it mocks requests to *raise*, i.e. a
    failed request); here we make requests SUCCEED with junk so resp.json()
    raises, and assert a tag comes back instead of a JSONDecodeError."""
    import requests
    from agent.mcp_tools import env_tools as E

    class _JunkResp:
        status_code = 200
        text = "<html>rate limited</html>"
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(requests, "get", lambda *a, **k: _JunkResp())
    res = E.search_package(package_name="samtools")
    assert isinstance(res, dict), res           # no JSONDecodeError escaped
    # the anaconda tier surfaces a broke tag; overall the tool returns a dict.
    assert res.get("outcome") in OUTCOME_CLASSES or "found" in res, res


def test_battery_covers_every_mcp_tool():
    """Ratchet: every @mcp.tool() primitive must have a battery entry, so a newly
    added tool can't silently escape crash-safety coverage."""
    import ast
    from pathlib import Path
    tool_dir = Path(__file__).parent.parent / "agent" / "mcp_tools"
    declared = set()
    for f in tool_dir.glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    # @mcp.tool()
                    if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                            and dec.func.attr == "tool"):
                        declared.add(node.name)
    covered = {b[0] for b in _BATTERY}
    missing = declared - covered
    assert not missing, f"MCP tools with no C4 crash-safety entry: {sorted(missing)}"
