"""
CoreTestData — download and register new sequencing data in core_test_data.

No sub-agent loop needed: the flow is deterministic.
  1. Stream-download + subset reads directly from EBI SRA (no intermediate cache)
  2. Measure read length from the subset FASTQ
  3. Write SampleMeta YAML sidecar (or merge subset into existing)
  4. Rebuild manifest via gen_manifest.py
"""

from __future__ import annotations

import gzip
import subprocess
from pathlib import Path
from typing import Any

from agent.models.core_data import SampleMeta, SubsetInfo


_SUBSET_SIZES: dict[str, int] = {
    "10K":  10_000,
    "50K":  50_000,
    "100K": 100_000,
    "500K": 500_000,
    "1M":   1_000_000,
}


def _ebi_urls(accession: str) -> dict[str, str]:
    prefix = accession[:6]
    sub = f"00{accession[-1]}"
    base = f"https://ftp.sra.ebi.ac.uk/vol1/fastq/{prefix}/{sub}/{accession}"
    return {
        "r1":     f"{base}/{accession}_1.fastq.gz",
        "r2":     f"{base}/{accession}_2.fastq.gz",
        "single": f"{base}/{accession}.fastq.gz",
    }


def _stream_subset(url: str, dst: Path, num_reads: int) -> bool:
    """Stream URL → gunzip → head → gzip → dst. No intermediate file on disk."""
    lines = num_reads * 4
    tmp = dst.with_suffix(".tmp.gz")
    cmd = (
        f"(set +o pipefail; curl -fsSL --retry 3 '{url}' | gunzip | head -{lines}) "
        f"| gzip > {tmp}"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, executable="/bin/bash")
    if result.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        tmp.rename(dst)
        return True
    tmp.unlink(missing_ok=True)
    return False


def _measure_read_length(fastq_gz: Path) -> int | None:
    try:
        with gzip.open(fastq_gz, "rt") as f:
            f.readline()  # @header
            seq = f.readline().strip()
            return len(seq) if seq else None
    except Exception:
        return None


def add_core_test_data(
    config: dict,
    accession: str,
    assay_type: str,
    end_type: str = "paired_end",
    genome_build: str = "hg38",
    sample: str = "",
    subset: str = "100K",
) -> dict[str, Any]:
    """
    Stream-download, subset, and register a new sequencing dataset.
    Idempotent: skips any step whose output already exists.
    """
    if not sample:
        sample = accession

    subset_key = subset.upper()
    num_reads = _SUBSET_SIZES.get(subset_key, 100_000)

    project_root = Path(__file__).parent.parent.parent.resolve()
    data_dir = project_root / config["paths"]["data_dir"]

    core_dir  = data_dir / f"core_test_data_{genome_build}"
    reads_dir = core_dir / "short_read" / end_type / assay_type
    reads_dir.mkdir(parents=True, exist_ok=True)

    sample_key = f"{sample}_{accession}"
    file_key   = f"{sample_key}_{subset_key}"
    urls       = _ebi_urls(accession)
    log: list[str] = []

    # ------------------------------------------------------------------
    # Stream-download directly to subset files
    # ------------------------------------------------------------------
    if end_type == "paired_end":
        subset_r1 = reads_dir / f"{file_key}_R1.fastq.gz"
        subset_r2 = reads_dir / f"{file_key}_R2.fastq.gz"

        for dst, url, label in [(subset_r1, urls["r1"], "R1"), (subset_r2, urls["r2"], "R2")]:
            if not dst.exists():
                log.append(f"Streaming {label} ({subset_key} reads) from EBI SRA...")
                if not _stream_subset(url, dst, num_reads):
                    return {"success": False, "error": f"Failed to download/subset {label} from {url}"}

        read_length = _measure_read_length(subset_r1)
        r1_rel = f"short_read/{end_type}/{assay_type}/{subset_r1.name}"
        r2_rel = f"short_read/{end_type}/{assay_type}/{subset_r2.name}"
        r1_out, r2_out = str(subset_r1), str(subset_r2)

    else:  # single_end
        subset_r1 = reads_dir / f"{file_key}_R1.fastq.gz"

        if not subset_r1.exists():
            log.append(f"Streaming reads ({subset_key}) from EBI SRA...")
            ok = _stream_subset(urls["r1"], subset_r1, num_reads) or \
                 _stream_subset(urls["single"], subset_r1, num_reads)
            if not ok:
                return {"success": False, "error": f"Failed to download/subset reads for {accession}"}

        read_length = _measure_read_length(subset_r1)
        r1_rel = f"short_read/{end_type}/{assay_type}/{subset_r1.name}"
        r2_rel = None
        r1_out, r2_out = str(subset_r1), None

    if read_length:
        log.append(f"Measured read length: {read_length}bp")

    # ------------------------------------------------------------------
    # SampleMeta sidecar (create or merge)
    # ------------------------------------------------------------------
    subset_info = SubsetInfo(r1=r1_rel, r2=r2_rel, num_reads=num_reads, available=True)
    meta_path = reads_dir / f"{sample_key}_sample_meta.yaml"

    if meta_path.exists():
        existing = SampleMeta.from_yaml(meta_path)
        existing.subsets[subset_key] = subset_info
        if read_length is not None:
            existing.read_length = read_length
        existing.write(meta_path)
        log.append(f"SampleMeta updated: {meta_path.name}")
    else:
        SampleMeta(
            sample=sample,
            accession=accession,
            read_type="short_read",
            end_type=end_type,
            assay_type=assay_type,
            database="EBI_SRA",
            read_length=read_length,
            source_urls={"r1": urls["r1"], **({"r2": urls["r2"]} if end_type == "paired_end" else {})},
            subsets={subset_key: subset_info},
        ).write(meta_path)
        log.append(f"SampleMeta written: {meta_path.name}")

    # ------------------------------------------------------------------
    # Rebuild manifest
    # ------------------------------------------------------------------
    gen_manifest = project_root / "scripts" / "gen_manifest.py"
    ret = subprocess.run(
        ["python3", str(gen_manifest), "--core-dir", str(core_dir)],
        capture_output=True, text=True,
    )
    if ret.returncode != 0:
        log.append(f"WARNING: gen_manifest failed: {ret.stderr[:300]}")
    else:
        log.append("Manifest rebuilt.")

    genome_dir = core_dir / "genome"
    genome_fasta = next(genome_dir.glob("*.fa"), None) if genome_dir.exists() else None

    result: dict[str, Any] = {
        "success": True,
        "accession": accession,
        "sample": sample,
        "sample_key": sample_key,
        "file_key": file_key,
        "genome_build": genome_build,
        "assay_type": assay_type,
        "end_type": end_type,
        "subset": subset_key,
        "num_reads": num_reads,
        "read_length": read_length,
        "r1": r1_out,
        "r2": r2_out,
        "sample_meta": str(meta_path),
        "core_dir": str(core_dir),
        "log": log,
    }
    if not genome_fasta:
        result["genome_warning"] = (
            f"No genome FASTA found for {genome_build} at {genome_dir}. "
            f"Run scripts/setup_core_test_data.sh --genome-build {genome_build} first."
        )
    return result
