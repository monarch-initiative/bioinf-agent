"""A rendered pipeline runs what the sealed run ran, for how-to shapes the rnaseq chain
does not have. Each test derives a record from a small sealed spec of one shape
(`probe_spec`) and pins either what main.nf / samples.csv carry or the refusal that
names why they cannot.

Pinned here:
- an output the command does not name (FastQC's `<reads stem>_fastqc.html`) is one
  artifact across rows, `*_fastqc.html`, with one trial or many; an output the
  command names keeps the name the command writes;
- a cohort fan-in collects the LAST per-sample stage that writes the file;
- a second per-sample identifier gets its own samples.csv column;
- an empty option at either end of a command still matches its sealed steps;
- `{OUTPUT_DIR}/` with nothing after it binds to the task's directory;
- a backslash in a value reaches the command as written;
- a value samples.csv cannot carry unquoted, a samplesheet column that is not an
  identifier, `resources=` naming no stage or a stage of both workflows, and
  `collect=` on a thread or samplesheet slot each refuse, naming the remedy.
"""
from __future__ import annotations

import pytest
import yaml
from pipeline_fixtures import SHEET, probe_spec, sealed_deseq2_spec, sheet_text

from agent.skills import pipeline_record as pr
from agent.skills.pipeline_render import render_pipeline_files

_SAMPLES = ["SRR1039508", "SRR1039509"]


def _main(rec) -> list[str]:
    return [line.strip() for line in render_pipeline_files(rec)["main.nf"].splitlines()]


def _refusal(code: str, **kw):
    with pytest.raises(pr.PipelineDerivationError) as e:
        pr.derive_pipeline_record(name="probe", **kw)
    assert e.value.code == code, str(e.value)
    return e.value


# ── outputs the command does not name ─────────────────────────────────────

_QC = ["fastqc -o {OUTPUT_DIR} {READS}", "multiqc -o {OUTPUT_DIR} {OUTPUT_DIR}"]


def _qc_spec(samples: list[str]):
    def outs(ti, run, s):
        stem = s["READS"].rsplit("/", 1)[-1].removesuffix(".fastq.gz")
        return [f"{run}/{stem}_fastqc.html", f"{run}/{stem}_fastqc.zip"] if ti == 0 else [f"{run}/multiqc_report.html"]
    return probe_spec(_QC, [(x, {"READS": f"/data/{x}_R1.fastq.gz"}) for x in samples],
                      [{"name": "OUTPUT_DIR", "files": ["*_fastqc.html", "multiqc_report.html"]}],
                      inputs=[{"name": "READS", "format": "fastq"}], step_outputs=outs)


@pytest.mark.parametrize("samples", [["s1"], ["s1", "s2"]], ids=["one_trial", "two_trials"])
def test_an_output_named_by_the_tool_is_one_artifact_across_rows(samples):
    rec = pr.derive_pipeline_record(_qc_spec(samples), name="qc")
    fastqc, multiqc = rec.stages
    assert [(o.artifact, o.published) for o in fastqc.outputs] == [("*_fastqc.html", True), ("*_fastqc.zip", False)]
    assert [(i.name, i.origin, i.from_stage) for i in multiqc.inputs] == [
        ("*_fastqc.html", "stage", fastqc.name), ("*_fastqc.zip", "stage", fastqc.name)]
    assert "tuple val(meta), path('*_fastqc.html'), emit: fastqc_html" in _main(rec)


def test_an_output_the_command_names_keeps_its_written_name():
    """`hg38.{SAMPLE}.txt` with SAMPLE=38: the name is the command's, not the observed
    file re-templated wherever `38` occurs."""
    sp = probe_spec(["tool {READS} > {OUTPUT_DIR}/hg38.{SAMPLE}.txt"], [("t", {"READS": "/d/a.fq", "SAMPLE": "38"})],
                    [{"name": "OUTPUT_DIR", "files": ["*.txt"]}], inputs=[{"name": "READS"}, {"name": "SAMPLE"}],
                    step_outputs=lambda ti, run, s: [f"{run}/hg38.{s['SAMPLE']}.txt"])
    assert [o.artifact for o in pr.derive_pipeline_record(sp, name="probe").stages[0].outputs] == ["hg38.{SAMPLE}.txt"]


# ── the cohort fan-in ─────────────────────────────────────────────────────

_COUNT_THEN_SORT = ["htseq-count {BAM} {GTF} > {OUTPUT_DIR}/{SAMPLE}.counts.tsv",
                    "sort -o {OUTPUT_DIR}/{SAMPLE}.counts.tsv {OUTPUT_DIR}/{SAMPLE}.counts.tsv"]


def _counts_spec():
    return probe_spec(_COUNT_THEN_SORT,
                      [(s, {"BAM": f"/data/{s}.bam", "GTF": "/data/g.gtf", "SAMPLE": s}) for s in _SAMPLES],
                      [{"name": "OUTPUT_DIR", "files": ["*.counts.tsv"]}],
                      inputs=[{"name": "BAM", "format": "bam"}, {"name": "GTF"}, {"name": "SAMPLE"}],
                      step_outputs=lambda ti, run, s: [f"{run}/{s['SAMPLE']}.counts.tsv"])


def _cohort(collect: dict, spec=None) -> pr.CohortRequest:
    return pr.CohortRequest(spec=spec or sealed_deseq2_spec(_SAMPLES), spec_path="/x", spec_sha256="d" * 64,
                            collect=collect)


_COLLECT = {"COUNTS_DIR": "{SAMPLE}.counts.tsv"}


def test_a_cohort_collects_the_last_stage_that_writes_the_file():
    rec = pr.derive_pipeline_record(_counts_spec(), name="probe", samplesheet_files={SHEET: sheet_text(_SAMPLES)},
                                    cohort=[_cohort(_COLLECT)])
    assert [c.from_stage for c in rec.stage("MERGE_COUNTS").inputs if c.origin == "collect"] == ["SORT"]
    assert any(line.startswith("MERGE_COUNTS(SORT.out.counts_tsv") for line in _main(rec))


# ── identifiers ───────────────────────────────────────────────────────────


def test_a_second_identifier_gets_its_own_column():
    sp = probe_spec(["tool --name {SAMPLE} --accession {SAMPLE_ID} {READS} > {OUTPUT_DIR}/{SAMPLE}.txt"],
                    [("t1", {"READS": "/data/a.fq", "SAMPLE": "patientA", "SAMPLE_ID": "SRR001"}),
                     ("t2", {"READS": "/data/b.fq", "SAMPLE": "patientB", "SAMPLE_ID": "SRR002"})],
                    [{"name": "OUTPUT_DIR", "files": ["*.txt"]}],
                    inputs=[{"name": "READS", "format": "fastq"}, {"name": "SAMPLE"}, {"name": "SAMPLE_ID"}],
                    step_outputs=lambda ti, run, s: [f"{run}/{s['SAMPLE']}.txt"])
    files = render_pipeline_files(pr.derive_pipeline_record(sp, name="probe"))
    assert files["samples.csv"].splitlines() == ["sample,sample_id,reads", "patientA,SRR001,/data/a.fq",
                                                 "patientB,SRR002,/data/b.fq"]
    assert "tool --name ${meta.sample} --accession ${meta.sample_id} ${reads} > ${meta.sample}.txt" in \
        [line.strip() for line in files["main.nf"].splitlines()]


# ── binding at the edges of a command ─────────────────────────────────────


@pytest.mark.parametrize("template", ["tool -o {OUTPUT_DIR}/{SAMPLE}.txt {READS} {EXTRA}",
                                      "{EXTRA} tool -o {OUTPUT_DIR}/{SAMPLE}.txt {READS}",
                                      "tool {EXTRA} -o {OUTPUT_DIR}/{SAMPLE}.txt {READS}"],
                         ids=["end", "start", "middle"])
def test_an_empty_option_anywhere_still_matches_its_steps(template):
    sp = probe_spec([template], [(x, {"READS": f"/data/{x}.fq", "SAMPLE": x, "EXTRA": ""}) for x in ("s1", "s2")],
                    [{"name": "OUTPUT_DIR", "files": ["*.txt"]}],
                    inputs=[{"name": "READS", "format": "fastq"}, {"name": "SAMPLE"}, {"name": "EXTRA"}],
                    step_outputs=lambda ti, run, s: [f"{run}/{s['SAMPLE']}.txt"])
    rec = pr.derive_pipeline_record(sp, name="probe")
    assert rec.stages[0].sealed_steps == [1, 2] and not rec.unmatched_steps


def test_an_output_slot_with_a_bare_slash_binds_to_the_task_directory():
    sp = probe_spec(["fastqc --outdir {OUTPUT_DIR}/ {READS}"], [(x, {"READS": f"/data/{x}.fq"}) for x in ("s1", "s2")],
                    [{"name": "OUTPUT_DIR", "files": ["*_fastqc.html"]}],
                    inputs=[{"name": "READS", "format": "fastq"}],
                    step_outputs=lambda ti, run, s: [f"{run}/{s['READS'].rsplit('/', 1)[-1][:-3]}_fastqc.html"])
    assert "fastqc --outdir ./ ${reads}" in _main(pr.derive_pipeline_record(sp, name="probe"))


def test_a_backslash_in_a_value_reaches_the_command_as_written():
    sp = probe_spec(["tool --sep '{SEP}' {READS} > {OUTPUT_DIR}/{SAMPLE}.txt"],
                    [(x, {"READS": f"/data/{x}.fq", "SAMPLE": x, "SEP": "\\t"}) for x in ("s1", "s2")],
                    [{"name": "OUTPUT_DIR", "files": ["*.txt"]}],
                    inputs=[{"name": "READS", "format": "fastq"}, {"name": "SAMPLE"}, {"name": "SEP"}],
                    step_outputs=lambda ti, run, s: [f"{run}/{s['SAMPLE']}.txt"])
    files = render_pipeline_files(pr.derive_pipeline_record(sp, name="probe"))
    assert "params.sep = '\\\\t'" in files["main.nf"].splitlines()
    assert yaml.safe_load(files["params.yaml"])["sep"] == "\\t"


# ── refusals ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label", ["{x},b", '"{x}"', " {x}", "{x} "])
def test_a_value_samples_csv_cannot_carry_unquoted_refuses(label):
    sp = probe_spec(["tool --label {LABEL} {READS} > {OUTPUT_DIR}/{SAMPLE}.txt"],
                    [(x, {"READS": f"/d/{x}.fq", "SAMPLE": x, "LABEL": label.format(x=x)}) for x in ("s1", "s2")],
                    [{"name": "OUTPUT_DIR", "files": ["*.txt"]}],
                    inputs=[{"name": "READS", "format": "fastq"}, {"name": "SAMPLE"}, {"name": "LABEL"}],
                    step_outputs=lambda ti, run, s: [f"{run}/{s['SAMPLE']}.txt"])
    _refusal("pipeline.sheet_cell_unwritable", spec=sp)


def test_a_samplesheet_column_that_is_not_an_identifier_refuses_at_render():
    sheet = sheet_text(_SAMPLES).replace("condition", "treatment-arm", 1)
    rec = pr.derive_pipeline_record(_counts_spec(), name="probe", samplesheet_files={SHEET: sheet},
                                    cohort=[_cohort(_COLLECT)])
    with pytest.raises(ValueError, match="samples.csv column treatment-arm"):
        render_pipeline_files(rec)


def test_resources_naming_no_stage_refuses():
    e = _refusal("pipeline.resources_unknown_stage", spec=_counts_spec(), resources={"HTSEQ": {"cpus": 2}})
    assert "HTSEQ_COUNT" in e.remedy


def test_resources_naming_a_stage_of_both_workflows_refuses():
    _refusal("pipeline.resources_ambiguous", spec=_counts_spec(), stage_names=["HTSEQ_COUNT", "MERGE_COUNTS"],
             resources={"MERGE_COUNTS": {"cpus": 2}}, samplesheet_files={SHEET: sheet_text(_SAMPLES)},
             cohort=[_cohort(_COLLECT)])


def test_collect_on_a_samplesheet_slot_refuses():
    _refusal("pipeline.samplesheet_kind", spec=_counts_spec(), samplesheet_files={SHEET: sheet_text(_SAMPLES)},
             cohort=[_cohort({**_COLLECT, "SAMPLESHEET": "{SAMPLE}.counts.tsv"})])


def test_collect_on_a_thread_slot_refuses():
    merge = probe_spec(["merge -p {THREADS} {COUNTS_DIR} > {OUTPUT_DIR}/m.tsv"],
                       [("c", {"THREADS": "2", "COUNTS_DIR": "/c"})], [{"name": "OUTPUT_DIR", "files": ["m.tsv"]}],
                       inputs=[{"name": "THREADS", "format": "threads"}, {"name": "COUNTS_DIR"}],
                       step_outputs=lambda ti, run, s: [f"{run}/m.tsv"])
    _refusal("pipeline.threads_kind", spec=_counts_spec(),
             cohort=[_cohort({**_COLLECT, "THREADS": "{SAMPLE}.counts.tsv"}, spec=merge)])
