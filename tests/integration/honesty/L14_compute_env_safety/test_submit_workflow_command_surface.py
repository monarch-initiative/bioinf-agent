"""
L14 cheat-guards — the sbatch leg's parsing and validation surface (agent/skills/
submit_workflow.py), shared by run_production_pipeline, run_step_on_cluster and the
reference-data download job:

  - the remote run directory is validated (abs path, no `..`, no shell metachars)
    BEFORE any I/O
  - the `sbatch --parsable` parser extracts job_ids from clean and federated stdouts
    and refuses anything else with the raw stdout surfaced
  - the render staging directory lives in the workspace, never the system temp dir
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.skills import submit_workflow


class TestWorkflowDirValidator:
    @pytest.mark.integration
    @pytest.mark.parametrize("good", [
        "/work/u/demo/run_001",
        "/work/u/demo/run_001/",
        "/x",
    ])
    def test_accepts_good_dirs(self, good):
        assert submit_workflow._validate_workflow_dir(good).startswith("/")

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "relative/path",
        "/work/../etc/shadow",
        "/work/u/demo$(id)",
        "/work/u/demo;rm",
        "/work/u/demo & evil",
        "",
        "/work with spaces/x",
        "/path/with\nnewline",
        "/path|pipe",
    ])
    def test_refuses_bad_dirs(self, bad):
        with pytest.raises(ValueError):
            submit_workflow._validate_workflow_dir(bad)

class TestSbatchParsableParser:
    @pytest.mark.integration
    @pytest.mark.parametrize("stdout,want", [
        ("12345\n",          "12345"),
        ("12345;rc1",        "12345"),
        ("123456789012\n",   "123456789012"),
        ("  98765  \n",      "98765"),
    ])
    def test_extracts_job_id(self, stdout, want):
        assert submit_workflow._parse_sbatch_parsable(stdout) == want

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "",
        "Submitted batch job 12345\n",
        "abc",
        "0".rjust(13, "9"),   # 13-digit (over the cap)
        "12345garbage",
    ])
    def test_refuses_unparseable(self, bad):
        assert submit_workflow._parse_sbatch_parsable(bad) is None

class TestSubmitRenderStagingLocation:
    @pytest.mark.integration
    def test_render_stage_dir_is_under_home_not_system_temp(self):
        import tempfile
        import agent.skills.submit_workflow as sw
        stage = sw._render_stage_dir().resolve()
        """Globus Connect Personal only scans its Accessible Folders (default
        $HOME) and REFUSES a system temp dir — that surfaced as a live
        `submit.upload_failed` on the first production run. Staging therefore
        goes to the workspace scratch zone, never to tempfile.gettempdir().

        The zone is checked, not the absolute prefix: under test the workspace IS
        redirected into pytest's tmp_path, which lives under the system temp dir.
        What keeps the REAL workspace Globus-readable is the $HOME guard that
        setup and the doctor share — see
        tests/test_workspace_resolution.py::test_home_containment_is_one_implementation.
        """
        from agent.skills import workspace
        sys_tmp = Path(tempfile.gettempdir()).resolve()
        assert stage != sys_tmp and stage.parent != sys_tmp, \
            f"staging {stage} is a bare system temp dir — Globus refuses to scan it"
        assert stage.is_relative_to(workspace.scratch_dir()), \
            f"staging {stage} is outside the workspace scratch zone"
