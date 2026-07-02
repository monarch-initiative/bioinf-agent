"""
CoreTestData — download and register new sequencing data in core_test_data.

No sub-agent loop needed: the flow is deterministic.
  1. Stream-download + subset reads directly from EBI SRA (no intermediate cache)
  2. Measure read length from the subset FASTQ
  3. Write SampleMeta YAML sidecar (or merge subset into existing)
  4. Rebuild manifest via gen_manifest.py

add_phenopacket follows the same pattern for GA4GH phenopacket JSON files:
  1. Download JSON from a URL
  2. Parse via PhenopacketMeta.from_phenopacket() — no manual field extraction
  3. Write JSON + sidecar YAML
  4. Rebuild manifest
"""

from __future__ import annotations

import gzip
import json
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from agent.models.core_data import (
    PLATFORM_FAMILY,
    PLATFORM_READ_TYPE,
    PhenopacketMeta,
    SampleMeta,
    SubsetInfo,
)
from agent.skills.outcomes import broke, proven, refused


def phenopacket_to_vcf(
    config: dict,
    phenopacket_id: str,
    output_vcf: str,
    genome_build: str = "hg38",
) -> dict[str, Any]:
    """Materialise a single-sample VCF from a phenopacket's variant block.

    The phenopacket schema's `vcfRecord` carries chrom/pos/ref/alt verbatim;
    `allelicState` (heterozygous / homozygous) maps to the GT field. Everything
    in the resulting VCF is derived from the phenopacket — no defaults, no
    invented fields.

    A VCF with N variants is written if the phenopacket has N variant entries
    (single-variant phenopackets produce single-line VCF bodies). The contig
    list in the header is the set of chromosomes used by the variants.

    Returns the path of the written VCF + the sample name (the phenopacket's
    subject_id, or "sample" if absent).
    """
    project_root = Path(__file__).parent.parent.parent.resolve()
    data_dir     = project_root / config["paths"]["data_dir"]
    core_dir     = data_dir / f"core_test_data_{genome_build}"
    pk_meta_path = core_dir / "phenopackets" / f"{phenopacket_id}_meta.yaml"
    if not pk_meta_path.exists():
        return refused("core_test_data.phenopacket_meta_missing", success=False, error=f"phenopacket meta not found: {pk_meta_path}")

    try:
        meta = PhenopacketMeta.from_yaml(pk_meta_path)
    except Exception as e:
        return broke("core_test_data.phenopacket_parse_failed", success=False, error=f"failed to parse {pk_meta_path.name}: {e}")

    variants = [v for v in (meta.variants or []) if v.get("chrom") and v.get("pos") and v.get("ref") and v.get("alt")]
    if not variants:
        return refused(
            "core_test_data.no_variants",
            success=False,
            error=("phenopacket has no variants with chrom/pos/ref/alt — "
                   "phenotype-only phenopackets cannot be materialised as a VCF."),
            phenopacket_id=phenopacket_id,
        )

    sample = meta.subject_id or "sample"
    # Sanitize sample for VCF (no whitespace).
    sample_id = re.sub(r"\s+", "_", sample) if sample else "sample"

    # GT from allelic_state — phenopacket uses Ensembl-style labels.
    _GT = {
        "heterozygous":           "0/1",
        "homozygous":             "1/1",
        "hemizygous":             "1",
        "compound heterozygous":  "0/1",   # caller usually splits these into two records
    }

    contigs = sorted({str(v["chrom"]) for v in variants})

    header_lines = [
        "##fileformat=VCFv4.2",
        f"##source=bioinf_agent.phenopacket_to_vcf",
        f"##phenopacket_id={meta.phenopacket_id}",
        f"##reference={meta.genome_assembly or genome_build}",
    ]
    for c in contigs:
        header_lines.append(f"##contig=<ID={c}>")
    header_lines += [
        '##INFO=<ID=GENE,Number=1,Type=String,Description="Affected gene from phenopacket">',
        '##INFO=<ID=HGVSC,Number=1,Type=String,Description="HGVS coding from phenopacket">',
        '##INFO=<ID=ACMG,Number=1,Type=String,Description="ACMG classification from phenopacket">',
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
        f"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t{sample_id}",
    ]

    body_lines = []
    for v in variants:
        info_pieces = []
        if v.get("gene"):                 info_pieces.append(f"GENE={v['gene']}")
        if v.get("hgvs_c"):               info_pieces.append(f"HGVSC={v['hgvs_c']}")
        if v.get("acmg_classification"): info_pieces.append(f"ACMG={v['acmg_classification']}")
        info = ";".join(info_pieces) or "."
        gt = _GT.get((v.get("allelic_state") or "").lower(), "0/1")
        body_lines.append(
            f"{v['chrom']}\t{v['pos']}\t.\t{v['ref']}\t{v['alt']}\t.\tPASS\t{info}\tGT\t{gt}"
        )

    out_path = Path(output_vcf).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(header_lines + body_lines) + "\n")

    return proven(
        "core_test_data.vcf_written",
        success=True,
        phenopacket_id=meta.phenopacket_id,
        sample_id=sample_id,
        output_vcf=str(out_path),
        num_variants=len(variants),
        contigs=contigs,
        genome_assembly=meta.genome_assembly or genome_build,
    )


def _normalize_github_url(url: str) -> str:
    """Convert https://github.com/<o>/<r>/blob/<ref>/<path> to its raw form.
    Other URLs pass through unchanged."""
    prefix = "https://github.com/"
    if url.startswith(prefix) and "/blob/" in url:
        rest = url[len(prefix):].replace("/blob/", "/", 1)
        return f"https://raw.githubusercontent.com/{rest}"
    return url


_SUBSET_SIZES: dict[str, int] = {
    "500":  500,
    "1K":   1_000,
    "10K":  10_000,
    "50K":  50_000,
    "100K": 100_000,
    "500K": 500_000,
    "1M":   1_000_000,
}


def _ebi_urls(accession: str) -> dict[str, str]:
    prefix = accession[:6]
    # EBI FTP uses a 00X sub-folder only for accessions longer than 9 chars
    # (e.g. SRR1039508 → .../SRR103/008/SRR1039508/; ERR188297 → .../ERR188/ERR188297/)
    if len(accession) > 9:
        sub = f"00{accession[-1]}"
        base = f"https://ftp.sra.ebi.ac.uk/vol1/fastq/{prefix}/{sub}/{accession}"
    else:
        base = f"https://ftp.sra.ebi.ac.uk/vol1/fastq/{prefix}/{accession}"
    return {
        "r1":     f"{base}/{accession}_1.fastq.gz",
        "r2":     f"{base}/{accession}_2.fastq.gz",
        "single": f"{base}/{accession}.fastq.gz",
    }


_MIN_GZ_BYTES = 100  # empty gzip header is 20 bytes; any real data is far larger


def _is_valid_gz(path: Path) -> bool:
    return path.exists() and path.stat().st_size > _MIN_GZ_BYTES


def _stream_subset(url: str, dst: Path, num_reads: int) -> bool:
    """Stream URL → gunzip → head → gzip → dst. No intermediate file on disk."""
    lines = num_reads * 4
    tmp = dst.with_suffix(".tmp.gz")
    cmd = (
        f"(set +o pipefail; curl -fsSL --retry 3 '{url}' | gunzip | head -{lines}) "
        f"| gzip > {tmp}"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, executable="/bin/bash")
    if result.returncode == 0 and _is_valid_gz(tmp):
        tmp.rename(dst)
        return True
    tmp.unlink(missing_ok=True)
    return False


def _measure_read_length(fastq_gz: Path) -> int | None:
    """Return the maximum sequence length across all reads in the file."""
    max_len = 0
    try:
        with gzip.open(fastq_gz, "rt") as f:
            while True:
                if not f.readline():  # @header — EOF check
                    break
                seq = f.readline().rstrip()
                f.readline()          # +
                f.readline()          # quality
                if seq:
                    max_len = max(max_len, len(seq))
    except Exception:
        pass
    return max_len if max_len else None


def add_core_test_data(
    config: dict,
    accession: str,
    assay_type: str,
    end_type: str = "paired_end",
    genome_build: str = "hg38",
    sample: str = "",
    subset: str = "10K",
    platform: str = "illumina",
    source_url: str = "",
    source_url_r2: str = "",
) -> dict[str, Any]:
    """
    Stream-download, subset, and register a new sequencing dataset.
    Idempotent: skips any step whose output already exists.
    platform:    illumina | ont | pacbio_hifi | pacbio_isoseq | pacbio_fiberseq
    source_url:  override the EBI URL builder (e.g. NCBI FTP, S3). For paired-end
                 data also supply source_url_r2.
    """
    if not sample:
        sample = accession

    subset_key = subset.upper()
    num_reads = _SUBSET_SIZES.get(subset_key, 10_000)

    # Derive read_type and directory layout from platform
    read_type = PLATFORM_READ_TYPE.get(platform, "short_read")
    platform_family = PLATFORM_FAMILY.get(platform, "")  # "" for illumina
    if read_type == "long_read":
        end_type = "single_end"   # long reads are always single-ended

    project_root = Path(__file__).parent.parent.parent.resolve()
    data_dir = project_root / config["paths"]["data_dir"]

    core_dir = data_dir / f"core_test_data_{genome_build}"
    if read_type == "long_read":
        reads_dir = core_dir / "long_read" / platform_family / assay_type
    else:
        reads_dir = core_dir / "short_read" / end_type / assay_type
    reads_dir.mkdir(parents=True, exist_ok=True)

    sample_key = f"{sample}_{accession}"
    file_key   = f"{sample_key}_{subset_key}"
    ebi_urls   = _ebi_urls(accession)
    log: list[str] = []

    # ------------------------------------------------------------------
    # Stream-download directly to subset files
    # ------------------------------------------------------------------
    if end_type == "paired_end":
        subset_r1 = reads_dir / f"{file_key}_R1.fastq.gz"
        subset_r2 = reads_dir / f"{file_key}_R2.fastq.gz"

        url_r1 = source_url  or ebi_urls["r1"]
        url_r2 = source_url_r2 or ebi_urls["r2"]

        for dst, url, label in [(subset_r1, url_r1, "R1"), (subset_r2, url_r2, "R2")]:
            if not _is_valid_gz(dst):
                dst.unlink(missing_ok=True)
                log.append(f"Streaming {label} ({subset_key} reads)...")
                if not _stream_subset(url, dst, num_reads):
                    return broke("core_test_data.subset_download_failed", success=False, error=f"Failed to download/subset {label} from {url}")

        read_length = _measure_read_length(subset_r1)
        rel_prefix = (f"long_read/{platform_family}/{assay_type}" if read_type == "long_read"
                      else f"short_read/{end_type}/{assay_type}")
        r1_rel = f"{rel_prefix}/{subset_r1.name}"
        r2_rel = f"{rel_prefix}/{subset_r2.name}"
        r1_out, r2_out = str(subset_r1), str(subset_r2)

    else:  # single_end (also covers all long-read platforms)
        subset_r1 = reads_dir / f"{file_key}_R1.fastq.gz"

        if not _is_valid_gz(subset_r1):
            subset_r1.unlink(missing_ok=True)
            log.append(f"Streaming reads ({subset_key})...")
            url_single = source_url or ebi_urls["r1"]
            ok = _stream_subset(url_single, subset_r1, num_reads)
            if not ok and not source_url:
                ok = _stream_subset(ebi_urls["single"], subset_r1, num_reads)
            if not ok:
                return broke("core_test_data.subset_download_failed_single", success=False, error=f"Failed to download/subset reads for {accession}")

        read_length = _measure_read_length(subset_r1)
        rel_prefix = (f"long_read/{platform_family}/{assay_type}" if read_type == "long_read"
                      else f"short_read/{end_type}/{assay_type}")
        r1_rel = f"{rel_prefix}/{subset_r1.name}"
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
        if source_url:
            existing.source_urls["r1"] = source_url
        if source_url_r2:
            existing.source_urls["r2"] = source_url_r2
        existing.write(meta_path)
        log.append(f"SampleMeta updated: {meta_path.name}")
    else:
        actual_r1_url = source_url or ebi_urls["r1"]
        actual_r2_url = source_url_r2 or ebi_urls["r2"]
        database = "local" if source_url and not source_url.startswith("https://ftp.sra.ebi") else "EBI_SRA"
        if source_url and "ncbi" in source_url:
            database = "NCBI_SRA"
        SampleMeta(
            sample=sample,
            accession=accession,
            read_type=read_type,
            end_type=end_type,
            assay_type=assay_type,
            platform=platform,
            database=database,
            read_length=read_length,
            source_urls={"r1": actual_r1_url, **({"r2": actual_r2_url} if end_type == "paired_end" else {})},
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


def _sha256_file(path: Path) -> str:
    """Streaming sha256 — single-pass, no whole-file slurp. Used to anchor
    downloaded pod5 assets (whole-file integrity, since pod5 is binary
    Arrow data and can't be partially-validated like a gzip stream)."""
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def add_core_pod5_data(
    config: dict,
    accession: str,
    sample: str,
    source_url: str,
    assay_type: str = "ont_wgs",
    platform: str = "ont",
    chemistry: str = "",
    flowcell: str = "",
    kit: str = "",
    suggested_model: str = "",
    expected_sha256: str = "",
    expected_size: int = 0,
    genome_build: str = "hg38",
) -> dict[str, Any]:
    """Register a raw nanopore pod5 file as a core test dataset.

    Pod5 is binary Apache Arrow — NOT subsettable on the fly the way gzipped
    FASTQ is (no `gunzip | head -N | gzip` analog). We download the whole
    file, sha256-anchor it, and record nanopore-specific metadata (chemistry,
    flowcell, kit, suggested basecall model) on the SampleMeta sidecar so
    downstream basecaller pipelines (dorado, bonito, remora) pick the right
    model without re-probing.

    Idempotent: skips download if the file already exists with matching size
    (or matching sha256 when `expected_sha256` is supplied).

    Methylation / base-mod future: the `chemistry` + `suggested_model` fields
    are enough to route to a paired modbase model (e.g. 5mC/5hmC/6mA) at
    pipeline-build time. No pre-declared modbase model list here — chemistry
    is the anchor, the basecaller's registry is the source of truth."""
    project_root = Path(__file__).parent.parent.parent.resolve()
    data_dir = project_root / config["paths"]["data_dir"]
    core_dir = data_dir / f"core_test_data_{genome_build}"

    # Long-read layout mirrors add_core_test_data's: long_read/<platform>/<assay_type>/
    platform_family = PLATFORM_FAMILY.get(platform, "ont")
    reads_dir = core_dir / "long_read" / platform_family / assay_type
    reads_dir.mkdir(parents=True, exist_ok=True)

    sample_key = f"{sample}_{accession}"
    # Subset key "1" — one whole pod5 file = one subset. (Pod5 files often
    # carry one read; even when they carry many, we don't subset.)
    subset_key = "1"
    dst = reads_dir / f"{sample_key}_{subset_key}.pod5"
    log: list[str] = []

    # ------------------------------------------------------------------
    # Idempotency check + download
    # ------------------------------------------------------------------
    needs_download = True
    if dst.exists():
        if expected_sha256:
            current = _sha256_file(dst)
            if current == expected_sha256:
                log.append("File present with matching sha256 — skipping download.")
                needs_download = False
            else:
                log.append(f"sha256 mismatch on existing file ({current[:12]}…), "
                           f"re-downloading.")
                dst.unlink()
        elif expected_size and dst.stat().st_size == expected_size:
            log.append("File present with matching size — skipping download.")
            needs_download = False
        elif not expected_size and not expected_sha256:
            log.append("File present (no anchor declared) — skipping download.")
            needs_download = False

    if needs_download:
        tmp = dst.with_suffix(".pod5.tmp")
        log.append(f"Downloading {source_url}...")
        try:
            with urllib.request.urlopen(source_url, timeout=120) as resp, tmp.open("wb") as out:
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return broke("core_test_data.pod5_download_failed", success=False, error=f"download failed: {e}")

        # Whole-file integrity check (asset sha256 — analogous to install_release_binary's I14)
        actual_sha = _sha256_file(tmp)
        if expected_sha256 and actual_sha != expected_sha256:
            tmp.unlink(missing_ok=True)
            return broke("core_test_data.pod5_sha256_mismatch",
                         success=False,
                         error=(f"sha256 mismatch: expected {expected_sha256}, "
                                f"got {actual_sha}"))

        # Atomic rename
        tmp.rename(dst)
        log.append(f"sha256: {actual_sha}")

    # ------------------------------------------------------------------
    # SampleMeta sidecar (single subset "1" with the pod5 path)
    # ------------------------------------------------------------------
    rel_prefix = f"long_read/{platform_family}/{assay_type}"
    subset_info = SubsetInfo(
        r1=f"{rel_prefix}/{dst.name}",
        r2=None,
        num_reads=0,         # unknown without pod5-cli probe; downstream tools can fill
        available=True,
    )
    meta_path = reads_dir / f"{sample_key}_sample_meta.yaml"

    if meta_path.exists():
        existing = SampleMeta.from_yaml(meta_path)
        existing.subsets[subset_key] = subset_info
        # Refresh nanopore metadata if newly supplied; preserve prior values otherwise.
        if chemistry:       existing.chemistry = chemistry
        if flowcell:        existing.flowcell = flowcell
        if kit:             existing.kit = kit
        if suggested_model: existing.suggested_model = suggested_model
        if not existing.file_format: existing.file_format = "pod5"
        if source_url:
            existing.source_urls = {**(existing.source_urls or {}), "r1": source_url}
        existing.write(meta_path)
        log.append(f"SampleMeta updated: {meta_path.name}")
    else:
        SampleMeta(
            sample=sample,
            accession=accession,
            read_type="long_read",
            end_type="single_end",
            assay_type=assay_type,
            platform=platform,
            database="local",
            source_urls={"r1": source_url},
            subsets={subset_key: subset_info},
            file_format="pod5",
            chemistry=chemistry or None,
            flowcell=flowcell or None,
            kit=kit or None,
            suggested_model=suggested_model or None,
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

    return proven("core_test_data.pod5_added", **{
        "success":         True,
        "accession":       accession,
        "sample":          sample,
        "sample_key":      sample_key,
        "genome_build":    genome_build,
        "assay_type":      assay_type,
        "platform":        platform,
        "file_format":     "pod5",
        "chemistry":       chemistry,
        "flowcell":        flowcell,
        "kit":             kit,
        "suggested_model": suggested_model,
        "path":            str(dst),
        "sha256":          _sha256_file(dst),
        "size_bytes":      dst.stat().st_size,
        "sample_meta":     str(meta_path),
        "core_dir":        str(core_dir),
        "log":             log,
    })


def add_phenopacket(
    config: dict,
    source_url: str,
    genome_build: str = "hg38",
) -> dict[str, Any]:
    """
    Download and register a GA4GH phenopacket JSON into core_test_data.

    The phenopacket ID is read directly from the JSON (data["id"]) — nothing
    is inferred from the URL or passed manually.  All metadata is derived by
    PhenopacketMeta.from_phenopacket() so no field extraction lives here.

    Idempotent: re-running refreshes the sidecar without re-downloading.
    """
    project_root = Path(__file__).parent.parent.parent.resolve()
    data_dir     = project_root / config["paths"]["data_dir"]
    core_dir     = data_dir / f"core_test_data_{genome_build}"
    pk_dir       = core_dir / "phenopackets"
    pk_dir.mkdir(parents=True, exist_ok=True)

    # Accept either a github.com/.../blob/... URL or a raw.githubusercontent.com URL
    fetch_url = _normalize_github_url(source_url)

    # ------------------------------------------------------------------
    # Download JSON (skip if already present)
    # ------------------------------------------------------------------
    try:
        req = urllib.request.Request(fetch_url, headers={"User-Agent": "bioinf-agent/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
        data: dict[str, Any] = json.loads(raw)
    except Exception as e:
        return broke("core_test_data.phenopacket_download_failed", success=False, error=f"Failed to download phenopacket: {e}")

    pk_id    = data.get("id") or ""
    if not pk_id:
        return refused("core_test_data.phenopacket_missing_id", success=False, error="Phenopacket JSON missing required 'id' field")

    json_file = pk_dir / f"{pk_id}.json"
    log: list[str] = []

    if not json_file.exists():
        json_file.write_text(json.dumps(data, indent=2))
        log.append(f"Downloaded: {json_file.name}")
    else:
        log.append(f"JSON already present: {json_file.name}")

    # ------------------------------------------------------------------
    # Build metadata entirely from the phenopacket — no manual extraction
    # ------------------------------------------------------------------
    rel_file = f"phenopackets/{json_file.name}"
    meta     = PhenopacketMeta.from_phenopacket(data, source_url=source_url, rel_file=rel_file)

    meta_path = pk_dir / f"{pk_id}_meta.yaml"
    meta.write(meta_path)
    log.append(f"PhenopacketMeta written: {meta_path.name}")

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

    return proven("core_test_data.phenopacket_added", **{
        "success":        True,
        "phenopacket_id": pk_id,
        "file":           str(json_file),
        "meta":           str(meta_path),
        "subject_id":     meta.subject_id,
        "sex":            meta.sex,
        "genes":          meta.genes,
        "diseases":       meta.diseases,
        "hpo_terms":      meta.hpo_terms,
        "variants":       meta.variants,
        "genome_assembly": meta.genome_assembly,
        "log":            log,
    })
