"""The pipeline record is derived from the seal and invents nothing."""
from __future__ import annotations

import pytest
from pipeline_fixtures import (DIGEST, GTF, INDEX, REQUEST_KEY, SAMPLES, TEMPLATES,
                               sealed_rnaseq_spec)

from agent.skills import pipeline_record as pr


def _record(**kw):
    return pr.derive_pipeline_record(sealed_rnaseq_spec(), name="rnaseq_counts", **kw)


class TestShapeAndParams:
    def test_three_trials_make_a_three_row_samplesheet(self):
        rec = _record()
        assert [c.name for c in rec.samplesheet.columns] == ["sample", "reads"]
        assert rec.samplesheet.columns[0].placeholder == "SAMPLE"      # the how-to's own identifier
        assert "row key" in rec.samplesheet.columns[0].description
        assert [r["sample"] for r in rec.samplesheet.rows] == SAMPLES
        assert rec.samplesheet.rows[0]["reads"].endswith("SRR1039508_10K_R1.fastq.gz")
        # a per-sample value is a samplesheet cell, never a param default
        assert rec.param("READS").default is None and rec.param("SAMPLE").default is None

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

    def test_one_trial_makes_a_one_row_samplesheet_and_reads_are_per_sample_by_format(self):
        rec = pr.derive_pipeline_record(sealed_rnaseq_spec(["SRR1039508"]), name="one")
        assert [c.name for c in rec.samplesheet.columns] == ["sample", "reads"]
        assert rec.samplesheet.rows == [{"sample": "SRR1039508",
                                         "reads": "/data/reads/SRR1039508_10K_R1.fastq.gz"}]
        assert rec.param("READS").kind == "per_sample"
        assert "fastq" in rec.param("READS").reason
        assert rec.param("STRANDED").kind == "shared"
        assert all(s.scope == "per_sample" for s in rec.stages)

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
        assert align.outputs[0].declared_pattern == "aligned.bam"   # matches the declared glob
        idx = rec.stage("SAMTOOLS")
        assert [i.name for i in idx.inputs] == ["aligned.bam"]
        assert idx.inputs[0].from_stage == "HISAT2"
        assert [o.artifact for o in idx.outputs] == ["aligned.bam.bai"]
        assert idx.outputs[0].declared_pattern is None     # observed, declared by nobody — still an output

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
        assert count.outputs[0].declared_pattern == "*.counts.tsv"
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
        assert len(rec.samplesheet.rows) == 3 and "declared trials" in rec.notes[0]


class TestSamplesheet:
    def test_one_rendering_header_then_the_trial_rows(self):
        rec = _record()
        csv = pr.render_samplesheet(rec)
        lines = csv.splitlines()
        assert lines[0] == "sample,reads"
        assert lines[1] == "SRR1039508,/data/reads/SRR1039508_10K_R1.fastq.gz"
        assert len(lines) == 4
        one = pr.derive_pipeline_record(sealed_rnaseq_spec(["SRR1039508"]), name="one")
        assert pr.render_samplesheet(one) == "sample,reads\nSRR1039508,/data/reads/SRR1039508_10K_R1.fastq.gz\n"


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
        assert {"stage_cut", "publish", "resume", "errors", "cache", "queue_size",
                "run_records", "cleanup", "sheet_preflight", "resources"} <= keys
        assert {d.source for d in rec.defaults} <= {"default", "caller", "seal"}
        # a failing sample must not kill the other samples' in-flight work
        assert {d.key: d.value for d in rec.defaults}["errors"] == (
            "finish: a failure submits nothing new and in-flight tasks complete; `-resume` re-runs what failed; "
            "no retries")
        assert {d.key: d.value for d in rec.defaults}["run_records"] == (
            "one directory per run, runs/<timestamp>/, never overwritten: run.json (the launch line, every param "
            "as resolved, the pipeline's provenance), samples.csv as read, trace.txt (every task), report.html, "
            "timeline.html")
        rec2 = _record(stages=[[0, 1, 2]])
        srcs = {d.key: d.source for d in rec2.defaults}
        assert srcs["stage_cut"] == "caller" and srcs["publish"] == "default"


def _bind_threads(spec, i: int, value: str):
    """Rebind the thread slot in trial `i` — in the proven transcript the record reads
    and in the declared trials it falls back on."""
    uv = spec.usage_verification
    trials = uv["trials"] if isinstance(uv, dict) else uv.trials
    t = trials[i]
    (t["substitutions"] if isinstance(t, dict) else t.substitutions)["THREADS"] = value
    spec.usage.trials[i].substitutions["THREADS"] = value


class TestThreadSlot:
    """A how-to input declared `format: threads` is a `cpus` param: bound to the stage's
    CPU request, never a column or a params.yaml value, with the sealed count as the
    request unless the caller sizes the stage."""

    def test_a_threads_input_is_a_cpus_param_and_the_stage_that_uses_it_requests_the_sealed_count(self):
        rec = pr.derive_pipeline_record(sealed_rnaseq_spec(threads=True), name="t")
        p = rec.param("THREADS")
        assert (p.kind, p.value_kind, p.default, p.source, p.format) == ("cpus", "value", "4", "literal", "threads")
        assert p.reason == "declared format 'threads': the stage's CPU request, 4 in the sealed run"
        assert p.used_by == ["HISAT2"]
        assert [c.name for c in rec.samplesheet.columns] == ["sample", "reads"]       # not a column
        r = rec.stage("HISAT2").resources
        assert (r.cpus, r.mem, r.time, r.requested_by, r.threads_slot) == (4, None, None, "seal", "THREADS")
        assert {i.name: i.origin for i in rec.stage("HISAT2").inputs}["THREADS"] == "request"
        for name in ("SAMTOOLS", "HTSEQ_COUNT"):
            rr = rec.stage(name).resources
            assert (rr.cpus, rr.requested_by, rr.threads_slot) == (None, "default", None)
        assert "THREADS: cpus (declared format 'threads': the stage's CPU request, 4 in the sealed run)" in rec.notes
        assert rec.stage("HISAT2").commands == [TEMPLATES[0].replace("-p 4", "-p {THREADS}")]

    def test_a_caller_request_wins_over_the_sealed_count_and_the_slot_stays(self):
        rec = pr.derive_pipeline_record(sealed_rnaseq_spec(threads=True), name="t", resources={"HISAT2": {"cpus": 8}})
        r = rec.stage("HISAT2").resources
        assert (r.cpus, r.requested_by, r.threads_slot) == (8, "caller", "THREADS")
        rec = pr.derive_pipeline_record(sealed_rnaseq_spec(threads=True), name="t", resources={"HISAT2": {"mem": "32G"}})
        r = rec.stage("HISAT2").resources
        assert (r.cpus, r.mem, r.requested_by, r.threads_slot) == (4, "32G", "caller", "THREADS")

    def test_without_the_declaration_a_literal_count_stays_in_the_command_and_nothing_is_a_slot(self):
        rec = _record()
        assert all(p.kind != "cpus" for p in rec.params)
        assert all(s.resources.threads_slot is None and s.resources.requested_by == "default" for s in rec.stages)
        assert rec.stage("HISAT2").commands == [TEMPLATES[0]] and "-p 4" in TEMPLATES[0]

    def test_a_count_that_is_not_a_positive_whole_number_is_refused(self):
        for bad in ("four", "0", "4.5", "-2"):
            spec = sealed_rnaseq_spec(threads=True)
            _bind_threads(spec, 0, bad)
            with pytest.raises(pr.PipelineDerivationError) as e:
                pr.derive_pipeline_record(spec, name="t")
            assert e.value.code == "pipeline.threads_not_a_count", bad
            assert f"['{bad}']" in e.value.error and "positive whole number" in e.value.remedy

    def test_counts_that_differ_across_trials_are_refused(self):
        spec = sealed_rnaseq_spec(threads=True)
        _bind_threads(spec, 1, "8")
        with pytest.raises(pr.PipelineDerivationError) as e:
            pr.derive_pipeline_record(spec, name="t")
        assert e.value.code == "pipeline.threads_vary"
        assert "['4', '8']" in e.value.error and e.value.remedy == "bind the same thread count in every trial"

    def test_a_caller_naming_the_slot_as_a_column_or_a_param_is_refused(self):
        for kw in ({"per_sample": ["THREADS"]}, {"shared": ["THREADS"]}):
            with pytest.raises(pr.PipelineDerivationError) as e:
                pr.derive_pipeline_record(sealed_rnaseq_spec(threads=True), name="t", **kw)
            assert e.value.code == "pipeline.threads_kind", kw
            assert "drop it from per_sample= / shared=" in e.value.remedy

    def test_the_slot_round_trips_through_yaml(self, tmp_path):
        rec = pr.derive_pipeline_record(sealed_rnaseq_spec(threads=True), name="t")
        assert pr.load_pipeline_record(pr.write_pipeline_record(rec, tmp_path / "t")) == rec


class TestClusterFields:
    def test_env_name_and_sif_path_ride_on_every_stage_of_that_env(self):
        rec = _record(env_names={REQUEST_KEY: "rnaseq_cli"},
                      sif_paths={REQUEST_KEY: "/cluster/containers/rnaseq_cli_48ac8c5b25d2.sif"})
        for st in rec.stages:
            assert st.env_name == "rnaseq_cli"
            assert st.sif_path == "/cluster/containers/rnaseq_cli_48ac8c5b25d2.sif"

    def test_without_a_cluster_the_fields_are_stated_absent(self):
        rec = _record()
        assert all(st.env_name is None and st.sif_path is None for st in rec.stages)


class TestLocalRuntime:
    def test_how_this_machine_provides_nextflow_rides_on_the_record(self, tmp_path):
        rt = pr.LocalRuntime(activate="/checkout/scripts/activate.sh", nextflow="/checkout/.conda_runtime/bin/nextflow")
        rec = _record(local_runtime=rt)
        assert rec.local_runtime == rt
        back = pr.load_pipeline_record(pr.write_pipeline_record(rec, tmp_path / "p"))
        assert back.local_runtime == rt

    def test_an_absent_nextflow_is_stated_not_guessed(self):
        rt = pr.LocalRuntime(activate="/checkout/scripts/activate.sh", nextflow=None)
        assert _record(local_runtime=rt).local_runtime.nextflow is None
        assert _record().local_runtime is None
