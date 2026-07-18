"""
agent_status — the "where am I?" snapshot. Pure-read survey across the
six subsystems the agent maintains state in.

These tests cover:
  - the record shape contract (all expected top-level keys present)
  - each subsystem query in isolation (drafts / frozen envs / sealed
    workflows / core test data / compute env bridge / background jobs / repo)
  - fault tolerance — a broken sub-state degrades to an error dict on that
    slice without crashing the whole call
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agent.skills.agent_status import (
    _background_jobs_summary,
    _compute_env_bridge_summary,
    _core_test_data_summary,
    _drafts_summary,
    _envs_on_disk_summary,
    _frozen_envs_summary,
    _repo_summary,
    _sealed_workflows_summary,
    agent_status,
)


# ---------------------------------------------------------------------------
# Helpers — fakes for the subsystems
# ---------------------------------------------------------------------------

class _FakePipelineState:
    """Minimal stand-in for PipelineState that exposes _drafts."""
    def __init__(self, drafts: dict | None = None):
        self._drafts = drafts or {}


class _FakeEnvCache:
    """Minimal stand-in for EnvCache that exposes all()."""
    def __init__(self, records: dict | None = None):
        self._records = records or {}
    def all(self) -> dict:
        return self._records


class _FakeJobManager:
    """Minimal stand-in for JobManager.list_jobs()."""
    def __init__(self, jobs: list[dict] | None = None, raise_on_call: bool = False):
        self._jobs = jobs or []
        self._raise = raise_on_call
    def list_jobs(self, include_terminated: bool = True) -> list[dict]:
        if self._raise:
            raise RuntimeError("simulated job manager crash")
        return self._jobs


# ---------------------------------------------------------------------------
# 1. Top-level shape — every expected key present, pure-read
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_agent_status_top_level_shape(tmp_path):
    """Pin the canonical record shape — downstream consumers (the agent's
    UI, future renderers, etc.) depend on the key set being stable."""
    config = {"paths": {"data_dir": str(tmp_path / "data")}}
    rec = agent_status(
        pipeline_state=_FakePipelineState(),
        env_cache=_FakeEnvCache(),
        job_manager=_FakeJobManager(),
        config=config,
        access_path=None,
        include_repo=False,
    )
    assert set(rec.keys()) == {
        "drafts", "envs_on_disk", "frozen_envs", "sealed_workflows",
        "core_test_data", "compute_env_bridge", "background_jobs",
    }


@pytest.mark.integration
def test_agent_status_include_repo_adds_repo_key(tmp_path):
    """include_repo=True adds a `repo` slice — even outside a git tree
    (where it'll be empty)."""
    config = {"paths": {"data_dir": str(tmp_path / "data")}}
    rec = agent_status(
        pipeline_state=_FakePipelineState(), env_cache=_FakeEnvCache(),
        job_manager=_FakeJobManager(), config=config, access_path=None,
        include_repo=True,
    )
    assert "repo" in rec


# ---------------------------------------------------------------------------
# 2. Drafts subsystem
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_drafts_summary_extracts_useful_fields(tmp_path):
    """Per-draft entry has the fields a user wants for orientation:
    pipeline_id, what tool, counts of install_steps / pipeline_steps / etc.,
    and the re-earned lifecycle `state` (Phase-3 Piece B). This draft has a
    conda env + install steps and no frozen/sealed pointer, so state=env_built."""
    ps = _FakePipelineState({
        "dorado_install": {
            "description": "Install dorado for nanopore basecalling",
            "conda_env": "bioinf_dorado",
            "install_steps": [{"step": 1}, {"step": 2}],
            "pipeline_steps": [],
            "packages": [],
            "verifications": {"dorado": {"verify_output": "2.0.0"}},
        },
    })
    rows = _drafts_summary(ps, _FakeEnvCache(), tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["pipeline_id"] == "dorado_install"
    assert r["state"] == "env_built"          # the dead env_status stamp is gone
    assert "env_status" not in r and "pipeline_status" not in r
    assert r["conda_env"] == "bioinf_dorado"
    assert r["install_steps"] == 2
    assert r["verifications"] == 1


@pytest.mark.integration
def test_drafts_summary_fault_tolerant(tmp_path):
    """A PipelineState-like object without `_drafts` should NOT crash the
    whole call — degrades to `[]` (empty list)."""
    obj = MagicMock(spec=[])   # no attrs
    rows = _drafts_summary(obj, _FakeEnvCache(), tmp_path)
    # Either empty (graceful) or {error: ...} — both are tolerated by the
    # higher-level call. We just require it's a list.
    assert isinstance(rows, list)


# ---------------------------------------------------------------------------
# 3. Frozen envs subsystem
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_frozen_envs_summary_attaches_deliverable_paths(tmp_path):
    """Frozen envs surface their deliverable paths (ENV.html, attestation,
    recipe) when those files exist on disk — None when they don't."""
    env_reports = tmp_path / "env_reports"
    env_reports.mkdir()
    # Create deliverables for one env but not the other
    (env_reports / "bioinf_samtools.ENV.html").write_text("<html>")
    (env_reports / "bioinf_samtools.attestation.json").write_text("{}")

    cache = _FakeEnvCache({
        "samtools=1.21|linux/amd64|none": {
            "name": "bioinf_samtools",
            "image": "bioinf_samtools:1.21",
            "image_digest": "sha256:abc",
            "content_digest": "abcd",
            "platform": "linux/amd64",
            "build_method": "build",
            "image_size_bytes": 412_000_000,
        },
        "unmaterialized=1.0|linux/amd64|none": {
            "name": "bioinf_unmaterialized",
            "image_digest": "sha256:xyz",
        },
    })
    rows = _frozen_envs_summary(cache, env_reports)
    assert len(rows) == 2

    by_name = {r["name"]: r for r in rows}
    # The one with deliverables on disk has them recorded.
    assert by_name["bioinf_samtools"]["env_report"] is not None
    assert by_name["bioinf_samtools"]["attestation"] is not None
    assert by_name["bioinf_samtools"]["recipe"] is None  # not present
    # The one without — fields are None, not missing.
    assert by_name["bioinf_unmaterialized"]["env_report"] is None
    assert by_name["bioinf_unmaterialized"]["attestation"] is None


# ---------------------------------------------------------------------------
# 4. Sealed workflows subsystem
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_sealed_workflows_summary_walks_workflow_yamls(tmp_path):
    """One row per workflow.yaml on disk, with the RUN.html dashboard sibling
    when it exists, and `validated_in_shipped_image` surfaced from the spec."""
    er = tmp_path / "env_reports"
    er.mkdir()
    # A complete workflow (spec + run dashboard)
    (er / "wgs.workflow.yaml").write_text(yaml.safe_dump({
        "workflow_name": "wgs",
        "description": "WGS alignment + variant calling",
        "validated_in_shipped_image": True,
        "envs": [{"image_digest": "sha256:aaa"}],
        "pipeline_steps": [{"step": 1}, {"step": 2}, {"step": 3}],
    }))
    (er / "wgs.RUN.html").write_text("<html>wgs run dashboard</html>")
    # An incomplete one (spec only, no dashboard)
    (er / "rnaseq.workflow.yaml").write_text(yaml.safe_dump({
        "workflow_name": "rnaseq",
        "validated_in_shipped_image": False,
        "envs": [],
        "pipeline_steps": [],
    }))

    rows = _sealed_workflows_summary(er)
    by_name = {r["name"]: r for r in rows}
    assert "wgs" in by_name and "rnaseq" in by_name

    wgs = by_name["wgs"]
    assert wgs["run_report"] and wgs["run_report"].endswith("wgs.RUN.html")
    assert wgs["validated_in_shipped_image"] is True
    assert wgs["pinned_env_digests"] == ["sha256:aaa"]
    assert wgs["pipeline_steps"] == 3
    # the spec-only workflow reports no dashboard rather than inventing one
    assert by_name["rnaseq"]["run_report"] is None

    rs = by_name["rnaseq"]
    assert rs["run_report"] is None
    assert rs["validated_in_shipped_image"] is False


# ---------------------------------------------------------------------------
# 5. Core test data subsystem
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_core_test_data_summary_counts_by_build(tmp_path):
    """Per-build counts of samples by category — short_read_fastq,
    long_read_fastq, long_read_pod5, phenopackets. Counts only; no full
    sample dump."""
    data_dir = tmp_path / "data"
    core_dir = data_dir / "core_test_data_hg38"
    core_dir.mkdir(parents=True)
    manifest = {
        "genome_build": "hg38",
        "sequencing_data": {
            "short_read": {"paired_end": {"exome": [
                {"sample": "a"}, {"sample": "b"}, {"sample": "c"}]}},
            "long_read": {"ont": {"ont_wgs": [
                {"sample": "x"},                             # fastq (default)
                {"sample": "y", "file_format": "pod5"},      # pod5
            ]}},
        },
        "phenopackets": [{"phenopacket_id": "p1"}],
    }
    (core_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest))

    summary = _core_test_data_summary(data_dir)
    assert "hg38" in summary["genome_builds"]
    counts = summary["by_build"]["hg38"]
    assert counts["short_read_fastq"] == 3
    assert counts["long_read_fastq"] == 1
    assert counts["long_read_pod5"] == 1
    assert counts["phenopackets"] == 1


@pytest.mark.integration
def test_core_test_data_summary_no_data_dir_returns_empty(tmp_path):
    """No data dir? No error. Just empty counts."""
    summary = _core_test_data_summary(tmp_path / "nonexistent")
    assert summary["genome_builds"] == []
    assert summary["by_build"] == {}


# ---------------------------------------------------------------------------
# 6. Envs on disk subsystem
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_envs_on_disk_summary_counts_bin_entries(tmp_path):
    """Each env dir's bin/ entries are counted (rough tool-count proxy)."""
    envs = tmp_path / "envs"
    envs.mkdir()
    e1 = envs / "bioinf_samtools"
    (e1 / "bin").mkdir(parents=True)
    (e1 / "bin" / "samtools").write_text("#!/usr/bin/env bash\n")
    (e1 / "bin" / "bcftools").write_text("#!/usr/bin/env bash\n")
    e2 = envs / "bioinf_empty"
    e2.mkdir()  # no bin/

    rows = _envs_on_disk_summary(envs)
    by_name = {r["name"]: r for r in rows}
    assert by_name["bioinf_samtools"]["bin_count"] == 2
    assert by_name["bioinf_empty"]["bin_count"] == 0


# ---------------------------------------------------------------------------
# 7. Compute env bridge subsystem
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_compute_env_bridge_summary_parses_access_yaml(tmp_path):
    """envs[] + projects[] lists are parsed from projects_access.yaml.
    Active ssh sockets are surfaced when present (we don't fabricate any
    here — just confirm the field is a list)."""
    access = tmp_path / "projects_access.yaml"
    access.write_text(yaml.safe_dump({
        "compute_envs": [
            {"name": "cluster", "type": "ssh", "host": "h", "user": "u"},
            {"name": "laptop", "type": "local"},
        ],
        "projects": [
            {"name": "wgs_proj", "compute_env_access": []},
            {"name": "envs_catalog", "compute_env_access": []},
        ],
    }))
    rec = _compute_env_bridge_summary(access)
    assert rec["envs"] == ["cluster", "laptop"]
    assert rec["projects"] == ["wgs_proj", "envs_catalog"]
    assert rec["config_path"].endswith("projects_access.yaml")
    assert isinstance(rec["active_ssh_sessions"], list)


@pytest.mark.integration
def test_compute_env_bridge_summary_missing_config(tmp_path):
    """No projects_access.yaml on disk → fields empty, not an error."""
    rec = _compute_env_bridge_summary(tmp_path / "nope.yaml")
    assert rec["config_path"] is None
    assert rec["envs"] == []
    assert rec["projects"] == []
    assert isinstance(rec["active_ssh_sessions"], list)


# ---------------------------------------------------------------------------
# 8. Background jobs subsystem — fault tolerance
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_background_jobs_summary_passes_through():
    """Passes JobManager.list_jobs() output through; running-only filter
    is applied by the wrapper."""
    jm = _FakeJobManager(jobs=[
        {"job_id": "j1", "state": "running"},
        {"job_id": "j2", "state": "running"},
    ])
    rows = _background_jobs_summary(jm)
    assert len(rows) == 2


@pytest.mark.integration
def test_background_jobs_summary_fault_tolerant():
    """A crashing JobManager doesn't take down the whole status call —
    returns an error entry instead."""
    jm = _FakeJobManager(raise_on_call=True)
    rows = _background_jobs_summary(jm)
    assert len(rows) == 1
    assert "error" in rows[0]


# ---------------------------------------------------------------------------
# 9. Repo subsystem — degrades gracefully outside a git tree
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_repo_summary_returns_empty_outside_git_tree(tmp_path):
    """No .git/ → empty dict (not an error)."""
    rec = _repo_summary(tmp_path)
    assert rec == {}
