"""
L14 cheat-guards — the sbatch leg's parsing and validation surface (agent/skills/
submit_workflow.py), shared by run_production_pipeline, run_step_on_cluster and the
reference-data download job:

  - the remote run directory is validated (abs path, no `..`, no shell metachars)
    BEFORE any I/O
  - the `sbatch --parsable` parser extracts job_ids from clean and federated stdouts
    and refuses anything else with the raw stdout surfaced
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
