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


def _reads(sample: str) -> str:
    return f"/data/reads/{sample}_10K_R1.fastq.gz"


def _usage(stranded: str = "reverse") -> dict:
    return {
        "description": "Align RNA-seq reads with HISAT2 and count reads per gene with htseq-count",
        "command_template": list(TEMPLATES),
        "inputs": [
            {"name": "HISAT2_INDEX", "format": "hisat2_index", "description": "HISAT2 index prefix"},
            {"name": "READS", "format": "fastq", "description": "single-end reads"},
            {"name": "STRANDED", "format": "option", "description": "htseq-count -s: yes | no | reverse"},
            {"name": "GTF", "format": "gtf", "description": "gene annotation"},
            {"name": "SAMPLE", "format": "id", "description": "sample identifier"},
        ],
        "outputs": [{"name": "OUTPUT_DIR", "files": ["*.counts.tsv", "aligned.bam"]}],
        "trials": [{"name": s, "substitutions": _subs(s, stranded, f"/out/{s}")} for s in SAMPLES],
    }


def _subs(sample: str, stranded: str, outdir: str) -> dict:
    return {"HISAT2_INDEX": INDEX, "READS": _reads(sample), "STRANDED": stranded,
            "GTF": GTF, "SAMPLE": sample, "OUTPUT_DIR": outdir}


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
                       with_index_step: bool = True, proven: bool = True):
    """A validated `WorkflowSpec` for the align + count chain over `samples`."""
    from agent.models.core_data import WorkflowSpec
    samples = list(samples if samples is not None else SAMPLES)
    templates = list(templates if templates is not None else TEMPLATES)
    steps: list[dict] = []
    if with_index_step:
        steps.append(_step("hisat2-build", f"hisat2-build {GENOME} {INDEX}", [GENOME],
                           [f"{INDEX}.{i}.ht2" for i in range(1, 9)], wall=42.0, rss=900.0))
    trials = []
    for s in samples:
        run = f"/runs/{s}"
        subs = _subs(s, stranded, run)
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
        tsubs = _subs(s, stranded, scratch)
        trials.append({"name": s, "ok": True,
                       "commands_run": [_resolved(t, tsubs) for t in templates],
                       "substitutions": tsubs, "output_slots": ["OUTPUT_DIR"],
                       "produced_files": [f"{scratch}/aligned.bam", f"{scratch}/{s}.counts.tsv"],
                       "validation_results": []})
    for n, st in enumerate(steps, 1):
        st["step"] = n
    usage = _usage(stranded)
    usage["command_template"] = templates
    usage["trials"] = [{"name": s, "substitutions": _subs(s, stranded, f"/out/{s}")} for s in samples]
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
