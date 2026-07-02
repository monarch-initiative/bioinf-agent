"""
Structural contract for the Layer-2 run dashboard (run_dashboard_html), the
sibling of the Layer-1 env report. The dashboard is the artifact the seal exists
to produce: proof the workflow actually runs on a given compute resource, plus a
distinct how-to panel — rendered PURELY from the machine-verified WorkflowSpec.

The invariants under test:
  • Pure + deterministic + escaped (same honesty posture as the env report).
  • Validated-evidence grouped by compute locus (cluster / local container / host).
  • The how-to is a DISTINCT panel (not markdown, not buried in the evidence).
  • Accretion is honest PER-DIGEST: a step whose evidence ran against a different
    env digest than the one the workflow pins is marked STALE, not shown green —
    the one rule the rebuild scenario imposes.
"""
from __future__ import annotations

import pytest

from agent.skills.run_dashboard_html import render_run_dashboard_html


def _cluster_spec() -> dict:
    return {
        "workflow_name": "samtools_view",
        "description": "filter to mapped reads on the cluster",
        "created_at": "2026-07-02T00:00:00+00:00",
        "env_request_key": "samtools=1.21|linux-64|none",
        "env_content_digest": "sha256:" + "ab" * 32,
        "env_image": "quay.io/biocontainers/samtools@sha256:" + "cd" * 32,
        "validated_in_shipped_image": True,
        "usage_verified": True,
        "usage": {
            "command_template": "samtools view -b -F 4 {INPUT_BAM} > {OUTPUT_DIR}/out.bam",
            "description": "filter to mapped reads",
            "inputs": [{"name": "INPUT_BAM", "format": "bam", "description": "a BAM"}],
            "outputs": [{"name": "out", "files": ["out.bam"], "type": "bam"}],
            "trials": [{"name": "single_bam", "description": "one coordinate-sorted BAM"}],
        },
        "pipeline_steps": [{
            "step": 1, "tool": "samtools", "returncode": 0,
            "command": "samtools view -b -F 4 in.bam > out.bam",
            "validation_locus": "cluster",
            "cluster_job_id": "57358567", "cluster_node": "b1006",
            "cluster_sif_sha256": "9d81" + "0" * 60,
            "cluster_image_verified": True, "cluster_image_digest_match": True,
            "cluster_apptainer_module": "apptainer/1.4.1",
            "cluster_slurm": {"queue": "general", "mem": "2G"},
            "resource_usage": {"wall_seconds": 43.0, "peak_rss_mb": 262.5,
                               "max_cpu_percent": 41.9, "locus": "cluster"},
            "validation": {"out.bam": {"passed": True, "validation_method": "tool"}},
        }],
        "reference_databases": [], "authored_artifacts": [],
    }


@pytest.mark.integration
def test_dashboard_is_well_formed_page():
    html = render_run_dashboard_html(_cluster_spec())
    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    assert "Workflow run report — samtools_view" in html


@pytest.mark.integration
def test_dashboard_is_deterministic():
    spec = _cluster_spec()
    env = {"name": "samtools", "image_digest": spec["env_image"]}
    assert render_run_dashboard_html(spec, env) == render_run_dashboard_html(spec, env)


@pytest.mark.integration
def test_validated_evidence_grouped_by_locus():
    html = render_run_dashboard_html(_cluster_spec())
    assert "Does it run?" in html
    # cluster locus header + the concrete run evidence
    assert ">cluster<" in html
    assert "57358567" in html and "b1006" in html
    assert "262.5 MB" in html
    # C2: the on-cluster .sif verification surfaces
    assert "verified on cluster" in html
    assert "digest matches the frozen env" in html
    assert "validated in shipped image" in html


@pytest.mark.integration
def test_howto_is_distinct_panel():
    """The how-to (the auto-generated user guide) is its OWN section, rendered
    from the verified usage block — no markdown file, a real dashboard panel."""
    html = render_run_dashboard_html(_cluster_spec())
    assert "How to run it" in html
    assert "samtools view -b -F 4 {INPUT_BAM}" in html
    # inputs + outputs + self-test trials all surface
    assert "INPUT_BAM" in html and "out.bam" in html
    assert "single_bam" in html
    assert "self-tested" in html


@pytest.mark.integration
def test_env_panel_links_env_report():
    html = render_run_dashboard_html(_cluster_spec(), env_record={"name": "samtools",
                                                                  "image_digest": "sha256:x"})
    assert "samtools.ENV.html" in html          # links the immutable Layer-1 artifact
    assert "sha256:" + "ab" * 32 in html         # content digest shown


@pytest.mark.integration
def test_escapes_workflow_name_and_command():
    spec = _cluster_spec()
    spec["workflow_name"] = "<script>alert('x')</script>"
    html = render_run_dashboard_html(spec)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.integration
def test_local_container_step_matching_digest_is_current():
    """A local-container step whose container_image_digest == the pinned env's is
    current — no stale marker."""
    spec = _cluster_spec()
    spec["pipeline_steps"] = [{
        "step": 1, "tool": "samtools", "returncode": 0,
        "command": "samtools flagstat in.bam",
        "ran_in_container": True,
        "container_image_digest": "sha256:MATCH",
        "resource_usage": {"wall_seconds": 2.0, "peak_rss_mb": 40.0, "locus": "container"},
        "validation": {"flagstat.txt": {"passed": True, "validation_method": "text"}},
    }]
    html = render_run_dashboard_html(spec, env_record={"name": "samtools",
                                                       "image_digest": "sha256:MATCH"})
    assert "local container" in html
    # the shared _CSS defines .stale, but no stale MARKER is rendered
    assert 'class="stale"' not in html
    assert "env rebuilt since" not in html
    assert "validated here" in html


@pytest.mark.integration
def test_stale_when_step_ran_against_a_different_digest():
    """THE rebuild-scenario rule: a step whose evidence ran against a DIFFERENT
    env digest than the workflow pins is marked stale, not shown as current — run
    evidence accretes per-digest, never across a rebuild as if nothing changed."""
    spec = _cluster_spec()
    spec["pipeline_steps"] = [{
        "step": 1, "tool": "samtools", "returncode": 0,
        "command": "samtools flagstat in.bam",
        "ran_in_container": True,
        "container_image_digest": "sha256:OLD_ENV_V1",
        "resource_usage": {"wall_seconds": 2.0, "peak_rss_mb": 40.0, "locus": "container"},
        "validation": {"flagstat.txt": {"passed": True, "validation_method": "text"}},
    }]
    # workflow is now pinned to the REBUILT env (v2)
    html = render_run_dashboard_html(spec, env_record={"name": "samtools",
                                                       "image_digest": "sha256:NEW_ENV_V2"})
    assert 'class="stale"' in html
    assert "env rebuilt since" in html


@pytest.mark.integration
def test_cluster_digest_mismatch_is_stale():
    """A cluster step whose .sif digest did NOT match the frozen env (the C2
    round-trip said so) is stale regardless of the primary digest compare."""
    spec = _cluster_spec()
    spec["pipeline_steps"][0]["cluster_image_digest_match"] = False
    spec["pipeline_steps"][0]["cluster_image_verified"] = False
    html = render_run_dashboard_html(spec)
    assert 'class="stale"' in html
    assert "env rebuilt since" in html


@pytest.mark.integration
def test_no_steps_shows_placeholder():
    spec = _cluster_spec()
    spec["pipeline_steps"] = []
    html = render_run_dashboard_html(spec)
    assert "no validated steps recorded" in html


@pytest.mark.integration
def test_reference_db_sha256_surfaces():
    spec = _cluster_spec()
    spec["reference_databases"] = [{"name": "gnomad", "sha256": "f" * 64, "size_bytes": 123}]
    html = render_run_dashboard_html(spec)
    assert "Reference databases" in html
    assert "gnomad" in html
    assert ("f" * 19) in html      # truncated sha256 prefix
