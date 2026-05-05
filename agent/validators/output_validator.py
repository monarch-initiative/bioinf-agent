"""
OutputValidator — verify bioinformatics output files are valid.

Tool resolution order: pipeline conda env → bioinf_validators env → system PATH.

Preferred validators per type:
  SAM/BAM        samtools quickcheck + flagstat
  VCF/BCF        bcftools stats
  FASTQ/FASTA    seqkit stats -T
  BED/GTF/counts text parsing (no universal lightweight tool)
  BigWig         magic bytes
"""

from __future__ import annotations

import gzip
import subprocess
from pathlib import Path
from typing import Any


class OutputValidator:
    def __init__(self, config: dict):
        self.config = config
        self._project_root = Path(__file__).parent.parent.parent.resolve()
        self._envs_dir = self._project_root / config["paths"]["conda_envs_prefix"]
        self._validators_env = config["conda"]["env_prefix"] + "validators"
        self._env_name: str | None = None

    def validate(self, file_path: str, expected_type: str, env_name: str | None = None) -> dict[str, Any]:
        path = Path(file_path)
        self._env_name = env_name

        if not path.exists():
            return {"passed": False, "file": file_path, "error": "File does not exist"}
        if path.stat().st_size == 0:
            return {"passed": False, "file": file_path, "error": "File is empty"}

        dispatch = {
            "sam":           self._check_sam,
            "bam":           self._check_sam,   # samtools handles both
            "bai":           self._check_bai,
            "fastq":         self._check_fastq,
            "fasta":         self._check_fasta,
            "vcf":           self._check_vcf,
            "bcf":           self._check_vcf,   # bcftools handles both
            "bed":           self._check_bed,
            "bigwig":        self._check_bigwig,
            "counts_matrix": self._check_counts_matrix,
            "gtf":           self._check_gtf,
            "gff":           self._check_gtf,
            "log":           self._check_log,
            "any":           self._check_any,
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
        """SAM and BAM — samtools quickcheck + flagstat."""
        ret = self._run_tool(["samtools", "quickcheck", str(path)], timeout=60)
        if ret.returncode != 0:
            return self._sam_text_fallback(path)
        stat = self._run_tool(["samtools", "flagstat", str(path)], timeout=120)
        if stat.returncode == 0:
            return {"passed": True, "validation_method": "tool", "flagstat": stat.stdout[:500]}
        return {"passed": True, "validation_method": "tool", "note": "samtools quickcheck passed"}

    def _sam_text_fallback(self, path: Path) -> dict:
        lines = self._head_lines(path, 20)
        has_header = any(l.startswith("@") for l in lines)
        data_lines = [l for l in lines if l and not l.startswith("@")]
        if not data_lines and not has_header:
            return {"passed": False, "validation_method": "text_fallback", "error": "No SAM header or alignment lines found"}
        if data_lines and len(data_lines[0].split("\t")) < 11:
            return {"passed": False, "validation_method": "text_fallback", "error": f"SAM line has only {len(data_lines[0].split(chr(9)))} fields"}
        return {"passed": True, "validation_method": "text_fallback", "has_header": has_header, "note": "samtools unavailable — text check only"}

    def _check_fastq(self, path: Path) -> dict:
        """FASTQ — seqkit stats for rich metadata, 4-line text fallback."""
        ret = self._run_tool(["seqkit", "stats", "-T", str(path)], timeout=60)
        if ret.returncode == 0:
            result = self._parse_seqkit_stats(ret.stdout) or {"passed": True, "note": "seqkit stats passed"}
            result["validation_method"] = "tool"
            return result
        # Fallback: manual 4-line check
        lines = self._head_lines(path, 8)
        if len(lines) < 4:
            return {"passed": False, "validation_method": "text_fallback", "error": "Fewer than 4 lines in FASTQ"}
        if not lines[0].startswith("@"):
            return {"passed": False, "validation_method": "text_fallback", "error": "FASTQ line 1 should start with '@'"}
        if not lines[2].startswith("+"):
            return {"passed": False, "validation_method": "text_fallback", "error": "FASTQ line 3 should start with '+'"}
        if len(lines[1]) != len(lines[3]):
            return {"passed": False, "validation_method": "text_fallback", "error": "Sequence and quality length mismatch"}
        return {"passed": True, "validation_method": "text_fallback", "read_length": self._max_fastq_read_length(path), "note": "seqkit unavailable — text check only"}

    def _check_fasta(self, path: Path) -> dict:
        """FASTA — seqkit stats, header text fallback."""
        ret = self._run_tool(["seqkit", "stats", "-T", str(path)], timeout=60)
        if ret.returncode == 0:
            result = self._parse_seqkit_stats(ret.stdout) or {"passed": True, "note": "seqkit stats passed"}
            result["validation_method"] = "tool"
            return result
        lines = self._head_lines(path, 5)
        if not lines:
            return {"passed": False, "validation_method": "text_fallback", "error": "Empty FASTA"}
        if not lines[0].startswith(">"):
            return {"passed": False, "validation_method": "text_fallback", "error": "FASTA does not start with '>'"}
        return {"passed": True, "validation_method": "text_fallback", "first_header": lines[0][:80], "note": "seqkit unavailable — text check only"}

    def _check_vcf(self, path: Path) -> dict:
        """VCF and BCF — bcftools stats, text fallback for plain VCF."""
        ret = self._run_tool(["bcftools", "stats", str(path)], timeout=60)
        if ret.returncode == 0:
            return {"passed": True, "validation_method": "tool", "bcftools_stats": self._parse_bcftools_sn(ret.stdout)}
        # Fallback: text check (plain VCF, bcftools not available)
        lines = self._head_lines(path, 30)
        if not any(l.startswith("##") for l in lines):
            return {"passed": False, "validation_method": "text_fallback", "error": "VCF missing ## meta lines"}
        data_lines = [l for l in lines if l and not l.startswith("#")]
        if data_lines and len(data_lines[0].split("\t")) < 8:
            return {"passed": False, "validation_method": "text_fallback", "error": f"VCF data line has only {len(data_lines[0].split(chr(9)))} fields (need ≥8)"}
        return {
            "passed": True,
            "validation_method": "text_fallback",
            "has_column_header": any(l.startswith("#CHROM") for l in lines),
            "data_lines_in_sample": len(data_lines),
            "note": "bcftools unavailable — text check only",
        }

    def _check_bed(self, path: Path) -> dict:
        lines = self._head_lines(path, 5)
        data = [l for l in lines if l and not l.startswith(("#", "track", "browser"))]
        if not data:
            return {"passed": False, "validation_method": "text_fallback", "error": "No BED data lines found"}
        fields = data[0].split("\t")
        if len(fields) < 3:
            return {"passed": False, "validation_method": "text_fallback", "error": f"BED line has only {len(fields)} fields (need ≥3)"}
        try:
            int(fields[1]); int(fields[2])
        except ValueError:
            return {"passed": False, "validation_method": "text_fallback", "error": "BED start/end are not integers"}
        return {"passed": True, "validation_method": "text_fallback", "fields_per_line": len(fields)}

    def _check_bai(self, path: Path) -> dict:
        """BAM index — check magic bytes (BAI\1 = 0x42 0x41 0x49 0x01)."""
        with open(path, "rb") as f:
            magic = f.read(4)
        if magic == b"\x42\x41\x49\x01":
            return {"passed": True, "validation_method": "magic_bytes"}
        return {"passed": False, "validation_method": "magic_bytes", "error": "BAI magic bytes not found"}

    def _check_bigwig(self, path: Path) -> dict:
        with open(path, "rb") as f:
            magic = f.read(4)
        if magic in (b"\x26\xfc\x8f\x88", b"\x88\x8f\xfc\x26"):
            return {"passed": True, "validation_method": "magic_bytes"}
        return {"passed": False, "validation_method": "magic_bytes", "error": "BigWig magic bytes not found"}

    def _check_counts_matrix(self, path: Path) -> dict:
        lines = self._head_lines(path, 5)
        data = [l for l in lines if l and not l.startswith("#")]
        if not data:
            return {"passed": False, "validation_method": "text_fallback", "error": "No non-comment lines found"}
        fields = data[0].split("\t")
        if len(fields) < 2:
            return {"passed": False, "validation_method": "text_fallback", "error": f"Counts file has only {len(fields)} columns"}
        return {"passed": True, "validation_method": "text_fallback", "columns": len(fields), "sample_header": data[0][:100]}

    def _check_gtf(self, path: Path) -> dict:
        lines = self._head_lines(path, 10)
        data = [l for l in lines if l and not l.startswith("#")]
        if not data:
            return {"passed": False, "validation_method": "text_fallback", "error": "No non-comment lines in GTF/GFF"}
        fields = data[0].split("\t")
        if len(fields) < 8:
            return {"passed": False, "validation_method": "text_fallback", "error": f"GTF/GFF line has {len(fields)} fields (need ≥8)"}
        return {"passed": True, "validation_method": "text_fallback", "sample_feature": fields[2] if len(fields) > 2 else ""}

    def _check_log(self, path: Path) -> dict:
        lines = self._head_lines(path, 5)
        return {"passed": bool(lines), "validation_method": "text_fallback", "lines": len(lines)}

    def _check_any(self, path: Path) -> dict:
        return {"passed": True, "validation_method": "exists_nonzero", "note": "Generic check — file exists and non-empty"}

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _run_tool(self, cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
        """Resolve binary: pipeline env → validators env → system PATH."""
        tool = cmd[0]
        for env in [self._env_name, self._validators_env]:
            if env:
                bin_path = self._envs_dir / env / "bin" / tool
                if bin_path.exists():
                    cmd = [str(bin_path)] + cmd[1:]
                    break
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr=str(e))

    def _max_fastq_read_length(self, path: Path, max_records: int = 1000) -> int:
        """Scan up to max_records FASTQ records and return the maximum sequence length."""
        max_len = 0
        try:
            opener = gzip.open if path.suffix in (".gz", ".bgz") else open
            with opener(path, "rt", errors="replace") as f:
                for _ in range(max_records):
                    if not f.readline():  # @header — EOF
                        break
                    seq = f.readline().rstrip()
                    f.readline()          # +
                    f.readline()          # quality
                    if seq:
                        max_len = max(max_len, len(seq))
        except Exception:
            pass
        return max_len

    def _head_lines(self, path: Path, n: int) -> list[str]:
        try:
            opener = gzip.open if path.suffix in (".gz", ".bgz") else open
            with opener(path, "rt", errors="replace") as f:
                return [f.readline().rstrip() for _ in range(n)]
        except Exception:
            return []

    @staticmethod
    def infer_type(filename: str) -> str:
        """Return the expected_type string for a filename based on its extension."""
        name = filename.lower()
        if name.endswith(".bam"):       return "bam"
        if name.endswith(".bam.bai"):   return "bai"
        if name.endswith(".bai"):       return "bai"
        if name.endswith(".sam"):       return "sam"
        if name.endswith(".vcf") or name.endswith(".vcf.gz"): return "vcf"
        if name.endswith(".bcf"):       return "bcf"
        if name.endswith(".fastq.gz") or name.endswith(".fastq") or name.endswith(".fq"): return "fastq"
        if name.endswith(".fasta") or name.endswith(".fa") or name.endswith(".fna"): return "fasta"
        if name.endswith(".bed"):       return "bed"
        if name.endswith(".bw") or name.endswith(".bigwig"): return "bigwig"
        if name.endswith(".gtf") or name.endswith(".gtf.gz"): return "gtf"
        if name.endswith(".gff") or name.endswith(".gff3"):   return "gff"
        if name.endswith(".bim"):       return "bim"
        if name.endswith(".fam"):       return "fam"
        if name.endswith(".log"):       return "log"
        if name.endswith(".txt"):       return "log"
        if name.endswith(".tsv"):       return "log"
        return "any"

    @staticmethod
    def _parse_seqkit_stats(stdout: str) -> dict | None:
        """Parse `seqkit stats -T` TSV: file format type num_seqs sum_len min_len avg_len max_len"""
        lines = stdout.strip().splitlines()
        if len(lines) < 2:
            return None
        fields = lines[1].split("\t")
        try:
            return {
                "passed": True,
                "num_seqs": int(fields[3]),
                "sum_len":  int(fields[4]),
                "min_len":  int(fields[5]),
                "avg_len":  float(fields[6]),
                "max_len":  int(fields[7]),
            }
        except (IndexError, ValueError):
            return None

    @staticmethod
    def _parse_bcftools_sn(stdout: str) -> dict:
        """Extract SN (summary numbers) section from bcftools stats output."""
        stats: dict[str, Any] = {}
        for line in stdout.splitlines():
            if line.startswith("SN"):
                parts = line.split("\t")
                if len(parts) >= 4:
                    key = parts[2].rstrip(":").strip()
                    try:
                        stats[key] = int(parts[3])
                    except ValueError:
                        stats[key] = parts[3].strip()
        return stats
