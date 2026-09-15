"""
The falsifier-drive fix sites (FD1–FD7 + the I4 host-fallback question), pinned.

Two drives (DESeq2 → sealed proven; Exomiser + 37 GB cluster data → sealed
degraded-honest) produced seven findings and one open question. None forced a change
to `agent/skills/invariants.py` — every defect was primitive-level, renderer-level,
or a protocol paper-cut. Each test here pins the FIX to the failure that was actually
observed, so the class cannot quietly return.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


# ─────────────────────────── FD1 — an error is not a negative observation ──────────

class TestPullableImageThreeStates:
    """resolve_tool's pullable_image said `found: false` for bioconductor-deseq2=1.50.2
    while freeze's identical probe, minutes later, adopted the image by digest. The
    swallowed exception arm returned the same shape as a genuine registry miss."""

    AVAIL = {"conda": {"available": True, "latest": "1.50.2",
                       "bioc_spec": "bioconductor-deseq2"}}

    def test_probe_error_is_unchecked_not_absent(self, monkeypatch):
        from agent.skills import resolver
        def boom(*a, **k):
            raise TimeoutError("quay.io timed out")
        monkeypatch.setattr(resolver, "_resolve_biocontainer", boom)
        r = resolver.pullable_image(self.AVAIL, "deseq2", version="1.50.2", chosen="conda")
        assert r["found"] is False
        assert r["checked"] is False, "a failed probe must not present as a registry answer"
        assert "TimeoutError" in r["probe_error"]
        assert "NOT evidence" in r["reason"]

    def test_genuine_miss_is_checked(self, monkeypatch):
        from agent.skills import resolver
        monkeypatch.setattr(resolver, "_resolve_biocontainer",
                            lambda *a, **k: {"found": False, "reason": "no such repo"})
        r = resolver.pullable_image(self.AVAIL, "deseq2", version="1.50.2", chosen="conda")
        assert r["found"] is False and r["checked"] is True
        assert r["reason"] == "no such repo"

    def test_non_conda_pick_is_not_probed_and_says_so(self):
        from agent.skills import resolver
        r = resolver.pullable_image({}, "sometool", chosen="binary")
        assert r["found"] is False and r["checked"] is False
        assert "not probed" in r["reason"]

    def test_hit_shape_unchanged(self, monkeypatch):
        from agent.skills import resolver
        monkeypatch.setattr(
            resolver, "_resolve_biocontainer",
            lambda *a, **k: {"found": True, "image": "quay.io/biocontainers/x:1",
                            "image_by_digest": "quay.io/biocontainers/x@sha256:ab",
                            "digest": "sha256:ab"})
        r = resolver.pullable_image(self.AVAIL, "deseq2", chosen="conda")
        assert r["found"] is True and r["source"] == "biocontainer"


# ─────────────────────────── FD2 — a load followed by self-checking work ────────────

class TestEvidenceDepthLoadThenWork:
    """`Rscript -e 'library(DESeq2); …; DESeq(dds); stopifnot(…)'` was disclosed as
    [import] on a real freeze while the command ran the tool's main entrypoint and
    asserted on the result. Promotion requires calls AND an assertion — a misfire
    without the assertion could only weaken disclosure, so it stays import."""

    FD2_COMMAND = ("Rscript -e 'suppressMessages(library(DESeq2)); set.seed(1); "
                   "m <- matrix(rnbinom(240, mu=50, size=10), nrow=40); "
                   "dds <- DESeqDataSetFromMatrix(m, cd, ~condition); "
                   "dds <- DESeq(dds, quiet=TRUE); r <- results(dds); "
                   "stopifnot(nrow(r)==40); cat(\"ok\")'")

    def test_the_fd2_command_is_functional(self):
        from agent.skills.env_honesty import evidence_depth
        assert evidence_depth(self.FD2_COMMAND, "bioconductor-deseq2") == "functional"

    @pytest.mark.parametrize("cmd", [
        "Rscript -e 'library(DESeq2)'",
        "python -c 'import numpy'",
        "python3 -m talos",
        # a load followed by UNASSERTED calls may run nothing of the tool — stays import
        "Rscript -e 'library(DESeq2); sessionInfo()'",
    ])
    def test_load_only_and_unasserted_stay_import(self, cmd):
        from agent.skills.env_honesty import evidence_depth
        assert evidence_depth(cmd, "bioconductor-deseq2") == "import"

    def test_assert_flag_on_a_bare_tool_cannot_reach_the_promotion(self):
        # the promotion lives INSIDE the import branch; a command with no load shape
        # never sees it, so `--assert-mode` cannot dress a probe as functional
        from agent.skills.env_honesty import evidence_depth
        assert evidence_depth("sometool --assert-mode", "sometool") != "functional"


# ─────────────────────────── FD3 — both truths on the dashboard headline ────────────

def _sealed_spec(failed_step: bool, usage_verified: bool) -> dict:
    steps = [{
        "step": 1, "tool": "t", "returncode": 0,
        "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 1.0,
                           "max_cpu_percent": 1.0, "locus": "host"},
        "validation": {"/x/out.tsv": {"passed": True}},
        "detected_outputs": ["/x/out.tsv"],
    }]
    if failed_step:
        steps.insert(0, {"step": 0, "tool": "t", "returncode": 1,
                         "detected_outputs": [], "validation": {}})
    spec = {
        "workflow_name": "fd3_probe",
        "created_at": "2026-09-15T00:00:00+00:00",
        "pipeline_steps": steps,
        "usage": {"command_template": "t {IN} > {OUTPUT_DIR}/out.tsv",
                  "description": "d", "inputs": [], "outputs": []},
    }
    if usage_verified:
        spec["usage_verification"] = {"status": "verified", "trial_count": 1,
                                      "passed": 1, "trials": []}
    return spec


class TestDashboardHeadlineBothTruths:
    def test_failed_iteration_does_not_outvote_a_proven_howto(self):
        from agent.skills.run_dashboard_html import render_run_dashboard_html
        html = render_run_dashboard_html(_sealed_spec(failed_step=True, usage_verified=True))
        assert "do not run this as-is" not in html, (
            "the drive-1 page led with 'do not run this as-is' over a workflow whose "
            "how-to the seal had just proven (FD3)")
        assert "declared how-to self-tested" in html
        assert "failed iteration step(s)" in html, "the failed steps must stay visible"

    def test_without_a_proven_howto_failed_still_headlines_do_not_run(self):
        from agent.skills.run_dashboard_html import render_run_dashboard_html
        html = render_run_dashboard_html(_sealed_spec(failed_step=True, usage_verified=False))
        assert "do not run this as-is" in html


# ─────────────────────────── FD4 — the sample name is the subject id ────────────────

def _write_ppkt_meta(tmp_path: Path, subject_id: str) -> dict:
    core = tmp_path / "data" / "core_test_data_hg38" / "phenopackets"
    core.mkdir(parents=True)
    import yaml
    (core / "PPK_1_meta.yaml").write_text(yaml.safe_dump({
        "phenopacket_id": "PPK_1", "subject_id": subject_id,
        "variants": [{"chrom": "chr7", "pos": 5527786, "ref": "C", "alt": "T",
                      "gene": "ACTB", "allelic_state": "heterozygous"}],
        "genome_assembly": "hg38", "source_url": "http://x", "file": "PPK_1.json",
    }))
    # config paths.data_dir is read relative to the PROJECT root, so hand the skill an
    # absolute path via a config rooted at tmp_path
    return {"paths": {"data_dir": str(tmp_path / "data")}}


class TestVcfSampleNameVerbatim:
    def test_space_in_subject_id_is_preserved(self, tmp_path, monkeypatch):
        from agent.skills import core_test_data
        cfg = _write_ppkt_meta(tmp_path, "Patient N")
        monkeypatch.setattr(core_test_data.Path, "resolve",
                            core_test_data.Path.resolve, raising=False)
        # point the module's project-root derivation at tmp_path by patching the
        # function's data-dir resolution: easiest honest route is a config whose
        # data_dir is absolute — the skill joins project_root/config-path, so make
        # the joined path exist by symlinking. Simpler: call with a data_dir that
        # resolves inside tmp via monkeypatched project root.
        monkeypatch.setattr(core_test_data, "__file__",
                            str(tmp_path / "agent" / "skills" / "core_test_data.py"))
        out = tmp_path / "out.vcf"
        r = core_test_data.phenopacket_to_vcf(cfg, "PPK_1", str(out))
        assert r["success"], r
        assert r["sample_id"] == "Patient N", (
            "Exomiser cross-checks the phenopacket subject id against the VCF sample "
            "name; flattening the space made it refuse the pair (FD4, job 1128831)")
        header = [l for l in out.read_text().splitlines() if l.startswith("#CHROM")][0]
        assert header.split("\t")[-1] == "Patient N"
        assert "sample_id_note" not in r

    def test_tab_is_rewritten_and_disclosed(self, tmp_path, monkeypatch):
        from agent.skills import core_test_data
        cfg = _write_ppkt_meta(tmp_path, "bad\tid")
        monkeypatch.setattr(core_test_data, "__file__",
                            str(tmp_path / "agent" / "skills" / "core_test_data.py"))
        out = tmp_path / "out.vcf"
        r = core_test_data.phenopacket_to_vcf(cfg, "PPK_1", str(out))
        assert r["success"], r
        assert r["sample_id"] == "bad_id"
        assert "sample_id_note" in r, "a forced rewrite must be disclosed, never silent"


# ─────────────────────────── FD5 — a producer registers its own output ──────────────

class _StubPipelineState:
    def __init__(self):
        self.artifacts: list = []
    def add_authored_artifact(self, pipeline_id, artifact):
        if pipeline_id != "known":
            return None
        self.artifacts.append(artifact)
        return len(self.artifacts) - 1


class TestProducerRegistersItsOutput:
    def test_record_generated_artifact_anchors_the_file(self, tmp_path, monkeypatch):
        from agent.mcp_tools import workflow_tools
        from agent import mcp_server as _ms
        stub = _StubPipelineState()
        monkeypatch.setattr(_ms, "_pipeline_state", stub, raising=False)
        f = tmp_path / "x.vcf"
        f.write_bytes(b"##fileformat=VCFv4.2\n")
        idx, art = workflow_tools.record_generated_artifact(
            "known", str(f), "test_input", "d", "phenopacket_to_vcf(...)")
        assert idx == 0
        assert art["sha256"] and art["size_bytes"] == f.stat().st_size
        assert art["generated_by"].startswith("phenopacket_to_vcf")

    def test_stage_authored_artifact_generated_by_mode_is_the_same_implementation(self):
        # identity at the source level: the generated_by branch must call the helper,
        # not re-spell the record construction (the drift this repo keeps paying for)
        import inspect
        from agent.mcp_tools import workflow_tools
        src = inspect.getsource(workflow_tools.stage_authored_artifact)
        assert "record_generated_artifact(" in src

    def test_phenopacket_to_vcf_merges_when_pipeline_id_given(self, tmp_path, monkeypatch):
        from agent.mcp_tools import data_tools
        from agent import mcp_server as _ms
        stub = _StubPipelineState()
        monkeypatch.setattr(_ms, "_pipeline_state", stub, raising=False)
        f = tmp_path / "y.vcf"
        f.write_bytes(b"##fileformat=VCFv4.2\n")
        monkeypatch.setattr(_ms, "_phenopacket_to_vcf",
                            lambda cfg, **k: {"success": True, "output_vcf": str(f),
                                              "sample_id": "S", "num_variants": 1,
                                              "genome_assembly": "hg38"},
                            raising=False)
        r = data_tools.phenopacket_to_vcf.fn(  # .fn: the undecorated callable
            phenopacket_id="P", output_vcf=str(f), pipeline_id="known") \
            if hasattr(data_tools.phenopacket_to_vcf, "fn") else \
            data_tools.phenopacket_to_vcf(
                phenopacket_id="P", output_vcf=str(f), pipeline_id="known")
        assert r["pipeline_merge"]["status"] == "merged"
        assert stub.artifacts and stub.artifacts[0]["role"] == "test_input"

    def test_unknown_pipeline_is_stated_not_silent(self, tmp_path, monkeypatch):
        from agent.mcp_tools import data_tools
        from agent import mcp_server as _ms
        monkeypatch.setattr(_ms, "_pipeline_state", _StubPipelineState(), raising=False)
        f = tmp_path / "z.vcf"
        f.write_bytes(b"x\n")
        monkeypatch.setattr(_ms, "_phenopacket_to_vcf",
                            lambda cfg, **k: {"success": True, "output_vcf": str(f),
                                              "sample_id": "S", "num_variants": 1,
                                              "genome_assembly": "hg38"},
                            raising=False)
        fn = getattr(data_tools.phenopacket_to_vcf, "fn", data_tools.phenopacket_to_vcf)
        r = fn(phenopacket_id="P", output_vcf=str(f), pipeline_id="nope")
        assert r["pipeline_merge"]["status"] == "unknown_pipeline"


# ─────────────────────────── FD6 — the orphan refusal names the exit ────────────────

class TestOrphanRefusalNamesTheRedrive:
    def test_i8_orphan_violation_carries_the_remedy(self):
        from agent.skills.spec_writer import check_workflow_invariants
        spec = {
            "pipeline_steps": [{
                "step": 1, "tool": "t", "returncode": 0,
                "inputs": [{"path": "/nowhere/orphan.bin"}],
                "detected_outputs": ["/tmp/x"],
                "validation": {"/tmp/x": {"passed": True}},
                "resource_usage": {"wall_seconds": 1, "peak_rss_mb": 1,
                                   "max_cpu_percent": 1},
            }],
        }
        v = [x for x in check_workflow_invariants(spec)
             if x.get("invariant") == "I8.composition_coherence"]
        assert v, "the orphan input must still refuse"
        assert "discard_pipeline_draft" in v[0].get("remedy", ""), (
            "the drive had to derive the redrive protocol from first principles at "
            "this refusal (FD6) — the gate is the guide")


# ─────────────────────────── FD7 — the null anchor says what the seal did ───────────

class TestClusterDbAnchorCell:
    def _spec(self, sha, locus):
        rdb = {"name": "db1", "sha256": sha, "size_bytes": 42, "local_path": "/w/db1"}
        if locus:
            rdb["locus"] = locus
        return {"workflow_name": "w", "created_at": "2026-09-15T00:00:00+00:00",
                "pipeline_steps": [], "reference_databases": [rdb],
                "usage": {"command_template": "", "description": "",
                          "inputs": [], "outputs": []}}

    def test_cluster_null_anchor_states_the_locus_verification(self):
        from agent.skills.run_dashboard_html import render_run_dashboard_html
        html = render_run_dashboard_html(self._spec(None, "cluster"))
        assert "verified at the cluster locus" in html, (
            "two 37 GB cluster DBs rendered as a bare dash beside fully-pinned "
            "artifacts (FD7)")

    def test_local_null_anchor_is_just_unanchored(self):
        from agent.skills.run_dashboard_html import render_run_dashboard_html
        html = render_run_dashboard_html(self._spec(None, None))
        assert "no content anchor recorded" in html
        assert "cluster locus" not in html

    def test_real_anchor_still_shows(self):
        from agent.skills.run_dashboard_html import render_run_dashboard_html
        html = render_run_dashboard_html(self._spec("ab" * 32, "cluster"))
        assert ("ab" * 32)[:19] in html


# ──────────────── the I4 host-fallback asks the same locus question ─────────────────

class TestHostFallbackLocusPrecondition:
    def _draft(self, path: str) -> dict:
        return {"conda_env": "some_env",
                "usage": {"command_template": "t {IN} > {OUTPUT_DIR}/o",
                          "inputs": [], "outputs": [{"files": ["o"]}],
                          "trials": [{"name": "t1", "substitutions": {"IN": path}}]}}

    def test_cluster_only_trial_yields_no_mounts(self):
        from agent.mcp_tools.workflow_tools import _local_trial_mounts
        assert _local_trial_mounts(self._draft("/work/cluster/only.vcf")) is None

    def test_local_trial_yields_mounts(self, tmp_path):
        from agent.mcp_tools.workflow_tools import _local_trial_mounts
        f = tmp_path / "in.vcf"
        f.write_text("x")
        mounts = _local_trial_mounts(self._draft(str(f)))
        assert mounts == [(str(tmp_path), str(tmp_path))]

    def test_empty_template_is_not_a_locus_problem(self):
        from agent.mcp_tools.workflow_tools import _local_trial_mounts
        assert _local_trial_mounts({"usage": {"command_template": ""}}) == []

    def test_image_runner_and_fallback_share_the_precondition(self):
        # one implementation: both decision sites must consult _local_trial_mounts
        import inspect
        from agent.mcp_tools import workflow_tools
        assert "_local_trial_mounts(draft)" in inspect.getsource(
            workflow_tools._image_usage_runner)
        seal_src = inspect.getsource(workflow_tools)
        assert "draft.get(\"conda_env\") and _local_trial_mounts(draft) is not None" in seal_src
