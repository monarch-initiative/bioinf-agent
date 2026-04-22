"""
TestRunner — handles downloading reference data and test datasets.

The actual algorithm execution is done via EnvManager.run_in_env
(called directly by the sub-agent via the run_command tool).
This module handles the data-layer side: fetching genomes and
test datasets listed in the manifests but not yet on disk.
"""

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

    # -----------------------------------------------------------------------
    # Test data downloading
    # -----------------------------------------------------------------------

    def _download_test_data(self, dataset_id: str) -> dict[str, Any]:
        # dataset_id format: "{build}_{assay_type}_{accession}_{subset}"
        # e.g. "hg38_exome_SRR1517830_100K"
        # Output goes to data/core_test_data_{build}/short_read/{end_type}/{assay_type}/
        parts = dataset_id.split("_")
        build = parts[0] if parts else "hg38"
        core_dir = self.data_dir / f"core_test_data_{build}"

        # Find metadata from core manifest
        manifest_path = core_dir / "manifest.yaml"
        if manifest_path.exists():
            with open(manifest_path) as f:
                m = yaml.safe_load(f) or {}
            for read_type, end_types in m.get("sequencing_data", {}).items():
                if not isinstance(end_types, dict):
                    continue
                for end_type, assay_types in end_types.items():
                    if not isinstance(assay_types, dict):
                        continue
                    for assay_type, samples in assay_types.items():
                        if not isinstance(samples, list):
                            continue
                        for smp in samples:
                            for subset, sinfo in smp.get("subsets", {}).items():
                                sid = f"{build}_{assay_type}_{smp.get('accession', '')}_{subset}"
                                if sid == dataset_id:
                                    out_dir = core_dir / read_type / end_type / assay_type
                                    out_dir.mkdir(parents=True, exist_ok=True)
                                    dataset = {
                                        "type": assay_type, "metadata": {
                                            "num_reads": sinfo.get("num_reads", 100000),
                                            "read_length": smp.get("read_length", 150),
                                            "layout": "paired" if end_type == "paired_end" else "single",
                                        },
                                        "files": {
                                            "r1": f"{smp.get('accession', 'sample')}_R1_{subset}.fastq.gz",
                                            "r2": f"{smp.get('accession', 'sample')}_R2_{subset}.fastq.gz",
                                        },
                                        "compatible_genomes": [build],
                                    }
                                    return self._generate_synthetic_reads(out_dir, dataset)

        return {"success": False, "error": f"Dataset '{dataset_id}' not found in any core manifest"}

    def _generate_synthetic_reads(self, out_dir: Path, dataset: dict) -> dict:
        """Generate synthetic short reads using wgsim (bundled with samtools)."""
        meta = dataset.get("metadata", {})
        num_reads = meta.get("num_reads", 100000)
        read_length = meta.get("read_length", 150)
        layout = meta.get("layout", "paired")

        # Find a compatible genome FASTA in core_test_data dirs
        fasta = self._find_available_genome_fasta(dataset.get("compatible_genomes", []))
        if not fasta:
            return {
                "success": False,
                "error": (
                    "No compatible genome FASTA found in core_test_data dirs. "
                    f"Need one of: {dataset.get('compatible_genomes')}. "
                    "Run setup_core_test_data.sh first."
                ),
            }

        files = dataset.get("files", {})
        if layout == "paired":
            r1 = out_dir / files.get("r1", "reads_R1.fastq.gz")
            r2 = out_dir / files.get("r2", "reads_R2.fastq.gz")
            r1_tmp = out_dir / "r1.fastq"
            r2_tmp = out_dir / "r2.fastq"

            cmd = (
                f"wgsim -N {num_reads} -1 {read_length} -2 {read_length} "
                f"-e 0.005 -d 350 -s 50 "
                f"{fasta} {r1_tmp} {r2_tmp}"
            )
            ret = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if ret.returncode != 0:
                return {"success": False, "error": f"wgsim failed: {ret.stderr[:500]}"}

            subprocess.run(f"gzip -c {r1_tmp} > {r1}", shell=True)
            subprocess.run(f"gzip -c {r2_tmp} > {r2}", shell=True)
            r1_tmp.unlink(missing_ok=True)
            r2_tmp.unlink(missing_ok=True)

            return {
                "success": True,
                "dataset_id": dataset["id"],
                "files": {"r1": str(r1), "r2": str(r2)},
                "num_reads": num_reads,
                "genome_used": genome_id,
            }
        else:
            reads_out = out_dir / files.get("reads", "reads.fastq.gz")
            reads_tmp = out_dir / "reads.fastq"
            cmd = (
                f"wgsim -N {num_reads} -1 {read_length} -2 0 "
                f"-e 0.005 {fasta} {reads_tmp} /dev/null"
            )
            ret = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if ret.returncode != 0:
                return {"success": False, "error": f"wgsim failed: {ret.stderr[:500]}"}
            subprocess.run(f"gzip -c {reads_tmp} > {reads_out}", shell=True)
            reads_tmp.unlink(missing_ok=True)
            return {
                "success": True,
                "dataset_id": dataset["id"],
                "files": {"reads": str(reads_out)},
                "num_reads": num_reads,
                "genome_used": genome_id,
            }

    def _generate_synthetic_long_reads(self, out_dir: Path, dataset: dict) -> dict:
        """Generate synthetic long reads using badread (if available) or wgsim with long params."""
        meta = dataset.get("metadata", {})
        num_reads = meta.get("num_reads", 5000)
        mean_length = meta.get("read_length_mean", 8000)

        fasta = self._find_available_genome_fasta(dataset.get("compatible_genomes", []))
        if not fasta:
            return {"success": False, "error": "No compatible genome FASTA found in core_test_data dirs."}

        files = dataset.get("files", {})
        reads_out = out_dir / files.get("reads", "reads.fastq.gz")
        reads_tmp = out_dir / "reads.fastq"

        # Try badread first (better for nanopore simulation)
        ret = subprocess.run("which badread", shell=True, capture_output=True)
        if ret.returncode == 0:
            cmd = (
                f"badread simulate --reference {fasta} "
                f"--quantity {num_reads}x --length {mean_length},2000 "
                f"--error_model nanopore2020 --qscore_model nanopore2020 "
                f"| gzip > {reads_out}"
            )
        else:
            cmd = (
                f"wgsim -N {num_reads} -1 {mean_length} -2 0 "
                f"-e 0.1 {fasta} {reads_tmp} /dev/null && "
                f"gzip -c {reads_tmp} > {reads_out}"
            )

        ret = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        reads_tmp.unlink(missing_ok=True) if reads_tmp.exists() else None

        return {
            "success": ret.returncode == 0,
            "dataset_id": dataset["id"],
            "files": {"reads": str(reads_out)},
            "error": ret.stderr[:500] if ret.returncode != 0 else None,
        }

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

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

    def _load_manifest(self, path: Path, key: str) -> list:
        if not path.exists():
            return []
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return data.get(key, [])

    def _mark_available(self, manifest_path: Path, key: str, resource_id: str):
        if not manifest_path.exists():
            return
        with open(manifest_path) as f:
            data = yaml.safe_load(f) or {}
        for item in data.get(key, []):
            if item.get("id") == resource_id:
                item["available"] = True
        with open(manifest_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
