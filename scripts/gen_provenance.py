#!/usr/bin/env python3
"""
Generate a validated provenance YAML for one bioinformatics pipeline run.

Called by setup_core_test_data.sh and usable standalone.
Tool versions are discovered live by querying the conda environment binaries.

Usage (bwa_samtools):
  python scripts/gen_provenance.py \
    --pipeline bwa_samtools \
    --conda-env /abs/path/to/envs/bioinf_bwa_samtools \
    --pipeline-spec ../../../../config/pipelines/bwa_samtools.yaml \
    --genome-build hg38 --chromosome chr22 \
    --reference ../../genome/chr22.fa \
    --reference-fai ../../genome/chr22.fa.fai \
    --sample HG00096 --accession SRR1517830 \
    --subset 100K --num-reads 100000 \
    --assay-type exome --end-type paired_end --database EBI_SRA \
    --r1 ../../short_read/paired_end/exome/SRR1517830_R1_100K.fastq.gz \
    --r2 ../../short_read/paired_end/exome/SRR1517830_R2_100K.fastq.gz \
    --outputs 'HG00096_SRR1517830_aligned.sam:sam,HG00096_SRR1517830_aligned_sorted.bam:bam:indexed,HG00096_SRR1517830_aligned_sorted.bam.bai:bai' \
    --out /abs/path/to/pipeline_outputs/bwa_samtools/HG00096_SRR1517830_provenance.yaml

Usage (freebayes):
  python scripts/gen_provenance.py \
    --pipeline freebayes \
    --conda-env /abs/path/to/envs/bioinf_freebayes \
    --pipeline-spec ../../../../config/pipelines/freebayes_1.3.10.yaml \
    --genome-build hg38 --chromosome chr22 \
    --reference ../../genome/chr22.fa \
    --reference-fai ../../genome/chr22.fa.fai \
    --bam ../bwa_samtools/HG00096_SRR1517830_aligned_sorted.bam \
    --bai ../bwa_samtools/HG00096_SRR1517830_aligned_sorted.bam.bai \
    --upstream-pipelines bwa_samtools \
    --parameters '--min-mapping-quality:20,--min-base-quality:20,--min-alternate-fraction:0.2,--min-alternate-count:2' \
    --outputs 'HG00096_SRR1517830_variants.vcf:vcf' \
    --out /abs/path/to/pipeline_outputs/freebayes/HG00096_SRR1517830_provenance.yaml
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.models.core_data import (
    BamInput,
    GenomeRef,
    OutputFile,
    Provenance,
    ReadInput,
)

# ---------------------------------------------------------------------------
# Tool version discovery
# ---------------------------------------------------------------------------

_PIPELINE_TOOLS: dict[str, list[str]] = {
    "bwa_samtools": ["bwa", "samtools"],
    "freebayes":    ["freebayes"],
    "star":         ["STAR"],
    "fastqc":       ["fastqc"],
    "bcftools":     ["bcftools"],
    "minimap2":     ["minimap2"],
}


def _discover_version(conda_env: str, tool: str) -> str:
    bin_path = Path(conda_env) / "bin" / tool
    if not bin_path.exists():
        return "unknown"
    try:
        if tool == "bwa":
            r = subprocess.run([str(bin_path)], capture_output=True, text=True)
            for line in (r.stdout + r.stderr).splitlines():
                if line.startswith("Version:"):
                    return line.split(None, 1)[-1].strip()
        elif tool == "samtools":
            r = subprocess.run([str(bin_path), "--version"], capture_output=True, text=True)
            if r.stdout:
                return r.stdout.splitlines()[0].split()[-1]
        elif tool == "freebayes":
            r = subprocess.run([str(bin_path), "--version"], capture_output=True, text=True)
            for line in (r.stdout + r.stderr).splitlines():
                if "version:" in line.lower():
                    return line.split()[-1].lstrip("v")
        elif tool in ("STAR", "fastqc", "bcftools", "minimap2"):
            r = subprocess.run([str(bin_path), "--version"], capture_output=True, text=True)
            if r.stdout:
                return r.stdout.splitlines()[0].split()[-1]
    except Exception:
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------


def _parse_outputs(s: str) -> list[OutputFile]:
    """Parse 'file:type[:indexed],...' into OutputFile list."""
    result = []
    for item in s.split(","):
        parts = [p.strip() for p in item.split(":")]
        if len(parts) >= 2:
            result.append(OutputFile(
                file=parts[0],
                type=parts[1],
                indexed="indexed" in parts[2:],
            ))
    return result


def _parse_parameters(s: str) -> dict | None:
    """Parse '--flag:value,...' into a dict, coercing numeric strings."""
    if not s:
        return None
    out: dict = {}
    for item in s.split(","):
        if ":" not in item:
            continue
        k, v = item.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.lstrip("-").isdigit():
            out[k] = int(v)
        else:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out or None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        description="Generate a validated provenance YAML for a bioinformatics pipeline run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Identity
    p.add_argument("--pipeline",      required=True, help="Pipeline name, e.g. bwa_samtools")
    p.add_argument("--conda-env",     required=True, help="Absolute path to conda environment")
    p.add_argument("--pipeline-spec", required=True, help="Relative path to pipeline spec YAML from provenance file location")

    # Reference genome (optional — omit for reference-free tools e.g. Exomiser, phenotype scorers)
    p.add_argument("--genome-build",   default="")
    p.add_argument("--chromosome",     default="")
    p.add_argument("--reference",      default="", help="Relative path to reference FASTA")
    p.add_argument("--reference-fai",  default="", help="Relative path to .fai index")

    # Read inputs (alignment pipelines)
    p.add_argument("--sample")
    p.add_argument("--accession")
    p.add_argument("--subset",     help="Subset label, e.g. 100K")
    p.add_argument("--num-reads",  type=int)
    p.add_argument("--r1",         help="Relative path to R1 FASTQ")
    p.add_argument("--r2",         help="Relative path to R2 FASTQ (omit for single-end)")
    p.add_argument("--read-type",  default="short_read")
    p.add_argument("--end-type",   default="paired_end")
    p.add_argument("--assay-type", default="exome")
    p.add_argument("--database",   default="EBI_SRA")

    # BAM inputs (variant-calling pipelines)
    p.add_argument("--bam", help="Relative path to input BAM")
    p.add_argument("--bai", help="Relative path to input BAI")

    # Common
    p.add_argument("--upstream-pipelines", default="",
                   help="Comma-separated upstream pipeline names")
    p.add_argument("--parameters", default="",
                   help="'--flag:value,...' pairs, e.g. '--min-mapping-quality:20'")
    p.add_argument("--outputs", required=True,
                   help="'file:type[:indexed],...' pairs describing outputs")
    p.add_argument("--out", required=True,
                   help="Absolute path for the output provenance YAML")

    args = p.parse_args()

    # Discover tool versions live from the conda env
    tools = _PIPELINE_TOOLS.get(args.pipeline, [])
    tool_versions = {t: _discover_version(args.conda_env, t) for t in tools}

    # Build sub-models
    genome = None
    if args.genome_build and args.reference:
        genome = GenomeRef(
            genome_build=args.genome_build,
            chromosome_subset=args.chromosome,
            reference=args.reference,
            reference_fai=args.reference_fai,
        )

    reads = None
    if args.r1:
        reads = [ReadInput(
            read_type=args.read_type,
            end_type=args.end_type,
            assay_type=args.assay_type,
            subset=args.subset or "",
            num_reads=args.num_reads or 0,
            r1=args.r1,
            r2=args.r2,
            sample=args.sample or "",
            accession=args.accession or "",
            database=args.database,
        )]

    bam_input = None
    if args.bam:
        bam_input = BamInput(
            bam=args.bam,
            bai=args.bai or args.bam + ".bai",
        )

    upstream = [x for x in args.upstream_pipelines.split(",") if x]
    parameters = _parse_parameters(args.parameters)
    outputs = _parse_outputs(args.outputs)

    prov = Provenance(
        pipeline=args.pipeline,
        pipeline_spec=args.pipeline_spec,
        conda_env=Path(args.conda_env).name,
        created_at=str(date.today()),
        tool_versions=tool_versions,
        genome=genome,
        reads=reads,
        bam_input=bam_input,
        upstream_pipelines=upstream,
        parameters=parameters,
        outputs=outputs,
    )

    out_path = prov.write(args.out)
    print(f"[gen_provenance] Written: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
