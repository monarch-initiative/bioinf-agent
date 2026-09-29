"""render_pipeline — the pipeline layer's MCP primitive.

The tool reads a SEALED workflow off the reports zone through the typed seam, derives
the record, renders into `<workspace>/pipelines/<name>/` and reports where. These tests
drive the tool function directly against a spec written the way seal writes it, in the
per-test sandbox workspace the conftest provides, and pin: the proven return and the
directory it names, the 1-based stage grouping at the surface, the forms default, the
refusals (no such workflow with the roster, unknown form, bad stage numbers, unknown
env, an edited directory) and that a derivation refusal reaches the caller with its own
code and remedy.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.mcp_tools import pipeline_tools as PT
from agent.skills import compute_access
from agent.skills import workspace
from agent.skills.pipeline_record import load_pipeline_record
from agent.skills.spec_writer import write_workflow_spec
from pipeline_fixtures import sealed_rnaseq_spec

ENV = {"name": "cluster", "type": "ssh", "host": "h", "user": "u",
       "apptainer_module": "apptainer/1.4.1", "nextflow_module": "nextflow/25.04.7",
       "slurm": {"account": "acct_demo", "partition": "cpu_part"}}


@pytest.fixture
def sealed() -> str:
    """The fixture spec written to the sandbox reports zone, as seal writes it."""
    spec = sealed_rnaseq_spec()
    out = write_workflow_spec(spec.model_dump(), {})
    assert "workflow_spec_path" in out, out
    return spec.workflow_name


def _tool(**kw) -> dict:
    return PT.render_pipeline(**kw)


class TestProven:
    def test_renders_the_plain_form_by_default_under_the_pipelines_zone(self, sealed):
        out = _tool(sealed_workflow=sealed)
        assert out["outcome"] == "proven" and out["code"] == "pipeline.rendered", out
        d = Path(out["dir"])
        assert d == workspace.pipelines_dir() / sealed
        assert out["forms"] == ["plain"]
        assert "run_all.sh" in out["files"] and "main.nf" not in out["files"]
        assert (d / "pipeline.html").is_file() and (d / "pipeline.yaml").is_file()
        assert (d / "MANIFEST.sha256").is_file()
        assert Path(out["page"]) == d / "pipeline.html"
        assert out["shape"] == "per_row"
        assert [s["name"] for s in out["stages"]] == ["HISAT2", "SAMTOOLS", "HTSEQ_COUNT"]
        assert all(s["sized"] is False for s in out["stages"])
        assert out["samplesheet_columns"] == ["sample", "reads"] and out["example_rows"] == 3
        assert {p["name"]: p["default"] for p in out["params"]}["STRANDED"] == "reverse"
        assert out["replaced_previous_render"] is False

    def test_nextflow_is_a_request(self, sealed):
        out = _tool(sealed_workflow=sealed, forms=["plain", "nextflow"])
        assert out["outcome"] == "proven"
        assert out["forms"] == ["plain", "nextflow"]
        assert {"main.nf", "nextflow.config", "run_all.sh"} <= set(out["files"])

    def test_name_picks_the_directory_and_the_record_name(self, sealed):
        out = _tool(sealed_workflow=sealed, name="rnaseq_v2")
        assert Path(out["dir"]) == workspace.pipelines_dir() / "rnaseq_v2"
        assert load_pipeline_record(Path(out["record"])).name == "rnaseq_v2"

    def test_stages_are_grouped_by_one_based_how_to_command_numbers(self, sealed):
        out = _tool(sealed_workflow=sealed, stages=[[1, 2], [3]], stage_names=["ALIGN", "COUNT"])
        assert out["outcome"] == "proven", out
        assert [s["name"] for s in out["stages"]] == ["ALIGN", "COUNT"]
        assert len(out["stages"][0]["commands"]) == 2

    def test_resources_size_a_stage(self, sealed):
        out = _tool(sealed_workflow=sealed,
                    resources={"HISAT2": {"cpus": 4, "mem": "16G", "time": "2:00:00"}})
        sized = {s["name"]: s["sized"] for s in out["stages"]}
        assert sized == {"HISAT2": True, "SAMTOOLS": False, "HTSEQ_COUNT": False}

    def test_env_reaches_the_cluster_files(self, sealed, monkeypatch):
        monkeypatch.setattr(compute_access, "load_access", lambda path=None: {"compute_envs": [ENV]})
        monkeypatch.setattr(compute_access, "get_compute_env", lambda name, access: ENV)
        out = _tool(sealed_workflow=sealed, env="cluster")
        assert out["outcome"] == "proven", out
        sbatch = (Path(out["dir"]) / "stages" / "01_hisat2.sbatch").read_text()
        assert "acct_demo" in sbatch and "apptainer/1.4.1" in sbatch

    def test_re_render_of_an_unedited_directory_replaces_it(self, sealed):
        _tool(sealed_workflow=sealed)
        out = _tool(sealed_workflow=sealed)
        assert out["outcome"] == "proven" and out["replaced_previous_render"] is True


class TestRefusals:
    def test_no_such_sealed_workflow_lists_the_roster(self, sealed):
        out = _tool(sealed_workflow="nope")
        assert out["outcome"] == "refused" and out["code"] == "pipeline.no_sealed_workflow"
        assert out["available_workflows"] == [sealed]
        assert "seal_workflow" in out["remedy"]

    def test_unknown_form(self, sealed):
        out = _tool(sealed_workflow=sealed, forms=["snakemake"])
        assert out["code"] == "pipeline.unknown_form" and "nextflow" in out["remedy"]

    @pytest.mark.parametrize("stages", [[[0, 1]], [[1, "x"]], []])
    def test_bad_stage_numbers(self, sealed, stages):
        out = _tool(sealed_workflow=sealed, stages=stages)
        assert out["outcome"] == "refused" and out["code"] == "pipeline.bad_stages"

    def test_unknown_env(self, sealed):
        out = _tool(sealed_workflow=sealed, env="no_such_env")
        assert out["code"] == "pipeline.env_unknown" and "projects_access.yaml" in out["remedy"]

    def test_a_derivation_refusal_reaches_the_caller_with_its_code(self, sealed):
        out = _tool(sealed_workflow=sealed, stages=[[1, 3], [2]])
        assert out["outcome"] == "refused"
        assert out["code"] == "pipeline.bad_stage_groups", out
        assert out["remedy"] and out["sealed_workflow"] == sealed

    def test_an_edited_directory_is_refused_until_overwrite(self, sealed):
        first = _tool(sealed_workflow=sealed)
        env_file = Path(first["dir"]) / "params.env"
        env_file.write_text(env_file.read_text() + "STRANDED=yes\n")
        out = _tool(sealed_workflow=sealed)
        assert out["code"] == "pipeline.dir_edited" and "params.env" in out["error"]
        assert "STRANDED=yes" in env_file.read_text()
        out = _tool(sealed_workflow=sealed, overwrite=True)
        assert out["outcome"] == "proven" and "STRANDED=yes" not in env_file.read_text()

    def test_a_malformed_spec_on_disk_is_refused_not_scraped(self, sealed):
        path = workspace.reports_dir() / f"{sealed}.workflow.yaml"
        path.write_text("workflow_name: x\n")
        out = _tool(sealed_workflow=sealed)
        assert out["code"] == "pipeline.spec_invalid"
