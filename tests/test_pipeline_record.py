"""The pipeline record is derived from the seal and invents nothing."""
from __future__ import annotations

import pytest
from pipeline_fixtures import (DIGEST, GTF, INDEX, REQUEST_KEY, SAMPLES, TEMPLATES,
                               sealed_rnaseq_spec)

from agent.skills import pipeline_record as pr


def _record(**kw):
    return pr.derive_pipeline_record(sealed_rnaseq_spec(), name="rnaseq_counts", **kw)


class TestShapeAndParams:
    def test_three_rows_make_a_per_row_pipeline_with_a_samplesheet(self):
        rec = _record()
        assert rec.shape == "per_row"
        assert rec.samplesheet is not None
        assert [c.name for c in rec.samplesheet.columns] == ["sample", "reads"]
        assert [r["sample"] for r in rec.samplesheet.rows] == SAMPLES
        assert rec.samplesheet.rows[0]["reads"].endswith("SRR1039508_10K_R1.fastq.gz")

    def test_per_sample_vs_shared_is_read_off_the_trials(self):
        rec = _record()
        assert rec.param("READS").kind == "per_sample"
        assert rec.param("SAMPLE").kind == "per_sample"
        assert rec.param("STRANDED").kind == "shared"
        assert rec.param("STRANDED").default == "reverse"
        assert rec.param("STRANDED").value_kind == "value"
        assert rec.param("GTF").kind == "shared" and rec.param("GTF").default == GTF
        assert "differs across the 3 trials" in rec.param("READS").reason
        assert "identical across the 3 trials" in rec.param("STRANDED").reason

    def test_a_shared_input_made_by_an_unmatched_step_is_provenance(self):
        rec = _record()
        idx = rec.param("HISAT2_INDEX")
        assert idx.kind == "shared" and idx.default == INDEX
        assert idx.source == "sealed_step:1"
        assert idx.value_kind == "prefix"                 # chr22.1.ht2 … chr22.8.ht2 share it
        assert rec.param("GTF").value_kind == "path"
        assert rec.param("STRANDED").value_kind == "value"
        assert [p.produces_param for p in rec.provenance_steps] == ["HISAT2_INDEX"]
        assert rec.unmatched_steps == []

    def test_one_row_is_linear_and_reads_are_per_sample_by_format(self):
        rec = pr.derive_pipeline_record(sealed_rnaseq_spec(["SRR1039508"]), name="one")
        assert rec.shape == "linear"
        assert rec.samplesheet is None
        assert rec.param("READS").kind == "per_sample"
        assert "fastq" in rec.param("READS").reason
        assert rec.param("STRANDED").kind == "shared"

    def test_caller_overrides_are_honoured_and_stated(self):
        rec = _record(shared=["READS"])
        assert rec.param("READS").kind == "shared"
        assert rec.param("READS").reason == "set by caller"
        with pytest.raises(pr.PipelineDerivationError) as e:
            _record(per_sample=["NOPE"])
        assert e.value.code == "pipeline.unknown_placeholder"


class TestStages:
    def test_default_is_one_stage_per_howto_command(self):
        rec = _record()
        assert [s.name for s in rec.stages] == ["HISAT2", "SAMTOOLS", "HTSEQ_COUNT"]
        assert [s.commands for s in rec.stages] == [[t] for t in TEMPLATES]
        assert all(s.scope == "per_sample" for s in rec.stages)
        # every row's step backs its stage
        assert rec.stage("HISAT2").sealed_steps == [2, 5, 8]
        assert rec.stage("HTSEQ_COUNT").sealed_steps == [4, 7, 10]

    def test_artifacts_flow_by_name_between_stages(self):
        rec = _record()
        align = rec.stage("HISAT2")
        assert [o.artifact for o in align.outputs] == ["aligned.bam"]
        assert align.outputs[0].observed == "aligned.bam"
        assert align.outputs[0].consumed_by == ["HTSEQ_COUNT", "SAMTOOLS"]
        assert align.outputs[0].published is True          # matches the declared glob
        assert align.outputs[0].declared_pattern == "aligned.bam"
        idx = rec.stage("SAMTOOLS")
        assert [i.name for i in idx.inputs] == ["aligned.bam"]
        assert idx.inputs[0].from_stage == "HISAT2"
        assert [o.artifact for o in idx.outputs] == ["aligned.bam.bai"]
        assert idx.outputs[0].published is False          # an intermediate nobody declared

    def test_the_count_stage_sees_params_columns_artifacts_and_sidecars(self):
        rec = _record()
        count = rec.stage("HTSEQ_COUNT")
        by_name = {i.name: i for i in count.inputs}
        assert by_name["STRANDED"].origin == "param"
        assert by_name["GTF"].origin == "param"
        assert by_name["SAMPLE"].origin == "column"
        assert by_name["aligned.bam"].origin == "stage" and by_name["aligned.bam"].from_stage == "HISAT2"
        assert by_name["aligned.bam.bai"].from_stage == "SAMTOOLS"   # the sidecar travels with its parent
        assert [o.artifact for o in count.outputs] == ["{SAMPLE}.counts.tsv"]
        assert count.outputs[0].observed == "SRR1039508.counts.tsv"
        assert count.outputs[0].published and count.outputs[0].declared_pattern == "*.counts.tsv"
        assert rec.param("STRANDED").used_by == ["HTSEQ_COUNT"]

    def test_every_stage_names_the_image_the_seal_observed(self):
        rec = _record()
        for s in rec.stages:
            assert s.image_digest == DIGEST
            assert s.request_key == REQUEST_KEY
            assert s.sif_sha256 is None
        assert rec.env_digests == [DIGEST]

    def test_resources_are_measured_maxima_with_their_authority_and_never_the_request(self):
        rec = _record()
        r = rec.stage("HISAT2").resources
        assert r.measured_authority == "authoritative"
        assert r.measured_peak_rss_mb == pytest.approx(1570.0)   # the largest row
        assert r.requested_by == "default" and r.cpus is None and r.mem is None
        rec2 = _record(resources={"HISAT2": {"cpus": 8, "mem": "32G", "time": "4:00:00"}})
        r2 = rec2.stage("HISAT2").resources
        assert (r2.cpus, r2.mem, r2.time, r2.requested_by) == (8, "32G", "4:00:00", "caller")
        assert rec2.stage("SAMTOOLS").resources.requested_by == "default"

    def test_caller_can_merge_consecutive_commands_into_one_stage(self):
        rec = _record(stages=[[0, 1], [2]], stage_names=["ALIGN", "COUNT"])
        assert [s.name for s in rec.stages] == ["ALIGN", "COUNT"]
        assert rec.stage("ALIGN").commands == TEMPLATES[:2]
        assert [o.artifact for o in rec.stage("ALIGN").outputs] == ["aligned.bam", "aligned.bam.bai"]
        assert {i.name for i in rec.stage("COUNT").inputs if i.origin == "stage"} == {"aligned.bam", "aligned.bam.bai"}
        assert rec.stage("ALIGN").sealed_steps == [2, 3, 5, 6, 8, 9]

    def test_a_bad_grouping_is_refused_with_the_rule(self):
        with pytest.raises(pr.PipelineDerivationError) as e:
            _record(stages=[[0, 2], [1]])
        assert e.value.code == "pipeline.bad_stage_groups"


class TestRefusals:
    def test_an_artifact_nobody_produces_is_an_orphan(self):
        bad = TEMPLATES[:1] + ["samtools index {OUTPUT_DIR}/missing.bam"]
        spec = sealed_rnaseq_spec(templates=bad)
        with pytest.raises(pr.PipelineDerivationError) as e:
            pr.derive_pipeline_record(spec, name="x")
        assert e.value.code == "pipeline.orphan_artifact"
        assert "missing.bam" in e.value.error and e.value.remedy

    def test_no_howto_is_a_refusal_naming_the_remedy(self):
        spec = sealed_rnaseq_spec()
        spec.usage = None
        with pytest.raises(pr.PipelineDerivationError) as e:
            pr.derive_pipeline_record(spec, name="x")
        assert e.value.code == "pipeline.no_howto" and "usage" in e.value.remedy

    def test_declared_trials_are_the_fallback_when_nothing_was_proven(self):
        rec = pr.derive_pipeline_record(sealed_rnaseq_spec(proven=False), name="x")
        assert rec.shape == "per_row" and "declared trials" in rec.notes[0]


class TestSamplesheet:
    def test_one_rendering_header_then_the_trial_rows(self):
        rec = _record()
        csv = pr.render_samplesheet(rec)
        lines = csv.splitlines()
        assert lines[0] == "sample,reads"
        assert lines[1] == "SRR1039508,/data/reads/SRR1039508_10K_R1.fastq.gz"
        assert len(lines) == 4
        assert pr.render_samplesheet(pr.derive_pipeline_record(sealed_rnaseq_spec(["SRR1039508"]), name="one")) == ""


class TestRecordOnDisk:
    def test_round_trips_through_yaml(self, tmp_path):
        rec = _record()
        path = pr.write_pipeline_record(rec, tmp_path / "rnaseq_counts")
        assert path.name == pr.RECORD_FILENAME
        back = pr.load_pipeline_record(path)
        assert back == rec

    def test_the_defaults_table_states_every_unsaid_thing_with_its_source(self):
        rec = _record()
        keys = {d.key for d in rec.defaults}
        assert {"shape", "stage_cut", "publish", "resume", "errors", "cache", "queue_size",
                "run_records", "cleanup", "sheet_preflight", "resources"} <= keys
        assert {d.source for d in rec.defaults} <= {"default", "caller", "seal"}
        rec2 = _record(stages=[[0, 1, 2]], publish="all")
        srcs = {d.key: d.source for d in rec2.defaults}
        assert srcs["stage_cut"] == "caller" and srcs["publish"] == "caller"
