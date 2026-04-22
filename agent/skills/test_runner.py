"""
TestRunner — handles downloading reference genomes for pipeline validation.

The actual algorithm execution is done via EnvManager.run_in_env
(called directly by the sub-agent via the run_command tool).
Test data (reads) are managed by agent/skills/core_test_data.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml


class TestRunner:
    def __init__(self, config: dict):
        self.config = config
        self.project_root = Path(__file__).parent.parent.parent.resolve()
        self.data_dir = self.project_root / config["paths"]["data_dir"]

    def download_resource(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        if resource_type == "genome":
            return self._download_genome(resource_id)
        if resource_type == "test_data":
            return self._download_test_data(resource_id)
        return {"success": False, "error": f"Unknown resource_type: {resource_type}"}

    # -----------------------------------------------------------------------
    # Genome downloading
    # -----------------------------------------------------------------------

    def _download_genome(self, genome_id: str) -> dict[str, Any]:
        # genome_id format: "{build}" or "{build}_chr{chrom}" e.g. "hg38" or "hg38_chr22"
        # Genome goes into data/core_test_data_{build}/genome/
        build = genome_id.replace("_chr22", "").replace("_chr1", "")
        chrom = self.config["testing"]["preferred_chromosome"]
        core_dir = self.data_dir / f"core_test_data_{build}"
        out_dir = core_dir / "genome"
        out_dir.mkdir(parents=True, exist_ok=True)

        fasta_path = out_dir / f"{chrom}.fa"
        if fasta_path.exists() and fasta_path.stat().st_size > 0:
            return {
                "success": True,
                "genome_id": genome_id,
                "path": str(out_dir),
                "fasta": str(fasta_path),
                "note": "Already on disk",
            }

        if "hg38" in build:
            result = self._download_ucsc_human_hg38(out_dir, [chrom], {"files": {"fasta": f"{chrom}.fa", "gtf": "genes.gtf"}})
        elif "mm10" in build or "mm39" in build:
            result = self._download_ucsc_mouse_mm10(out_dir, [chrom], {"files": {"fasta": f"{chrom}.fa"}})
        elif "ecoli" in build or "k12" in build:
            result = self._download_ncbi_ecoli(out_dir, {"files": {"fasta": "genome.fa"}})
        else:
            return {
                "success": False,
                "error": (
                    f"No automatic download handler for build '{build}'. "
                    "Download manually and place FASTA at: " + str(fasta_path)
                ),
            }

        return result

    def _download_ucsc_human_hg38(
        self, out_dir: Path, chromosomes: list[str], genome: dict
    ) -> dict:
        fasta_path = out_dir / genome["files"]["fasta"]
        parts = []
        for chrom in chromosomes:
            url = f"https://hgdownload.soe.ucsc.edu/goldenPath/hg38/chromosomes/{chrom}.fa.gz"
            gz_path = out_dir / f"{chrom}.fa.gz"
            ok = self._download_file(url, gz_path)
            if not ok:
                return {"success": False, "error": f"Failed to download {url}"}
            parts.append(gz_path)

        # Decompress + cat into single fasta
        cat_cmd = f"zcat {' '.join(str(p) for p in parts)} > {fasta_path}"
        ret = subprocess.run(cat_cmd, shell=True, capture_output=True, text=True)
        for p in parts:
            p.unlink(missing_ok=True)

        if ret.returncode != 0:
            return {"success": False, "error": ret.stderr}

        gtf_url = (
            "https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_45/"
            "GRCh38.primary_assembly.genome.fa.gz"
        )
        # Download a chr22-filtered GTF from GENCODE
        gtf_gz = out_dir / "genes.gtf.gz"
        gtf_path = out_dir / genome["files"]["gtf"]
        gtf_url_small = (
            "https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_45/"
            "gencode.v45.annotation.gtf.gz"
        )
        self._download_file(gtf_url_small, gtf_gz)
        if gtf_gz.exists():
            subprocess.run(
                f"zcat {gtf_gz} | awk '$1==\"chr22\"' > {gtf_path}",
                shell=True, capture_output=True,
            )
            gtf_gz.unlink(missing_ok=True)

        self._index_fasta(out_dir, fasta_path)
        return {"success": True, "path": str(out_dir), "fasta": str(fasta_path)}

    def _download_ucsc_mouse_mm10(
        self, out_dir: Path, chromosomes: list[str], genome: dict
    ) -> dict:
        fasta_path = out_dir / genome["files"]["fasta"]
        parts = []
        for chrom in chromosomes:
            url = f"https://hgdownload.soe.ucsc.edu/goldenPath/mm10/chromosomes/{chrom}.fa.gz"
            gz_path = out_dir / f"{chrom}.fa.gz"
            ok = self._download_file(url, gz_path)
            if not ok:
                return {"success": False, "error": f"Failed to download {url}"}
            parts.append(gz_path)

        cat_cmd = f"zcat {' '.join(str(p) for p in parts)} > {fasta_path}"
        ret = subprocess.run(cat_cmd, shell=True, capture_output=True, text=True)
        for p in parts:
            p.unlink(missing_ok=True)

        if ret.returncode != 0:
            return {"success": False, "error": ret.stderr}

        self._index_fasta(out_dir, fasta_path)
        return {"success": True, "path": str(out_dir), "fasta": str(fasta_path)}

    def _download_ncbi_ecoli(self, out_dir: Path, genome: dict) -> dict:
        fasta_path = out_dir / genome["files"]["fasta"]
        # NCBI RefSeq accession for E. coli K-12 MG1655
        url = (
            "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/005/845/"
            "GCF_000005845.2_ASM584v2/GCF_000005845.2_ASM584v2_genomic.fna.gz"
        )
        gz_path = out_dir / "ecoli.fa.gz"
        ok = self._download_file(url, gz_path)
        if not ok:
            return {"success": False, "error": f"Failed to download {url}"}

        ret = subprocess.run(
            f"zcat {gz_path} > {fasta_path}", shell=True, capture_output=True, text=True
        )
        gz_path.unlink(missing_ok=True)

        if ret.returncode != 0:
            return {"success": False, "error": ret.stderr}

        # GTF/GFF
        gff_url = (
            "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/005/845/"
            "GCF_000005845.2_ASM584v2/GCF_000005845.2_ASM584v2_genomic.gff.gz"
        )
        gff_gz = out_dir / "genes.gff.gz"
        gff_path = out_dir / genome["files"].get("gtf", "genes.gtf")
        self._download_file(gff_url, gff_gz)
        if gff_gz.exists():
            subprocess.run(f"zcat {gff_gz} > {gff_path}", shell=True, capture_output=True)
            gff_gz.unlink(missing_ok=True)

        self._index_fasta(out_dir, fasta_path)
        return {"success": True, "path": str(out_dir), "fasta": str(fasta_path)}

    def _find_available_genome_fasta(self, compatible_builds: list[str]) -> Path | None:
        """Find a FASTA in any core_test_data dir matching compatible builds."""
        for core_dir in sorted(self.data_dir.glob("core_test_data_*")):
            manifest_path = core_dir / "manifest.yaml"
            if not manifest_path.exists():
                continue
            with open(manifest_path) as f:
                m = yaml.safe_load(f) or {}
            build = m.get("genome_build", "")
            if compatible_builds and not any(b in build for b in compatible_builds):
                continue
            ginfo = m.get("genome", {})
            if not ginfo:
                continue
            fasta = core_dir / ginfo.get("fasta", "")
            if fasta.exists() and fasta.stat().st_size > 0:
                return fasta
        return None

    def _index_fasta(self, out_dir: Path, fasta_path: Path):
        subprocess.run(f"samtools faidx {fasta_path}", shell=True, capture_output=True)
        subprocess.run(
            f"samtools dict {fasta_path} > {out_dir / fasta_path.stem}.dict",
            shell=True, capture_output=True,
        )

    def _download_file(self, url: str, dest: Path) -> bool:
        ret = subprocess.run(
            ["curl", "-fsSL", "--retry", "3", "-o", str(dest), url],
            capture_output=True,
            timeout=600,
        )
        return ret.returncode == 0 and dest.exists() and dest.stat().st_size > 0

