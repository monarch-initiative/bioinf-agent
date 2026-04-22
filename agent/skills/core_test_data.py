"""
CoreTestData — download and register new sequencing data in core_test_data.

No sub-agent loop needed: the flow is deterministic.
  1. Download reads from EBI SRA to data/sources/{accession}/ (cache)
  2. Subset to N reads into core_test_data_{build}/short_read/{end_type}/{assay_type}/
  3. Write SampleMeta YAML sidecar (or merge subset into existing)
  4. Rebuild manifest via gen_manifest.py
"""

from __future__ import annotations

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


def _download(url: str, dest: Path) -> bool:
    r = subprocess.run(
        ["curl", "-fsSL", "--retry", "3", "-o", str(dest), url],
        capture_output=True, timeout=600,
    )
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def _subset(src: Path, dst: Path, num_reads: int) -> bool:
    lines = num_reads * 4
    subprocess.run(
        f"{{ gunzip -c {src} || true; }} | head -{lines} | gzip > {dst}",
        shell=True, capture_output=True,
    )
    return dst.exists() and dst.stat().st_size > 0


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
    Download, subset, and register a new sequencing dataset in core_test_data.
    Idempotent: skips any step whose output already exists.
    """
    if not sample:
        sample = accession

    subset_key = subset.upper()
    num_reads = _SUBSET_SIZES.get(subset_key, 100_000)

    project_root = Path(__file__).parent.parent.parent.resolve()
    data_dir = project_root / config["paths"]["data_dir"]

    core_dir   = data_dir / f"core_test_data_{genome_build}"
    reads_dir  = core_dir / "short_read" / end_type / assay_type
    sources_dir = data_dir / "sources" / accession

    reads_dir.mkdir(parents=True, exist_ok=True)
    sources_dir.mkdir(parents=True, exist_ok=True)

    sample_key = f"{sample}_{accession}"
    file_key   = f"{sample_key}_{subset_key}"
    urls       = _ebi_urls(accession)
    log: list[str] = []

    # ------------------------------------------------------------------
    # Download + subset
    # ------------------------------------------------------------------
    if end_type == "paired_end":
        full_r1   = sources_dir / f"{accession}_1.fastq.gz"
        full_r2   = sources_dir / f"{accession}_2.fastq.gz"
        subset_r1 = reads_dir / f"{file_key}_R1.fastq.gz"
        subset_r2 = reads_dir / f"{file_key}_R2.fastq.gz"

        for full, url, label in [(full_r1, urls["r1"], "R1"), (full_r2, urls["r2"], "R2")]:
            if not full.exists():
                log.append(f"Downloading {label} from EBI SRA...")
                if not _download(url, full):
                    return {"success": False, "error": f"Failed to download {label} from {url}"}

        for full, subset_file, label in [(full_r1, subset_r1, "R1"), (full_r2, subset_r2, "R2")]:
            if not subset_file.exists():
                log.append(f"Subsetting {label} to {subset_key}...")
                if not _subset(full, subset_file, num_reads):
                    return {"success": False, "error": f"Failed to subset {label}"}

        r1_rel = f"short_read/{end_type}/{assay_type}/{subset_r1.name}"
        r2_rel = f"short_read/{end_type}/{assay_type}/{subset_r2.name}"
        r1_out, r2_out = str(subset_r1), str(subset_r2)

    else:  # single_end
        full_r1   = sources_dir / f"{accession}_1.fastq.gz"
        subset_r1 = reads_dir / f"{file_key}_R1.fastq.gz"

        if not full_r1.exists():
            log.append("Downloading reads from EBI SRA...")
            ok = _download(urls["r1"], full_r1) or _download(urls["single"], full_r1)
            if not ok:
                return {"success": False, "error": f"Failed to download reads for {accession}"}

        if not subset_r1.exists():
            log.append(f"Subsetting to {subset_key}...")
            if not _subset(full_r1, subset_r1, num_reads):
                return {"success": False, "error": "Failed to subset reads"}

        r1_rel = f"short_read/{end_type}/{assay_type}/{subset_r1.name}"
        r2_rel = None
        r1_out, r2_out = str(subset_r1), None

    # ------------------------------------------------------------------
    # SampleMeta sidecar (create or merge)
    # ------------------------------------------------------------------
    meta_path = reads_dir / f"{sample_key}_sample_meta.yaml"
    subset_info = SubsetInfo(r1=r1_rel, r2=r2_rel, num_reads=num_reads, available=True)

    if meta_path.exists():
        existing = SampleMeta.from_yaml(meta_path)
        existing.subsets[subset_key] = subset_info
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

    # Check if genome exists for this build (informational warning only)
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
        "r1": r1_out,
        "r2": r2_out,
        "sample_meta": str(meta_path),
        "core_dir": str(core_dir),
        "log": log,
    }
    if not genome_fasta:
        result["genome_warning"] = (
            f"No genome FASTA found for {genome_build} at {genome_dir}. "
            f"Run scripts/setup_core_test_data.sh --genome-build {genome_build} --no-reads "
            f"to bootstrap the reference genome and indexes."
        )
    return result
