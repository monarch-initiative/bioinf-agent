#!/usr/bin/env python3
"""
Rebuild manifest.yaml for a core_test_data directory.

Derives content from:
  - Disk state  (which genome/FASTQ/BAM/VCF files actually exist)
  - SampleMeta YAML sidecars  ({accession}_sample_meta.yaml alongside FASTQs)
  - Provenance YAML files  ({sample_key}_provenance.yaml in pipeline_outputs/*/)

Usage:
  python scripts/gen_manifest.py --core-dir /abs/path/to/data/core_test_data_hg38
"""

import argparse
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.models.core_data import Provenance, SampleMeta


# ---------------------------------------------------------------------------
# Genome section
# ---------------------------------------------------------------------------


def _genome_section(core_dir: Path) -> dict:
    genome_dir = core_dir / "genome"
    fa_files = sorted(genome_dir.glob("*.fa")) if genome_dir.exists() else []
    if not fa_files:
        return {}

    fa = fa_files[0]
    chrom = fa.stem  # e.g. "chr22"

    # Collect index files
    bwa_indexes = [
        f"genome/{fa.name}{ext}"
        for ext in (".amb", ".ann", ".bwt", ".pac", ".sa")
        if (genome_dir / (fa.name + ext)).exists()
    ]
    # Infer build from core_dir name, e.g. core_test_data_hg38 → hg38
    build = core_dir.name.replace("core_test_data_", "")

    return {
        "fasta":      f"genome/{fa.name}",
        "fai":        f"genome/{fa.name}.fai",
        "chromosome": chrom,
        "source_url": (
            f"https://hgdownload.soe.ucsc.edu/goldenPath/{build}/chromosomes/{fa.name}.gz"
        ),
        "indexes": {"bwa": bwa_indexes} if bwa_indexes else {},
    }


# ---------------------------------------------------------------------------
# Sequencing data section  (reads from SampleMeta sidecars)
# ---------------------------------------------------------------------------


def _sequencing_data_section(core_dir: Path) -> dict:
    """
    Walk short_read/*/*/  looking for *_sample_meta.yaml sidecars.
    Falls back to scanning for FASTQ files without a sidecar.
    """
    sd: dict = {}

    short_read_root = core_dir / "short_read"
    if not short_read_root.exists():
        return sd

    for end_type_dir in sorted(short_read_root.iterdir()):
        if not end_type_dir.is_dir():
            continue
        end_type = end_type_dir.name  # paired_end, single_end, mate_pair

        for assay_dir in sorted(end_type_dir.iterdir()):
            if not assay_dir.is_dir():
                continue
            assay_type = assay_dir.name

            samples_list = []

            # Prefer sidecar YAML
            for meta_file in sorted(assay_dir.glob("*_sample_meta.yaml")):
                try:
                    meta = SampleMeta.from_yaml(meta_file)
                    entry = meta.model_dump(exclude_none=True)
                    # Replace SubsetInfo objects with plain dicts (already done by model_dump)
                    samples_list.append(entry)
                except Exception as e:
                    print(f"[gen_manifest] WARN: could not parse {meta_file}: {e}", file=sys.stderr)

            # If no sidecars, create a minimal entry from FASTQ filenames.
            # Expected naming: {sample}_{accession}_{subset}_R1.fastq.gz
            if not samples_list:
                r1_files = sorted(assay_dir.glob("*_R1.fastq.gz"))
                seen: set[str] = set()
                for r1 in r1_files:
                    base = r1.name.replace("_R1.fastq.gz", "")  # {sample}_{accession}_{subset}
                    if base in seen:
                        continue
                    seen.add(base)
                    # Infer subset from last underscore-delimited token (e.g. "100K")
                    parts = base.rsplit("_", 1)
                    subset = parts[-1] if len(parts) == 2 else ""
                    r2 = assay_dir / f"{base}_R2.fastq.gz"
                    samples_list.append({
                        "file_key": base,
                        "subsets": {
                            subset: {
                                "r1": f"short_read/{end_type}/{assay_type}/{r1.name}",
                                "r2": (f"short_read/{end_type}/{assay_type}/{r2.name}"
                                       if r2.exists() else None),
                                "num_reads": (
                                    int(subset.rstrip("KMG")) *
                                    (1_000_000 if subset.endswith("M") else 1000)
                                ) if subset else 0,
                                "available": r1.exists(),
                            }
                        },
                    })

            if samples_list:
                sd.setdefault("short_read", {}).setdefault(end_type, {})[assay_type] = samples_list

    return sd


# ---------------------------------------------------------------------------
# Pipeline outputs section  (reads from Provenance sidecars)
# ---------------------------------------------------------------------------


def _pipeline_outputs_section(core_dir: Path) -> dict:
    po_root = core_dir / "pipeline_outputs"
    if not po_root.exists():
        return {}

    po: dict = {}

    for pipeline_dir in sorted(po_root.iterdir()):
        if not pipeline_dir.is_dir():
            continue
        pipeline_name = pipeline_dir.name

        # Discover provenance files: {sample_key}_provenance.yaml
        prov_files = sorted(pipeline_dir.glob("*_provenance.yaml"))
        if not prov_files:
            continue

        samples: dict = {}
        upstream_pipelines: list[str] = []

        for prov_file in prov_files:
            # sample_key is filename without _provenance.yaml
            sample_key = prov_file.name.replace("_provenance.yaml", "")
            try:
                prov = Provenance.from_yaml(prov_file)
                if prov.upstream_pipelines:
                    upstream_pipelines = prov.upstream_pipelines

                # Relative path from core_dir to provenance file
                prov_rel = prov_file.relative_to(core_dir)

                files = []
                for out_file in prov.outputs:
                    abs_path = pipeline_dir / out_file.file
                    rel_path = f"pipeline_outputs/{pipeline_name}/{out_file.file}"
                    entry: dict = {
                        "path": rel_path,
                        "type": out_file.type,
                    }
                    if abs_path.exists():
                        entry["size_bytes"] = abs_path.stat().st_size
                    if out_file.type == "vcf" and abs_path.exists():
                        try:
                            count = sum(
                                1 for ln in abs_path.open()
                                if not ln.startswith("#")
                            )
                            entry["variant_records"] = count
                        except Exception:
                            pass
                    files.append(entry)

                    # Add index file entry if flagged
                    if out_file.indexed:
                        bai = pipeline_dir / (out_file.file + ".bai")
                        if bai.exists():
                            files.append({
                                "path": f"pipeline_outputs/{pipeline_name}/{bai.name}",
                                "type": "bai",
                                "size_bytes": bai.stat().st_size,
                            })

                samples[sample_key] = {
                    "provenance": str(prov_rel),
                    "files": files,
                }
            except Exception as e:
                print(f"[gen_manifest] WARN: could not parse {prov_file}: {e}", file=sys.stderr)

        if samples:
            entry_po: dict = {
                "available": True,
                "samples": samples,
            }
            if upstream_pipelines:
                entry_po["upstream_pipelines"] = upstream_pipelines
            po[pipeline_name] = entry_po

    return po


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        description="Rebuild manifest.yaml from disk state + sidecar YAML files"
    )
    p.add_argument(
        "--core-dir", required=True,
        help="Absolute path to a core_test_data_{build} directory",
    )
    args = p.parse_args()

    core_dir = Path(args.core_dir).resolve()
    if not core_dir.is_dir():
        print(f"ERROR: {core_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    build = core_dir.name.replace("core_test_data_", "")
    genome = _genome_section(core_dir)
    chrom = genome.get("chromosome", "")
    sequencing_data = _sequencing_data_section(core_dir)
    pipeline_outputs = _pipeline_outputs_section(core_dir)

    manifest = {
        "genome_build":       build,
        "species":            "homo_sapiens" if "hg" in build else "unknown",
        "chromosome_subset":  chrom,
        "generated_at":       str(date.today()),
        "genome":             genome,
        "sequencing_data":    sequencing_data,
        "pipeline_outputs":   pipeline_outputs,
    }

    out_path = core_dir / "manifest.yaml"
    header = (
        f"# Core test dataset — {build}, {chrom} subset\n"
        f"# Generated by scripts/gen_manifest.py on {date.today()}\n"
        f"#\n"
        f"# Controlled vocabulary:\n"
        f"#   read_type:  short_read | long_read\n"
        f"#   end_type:   paired_end | single_end | mate_pair\n"
        f"#   assay_type: exome | wgs | rnaseq | chipseq | atacseq | hic | amplicon\n"
        f"#   file_type:  fastq | bam | sam | bai | vcf | bed | bigwig | pod5 | fast5\n"
        f"\n"
    )
    out_path.write_text(header + yaml.dump(manifest, default_flow_style=False, sort_keys=False))
    print(f"[gen_manifest] Written: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
