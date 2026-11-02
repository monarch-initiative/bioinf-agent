"""The how-to is derived from the recorded steps; nothing is typed again (I4).

Every run tool records, beside the literal command, the template it was given, the
bindings each slot took and the directory it ran in. `derive_usage` reads those into the
`usage` + `usage_verification` record an author used to type by hand, so every reader is
unchanged and a how-to that differs from what ran cannot exist. The pins:

- the templates in step order, one trial per sample (steps grouped by agreeing bindings,
  fixed values filled in), outputs by their path under the work dir with the sample's
  values put back as slots, the transcript per sample;
- a preparation step (an index build whose output a later step binds) is provenance,
  not a how-to command; a per-sample step's file bound by a later step is refused at
  the run call;
- a step recorded without its template refuses the derivation naming the step; a draft
  in which no sample ran every command refuses naming the gaps; a sample that ran some
  commands is named as incomplete;
- the walk re-checks a derived block against its steps (I4.howto_from_steps,
  I4.trial_unproven) and leaves an authored block alone;
- the cluster step records its command in the same grammar;
- the bare shape (`-S {SAMPLE}.sam`, then `samtools sort {SAMPLE}.sam` in one working
  directory) renders every stage with its inputs wired.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.models import core_data as cd
from agent.skills import spec_writer as sw

RU = {"wall_seconds": 1.0, "peak_rss_mb": 10.0, "max_cpu_percent": 50.0}
DIGEST = "sha256:" + "ab" * 32
INDEX_WD = "/work/prep"
GENOME = "/data/genome/chr22.fa"
GTF = "/data/annotation/chr22.gtf"
T_ALIGN = "hisat2 -p {THREADS} -x {INDEX} -U {R1} -S {OUTPUT_DIR}/{SAMPLE}.sam"
T_SORT = "samtools sort -o {OUTPUT_DIR}/{SAMPLE}.sorted.bam {OUTPUT_DIR}/{SAMPLE}.sam"
T_COUNT = "htseq-count -s {STRANDED} -f bam {OUTPUT_DIR}/{SAMPLE}.sorted.bam {GTF} > {OUTPUT_DIR}/{SAMPLE}.counts.txt"


def _step(n, template, bindings, work_dir, outputs, formats=None, **extra):
    command = cd.fill_template(template, bindings, work_dir)
    rel_outs = [f"{work_dir}/{o}" for o in outputs]
    return cd.PipelineStep.produce(
        tool=command.split()[0], command=command, returncode=0, inputs=[],
        detected_outputs=rel_outs,
        validation={o: {"passed": True, "expected_type": o.rsplit(".", 1)[-1], "validation_method": "exists_nonzero"}
                    for o in rel_outs},
        resource_usage=RU, ran_in_container=True, container_image="rnaseq:latest", container_image_digest=DIGEST,
        template=template, bindings=bindings or None, formats=formats, work_dir=work_dir, **extra) | {"step": n}


def _sample(sample, n0, stranded="reverse"):
    wd = f"/work/{sample}"
    base = {"SAMPLE": sample, "THREADS": "4", "INDEX": f"{INDEX_WD}/chr22", "R1": f"/data/reads/{sample}_R1.fastq.gz"}
    return [_step(n0, T_ALIGN, base, wd, [f"{sample}.sam"], formats={"THREADS": "threads"}),
            _step(n0 + 1, T_SORT, {"SAMPLE": sample}, wd, [f"{sample}.sorted.bam", f"{sample}.sorted.bam.bai"]),
            _step(n0 + 2, T_COUNT, {"SAMPLE": sample, "STRANDED": stranded, "GTF": GTF}, wd, [f"{sample}.counts.txt"])]


def _draft(samples=("SRR1039508", "SRR1039509", "SRR1039512"), prep=True):
    steps = []
    if prep:
        steps.append(_step(1, "hisat2-build {FASTA} {OUTPUT_DIR}/chr22", {"FASTA": GENOME}, INDEX_WD,
                           [f"chr22.{i}.ht2" for i in range(1, 9)]))
    for s in samples:
        steps += _sample(s, len(steps) + 1)
    first = samples[0] if samples else "A"
    return {"description": "align, sort, count", "test_data": {"genome_build": "hg38", "r1": f"/data/reads/{first}_R1.fastq.gz"},
            "pipeline_steps": steps}


class TestDerivation:
    def test_templates_in_step_order_one_trial_per_sample_and_the_prep_step_is_provenance(self):
        d = sw.derive_usage(_draft())
        assert "problems" not in d, d
        u, uv = d["usage"], d["usage_verification"]
        assert u["command_template"] == [T_ALIGN, T_SORT, T_COUNT]
        assert [i["name"] for i in u["inputs"]] == ["THREADS", "INDEX", "R1", "SAMPLE", "STRANDED", "GTF"]
        assert {i["name"]: i["format"] for i in u["inputs"]}["THREADS"] == "threads"
        assert {i["name"]: i["format"] for i in u["inputs"]}["R1"] == "fastq.gz"
        assert [t["name"] for t in u["trials"]] == ["SRR1039508", "SRR1039509", "SRR1039512"]
        assert u["trials"][1]["substitutions"] == {
            "THREADS": "4", "INDEX": f"{INDEX_WD}/chr22", "R1": "/data/reads/SRR1039509_R1.fastq.gz",
            "SAMPLE": "SRR1039509", "STRANDED": "reverse", "GTF": GTF}
        assert u["outputs"] == [{"name": "OUTPUT_DIR", "files": [
            "{SAMPLE}.sam", "{SAMPLE}.sorted.bam", "{SAMPLE}.sorted.bam.bai", "{SAMPLE}.counts.txt"]}]
        assert uv["status"] == "verified" and uv["source"] == "steps" and uv["locus"] == "image"
        assert uv["trial_count"] == uv["passed"] == 3 and uv["preparation_steps"] == [1]
        t0 = uv["trials"][0]
        assert t0["steps"] == [2, 3, 4] and t0["ok"] is True
        assert t0["commands_run"][0] == ("hisat2 -p 4 -x /work/prep/chr22 -U /data/reads/SRR1039508_R1.fastq.gz "
                                         "-S /work/SRR1039508/SRR1039508.sam")
        assert t0["images_run"] == [DIGEST] * 3 and t0["output_slots"] == ["OUTPUT_DIR"]
        assert t0["substitutions"]["OUTPUT_DIR"] == "/work/SRR1039508"
        assert t0["produced_files"] == ["SRR1039508.sam", "SRR1039508.sorted.bam", "SRR1039508.sorted.bam.bai",
                                        "SRR1039508.counts.txt"]
        assert {v["expected_type"] for v in t0["validation_results"]} == {"sam", "bam", "bai", "txt"}
        assert "partial" not in uv
        # the record shape every reader already knows
        cd.UsageTemplate.model_validate(u)
        assert cd.usage_commands(u) == [T_ALIGN, T_SORT, T_COUNT]

    def test_a_fixed_command_proves_every_sample_and_an_incomplete_sample_is_named(self):
        d = _draft(samples=("A", "B"))
        d["pipeline_steps"] = d["pipeline_steps"][:-1]            # B never counted
        out = sw.derive_usage(d)
        assert "problems" not in out, out
        assert [t["name"] for t in out["usage"]["trials"]] == ["A"]
        assert out["usage_verification"]["partial"] == [{"name": "B", "missing_commands": [3]}]

    def test_no_sample_that_ran_every_command_refuses_naming_the_gaps(self):
        """A aligned and sorted, B only counted: three commands, no sample ran all three."""
        d = _draft(samples=("A", "B"), prep=False)
        steps = d["pipeline_steps"]
        d["pipeline_steps"] = [steps[0], steps[1], steps[5]]
        out = sw.derive_usage(d)
        assert out["problems"] and "no sample ran every how-to command" in out["problems"][0]
        assert "A: no step ran how-to command(s) 3" in out["problems"][0]
        assert "B: no step ran how-to command(s) 1, 2" in out["problems"][0]

    def test_a_step_without_its_template_refuses_naming_the_step(self):
        d = _draft(samples=("A",))
        d["pipeline_steps"][2].pop("template")
        out = sw.derive_usage(d)
        assert out["problems"] and "step 3 (samtools)" in out["problems"][0] and "bindings=" in out["problems"][0]

    def test_a_draft_with_no_clean_step_refuses(self):
        assert "no recorded step exited 0" in sw.derive_usage({"pipeline_steps": []})["problems"][0]

    def test_the_same_command_run_twice_is_one_proof(self):
        d = _draft(samples=("A",), prep=False)
        again = dict(d["pipeline_steps"][0]) | {"step": 4}
        d["pipeline_steps"].append(again)
        out = sw.derive_usage(d)
        assert [t["name"] for t in out["usage"]["trials"]] == ["A"] and "partial" not in out["usage_verification"]

    def test_a_cohort_step_with_no_slot_is_one_fixed_command(self):
        d = {"pipeline_steps": [_step(1, "Rscript deseq2.R counts/ > {OUTPUT_DIR}/results.tsv", {}, "/work/de", ["results.tsv"])]}
        out = sw.derive_usage(d)
        u = out["usage"]
        assert u["command_template"] == ["Rscript deseq2.R counts/ > {OUTPUT_DIR}/results.tsv"] and u["inputs"] == []
        assert [t["name"] for t in u["trials"]] == ["trial_1"] and u["trials"][0]["substitutions"] == {}


class TestTheWalk:
    def _spec(self):
        d = _draft(samples=("A",))
        out = sw.derive_usage(d)
        return {**d, "usage": out["usage"], "usage_verified": True, "usage_verification": out["usage_verification"]}

    def _i4(self, spec):
        return [v["invariant"] for v in sw.check_invariants(spec) if v["invariant"].startswith("I4")]

    def test_a_derived_block_that_matches_its_steps_passes(self):
        assert self._i4(self._spec()) == []

    def test_a_command_no_step_ran_and_a_trial_no_step_bound_are_each_refused(self):
        spec = self._spec()
        spec["usage"]["command_template"].append("rm -rf {OUTPUT_DIR}")
        spec["usage"]["trials"].append({"name": "ghost", "substitutions": {**spec["usage"]["trials"][0]["substitutions"], "SAMPLE": "ghost"}})
        ids = self._i4(spec)
        assert "I4.howto_from_steps" in ids and "I4.trial_unproven" in ids

    def test_an_authored_block_is_not_asked(self):
        spec = self._spec()
        spec["usage_verification"] = {"status": "verified", "reason": "", "trials": []}   # a seal before the derivation
        spec["usage"]["command_template"] = ["something the author typed {R1} > {OUTPUT_DIR}/x"]
        assert self._i4(spec) == []


class TestTheRunCall:
    def test_binding_a_file_a_per_sample_step_wrote_is_refused_naming_the_directory(self):
        d = _draft(samples=("A",), prep=False)
        problems = sw.binds_prior_sample_output(d, {"SAM": "/work/A/A.sam", "SAMPLE": "A"})
        assert len(problems) == 1 and "{SAM} is bound to /work/A/A.sam" in problems[0]
        assert "`{OUTPUT_DIR}/A.sam`" in problems[0] and "watch_dir='/work/A'" in problems[0]

    def test_binding_a_preparation_steps_output_is_fine(self):
        d = _draft(samples=(), prep=True)
        assert sw.binds_prior_sample_output(d, {"INDEX": f"{INDEX_WD}/chr22"}) == []

    def test_run_step_in_container_fills_the_slots_records_the_template_and_adds_bound_paths_as_inputs(self, monkeypatch, tmp_path):
        from agent import mcp_server as m
        from agent.mcp_tools import run_tools as R
        reads = tmp_path / "A_R1.fastq.gz"
        reads.write_text("x")
        rec = {"image": "env:latest", "image_digest": DIGEST, "platform": "linux/amd64"}
        monkeypatch.setattr(m._env_cache, "lookup_verified", lambda k: (rec, []))
        monkeypatch.setattr(m, "_check_docker_available", lambda: None)
        monkeypatch.setattr(m._locus, "daemon_is_remote", lambda: False)
        monkeypatch.setattr(m._docker, "_run", lambda *a, **k: {"returncode": 0, "stdout": "", "stderr": ""})
        monkeypatch.setattr(m._docker, "image_digest", lambda img: DIGEST)
        ran = {}

        def _run(ref, command, mounts=None, workdir=None, platform=None, timeout=None):
            ran["command"], ran["workdir"] = command, workdir
            Path(workdir, "A.sam").write_text("x")
            return {"returncode": 0, "stdout": "", "stderr": "", "resource_usage": dict(RU)}
        monkeypatch.setattr(m._docker, "run_in_container", _run)
        monkeypatch.setattr(m._env_mgr, "hash_outputs", lambda outs: {})
        monkeypatch.setattr(m._validator, "validate", lambda *a, **k: {"passed": True, "validation_method": "stub"})
        recorded = {}
        monkeypatch.setattr(m._pipeline_state, "add_step", lambda pid, data, replace_step=0: recorded.update(data) or 1)
        monkeypatch.setattr(m._pipeline_state, "add_validations", lambda *a, **k: None)
        monkeypatch.setattr(m._pipeline_state, "get_draft", lambda pid: {"pipeline_steps": []})
        wd = tmp_path / "A"
        out = R.run_step_in_container("k", "hisat2 -p {THREADS} -U {R1} -S {OUTPUT_DIR}/{SAMPLE}.sam", "p",
                                      bindings={"THREADS": "4", "R1": str(reads), "SAMPLE": "A"},
                                      formats={"THREADS": "threads"}, watch_dir=str(wd))
        assert out["code"] == "run_container.step_ran", out
        assert ran["command"] == f"hisat2 -p 4 -U {reads} -S {wd.resolve()}/A.sam"
        assert recorded["template"] == "hisat2 -p {THREADS} -U {R1} -S {OUTPUT_DIR}/{SAMPLE}.sam"
        assert recorded["bindings"] == {"THREADS": "4", "R1": str(reads), "SAMPLE": "A"}
        assert recorded["formats"] == {"THREADS": "threads"} and recorded["work_dir"] == str(wd.resolve())
        assert recorded["command"] == ran["command"]
        assert [i["path"] for i in recorded["inputs"]] == [str(reads)], "a bound file is an input"

    def test_the_cluster_step_records_its_command_in_the_same_grammar(self):
        from agent.skills.run_cluster_step import cluster_howto
        template, bindings = cluster_howto("hisat2 -x ${index} -U ${reads} -S ${out_sam}",
                                           {"index": "/c/idx/chr22", "reads": "/c/r/A.fq"}, {"out_sam": "A.sam"},
                                           "/c/scratch/p/wf")
        assert template == "hisat2 -x {INDEX} -U {READS} -S {OUTPUT_DIR}/A.sam"
        assert bindings == {"INDEX": "/c/idx/chr22", "READS": "/c/r/A.fq"}


class TestTheBareShape:
    """c3's shape: a step run in its working directory writes `-S {SAMPLE}.sam` and the
    next reads `{SAMPLE}.sam` bare. Every stage renders, with its inputs wired."""

    def _spec(self):
        from agent.models.core_data import WorkflowSpec
        t_align = "hisat2 -x {INDEX} -U {R1} -S {SAMPLE}.sam"
        t_sort = "samtools sort -o {SAMPLE}.sorted.bam {SAMPLE}.sam"
        t_index = "samtools index {SAMPLE}.sorted.bam"
        t_count = "htseq-count -f bam {SAMPLE}.sorted.bam {GTF} > {SAMPLE}.counts.txt"
        steps = []
        for i, s in enumerate(("A", "B")):
            wd = f"/work/{s}"
            b = {"SAMPLE": s, "INDEX": f"{INDEX_WD}/chr22", "R1": f"/data/reads/{s}_R1.fastq.gz", "GTF": GTF}
            n = 4 * i
            steps += [_step(n + 1, t_align, {k: b[k] for k in ("SAMPLE", "INDEX", "R1")}, wd, [f"{s}.sam"]),
                      _step(n + 2, t_sort, {"SAMPLE": s}, wd, [f"{s}.sorted.bam"]),
                      _step(n + 3, t_index, {"SAMPLE": s}, wd, [f"{s}.sorted.bam.bai"]),
                      _step(n + 4, t_count, {"SAMPLE": s, "GTF": GTF}, wd, [f"{s}.counts.txt"])]
        d = {"description": "c3", "pipeline_steps": steps,
             "test_data": {"genome_build": "hg38", "r1": "/data/reads/A_R1.fastq.gz",
                           "samples": [{"r1": "/data/reads/A_R1.fastq.gz"}, {"r1": "/data/reads/B_R1.fastq.gz"}]}}
        out = sw.derive_usage(d)
        assert "problems" not in out, out
        return WorkflowSpec.model_validate({
            "workflow_name": "c3_counts", "description": "c3", "created_at": "2026-10-28T00:00:00+00:00",
            "env_request_key": "k", "env_content_digest": "sha256:c", "env_image": f"rnaseq@{DIGEST}",
            "pipeline_status": "fully_validated", "usage_verified": True, "validated_in_shipped_image": True,
            "usage": out["usage"], "usage_verification": out["usage_verification"], "pipeline_steps": steps,
            "envs": [{"request_key": "k", "image": f"rnaseq@{DIGEST}", "image_digest": DIGEST}]})

    def test_all_four_stages_render_and_each_consumes_the_one_before(self):
        from agent.skills import pipeline_record as pr
        from agent.skills.pipeline_render import render_pipeline_files
        rec = pr.derive_pipeline_record(self._spec(), name="c3")
        assert [s.name for s in rec.stages] == ["HISAT2", "SAMTOOLS", "SAMTOOLS_2", "HTSEQ_COUNT"]
        assert rec.unmatched_steps == []
        assert [(i.name, i.from_stage) for i in rec.stages[1].inputs if i.origin == "stage"] == [("{SAMPLE}.sam", "HISAT2")]
        assert [(i.name, i.from_stage) for i in rec.stages[2].inputs if i.origin == "stage"] == [("{SAMPLE}.sorted.bam", "SAMTOOLS")]
        third = {(i.name, i.from_stage) for i in rec.stages[3].inputs if i.origin == "stage"}
        assert ("{SAMPLE}.sorted.bam", "SAMTOOLS") in third and ("{SAMPLE}.sorted.bam.bai", "SAMTOOLS_2") in third
        assert [r["sample"] for r in rec.samplesheet.rows] == ["A", "B"]
        main = render_pipeline_files(rec)["main.nf"]
        assert 'samtools sort -o ${meta.sample}.sorted.bam ${meta.sample}.sam' in main
        assert 'tuple val(meta), path("${meta.sample}.counts.txt"), emit: counts_txt' in main


class TestThePrefixFamily:
    """One reading of an index prefix: the files named after a value that is itself nothing
    on disk (`/ref/idx/chr22` for `chr22.1.ht2` …)."""

    def test_a_prefix_names_the_files_beside_it_and_nothing_else_is_one(self, tmp_path):
        from agent.models.core_data import prefix_family
        idx = tmp_path / "idx"
        idx.mkdir()
        fam = [idx / f"chr22.{i}.ht2" for i in (1, 2, 8)]
        for f in fam:
            f.write_text("x")
        (idx / "chr22b.fa").write_text("x")          # shares the letters, not the family
        (idx / "chr22.notes").mkdir()                 # a directory is not a member
        assert prefix_family(f"{idx}/chr22") == sorted(str(f) for f in fam)
        assert prefix_family(str(fam[0])) == []        # a file is a file
        assert prefix_family(str(idx)) == []           # a directory is a directory
        assert prefix_family(f"{idx}/chr21") == []     # no family
        assert prefix_family(f"{tmp_path}/gone/chr22") == []
        assert prefix_family("chr22") == [] and prefix_family("{INDEX}") == [] and prefix_family(None) == []

    def test_the_run_call_admits_a_prefix_by_its_directory_and_a_text_slot_never(self, tmp_path):
        """c3: `-x {INDEX}` bound to a HISAT2 prefix was no input, so the container never saw
        the index's directory and hisat2 reported it missing."""
        from agent.mcp_tools import run_tools as R
        idx = tmp_path / "idx"
        idx.mkdir()
        for i in (1, 2):
            (idx / f"chr22.{i}.ht2").write_text("x")
        reads = tmp_path / "A_R1.fastq.gz"
        reads.write_text("x")
        inside = tmp_path / "inside_the_image"
        inside.mkdir()
        got = R._bound_inputs([], {"THREADS": "4", "INDEX": f"{idx}/chr22", "R1": str(reads), "DB": str(inside),
                                   "SAMPLE": "A", "LONE": f"{tmp_path}/nothing"},
                              {"THREADS": "threads", "DB": "value"})
        assert got == [str(idx), str(reads)], got
        assert R._bound_inputs([str(reads)], {"R1": str(reads)}, {}) == [str(reads)], "declared once, listed once"


class TestTheDerivedInputFormat:
    def test_the_format_is_the_last_extension_with_its_compression_a_directory_or_a_prefix(self, tmp_path):
        idx = tmp_path / "idx"
        idx.mkdir()
        (idx / "chr22.1.ht2").write_text("x")
        f = sw._input_format
        assert f("GTF", ["/r/gencode.v47.basic.chr22.gtf"], {}) == "gtf"
        assert f("VCF", ["/r/sample.vcf.gz"], {}) == "vcf.gz" and f("R1", ["/r/x_10K_R1.fastq.gz"], {}) == "fastq.gz"
        assert f("A", ["/r/bundle.tar.gz"], {}) == "tar.gz" and f("B", ["/r/README"], {}) is None
        assert f("D", [str(idx)], {}) == "directory" and f("I", [f"{idx}/chr22"], {}) == "prefix"
        assert f("T", ["4"], {"T": "threads"}) == "threads" and f("S", ["A"], {}) is None
