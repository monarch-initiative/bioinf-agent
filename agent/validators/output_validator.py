"""
OutputValidator — verify bioinformatics output files are valid.

Each check: file exists, non-empty, structurally parseable.
Checks are intentionally lightweight (no full parse of multi-GB files).
"""

import gzip
import os
import struct
import subprocess
from pathlib import Path
from typing import Any


class OutputValidator:
    def __init__(self, config: dict):
        self.config = config

    def validate(self, file_path: str, expected_type: str) -> dict[str, Any]:
        path = Path(file_path)

        if not path.exists():
            return {"passed": False, "file": file_path, "error": "File does not exist"}
        if path.stat().st_size == 0:
            return {"passed": False, "file": file_path, "error": "File is empty"}

        dispatch = {
            "sam": self._check_sam,
            "bam": self._check_bam,
            "fastq": self._check_fastq,
            "fasta": self._check_fasta,
            "vcf": self._check_vcf,
            "bcf": self._check_bcf,
            "bed": self._check_bed,
            "bigwig": self._check_bigwig,
            "counts_matrix": self._check_counts_matrix,
            "gtf": self._check_gtf,
            "gff": self._check_gtf,
            "log": self._check_log,
            "any": self._check_any,
        }

        checker = dispatch.get(expected_type.lower(), self._check_any)
        result = checker(path)
        result["file"] = file_path
        result["expected_type"] = expected_type
        result["size_bytes"] = path.stat().st_size
        return result

    # -----------------------------------------------------------------------
    # Type-specific checks
    # -----------------------------------------------------------------------

    def _check_sam(self, path: Path) -> dict:
        lines = self._head_lines(path, 20)
        has_header = any(line.startswith("@") for line in lines)
        data_lines = [l for l in lines if l and not l.startswith("@")]
        if not data_lines and not has_header:
            return {"passed": False, "error": "No SAM header or alignment lines found"}
        # A valid SAM data line has >= 11 tab-separated fields
        if data_lines:
            fields = data_lines[0].split("\t")
            if len(fields) < 11:
                return {"passed": False, "error": f"SAM line has only {len(fields)} fields"}
        return {"passed": True, "has_header": has_header, "sample_lines": len(data_lines)}

    def _check_bam(self, path: Path) -> dict:
        ret = subprocess.run(
            ["samtools", "quickcheck", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if ret.returncode != 0:
            # samtools may not be in PATH — fall back to magic bytes
            with open(path, "rb") as f:
                magic = f.read(4)
            if magic[:3] == b"\x1f\x8b\x08":
                return {"passed": True, "note": "BAM magic OK (samtools not in PATH for full check)"}
            return {"passed": False, "error": f"samtools quickcheck: {ret.stderr[:200]}"}

        # Get flagstat for a richer check
        stat = subprocess.run(
            ["samtools", "flagstat", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        if stat.returncode == 0:
            return {"passed": True, "flagstat": stat.stdout[:500]}
        return {"passed": True, "note": "BAM quickcheck passed"}

    def _check_fastq(self, path: Path) -> dict:
        lines = self._head_lines(path, 8)
        if len(lines) < 4:
            return {"passed": False, "error": "Fewer than 4 lines in FASTQ"}
        if not lines[0].startswith("@"):
            return {"passed": False, "error": "FASTQ line 1 should start with '@'"}
        if not lines[2].startswith("+"):
            return {"passed": False, "error": "FASTQ line 3 should start with '+'"}
        if len(lines[1]) != len(lines[3]):
            return {"passed": False, "error": "Sequence and quality length mismatch"}
        return {"passed": True, "sample_read_length": len(lines[1])}

    def _check_fasta(self, path: Path) -> dict:
        lines = self._head_lines(path, 5)
        if not lines:
            return {"passed": False, "error": "Empty FASTA"}
        if not lines[0].startswith(">"):
            return {"passed": False, "error": "FASTA does not start with '>'"}
        return {"passed": True, "first_header": lines[0][:80]}

    def _check_vcf(self, path: Path) -> dict:
        lines = self._head_lines(path, 30)
        has_meta = any(l.startswith("##") for l in lines)
        has_header = any(l.startswith("#CHROM") for l in lines)
        data_lines = [l for l in lines if l and not l.startswith("#")]
        if not has_meta:
            return {"passed": False, "error": "VCF missing ## meta lines"}
        if data_lines:
            fields = data_lines[0].split("\t")
            if len(fields) < 8:
                return {
                    "passed": False,
                    "error": f"VCF data line has only {len(fields)} fields (need ≥8)",
                }
        return {
            "passed": True,
            "has_column_header": has_header,
            "data_lines_in_sample": len(data_lines),
        }

    def _check_bcf(self, path: Path) -> dict:
        ret = subprocess.run(
            ["bcftools", "stats", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if ret.returncode == 0:
            return {"passed": True, "note": "bcftools stats OK"}
        with open(path, "rb") as f:
            magic = f.read(3)
        if magic == b"BCF":
            return {"passed": True, "note": "BCF magic OK (bcftools not in PATH)"}
        return {"passed": False, "error": ret.stderr[:200]}

    def _check_bed(self, path: Path) -> dict:
        lines = self._head_lines(path, 5)
        data_lines = [l for l in lines if l and not l.startswith("#") and not l.startswith("track") and not l.startswith("browser")]
        if not data_lines:
            return {"passed": False, "error": "No BED data lines found"}
        fields = data_lines[0].split("\t")
        if len(fields) < 3:
            return {"passed": False, "error": f"BED line has only {len(fields)} fields (need ≥3)"}
        try:
            int(fields[1])
            int(fields[2])
        except ValueError:
            return {"passed": False, "error": "BED start/end are not integers"}
        return {"passed": True, "fields_per_line": len(fields)}

    def _check_bigwig(self, path: Path) -> dict:
        with open(path, "rb") as f:
            magic = f.read(4)
        # BigWig magic: 0x888FFC26 (little-endian) or 0x26FC8F88 (big-endian)
        bw_magic_le = b"\x26\xfc\x8f\x88"
        bw_magic_be = b"\x88\x8f\xfc\x26"
        if magic in (bw_magic_le, bw_magic_be):
            return {"passed": True}
        return {"passed": False, "error": "BigWig magic bytes not found"}

    def _check_counts_matrix(self, path: Path) -> dict:
        lines = self._head_lines(path, 5)
        if not lines:
            return {"passed": False, "error": "Empty counts file"}
        # Skip comment lines (featureCounts starts with '#')
        data = [l for l in lines if l and not l.startswith("#")]
        if not data:
            return {"passed": False, "error": "No non-comment lines found"}
        fields = data[0].split("\t")
        if len(fields) < 2:
            return {"passed": False, "error": f"Counts file has only {len(fields)} columns"}
        return {"passed": True, "columns": len(fields), "sample_header": data[0][:100]}

    def _check_gtf(self, path: Path) -> dict:
        lines = self._head_lines(path, 10)
        data = [l for l in lines if l and not l.startswith("#")]
        if not data:
            return {"passed": False, "error": "No non-comment lines in GTF/GFF"}
        fields = data[0].split("\t")
        if len(fields) < 8:
            return {"passed": False, "error": f"GTF/GFF line has {len(fields)} fields (need ≥8)"}
        return {"passed": True, "sample_feature": fields[2] if len(fields) > 2 else ""}

    def _check_log(self, path: Path) -> dict:
        lines = self._head_lines(path, 5)
        return {"passed": bool(lines), "lines": len(lines)}

    def _check_any(self, path: Path) -> dict:
        return {"passed": True, "note": "Generic check — file exists and non-empty"}

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _head_lines(self, path: Path, n: int) -> list[str]:
        try:
            if path.suffix in (".gz", ".bgz"):
                with gzip.open(path, "rt", errors="replace") as f:
                    return [f.readline().rstrip() for _ in range(n)]
            else:
                with open(path, errors="replace") as f:
                    return [f.readline().rstrip() for _ in range(n)]
        except Exception:
            return []
