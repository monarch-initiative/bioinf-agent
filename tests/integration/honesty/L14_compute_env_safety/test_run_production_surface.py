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
        assert r["outcome"] == "proven" and r["code"] == "run_production.local_launched", r
        run = d / "run1"
        for rel in ("main.nf", "nextflow.config", "params.yaml", "launcher.sh", "samples.csv", MANIFEST_PATH):
            assert (run / rel).is_file(), rel
        assert (run / "samples.csv").read_text().startswith("sample,reads\ns0,")
        assert (pdir / "samples.csv").read_text() != (run / "samples.csv").read_text()   # ours, not the example
        call = jm.calls[0]
        assert call["working_dir"] == str(run) and call["tool"] == "run_production_pipeline"
        assert "nextflow run main.nf -profile local -params-file params.yaml -resume --hisat2_index " in call["command"]
        assert call["command"].endswith("--stranded yes")
        assert call["command"].startswith("source ")
        assert r["params"]["stranded"] == "yes" and r["reused_run_dir"] is False
        assert Path(r["manifest_path"]).is_file()
        manifest = __import__("json").loads(Path(r["manifest_path"]).read_text())
        assert manifest["pipeline"] == "rnaseq_counts" and manifest["samplesheet"]["rows"] == 1
        assert manifest["params_overridden"]["stranded"] == "yes"
        assert "check_job" in r["follow_up"]["poll"]

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


# ===========================================================================
# The cluster: fakes for every remote probe; the sbatch tokens
# ===========================================================================

class _Remote:
    """Stands in for the cluster: which paths exist, which sifs are staged, what the run
    directory holds, and what sbatch was asked."""
    def __init__(self, *, existing=(), sifs=(), run_state=None):
        self.existing = set(existing)
        self.sifs = set(sifs)
        self.run_state = run_state or {"main_nf": False, "samplesheet": False, "manifest": ""}
        self.uploads = []
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
        monkeypatch.setattr(run_production, "_remote_run_dir_state", lambda env, run_dir, *, timeout: dict(self.run_state))
        monkeypatch.setattr(run_production, "_observe_cluster",
                            lambda env, paths, *, timeout: {p: {"exists": True, "sha256": ""} for p in paths})

        def upload(project_name, compute_env_name, local_path, remote_abs_path, access_path=None, timeout=600):
            self.uploads.append((local_path, remote_abs_path))
            return {"remote_abs_path": remote_abs_path}
        monkeypatch.setattr(transfer, "upload", upload)

        def sbatch(env, workflow_dir, *, timeout=300, sbatch_args=(), script_args=()):
            self.sbatch = {"dir": workflow_dir, "sbatch_args": list(sbatch_args), "script_args": list(script_args)}
            return {"job_id": "4242", "launcher": f"{workflow_dir}/launcher.sh",
                    "sbatch_command": f"sbatch --parsable {' '.join(sbatch_args)} launcher.sh {' '.join(script_args)}"}
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
    def test_the_run_dir_is_made_by_the_first_upload_never_probed_first(self, tmp_path, monkeypatch):
        # /work/pipelines/new is not on the cluster yet: the launch goes ahead, the uploads
        # into it create it (the transfer provider makes the parent), and nothing probes it.
        remote = _Remote(existing={"/data/r1.fq"} | _REFS, sifs=set(_SIF.values())).install(monkeypatch)
        _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/new/run1",
                     samplesheet=_sheet(tmp_path, ["/data/r1.fq"]))
        assert r["outcome"] == "proven" and r["code"] == "run_production.submitted", r
        assert "/work/pipelines/new/run1/main.nf" in [dst for _, dst in remote.uploads]
        assert remote.sbatch["dir"] == "/work/pipelines/new/run1"

    @pytest.mark.integration
    def test_uploads_the_render_and_the_sheet_then_sbatches_with_the_tokens(self, tmp_path, monkeypatch):
        remote = _Remote(existing={"/work/pipelines", "/data/r1.fq", "/data/r2.fq"} | _REFS,
                         sifs=set(_SIF.values())).install(monkeypatch)
        pdir = _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        sheet = _sheet(tmp_path, ["/data/r1.fq", "/data/r2.fq"])
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1", samplesheet=sheet,
                     params={"stranded": "reverse", "outdir": "/work/pipelines/run1/out"}, walltime="1-00:00:00")
        assert r["outcome"] == "proven" and r["code"] == "run_production.submitted", r
        assert r["job_id"] == "4242" and r["locus"] == "cluster"
        remotes = [dst for _, dst in remote.uploads]
        assert "/work/pipelines/run1/main.nf" in remotes and "/work/pipelines/run1/launcher.sh" in remotes
        assert f"/work/pipelines/run1/{MANIFEST_PATH}" in remotes
        assert (sheet, "/work/pipelines/run1/samples.csv") in remote.uploads
        assert (str(pdir / "samples.csv"), "/work/pipelines/run1/samples.csv") not in remote.uploads
        assert remote.sbatch == {"dir": "/work/pipelines/run1", "sbatch_args": ["--time=1-00:00:00"],
                                 "script_args": ["--stranded", "reverse", "--outdir", "/work/pipelines/run1/out"]}
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
    def test_relaunch_reuses_the_remote_directory(self, tmp_path, monkeypatch):
        pdir = _render(tmp_path, compute_env="hpc", sif_paths=_SIF)
        ours = (pdir / MANIFEST_PATH).read_text()
        remote = _Remote(existing={"/work/pipelines"} | _REFS, sifs=set(_SIF.values()),
                         run_state={"main_nf": True, "samplesheet": True, "manifest": ours}).install(monkeypatch)
        r = _run_ssh(tmp_path, "rnaseq_counts", "/work/pipelines/run1")
        assert r["outcome"] == "proven" and r["reused_run_dir"] is True and remote.uploads == []
        assert remote.sbatch["script_args"] == []

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
