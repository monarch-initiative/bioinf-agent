"""
L14 cheat-guards — run_production_pipeline, the production verb for a RENDERED pipeline.

The verb copies a rendered pipeline directory into `run_dir` on the env named, makes the
caller's samplesheet its samples.csv, checks the images and every bound path AT THE
LOCUS and the references against the seal, then launches the one way a rendered
pipeline runs (sbatch launcher.sh / nextflow run -profile local). Pure/surface tests —
no real docker, no ssh: every remote probe is a fake that records what it was asked.

They pin:
  - the gates before anything is copied: unknown pipeline, unsafe run_dir, the
    authorization wall (upload AND exec; never common_data / container zones), a
    parent that does not exist, the samplesheet's columns and path cells, an unknown
    or unsafe params override, a missing image, a missing bound path
  - the run directory contract: first launch copies + requires a samplesheet; the
    same render again re-launches without copying and refuses a second samplesheet;
    another render in the directory is refused; the template itself is never run in
  - the launch: the exact nextflow line locally (overrides on the line), the sbatch
    tokens on the cluster, the manifest, the follow-up
  - the data pins: a rebound reference launches as `degraded`, a matching one proven
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import yaml

from agent.models.core_data import WorkflowSpec
from agent.skills import cluster_jobs, run_production, stage_apptainer, submit_workflow, transfer, workspace
from agent.skills.pipeline_record import derive_pipeline_record
from agent.skills.pipeline_render import MANIFEST_PATH, render_pipeline_dir
from pipeline_fixtures import sealed_rnaseq_spec


# ===========================================================================
# Fixtures: a rendered pipeline in the pipelines zone, a samplesheet, access files
# ===========================================================================

def _render(tmp_path: Path, name: str = "rnaseq_counts", *, compute_env=None, sif_paths=None,
            spec=None) -> Path:
    """A rendered pipeline under the (sandboxed) pipelines zone. Its sealed spec is
    written where the record points so the data-pin check can read it."""
    spec = spec if spec is not None else sealed_rnaseq_spec()
    spec_path = tmp_path / f"{name}.workflow.yaml"
    spec_path.write_text(yaml.safe_dump(spec.model_dump()))
    record = derive_pipeline_record(
        spec, name=name, spec_path=str(spec_path),
        sif_paths=sif_paths, compute_env=compute_env,
        modules=["apptainer/1.5.0", "nextflow/25.04.7"] if compute_env else None,
        local_runtime=_local_runtime(tmp_path))
    out = workspace.pipelines_dir() / name
    render_pipeline_dir(record, out)
    return out


def _local_runtime(tmp_path: Path):
    from agent.skills.pipeline_record import LocalRuntime
    act = tmp_path / "activate.sh"
    act.write_text("export PATH=/nowhere:$PATH\n")
    return LocalRuntime(activate=str(act), nextflow="/nowhere/nextflow")


def _refs(tmp_path: Path) -> dict:
    """The fixture's shared references live under /data, which this machine must never
    grow: a local run binds these instead, through the params override."""
    idx = tmp_path / "idx"; idx.mkdir(exist_ok=True)
    (idx / "chr22.1.ht2").write_text("ht2")
    gtf = tmp_path / "chr22.gtf"
    if not gtf.exists():
        gtf.write_text("chr22\tgene\n")
    return {"hisat2_index": str(idx / "chr22"), "gtf": str(gtf)}


def _sheet(tmp_path: Path, reads: list[str], name="samples.csv") -> str:
    p = tmp_path / name
    rows = ["sample,reads"] + [f"s{i},{r}" for i, r in enumerate(reads)]
    p.write_text("\n".join(rows) + "\n")
    return str(p)


def _write(tmp_path: Path, data: dict) -> str:
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(data))
    return str(p)


def _local_access(tmp_path: Path, granted_dir: str, perms=("file_name_only", "upload", "download", "exec"),
                  pipelines_zone: str | None = None) -> str:
    env = {"name": "laptop", "type": "local", "container_upload_target": None}
    if pipelines_zone:
        env["agent_pipelines_target"] = {"path": pipelines_zone,
                                         "permissions": ["file_name_only", "upload", "download", "exec"],
                                         "description": "runs"}
    return _write(tmp_path, {
        "compute_envs": [env],
        "projects": [{"name": "demo", "compute_envs": ["laptop"],
                      "directories": [{"env": "laptop", "path": granted_dir, "permissions": list(perms),
                                       "description": "x"}]}],
    })


def _ssh_access(tmp_path: Path) -> str:
    env = {"name": "hpc", "type": "ssh", "host": "h.example.edu", "user": "u",
           "apptainer_module": "apptainer/1.5.0", "nextflow_module": "nextflow/25.04.7",
           "container_upload_target": {"path": "/work/containers", "permissions": ["upload"]},
           "agent_pipelines_target": {"path": "/work/pipelines",
                                      "permissions": ["file_name_only", "upload", "download", "exec"]}}
    return _write(tmp_path, {
        "compute_envs": [env],
        "projects": [{"name": "demo", "compute_envs": ["hpc"],
                      "directories": [{"env": "hpc", "path": "/work/demo", "permissions": ["upload", "exec"],
                                       "description": "x"}]}],
    })


class _FakeJobManager:
    def __init__(self):
        self.calls = []
        self.running_in = ""        # a run directory a job of ours is still running in

    def list_jobs(self, include_terminated=True):
        return ([{"job_id": "pipeline_x", "state": "running", "working_dir": self.running_in}]
                if self.running_in else [])

    def start(self, command, env_name="", job_id="", working_dir="", tool=""):
        self.calls.append({"command": command, "job_id": job_id, "working_dir": working_dir, "tool": tool})
        return {"job_id": job_id or "j1", "state": "running"}


@pytest.fixture
def local_ok(monkeypatch):
    """Docker present, every image present, every path present: the laptop happy path."""
    import agent.mcp_server as ms
    monkeypatch.setattr(ms, "_check_docker_available", lambda: None)
    monkeypatch.setattr(run_production, "_docker_image_present", lambda image: True)


def _run_local(tmp_path, pipeline, run_dir, *, samplesheet="", params=None, access=None, jm=None, refs=True):
    overrides = {**(_refs(tmp_path) if refs else {}), **(params or {})}
    return run_production.run_production_pipeline(
        project_name="demo", compute_env_name="laptop", pipeline=pipeline, run_dir=str(run_dir),
        samplesheet=samplesheet, params=overrides, access_path=access, _job_manager=jm or _FakeJobManager())


# ===========================================================================
# Gates before anything is copied
# ===========================================================================

class TestGates:
    @pytest.mark.integration
    def test_unknown_pipeline_refused_with_what_is_rendered(self, tmp_path, local_ok):
        _render(tmp_path, "have")
        d = tmp_path / "proj"; d.mkdir()
        r = _run_local(tmp_path, "nope", d / "run1", access=_local_access(tmp_path, str(d)))
        assert r["outcome"] == "refused" and r["code"] == "run_production.no_rendered_pipeline"
        assert r["rendered_pipelines"] == ["have"]

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", ["relative/run", "/run;rm -rf /", "/run dir"])
    def test_unsafe_run_dir_refused(self, tmp_path, local_ok, bad):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        r = _run_local(tmp_path, "rnaseq_counts", bad, access=_local_access(tmp_path, str(d)))
        assert r["outcome"] == "refused" and r["code"] == "run_production.refused"

    @pytest.mark.integration
    def test_unauthorized_dir_refused_as_a_refusal(self, tmp_path, local_ok):
        _render(tmp_path)
        granted = tmp_path / "granted"; granted.mkdir()
        other = tmp_path / "other"; other.mkdir()
        r = _run_local(tmp_path, "rnaseq_counts", other / "run1", access=_local_access(tmp_path, str(granted)))
        assert r["outcome"] == "refused" and r["code"] == "run_production.refused"
        assert "not authorized" in r["error"]

    @pytest.mark.integration
    def test_upload_only_dir_refused_for_exec(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1",
                       access=_local_access(tmp_path, str(d), perms=("file_name_only", "upload")))
        assert r["outcome"] == "refused" and "exec" in r["error"]

    @pytest.mark.integration
    def test_a_reference_zone_is_not_a_run_directory(self, tmp_path, local_ok):
        _render(tmp_path)
        common = tmp_path / "common"; common.mkdir()
        ap = _write(tmp_path, {
            "compute_envs": [{"name": "laptop", "type": "local", "container_upload_target": None,
                              "agent_common_data_target": {"path": str(common),
                                                           "permissions": ["file_name_only", "upload", "download", "exec"]}}],
            "projects": [{"name": "demo", "compute_envs": ["laptop"], "directories": []}]})
        r = _run_local(tmp_path, "rnaseq_counts", common / "run1", access=ap)
        assert r["code"] == "run_production.run_dir_zone"

    @pytest.mark.integration
    def test_a_run_dir_whose_parent_is_missing_is_created_under_the_grant(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        run_dir = d / "not_yet" / "run1"
        r = _run_local(tmp_path, "rnaseq_counts", run_dir, samplesheet=_sheet(tmp_path, [str(fq)]),
                       access=_local_access(tmp_path, str(d)))
        assert r["outcome"] == "proven" and r["code"] == "run_production.local_launched", r
        assert (run_dir / "main.nf").is_file() and (run_dir / "samples.csv").is_file()

    @pytest.mark.integration
    def test_a_run_dir_outside_the_grant_is_never_created(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        run_dir = tmp_path / "elsewhere" / "run1"
        r = _run_local(tmp_path, "rnaseq_counts", run_dir, samplesheet=_sheet(tmp_path, [str(fq)]),
                       access=_local_access(tmp_path, str(d)))
        assert r["outcome"] == "refused" and r["code"] == "run_production.refused"
        assert not run_dir.parent.exists()

    @pytest.mark.integration
    def test_the_first_launch_needs_a_samplesheet(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", access=_local_access(tmp_path, str(d)))
        assert r["code"] == "run_production.samplesheet_required"
        assert not (d / "run1" / "main.nf").exists()

    @pytest.mark.integration
    def test_samplesheet_missing_a_column_refused(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        bad = tmp_path / "bad.csv"; bad.write_text("sample,fastq\ns1,/x.fq\n")
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=str(bad),
                       access=_local_access(tmp_path, str(d)))
        assert r["code"] == "run_production.samplesheet_columns" and r["required_columns"] == ["sample", "reads"]

    @pytest.mark.integration
    def test_samplesheet_relative_path_refused(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, ["data/r1.fq"]),
                       access=_local_access(tmp_path, str(d)))
        assert r["code"] == "run_production.samplesheet_relative_path"

    @pytest.mark.integration
    @pytest.mark.parametrize("sample", ["WT rep1", "../escape", "s0;touch $HOME/x", ".hidden"])
    def test_a_sample_name_that_cannot_name_a_directory_is_refused(self, tmp_path, local_ok, sample):
        """`sample` names results/<sample>/ and is pasted into commands (`> ${meta.sample}.counts.tsv`):
        a space breaks the command, `..` writes outside results/, `;` runs a second command."""
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        sheet = tmp_path / "bad.csv"
        sheet.write_text(f'sample,reads\n{sample},{fq}\n')
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=str(sheet),
                       access=_local_access(tmp_path, str(d)))
        assert (r["outcome"], r["code"]) == ("refused", "run_production.samplesheet_unsafe_cell"), r
        assert "starting with a letter or digit" in r["error"], r
        assert not (d / "run1").exists(), "refused before anything was copied"

    @pytest.mark.integration
    def test_a_spreadsheets_byte_order_mark_is_read_as_nextflow_reads_it(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        sheet = tmp_path / "excel.csv"
        sheet.write_text(f"\ufeffsample,reads\ns1,{fq}\n", encoding="utf-8")
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=str(sheet),
                       access=_local_access(tmp_path, str(d)))
        assert r["outcome"] == "proven", r

    @pytest.mark.integration
    @pytest.mark.parametrize("row,code", [
        ('"s1",{fq}', "samplesheet_unsafe_cell"),
        ("s1, {fq}", "samplesheet_unsafe_cell"),
        ("s1,{fq} ", "samplesheet_unsafe_cell"),
        ("s1,{fq},extra", "samplesheet_malformed"),
        ("s1", "samplesheet_malformed"),
        ("s1,{fq},", "samplesheet_malformed"),
    ], ids=["quoted", "padded", "trailing_space", "extra_field", "missing_field", "empty_column_name"])
    def test_a_sheet_nextflow_reads_differently_is_refused(self, tmp_path, local_ok, row, code):
        """Nextflow's splitCsv(header: true) keeps quotes and spaces in the value, drops
        a row's extra fields and reads a missing one as empty; Python's csv unquotes,
        and a gate that read the sheet that way would pass what the run then breaks on."""
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        sheet = tmp_path / "odd.csv"
        head = "sample,reads," if row.endswith(",") else "sample,reads"
        sheet.write_text(head + "\n" + row.format(fq=fq) + "\n")
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=str(sheet),
                       access=_local_access(tmp_path, str(d)))
        assert (r["outcome"], r["code"]) == ("refused", f"run_production.{code}"), r
        assert not (d / "run1").exists()

    @pytest.mark.integration
    def test_a_relaunch_holds_its_own_samplesheet_to_the_same_rules(self, tmp_path, local_ok):
        """A re-launch runs the samples.csv already in the run directory; a path in it
        that is not there is refused as it would be on a first launch."""
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        ap = _local_access(tmp_path, str(d))
        assert _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                          access=ap)["success"]
        (d / "run1" / "samples.csv").write_text(f"sample,reads\nS9,{d}/nowhere/r9.fq\n")
        again = _run_local(tmp_path, "rnaseq_counts", d / "run1", access=ap)
        assert again["outcome"] == "refused" and "r9.fq" in again["error"], again

    @pytest.mark.integration
    @pytest.mark.parametrize("outdir", ["{elsewhere}", "../../elsewhere"])
    def test_results_published_outside_the_grant_are_refused(self, tmp_path, local_ok, outdir):
        """outdir is where the job writes; outside the run directory it needs the
        grant a run directory needs, whether named absolutely or with `..`."""
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                       params={"outdir": outdir.format(elsewhere=tmp_path / "elsewhere")},
                       access=_local_access(tmp_path, str(d)))
        assert r["outcome"] == "refused" and "elsewhere" in r["error"], r
        assert not (d / "run1").exists()

    @pytest.mark.integration
    def test_results_outside_the_run_need_exec_only_and_name_the_outdir(self, tmp_path, local_ok):
        """The job writes the results; nothing is uploaded there. An exec-only grant is
        enough, and a grant without exec is refused naming the outdir."""
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        out = tmp_path / "out"; out.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        for perms, ok in ((["exec"], True), (["download"], False)):
            ap = _write(tmp_path, {
                "compute_envs": [{"name": "laptop", "type": "local", "container_upload_target": None}],
                "projects": [{"name": "demo", "compute_envs": ["laptop"], "directories": [
                    {"env": "laptop", "path": str(d), "permissions": ["upload", "exec"], "description": "x"},
                    {"env": "laptop", "path": str(out), "permissions": perms, "description": "x"}]}]})
            run = d / f"run_{perms[0]}"
            r = _run_local(tmp_path, "rnaseq_counts", run, samplesheet=_sheet(tmp_path, [str(fq)]),
                           params={"outdir": str(out / perms[0])}, access=ap)
            assert r.get("success", False) is ok, r
            if not ok:
                assert str(out / perms[0]) in r["error"] and "main.nf" not in r["error"], r

    @pytest.mark.integration
    def test_results_in_the_reference_data_zone_are_refused(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        common = tmp_path / "common"; common.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        ap = _write(tmp_path, {
            "compute_envs": [{"name": "laptop", "type": "local", "container_upload_target": None,
                              "agent_common_data_target": {"path": str(common),
                                                           "permissions": ["upload", "download", "exec"]}}],
            "projects": [{"name": "demo", "compute_envs": ["laptop"], "directories": [
                {"env": "laptop", "path": str(d), "permissions": ["upload", "exec"], "description": "x"}]}]})
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                       params={"outdir": str(common / "results")}, access=ap)
        assert (r["outcome"], r["code"]) == ("refused", "run_production.outdir_zone"), r

    @pytest.mark.integration
    def test_an_empty_outdir_is_refused(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                       params={"outdir": ""}, access=_local_access(tmp_path, str(d)))
        assert (r["outcome"], r["code"]) == ("refused", "run_production.outdir_empty"), r
        assert not (d / "run1").exists()

    @pytest.mark.integration
    def test_the_results_path_reported_is_where_the_run_writes(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                       params={"outdir": str(d / "shared_results")}, access=_local_access(tmp_path, str(d)))
        assert r["success"] and r["follow_up"]["results"] == f"{d / 'shared_results'}/", r["follow_up"]

    @pytest.mark.integration
    def test_a_bound_path_that_is_not_there_refused(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, ["/nowhere/r1.fq"]),
                       access=_local_access(tmp_path, str(d)))
        assert r["code"] == "run_production.inputs_missing" and "/nowhere/r1.fq" in r["missing_inputs"]

    @pytest.mark.integration
    def test_unknown_and_unsafe_param_overrides_refused(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("@r\nA\n+\nF\n")
        ap = _local_access(tmp_path, str(d))
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                       params={"not_a_param": 1}, access=ap)
        assert r["code"] == "run_production.unknown_param" and "stranded" in r["params"]
        assert "hisat2_index" in r["params"] and "samplesheet" not in r["params"]
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                       params={"stranded": "yes; rm -rf /"}, access=ap)
        assert r["code"] == "run_production.unsafe_param_value"
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                       params={"samplesheet": "/elsewhere.csv"}, access=ap)
        assert r["code"] == "run_production.unknown_param"

    @pytest.mark.integration
    def test_a_missing_docker_image_refused_before_any_copy(self, tmp_path, monkeypatch):
        import agent.mcp_server as ms
        monkeypatch.setattr(ms, "_check_docker_available", lambda: None)
        monkeypatch.setattr(run_production, "_docker_image_present", lambda image: False)
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                       access=_local_access(tmp_path, str(d)))
        assert r["code"] == "run_production.image_missing"
        assert "docker load" in r["remedy"] and not (d / "run1").exists()


# ===========================================================================
# The laptop: copy, launch, manifest; re-launch in the same directory
# ===========================================================================

class TestLocalLaunch:
    @pytest.mark.integration
    def test_copies_the_render_and_launches_nextflow_with_the_override(self, tmp_path, local_ok):
        pdir = _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        jm = _FakeJobManager()
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                       params={"stranded": "yes"}, access=_local_access(tmp_path, str(d)), jm=jm)
        # launched; and said so: the seal never ran stranded=yes
        assert (r["outcome"], r["code"]) == ("degraded", "run_production.values_unproven"), r
        run = d / "run1"
        for rel in ("main.nf", "nextflow.config", "params.yaml", "launcher.sh", "samples.csv", MANIFEST_PATH):
            assert (run / rel).is_file(), rel
        assert (run / "samples.csv").read_text().startswith("sample,reads\ns0,")
        assert (pdir / "samples.csv").read_text() != (run / "samples.csv").read_text()   # ours, not the example
        call = jm.calls[0]
        assert call["working_dir"] == str(run) and call["tool"] == "run_production_pipeline"
        assert call["command"].endswith("nextflow run main.nf -profile local -params-file params.yaml -resume")
        # YAML 1.1 (Nextflow's reader) takes a bare `yes` as a boolean: the word is quoted
        assert "\nstranded: 'yes'\n" in (run / "params.yaml").read_text()
        assert call["command"].startswith("source ")
        assert r["params"]["stranded"] == "yes" and r["reused_run_dir"] is False
        assert Path(r["manifest_path"]).is_file()
        manifest = __import__("json").loads(Path(r["manifest_path"]).read_text())
        assert manifest["pipeline"] == "rnaseq_counts" and manifest["samplesheet"]["rows"] == 1
        assert manifest["params_overridden"]["stranded"] == "yes"
        assert "check_job" in r["follow_up"]["poll"]

    @pytest.mark.integration
    def test_a_value_the_seal_never_ran_is_named_beside_the_launch(self, tmp_path, local_ok):
        """The sealed self-test ran STRANDED=reverse only: a launch in another mode is a
        legitimate run of a how-to that was never proven in it, and the result says so
        (paths and sample names are not modes and are never named)."""
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        ap = _local_access(tmp_path, str(d))
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                       params={"stranded": "yes"}, access=ap)
        assert (r["outcome"], r["code"]) == ("degraded", "run_production.values_unproven"), r
        assert r["value_check"] == {"status": "checked",
                                    "unproven": [{"param": "STRANDED", "values": ["yes"], "proven": ["reverse"]}]}
        manifest = __import__("json").loads(Path(r["manifest_path"]).read_text())
        assert manifest["value_check"] == r["value_check"]
        again = _run_local(tmp_path, "rnaseq_counts", d / "run1", access=ap)     # the run's own params.yaml
        assert again["code"] == "run_production.values_unproven"
        same = _run_local(tmp_path, "rnaseq_counts", d / "run2", samplesheet=_sheet(tmp_path, [str(fq)]), access=ap)
        assert same["outcome"] == "proven" and same["value_check"] == {"status": "checked", "unproven": []}

    @pytest.mark.integration
    def test_the_same_directory_again_relaunches_without_copying(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        ap = _local_access(tmp_path, str(d))
        sheet = _sheet(tmp_path, [str(fq)])
        assert _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=sheet, access=ap)["success"]
        (d / "run1" / "samples.csv").write_text("sample,reads\nedited," + str(fq) + "\n")
        again = _run_local(tmp_path, "rnaseq_counts", d / "run1", access=ap)
        assert again["outcome"] == "proven" and again["reused_run_dir"] is True
        assert (d / "run1" / "samples.csv").read_text().startswith("sample,reads\nedited")   # untouched
        with_sheet = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=sheet, access=ap)
        assert with_sheet["code"] == "run_production.samplesheet_already_there"
        changed = _run_local(tmp_path, "rnaseq_counts", d / "run1", params={"stranded": "no"}, access=ap)
        assert changed["files_rewritten"] == ["params.yaml"]
        assert "\nstranded: 'no'\n" in (d / "run1" / "params.yaml").read_text()

    @pytest.mark.integration
    def test_an_existing_directory_that_holds_no_run_is_refused(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        (d / "run1").mkdir()
        (d / "run1" / "params.yaml").write_text("mine: 1\n")
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                       access=_local_access(tmp_path, str(d)))
        assert (r["outcome"], r["code"]) == ("refused", "run_production.run_dir_exists"), r
        assert (d / "run1" / "params.yaml").read_text() == "mine: 1\n"

    @pytest.mark.integration
    def test_a_directory_holding_part_of_this_render_is_refused(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        ap = _local_access(tmp_path, str(d))
        assert _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                          access=ap)["success"]
        (d / "run1" / "params.yaml").unlink()
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", access=ap)
        assert (r["outcome"], r["code"]) == ("refused", "run_production.run_dir_incomplete"), r
        assert "params.yaml" in r["error"]

    @pytest.mark.integration
    def test_a_walltime_or_a_param_off_its_own_line_is_refused_not_ignored(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        ap = _local_access(tmp_path, str(d))
        sheet = _sheet(tmp_path, [str(fq)])
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="laptop", pipeline="rnaseq_counts", run_dir=str(d / "run1"),
            samplesheet=sheet, walltime="01:00:00", access_path=ap, _job_manager=_FakeJobManager())
        assert r["code"] == "run_production.walltime_on_local"
        assert _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=sheet, access=ap)["success"]
        params = d / "run1" / "params.yaml"
        params.write_text(params.read_text().replace("\nstranded: reverse\n", "\nstranded: >-\n  reverse\n"))
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", params={"stranded": "no"}, access=ap)
        assert r["code"] == "run_production.param_line_not_found", r
        assert "stranded: >-" in params.read_text(), "nothing written"

    @pytest.mark.integration
    def test_a_directory_holding_another_render_is_refused(self, tmp_path, local_ok):
        _render(tmp_path, "a")
        _render(tmp_path, "b", spec=sealed_rnaseq_spec(["SRR1039508"]))
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        ap = _local_access(tmp_path, str(d))
        assert _run_local(tmp_path, "a", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]), access=ap)["success"]
        r = _run_local(tmp_path, "b", d / "run1", access=ap)
        assert r["code"] == "run_production.run_dir_holds_another_render"

    @pytest.mark.integration
    def test_the_template_itself_is_never_run_in(self, tmp_path, local_ok):
        pdir = _render(tmp_path)
        fq = tmp_path / "r1.fq"; fq.write_text("x")
        ap = _local_access(tmp_path, str(tmp_path), pipelines_zone=str(workspace.pipelines_dir()))
        r = _run_local(tmp_path, "rnaseq_counts", pdir, samplesheet=_sheet(tmp_path, [str(fq)]), access=ap)
        assert r["code"] == "run_production.run_dir_is_the_template"

    @pytest.mark.integration
    def test_edits_to_the_template_are_disclosed_not_refused(self, tmp_path, local_ok):
        pdir = _render(tmp_path)
        (pdir / "params.yaml").write_text((pdir / "params.yaml").read_text() + "# edited\n")
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                       access=_local_access(tmp_path, str(d)))
        assert r["success"] and r["edited_since_render"] == ["params.yaml"]

    @pytest.mark.integration
    def test_a_relaunch_discloses_edits_in_the_run_directory_not_the_template(self, tmp_path, local_ok):
        """What a re-launch runs is the run directory's copy; a hand edit there is
        what the record must name, whatever the template looks like."""
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        ap = _local_access(tmp_path, str(d))
        assert _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                          access=ap)["success"]
        (d / "run1" / "main.nf").write_text((d / "run1" / "main.nf").read_text() + "// tuned by hand\n")
        again = _run_local(tmp_path, "rnaseq_counts", d / "run1", access=ap)
        assert again["reused_run_dir"] is True and again["edited_since_render"] == ["main.nf"]
        # launcher.sh: the time line a launch's walltime= writes is not an edit; a hand edit is
        launcher = d / "run1" / "launcher.sh"
        launcher.write_text(re.sub(r"(?m)^#SBATCH --time=\S*", "#SBATCH --time=2:00:00", launcher.read_text()))
        assert "#SBATCH --time=2:00:00" in launcher.read_text()
        assert _run_local(tmp_path, "rnaseq_counts", d / "run1", access=ap)["edited_since_render"] == ["main.nf"]
        launcher.write_text(launcher.read_text().replace("#!/usr/bin/env bash\n", "#!/usr/bin/env bash\nulimit -n 4096\n", 1))
        assert _run_local(tmp_path, "rnaseq_counts", d / "run1",
                          access=ap)["edited_since_render"] == ["launcher.sh", "main.nf"]


# ===========================================================================
# The cluster: fakes for every remote probe; the sbatch tokens
# ===========================================================================

class _Remote:
    """Stands in for the cluster: which paths exist, which sifs are staged, what the run
    directory holds, and what sbatch was asked."""
    def __init__(self, *, existing=(), sifs=(), run_state=None):
        self.existing = set(existing)
        self.sifs = set(sifs)
        # `missing: None` stands for "every carried file": nothing of the render is there.
        self.run_state = run_state or {"exists": False, "missing": None, "samplesheet": False, "texts": {}}
        self.writes = {}    # files a re-launch rewrote in place: {remote path: text}
        self.uploads = []
        self.sent = {}      # what the uploads carried: {remote path: bytes}
        self.upload_error = ""
        self.upload_outcome = "loop"
        self.sbatch = None

    def install(self, monkeypatch):
        def paths_exist(env, paths, *, timeout=120):
            missing = [p for p in paths if p not in self.existing]
            return ({"ok": True, "checked": list(paths)} if not missing
                    else {"outcome": "refused", "code": "cluster.inputs_missing", "error": "x", "missing_paths": missing})
        monkeypatch.setattr(cluster_jobs, "remote_paths_exist", paths_exist)
        monkeypatch.setattr(run_production, "_remote_prefixes_exist",
                            lambda env, prefixes, *, timeout: {"ok": True, "missing": []})
        monkeypatch.setattr(stage_apptainer, "_remote_sif_exists", lambda env, sif, *, timeout=120: sif in self.sifs)
        def run_dir_state(env, run_dir, rels, *, timeout):
            missing = self.run_state["missing"]
            texts = {rel: self.run_state["texts"].get(rel, "") for rel in run_production._RUN_TEXTS}
            return {**self.run_state, "missing": list(rels) if missing is None else list(missing),
                    "texts": texts, "live": self.run_state.get("live", []),
                    "sums": self.run_state.get("sums", {})}
        monkeypatch.setattr(run_production, "_remote_run_dir_state", run_dir_state)
        monkeypatch.setattr(run_production, "_write_remote",
                            lambda env, run_dir, texts, *, timeout: self.writes.update(
                                {f"{run_dir}/{rel}": text for rel, text in texts.items()}))
        monkeypatch.setattr(run_production, "_observe_cluster",
                            lambda env, paths, *, timeout: {p: {"exists": True, "sha256": ""} for p in paths})

        def upload(project_name, compute_env_name, local_path, remote_abs_path, access_path=None, timeout=600):
            self.uploads.append((local_path, remote_abs_path))
            if self.upload_error:
                return {"outcome": self.upload_outcome, "error": self.upload_error,
                        "task_id": "33333333-3333-3333-3333-333333333333"}
            src = Path(local_path)
            files = sorted(f for f in src.rglob("*") if f.is_file()) if src.is_dir() else [src]
            for f in files:
                rel = f.relative_to(src).as_posix() if src.is_dir() else ""
                self.sent[f"{remote_abs_path}/{rel}".rstrip("/")] = f.read_bytes()
            return {"remote_abs_path": remote_abs_path}
        monkeypatch.setattr(transfer, "upload", upload)

        def sbatch(env, workflow_dir, *, timeout=300, sbatch_args=()):
            self.sbatch = {"dir": workflow_dir, "sbatch_args": list(sbatch_args)}
            return {"job_id": "4242", "launcher": f"{workflow_dir}/launcher.sh",
                    "sbatch_command": f"sbatch --parsable {' '.join(sbatch_args)} launcher.sh"}
        monkeypatch.setattr(submit_workflow, "sbatch_via_ssh", sbatch)
        return self


_SIF = {"fr_rnaseq_cli_0001": "/work/containers/rnaseq_counts_abc.sif"}     # the fixture's request key
_REFS = {"/data/annotation/chr22.gtf"}     # the rendered params.yaml's shared path on the cluster


def _run_ssh(tmp_path, pipeline, run_dir, *, samplesheet="", params=None, walltime="", access=None):
    return run_production.run_production_pipeline(
        project_name="demo", compute_env_name="hpc", pipeline=pipeline, run_dir=run_dir,
        samplesheet=samplesheet, params=params, walltime=walltime, access_path=access or _ssh_access(tmp_path))


class TestClusterLaunch:
    @pytest.mark.integration
    def test_rendered_for_another_env_refused(self, tmp_path, monkeypatch):
        _Remote().install(monkeypatch)
        _render(tmp_path, compute_env="other_cluster", sif_paths=_SIF)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1",
                     samplesheet=_sheet(tmp_path, ["/data/r1.fq"]))
        assert r["code"] == "run_production.rendered_for_other_env"

    @pytest.mark.integration
    def test_rendered_without_a_cluster_refused(self, tmp_path, monkeypatch):
        _Remote().install(monkeypatch)
        _render(tmp_path)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1",
                     samplesheet=_sheet(tmp_path, ["/data/r1.fq"]))
        assert r["code"] == "run_production.rendered_without_cluster" and "env='hpc'" in r["remedy"]

    @pytest.mark.integration
    def test_sif_not_staged_refused_with_the_staging_call(self, tmp_path, monkeypatch):
        _Remote(existing={"/work/pipelines", "/data/r1.fq"}).install(monkeypatch)
        _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1",
                     samplesheet=_sheet(tmp_path, ["/data/r1.fq"]))
        assert r["code"] == "run_production.sif_not_staged"
        assert "stage_apptainer_image(project_name='demo', compute_env_name='hpc', freeze_request_key=" in r["remedy"]

    @pytest.mark.integration
    def test_rendered_for_a_zone_the_env_no_longer_uses_says_rerender(self, tmp_path, monkeypatch):
        """The access file moved the env's container zone after the render. Staging writes
        into the NEW zone, so a "stage it" refusal would never be satisfied; the remedy is
        the re-render, named with the record's own arguments."""
        old = {"fr_rnaseq_cli_0001": "/old/containers/rnaseq_counts_abc.sif"}
        _Remote(existing={"/work/pipelines", "/data/r1.fq"} | _REFS, sifs=set(old.values())).install(monkeypatch)
        _render(tmp_path, compute_env="hpc", sif_paths=old)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1",
                     samplesheet=_sheet(tmp_path, ["/data/r1.fq"]))
        assert r["code"] == "run_production.rendered_for_other_zone" and r["container_zone"] == "/work/containers"
        assert r["sif_paths"] == ["/old/containers/rnaseq_counts_abc.sif"]
        assert "render_pipeline(sealed_workflow='rnaseq_counts_workflow', name='rnaseq_counts', env='hpc', overwrite=True)" in r["remedy"]

    @pytest.mark.integration
    def test_a_sheet_path_missing_on_the_cluster_refused(self, tmp_path, monkeypatch):
        _Remote(existing={"/work/pipelines"} | _REFS, sifs=set(_SIF.values())).install(monkeypatch)
        _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1",
                     samplesheet=_sheet(tmp_path, ["/data/r1.fq"]))
        assert r["code"] == "run_production.inputs_missing" and r["locus"] == "cluster"
        assert "/data/r1.fq" in r["missing_inputs"]

    @pytest.mark.integration
    def test_the_run_dir_is_made_by_one_upload_of_the_whole_run(self, tmp_path, monkeypatch):
        # /work/pipelines/new is not on the cluster yet: the launch goes ahead, and ONE
        # transfer of the assembled run directory creates it (parents included).
        remote = _Remote(existing={"/data/r1.fq"} | _REFS, sifs=set(_SIF.values())).install(monkeypatch)
        _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/new/run1",
                     samplesheet=_sheet(tmp_path, ["/data/r1.fq"]))
        assert r["outcome"] == "proven" and r["code"] == "run_production.submitted", r
        assert [dst for _, dst in remote.uploads] == ["/work/pipelines/new/run1"]
        assert "/work/pipelines/new/run1/main.nf" in remote.sent
        assert sorted(r["files_uploaded"]) == sorted(remote.sent)
        assert remote.sbatch["dir"] == "/work/pipelines/new/run1"

    @pytest.mark.integration
    def test_an_existing_directory_that_holds_no_run_is_refused(self, tmp_path, monkeypatch):
        """A launch never writes over files it did not put there: an existing directory is
        either this pipeline's run (a re-launch) or refused before a byte moves."""
        remote = _Remote(existing={"/data/r1.fq"} | _REFS, sifs=set(_SIF.values()),
                         run_state={"exists": True, "missing": None, "samplesheet": False,
                                    "texts": {}}).install(monkeypatch)
        _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1",
                     samplesheet=_sheet(tmp_path, ["/data/r1.fq"]))
        assert (r["outcome"], r["code"]) == ("refused", "run_production.run_dir_exists"), r
        assert remote.uploads == [] and remote.sbatch is None

    @pytest.mark.integration
    def test_a_directory_its_first_copy_did_not_finish_is_never_launched(self, tmp_path, monkeypatch):
        pdir = _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        remote = _Remote(existing=_REFS, sifs=set(_SIF.values()),
                         run_state={"exists": True, "missing": ["nextflow.config"], "samplesheet": True,
                                    "texts": {MANIFEST_PATH: (pdir / MANIFEST_PATH).read_text()}}).install(monkeypatch)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1")
        assert (r["outcome"], r["code"]) == ("refused", "run_production.run_dir_incomplete"), r
        assert "nextflow.config" in r["error"] and remote.sbatch is None

    @pytest.mark.integration
    def test_an_upload_that_did_not_finish_keeps_its_staged_copy_and_says_what_to_do(self, tmp_path, monkeypatch):
        """A transfer whose end was not seen may still be reading the staged run
        directory, so it is never deleted under it; the result says the upload may
        still be running, names the copy, and the way on."""
        remote = _Remote(existing={"/data/r1.fq"} | _REFS, sifs=set(_SIF.values())).install(monkeypatch)
        remote.upload_error = "Globus task 3333 did not reach a terminal state within the 300s sync-wait cap"
        _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1",
                     samplesheet=_sheet(tmp_path, ["/data/r1.fq"]))
        assert (r["outcome"], r["code"]) == ("loop", "run_production.upload_still_running"), r
        assert (Path(r["staged"]) / "main.nf").is_file() and r["task_id"].startswith("3333")
        assert "same run_dir and no samplesheet" in r["remedy"] and remote.sbatch is None

    @pytest.mark.integration
    def test_an_upload_that_failed_is_a_failure_and_leaves_nothing_staged(self, tmp_path, monkeypatch):
        remote = _Remote(existing={"/data/r1.fq"} | _REFS, sifs=set(_SIF.values())).install(monkeypatch)
        remote.upload_error, remote.upload_outcome = "Globus task 3333 FAILED: ENDPOINT_ERROR", "broke"
        _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1",
                     samplesheet=_sheet(tmp_path, ["/data/r1.fq"]))
        assert (r["outcome"], r["code"]) == ("broke", "run_production.upload_failed"), r
        assert "staged" not in r and remote.sbatch is None

    @pytest.mark.integration
    def test_uploads_the_run_directory_with_the_overrides_written_into_it(self, tmp_path, monkeypatch):
        remote = _Remote(existing={"/work/pipelines", "/data/r1.fq", "/data/r2.fq"} | _REFS,
                         sifs=set(_SIF.values())).install(monkeypatch)
        pdir = _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        sheet = _sheet(tmp_path, ["/data/r1.fq", "/data/r2.fq"])
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1", samplesheet=sheet,
                     params={"stranded": "reverse", "outdir": "/work/pipelines/run1/out"}, walltime="1-00:00:00")
        assert r["outcome"] == "proven" and r["code"] == "run_production.submitted", r
        assert r["job_id"] == "4242" and r["locus"] == "cluster"
        assert len(remote.uploads) == 1, "the run directory goes up as one transfer"
        assert "/work/pipelines/run1/main.nf" in remote.sent and "/work/pipelines/run1/launcher.sh" in remote.sent
        assert f"/work/pipelines/run1/{MANIFEST_PATH}" in remote.sent
        # the run's samples.csv is the samplesheet given, never the template's example
        assert remote.sent["/work/pipelines/run1/samples.csv"] == Path(sheet).read_bytes()
        assert remote.sent["/work/pipelines/run1/samples.csv"] != (pdir / "samples.csv").read_bytes()
        # params= and walltime= are IN the run directory, so a launch by hand runs the same
        params_yaml = remote.sent["/work/pipelines/run1/params.yaml"].decode()
        assert "\nstranded: reverse\n" in params_yaml and "\noutdir: /work/pipelines/run1/out\n" in params_yaml
        assert params_yaml.startswith("# rendered from sealed workflow"), "comments are kept"
        launcher = remote.sent["/work/pipelines/run1/launcher.sh"].decode()
        assert "\n#SBATCH --time=1-00:00:00\n" in launcher
        assert remote.sbatch == {"dir": "/work/pipelines/run1", "sbatch_args": []}
        assert r["params"]["stranded"] == "reverse"
        assert "run_dir='/work/pipelines/run1'" in r["follow_up"]["poll"]
        assert r["samplesheet"]["rows"] == 2
        assert Path(r["manifest_path"]).is_file()
        # The index prefix names a family of files: observed as such (compgen), never as a
        # file that is "not there"; and the seal's own step built it, which the finding says.
        by_slot = {f["slot"]: f for f in r["reference_check"]["findings"]}
        assert by_slot["hisat2_index"]["exists"] is True
        assert by_slot["hisat2_index"]["verdict"] == "unanchored"
        assert by_slot["hisat2_index"]["reason"].startswith("produced by the sealed workflow's own step ")

    @pytest.mark.integration
    def test_a_project_directory_needs_exec_too(self, tmp_path, monkeypatch):
        _Remote(existing={"/work", "/data/r1.fq"} | _REFS, sifs=set(_SIF.values())).install(monkeypatch)
        _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        ap = _write(tmp_path, {
            "compute_envs": [{"name": "hpc", "type": "ssh", "host": "h", "user": "u",
                              "apptainer_module": "a/1", "nextflow_module": "n/1",
                              "container_upload_target": {"path": "/work/containers", "permissions": ["upload"]}}],
            "projects": [{"name": "demo", "compute_envs": ["hpc"],
                          "directories": [{"env": "hpc", "path": "/work/demo", "permissions": ["upload"],
                                           "description": "x"}]}]})
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/demo/run1", samplesheet=_sheet(tmp_path, ["/data/r1.fq"]),
                     access=ap)
        assert r["outcome"] == "refused" and "exec" in r["error"]

    @pytest.mark.integration
    def test_the_follow_up_names_only_a_status_call_the_grant_allows(self, tmp_path, monkeypatch):
        """cluster_job_status reads a run's tasks only where the project may download:
        under an upload+exec grant the poll names the job alone, and says why."""
        _Remote(existing={"/work/demo", "/data/r1.fq"} | _REFS, sifs=set(_SIF.values())).install(monkeypatch)
        _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/demo/run1", samplesheet=_sheet(tmp_path, ["/data/r1.fq"]))
        assert r["outcome"] == "proven", r
        assert "run_dir=" not in r["follow_up"]["poll"] and "download" in r["follow_up"]["tasks"], r["follow_up"]

    @pytest.mark.integration
    def test_relaunch_reuses_the_remote_directory(self, tmp_path, monkeypatch):
        pdir = _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        ours = (pdir / MANIFEST_PATH).read_text()
        texts = {MANIFEST_PATH: ours, "launcher.sh": (pdir / "launcher.sh").read_text(),
                 "params.yaml": (pdir / "params.yaml").read_text().replace("\nstranded: reverse\n", "\nstranded: 'yes'\n"),
                 "samples.csv": "sample,reads\nS1,/data/r1.fq\n"}
        assert texts["params.yaml"] != (pdir / "params.yaml").read_text(), "the hand edit took"
        remote = _Remote(existing={"/work/pipelines", "/data/r1.fq"} | _REFS, sifs=set(_SIF.values()),
                         run_state={"exists": True, "missing": [], "samplesheet": True,
                                    "texts": texts}).install(monkeypatch)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1")
        assert r["outcome"] == "degraded" and r["reused_run_dir"] is True and remote.uploads == []
        assert remote.writes == {} and remote.sbatch["sbatch_args"] == []
        assert r["params"]["stranded"] == "yes", "the run directory's own params.yaml, hand edit included"

        again = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1",
                         params={"stranded": "reverse"}, walltime="12:00:00")
        assert again["outcome"] == "proven" and again["files_rewritten"] == ["launcher.sh", "params.yaml"]
        assert "\nstranded: reverse\n" in remote.writes["/work/pipelines/run1/params.yaml"]
        assert "\n#SBATCH --time=12:00:00\n" in remote.writes["/work/pipelines/run1/launcher.sh"]

    @pytest.mark.integration
    def test_a_run_the_scheduler_still_holds_is_never_launched_over(self, tmp_path, monkeypatch):
        """A queued manager reads params.yaml when it STARTS: rewriting it under that
        job would change what the job runs, and a second job would race it."""
        pdir = _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        remote = _Remote(existing=_REFS, sifs=set(_SIF.values()),
                         run_state={"exists": True, "missing": [], "samplesheet": True, "live": ["4241 PENDING"],
                                    "texts": {MANIFEST_PATH: (pdir / MANIFEST_PATH).read_text()}}).install(monkeypatch)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1", params={"stranded": "no"})
        assert (r["outcome"], r["code"]) == ("refused", "run_production.run_in_progress"), r
        assert "4241 PENDING" in r["error"] and remote.writes == {} and remote.sbatch is None

    @pytest.mark.integration
    def test_a_bad_param_is_refused_before_anything_is_asked_of_the_cluster(self, tmp_path, monkeypatch):
        remote = _Remote().install(monkeypatch)
        probed = []
        monkeypatch.setattr(run_production, "_remote_run_dir_state",
                            lambda *a, **kw: probed.append(1) or {})
        _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1",
                     samplesheet=_sheet(tmp_path, ["/data/r1.fq"]), params={"no_such_param": "x"})
        assert r["code"] == "run_production.unknown_param" and probed == []

    @pytest.mark.integration
    def test_bad_walltime_refused(self, tmp_path, monkeypatch):
        _Remote().install(monkeypatch)
        _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1", walltime="tomorrow")
        assert r["code"] == "run_production.bad_walltime"


# ===========================================================================
# The data pins
# ===========================================================================

class TestReferencePins:
    """The shared reference a run binds against what the workflow was sealed with. The
    fixture's GTF is a shared path parameter; when the run binds the SAME path as the
    seal, the bytes decide: same → proven, changed → degraded, never a refusal."""

    def _setup(self, tmp_path, *, gtf_text: str, rebind_text: str | None):
        gpath = tmp_path / "chr22.gtf"
        gpath.write_text(gtf_text)
        data = sealed_rnaseq_spec().model_dump()
        data["reference_databases"].append({
            "name": "gtf", "version": "v47", "local_path": str(gpath), "available": True,
            "sha256": hashlib.sha256(gtf_text.encode()).hexdigest(), "size_bytes": len(gtf_text)})
        if rebind_text is not None:
            gpath.write_text(rebind_text)
        _render(tmp_path, spec=WorkflowSpec.model_validate(data))
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        return d, fq

    @pytest.mark.integration
    def test_a_rebound_reference_launches_degraded(self, tmp_path, local_ok):
        d, fq = self._setup(tmp_path, gtf_text="chr22\tv47\n", rebind_text="chr22\tv39\n")
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                       access=_local_access(tmp_path, str(d)))
        assert r["outcome"] == "degraded" and r["code"] == "run_production.reference_diverged", r
        assert r["reference_check"]["status"] == "diverged" and r.get("job_id")

    @pytest.mark.integration
    def test_the_matching_reference_is_a_clean_proven_run(self, tmp_path, local_ok):
        """The pinned GTF matches by content; the index was never pinned by the seal
        (it was built by a recorded step), which is STATED as unanchored and is no
        downgrade — the run is proven, and the manifest says exactly which is which."""
        d, fq = self._setup(tmp_path, gtf_text="chr22\tv47\n", rebind_text=None)
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                       access=_local_access(tmp_path, str(d)))
        assert r["outcome"] == "proven", r
        check = r["reference_check"]
        assert check["status"] == "unanchored" and check["counts"] == {"match": 1, "diverged": 0,
                                                                        "unanchored": 1, "unverified": 0}
        by_slot = {f["slot"]: f["verdict"] for f in check["findings"]}
        assert by_slot == {"gtf": "match", "hisat2_index": "unanchored"}
        assert check["summary"] == "1 matched, 1 not pinned by the sealed workflow"
        manifest = __import__("json").loads(Path(r["manifest_path"]).read_text())
        assert manifest["reference_check"]["status"] == "unanchored"


# ===========================================================================
# A run in progress is never launched over: the guard fails closed when the
# scheduler cannot answer, sees a run directory reached through a link, sees
# the tasks under work/, and on a laptop asks the job manager.
# ===========================================================================

def _probe_with_squeue(tmp_path, monkeypatch, run_dir: Path, squeue_body: str) -> dict:
    """Run the real probe through bash here, with `squeue` a script printing what a
    scheduler would."""
    import subprocess
    bin_dir = tmp_path / "fakebin"; bin_dir.mkdir(exist_ok=True)
    (bin_dir / "squeue").write_text("#!/bin/sh\n" + squeue_body)
    (bin_dir / "squeue").chmod(0o755)
    real = subprocess.run

    def via_bash(argv, *a, **kw):
        import os, shlex
        script = shlex.split(argv[-1])[-1]         # ssh's `bash -lc '<script>'`, minus the login shell
        return real(["bash", "-c", script], capture_output=True, text=True,
                    env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", "USER": "u"})
    monkeypatch.setattr(subprocess, "run", via_bash)
    return run_production._remote_run_dir_state({"host": "h", "user": "u"}, str(run_dir), ["main.nf"],
                                                timeout=30)


class TestARunInProgress:
    @pytest.mark.integration
    def test_a_scheduler_that_cannot_answer_is_never_read_as_nothing_running(self, tmp_path, monkeypatch):
        run = tmp_path / "run1"; run.mkdir()
        with pytest.raises(run_production._Refusal) as e:
            _probe_with_squeue(tmp_path, monkeypatch, run,
                               "echo 'slurm_load_jobs error: Socket timed out' >&2; exit 1\n")
        assert e.value.result["code"] == "run_production.run_dir_probe_failed"
        assert "Socket timed out" in e.value.result["error"], "the scheduler's own words say why"

    @pytest.mark.integration
    def test_the_manager_and_its_tasks_are_seen_through_a_link(self, tmp_path, monkeypatch):
        real = tmp_path / "gpfs" / "run1"; (real / "work" / "ab").mkdir(parents=True)
        link = tmp_path / "project"; link.symlink_to(tmp_path / "gpfs")
        st = _probe_with_squeue(tmp_path, monkeypatch, link / "run1",
                                f"echo '4242 RUNNING {real.resolve()}'\n"
                                f"echo '4243 PENDING {real.resolve()}/work/ab/cd'\n"
                                f"echo '9999 RUNNING /elsewhere'\n")
        assert st["live"] == ["4242 RUNNING", "4243 PENDING"]

    @pytest.mark.integration
    def test_a_laptop_run_still_going_is_never_launched_over(self, tmp_path, local_ok):
        _render(tmp_path)
        d = tmp_path / "proj"; d.mkdir()
        fq = d / "r1.fq"; fq.write_text("x")
        ap = _local_access(tmp_path, str(d))
        assert _run_local(tmp_path, "rnaseq_counts", d / "run1", samplesheet=_sheet(tmp_path, [str(fq)]),
                          access=ap)["success"]
        busy = _FakeJobManager()
        busy.running_in = str(d / "run1")
        r = _run_local(tmp_path, "rnaseq_counts", d / "run1", params={"stranded": "no"}, access=ap, jm=busy)
        assert (r["outcome"], r["code"]) == ("refused", "run_production.run_in_progress"), r
        assert "check_job(" in r["remedy"] and "cancel_job(" in r["remedy"], "a laptop run has no scheduler"
