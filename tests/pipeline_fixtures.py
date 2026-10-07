"""Sealed-workflow fixtures for the pipeline layer — built THROUGH the models, so a
fixture can never encode a shape no producer emits (the env_records.py rule).

`sealed_rnaseq_spec()` is the alignment + counts scenario: one CLI env, N sample rows
(the airway study), three how-to commands (align, index, count), an index-build step
that is provenance for a shared input, and a proven I4 transcript with one trial per
row. Every path is absolute and nothing is read from disk.
"""
from __future__ import annotations

from typing import Optional

IMAGE = "bioinf_rnaseq_cli:latest"
DIGEST = "sha256:aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"
REQUEST_KEY = "fr_rnaseq_cli_0001"
SAMPLES = ["SRR1039508", "SRR1039509", "SRR1039512"]
INDEX = "/data/idx/chr22"
GTF = "/data/annotation/chr22.gtf"
GENOME = "/data/genome/chr22.fa"

TEMPLATES = [
    "hisat2 -p 4 -x {HISAT2_INDEX} -U {READS} | samtools sort -o {OUTPUT_DIR}/aligned.bam",
    "samtools index {OUTPUT_DIR}/aligned.bam",
    "htseq-count -s {STRANDED} -f bam {OUTPUT_DIR}/aligned.bam {GTF} > {OUTPUT_DIR}/{SAMPLE}.counts.tsv",
]
#: The same how-to with the aligner's thread count declared as a slot (`format: threads`)
#: instead of written as a literal: the sealed run bound 4, so every step's command is
#: the same text either way.
TEMPLATES_THREADS = [TEMPLATES[0].replace("-p 4", "-p {THREADS}")] + TEMPLATES[1:]
THREADS = "4"


def _reads(sample: str) -> str:
    return f"/data/reads/{sample}_10K_R1.fastq.gz"


def _usage(stranded: str = "reverse", threads: bool = False) -> dict:
    inputs = [
        {"name": "HISAT2_INDEX", "format": "hisat2_index", "description": "HISAT2 index prefix"},
        {"name": "READS", "format": "fastq", "description": "single-end reads"},
        {"name": "STRANDED", "format": "option", "description": "htseq-count -s: yes | no | reverse"},
        {"name": "GTF", "format": "gtf", "description": "gene annotation"},
        {"name": "SAMPLE", "format": "id", "description": "sample identifier"},
    ]
    if threads:
        inputs.append({"name": "THREADS", "format": "threads", "description": "hisat2 -p: alignment threads"})
    return {
        "description": "Align RNA-seq reads with HISAT2 and count reads per gene with htseq-count",
        "command_template": list(TEMPLATES_THREADS if threads else TEMPLATES),
        "inputs": inputs,
        "outputs": [{"name": "OUTPUT_DIR", "files": ["*.counts.tsv", "aligned.bam"]}],
        "trials": [{"name": s, "substitutions": _subs(s, stranded, f"/out/{s}", threads)} for s in SAMPLES],
    }


def _subs(sample: str, stranded: str, outdir: str, threads: bool = False) -> dict:
    subs = {"HISAT2_INDEX": INDEX, "READS": _reads(sample), "STRANDED": stranded,
            "GTF": GTF, "SAMPLE": sample, "OUTPUT_DIR": outdir}
    if threads:
        subs["THREADS"] = THREADS
    return subs


def _resolved(template: str, subs: dict) -> str:
    out = template
    for k, v in subs.items():
        out = out.replace("{" + k + "}", v)
    return out


def _ru(wall: float, rss: float, cpu: float = 100.0) -> dict:
    return {"wall_seconds": wall, "peak_rss_mb": rss, "max_cpu_percent": cpu,
            "locus": "native", "i7_authoritative": True}


def _step(tool: str, command: str, inputs: list[str], outputs: list[str], *, wall: float, rss: float) -> dict:
    from agent.models.core_data import PipelineStep
    return PipelineStep.produce(
        tool=tool, command=command, returncode=0,
        inputs=[{"path": p, "references": []} for p in inputs],
        detected_outputs=outputs,
        validation={p: {"passed": True} for p in outputs},
        resource_usage=_ru(wall, rss),
        ran_in_container=True, container_image=IMAGE, container_image_digest=DIGEST)


def sealed_rnaseq_spec(samples: Optional[list[str]] = None, *, stranded: str = "reverse",
                       templates: Optional[list[str]] = None,
                       with_index_step: bool = True, proven: bool = True, threads: bool = False):
    """A validated `WorkflowSpec` for the align + count chain over `samples`. With
    `threads=True` the aligner's thread count is a declared slot (`TEMPLATES_THREADS`)."""
    from agent.models.core_data import WorkflowSpec
    samples = list(samples if samples is not None else SAMPLES)
    templates = list(templates if templates is not None else (TEMPLATES_THREADS if threads else TEMPLATES))
    steps: list[dict] = []
    if with_index_step:
        steps.append(_step("hisat2-build", f"hisat2-build {GENOME} {INDEX}", [GENOME],
                           [f"{INDEX}.{i}.ht2" for i in range(1, 9)], wall=42.0, rss=900.0))
    trials = []
    for s in samples:
        run = f"/runs/{s}"
        subs = _subs(s, stranded, run, threads)
        cmds = [_resolved(t, subs) for t in templates]
        steps.append(_step("hisat2", cmds[0], [f"{INDEX}.1.ht2", _reads(s)], [f"{run}/aligned.bam"],
                           wall=61.0 + len(steps), rss=1500.0 + 10 * len(steps)))
        if len(templates) > 1:
            steps.append(_step("samtools", cmds[1], [f"{run}/aligned.bam"], [f"{run}/aligned.bam.bai"],
                               wall=2.0, rss=40.0))
        if len(templates) > 2:
            steps.append(_step("htseq-count", cmds[2], [f"{run}/aligned.bam", GTF],
                               [f"{run}/{s}.counts.tsv"], wall=30.0, rss=300.0))
        scratch = f"/scratch/i4/{s}"
        tsubs = _subs(s, stranded, scratch, threads)
        trials.append({"name": s, "ok": True,
                       "commands_run": [_resolved(t, tsubs) for t in templates],
                       "substitutions": tsubs, "output_slots": ["OUTPUT_DIR"],
                       "produced_files": [f"{scratch}/aligned.bam", f"{scratch}/{s}.counts.tsv"],
                       "validation_results": []})
    for n, st in enumerate(steps, 1):
        st["step"] = n
    usage = _usage(stranded, threads)
    usage["command_template"] = templates
    usage["trials"] = [{"name": s, "substitutions": _subs(s, stranded, f"/out/{s}", threads)} for s in samples]
    spec = {
        "workflow_name": "rnaseq_counts_workflow",
        "description": "HISAT2 alignment + htseq-count over the airway samples",
        "created_at": "2026-09-29T00:00:00+00:00",
        "env_request_key": REQUEST_KEY,
        "env_content_digest": "sha256:content0000",
        "env_image": f"bioinf_rnaseq_cli@{DIGEST}",
        "pipeline_status": "fully_validated",
        "usage_verified": True,
        "usage_verification": ({"status": "verified", "reason": "", "locus": "native",
                                "trial_count": len(trials), "passed": len(trials), "trials": trials}
                               if proven else {"status": "not_attempted", "reason": "fixture", "trials": []}),
        "validated_in_shipped_image": True,
        "usage": usage,
        "pipeline_steps": steps,
        "test_data": {"genome_build": "hg38", "assay_type": "rnaseq", "end_type": "single_end",
                      "r1": _reads(samples[0]), "reference_fasta": GENOME},
        "envs": [{"request_key": REQUEST_KEY, "image": f"bioinf_rnaseq_cli@{DIGEST}", "image_digest": DIGEST}],
    }
    return WorkflowSpec.model_validate(spec)


# ── the cohort workflow: counts tables → one matrix → DESeq2 ────────────────

IMAGE_DE = "bioinf_rnaseq_de:latest"
DIGEST_DE = "sha256:dddd4444dddd4444dddd4444dddd4444dddd4444dddd4444dddd4444dddd4444"
REQUEST_KEY_DE = "fr_rnaseq_de_0001"
COUNTS_DIR = "/data/de/counts"
SHEET = "/data/de/samples.csv"
MERGE_SCRIPT = "/data/de/scripts/merge_counts.R"
DESEQ2_SCRIPT = "/data/de/scripts/deseq2.R"
MERGE_TEXT = "#!/usr/bin/env Rscript\n# merge every *.counts.tsv in a directory into one matrix\nargs <- commandArgs(TRUE)\n"
DESEQ2_TEXT = "#!/usr/bin/env Rscript\n# DESeq2 over a counts matrix, design from the samplesheet\nargs <- commandArgs(TRUE)\n"
DESIGN = {"SRR1039508": ("N61311", "untrt"), "SRR1039509": ("N61311", "dex"), "SRR1039512": ("N052611", "untrt"),
          "SRR1039513": ("N052611", "dex")}
TEMPLATES_DE = [
    "Rscript {MERGE_SCRIPT} {COUNTS_DIR} {OUTPUT_DIR}/counts_matrix.tsv",
    'Rscript {DESEQ2_SCRIPT} {OUTPUT_DIR}/counts_matrix.tsv {SAMPLESHEET} --design "{DESIGN}" {OUTPUT_DIR}',
]
DE_OUTPUTS = ["deseq2_results.tsv", "normalized_counts.tsv", "ma_plot.png", "pca.png", "deseq2_session.txt"]


def sheet_text(samples: Optional[list[str]] = None) -> str:
    """The cohort's samplesheet as the sealed run read it: `sample` plus the design columns."""
    rows = [f"{s},{DESIGN[s][0]},{DESIGN[s][1]}" for s in (samples if samples is not None else list(DESIGN))]
    return "sample,donor,condition\n" + "\n".join(rows) + "\n"


def _authored(path: str, role: str, content: Optional[str], language: str) -> dict:
    import hashlib
    raw = (content or "x").encode()
    a = {"path": path, "role": role, "description": f"{role} at {path}", "sha256": hashlib.sha256(raw).hexdigest(),
         "size_bytes": len(raw), "created_at": "2026-10-06T00:00:00+00:00", "language": language}
    if content is not None:
        a["content_excerpt"] = content
    else:
        a["generated_by"] = "nextflow run main.nf -profile local -params-file params.yaml"
    return a


def sealed_deseq2_spec(samples: Optional[list[str]] = None, *, design: str = "condition",
                       templates: Optional[list[str]] = None, sheet: Optional[str] = None,
                       trials: Optional[list[dict]] = None, script_text: Optional[str] = MERGE_TEXT):
    """A validated `WorkflowSpec` for the cohort chain: one R env, two how-to commands
    (merge the per-sample counts tables, DESeq2 over the matrix), the scripts and the
    samplesheet as authored artifacts, the counts tables as staged inputs, one trial
    over the whole cohort. `script_text=None` leaves the merge script's text out of the
    record (a large artifact the seal only excerpted)."""
    from agent.models.core_data import WorkflowSpec
    samples = list(samples if samples is not None else DESIGN)
    templates = list(templates if templates is not None else TEMPLATES_DE)
    sheet = sheet if sheet is not None else SHEET
    counts = [f"{COUNTS_DIR}/{s}.counts.tsv" for s in samples]
    run = "/runs/de"
    subs = {"MERGE_SCRIPT": MERGE_SCRIPT, "COUNTS_DIR": COUNTS_DIR, "DESEQ2_SCRIPT": DESEQ2_SCRIPT,
            "SAMPLESHEET": sheet, "DESIGN": design, "OUTPUT_DIR": run}
    cmds = [_resolved(t, subs) for t in templates]
    steps = [_step("Rscript", cmds[0], [MERGE_SCRIPT, *counts], [f"{run}/counts_matrix.tsv"], wall=3.0, rss=120.0)]
    if len(templates) > 1:
        steps.append(_step("Rscript", cmds[1], [DESEQ2_SCRIPT, f"{run}/counts_matrix.tsv", sheet],
                           [f"{run}/{o}" for o in DE_OUTPUTS], wall=25.0, rss=640.0))
    for st in steps:
        st["container_image"], st["container_image_digest"] = IMAGE_DE, DIGEST_DE
    for n, st in enumerate(steps, 1):
        st["step"] = n
    scratch = "/scratch/i4/de"
    tsubs = {**subs, "OUTPUT_DIR": scratch}
    proven = [{"name": "airway_2x2", "ok": True, "commands_run": [_resolved(t, tsubs) for t in templates],
               "substitutions": tsubs, "output_slots": ["OUTPUT_DIR"],
               "produced_files": [f"{scratch}/counts_matrix.tsv"] + [f"{scratch}/{o}" for o in DE_OUTPUTS],
               "validation_results": []}]
    declared = trials if trials is not None else [{"name": "airway_2x2", "substitutions": subs}]
    usage = {
        "description": "Merge every sample's htseq-count table into one matrix, then DESeq2 over it",
        "command_template": templates,
        "inputs": [
            {"name": "MERGE_SCRIPT", "format": "r_script", "description": "merge_counts.R"},
            {"name": "COUNTS_DIR", "format": "directory", "description": "every sample's <sample>.counts.tsv"},
            {"name": "DESEQ2_SCRIPT", "format": "r_script", "description": "deseq2.R"},
            {"name": "SAMPLESHEET", "format": "samplesheet", "description": "sample + one column per design term"},
            {"name": "DESIGN", "format": "value", "description": "the design's terms: condition, or donor + condition"},
        ],
        "outputs": [{"name": "OUTPUT_DIR", "files": ["counts_matrix.tsv", *DE_OUTPUTS]}],
        "trials": declared,
    }
    spec = {
        "workflow_name": "rnaseq_de_workflow",
        "description": "DESeq2 over the airway cohort",
        "created_at": "2026-10-06T00:00:00+00:00",
        "env_request_key": REQUEST_KEY_DE,
        "env_content_digest": "sha256:content_de00",
        "env_image": f"bioinf_rnaseq_de@{DIGEST_DE}",
        "pipeline_status": "fully_validated",
        "usage_verified": True,
        "usage_verification": {"status": "verified", "reason": "", "locus": "image", "trial_count": 1,
                               "passed": 1, "trials": proven},
        "validated_in_shipped_image": True,
        "usage": usage,
        "pipeline_steps": steps,
        "authored_artifacts": [
            _authored(MERGE_SCRIPT, "driver_script", script_text, "r"),
            _authored(DESEQ2_SCRIPT, "driver_script", DESEQ2_TEXT, "r"),
            _authored(sheet, "samplesheet", sheet_text(samples), "csv"),
            *[_authored(c, "staged_input", None, "tsv") for c in counts],
        ],
        "envs": [{"request_key": REQUEST_KEY_DE, "image": f"bioinf_rnaseq_de@{DIGEST_DE}", "image_digest": DIGEST_DE}],
    }
    return WorkflowSpec.model_validate(spec)
