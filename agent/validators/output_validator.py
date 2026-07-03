"""
OutputValidator — verify bioinformatics output files are valid.

Tool resolution order: pipeline conda env → core_tools env → system PATH.
The core_tools env name is read from config["core_tools"]["env_name"]
(default: bioinf_core_tools); created by scripts/setup_core_test_data.sh.

Preferred validators per type:
  SAM/BAM        samtools quickcheck + flagstat
  VCF/BCF        bcftools stats
  FASTQ/FASTA    seqkit stats -T
  BED/GTF/counts text parsing (no universal lightweight tool)
  BigWig         magic bytes
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agent.skills.outcomes import proven, refused, broke


class OutputValidator:
    def __init__(self, config: dict):
        self.config = config
        self._project_root = Path(__file__).parent.parent.parent.resolve()
        self._envs_dir = self._project_root / config["paths"]["conda_envs_prefix"]
        # Core tools env (samtools / bcftools / seqkit / bwa) is created by
        # the bootstrap and is the canonical source of validator binaries.
        # Falls back to the legacy "{prefix}validators" name for older installs.
        self._core_tools_env = (
            config.get("core_tools", {}).get("env_name")
            or config["conda"]["env_prefix"] + "validators"
        )
        self._env_name: str | None = None

    def validate(
        self,
        file_path: str,
        expected_type: str,
        env_name: str | None = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        """Validate that `file_path` is a real file of `expected_type`.

        allow_empty: when True, an empty file passes validation instead of failing.
        Real-world need: some tools emit a `.err` / `.log` and write nothing to
        it on success (the empty file IS the success signal). The caller should
        set this when "empty is OK" for that file's role; default is the safer
        "empty is suspicious".
        """
        path = Path(file_path)
        self._env_name = env_name

        if not path.exists():
            return refused("validate.file_missing",
                           passed=False, file=file_path, error="File does not exist")
        if path.stat().st_size == 0:
            if allow_empty:
                return proven(
                    "validate.empty_allowed",
                    passed=True,
                    file=file_path,
                    expected_type=expected_type,
                    size_bytes=0,
                    method="empty_allowed",
                    note="empty file accepted because allow_empty=True",
                )
            return refused("validate.file_empty",
                           passed=False, file=file_path, error="File is empty")

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
            "gfa":           self._check_gfa,
            "gaf":           self._check_gfa,   # same record-type structure
            "log":           self._check_log,
            "json":          self._check_json,
            "jsonl":         self._check_jsonl,
            "html":          self._check_html,
            "tsv":           self._check_tsv,
            "csv":           self._check_csv,
            "txt":           self._check_txt,
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
            if getattr(ret, "tool_found", False):
                # samtools RAN and rejected the file — a truncated/corrupt BAM.
                # Falling back to the lenient text check here would launder a
                # real rejection into a pass (H1). Fail loudly instead.
                return broke("validate.sam_tool_rejected",
                        passed=False, validation_method="tool",
                        error=f"samtools quickcheck rejected the file: {ret.stderr.strip()[:200]}")
            return self._sam_text_fallback(path)
        stat = self._run_tool(["samtools", "flagstat", str(path)], timeout=120)
        if stat.returncode == 0:
            return proven("validate.sam_ok",
                          passed=True, validation_method="tool", flagstat=stat.stdout[:500])
        return proven("validate.sam_quickcheck_ok",
                      passed=True, validation_method="tool", note="samtools quickcheck passed")

    def _sam_text_fallback(self, path: Path) -> dict:
        lines = self._head_lines(path, 20)
        has_header = any(l.startswith("@") for l in lines)
        data_lines = [l for l in lines if l and not l.startswith("@")]
        if not data_lines and not has_header:
            return refused("validate.sam_no_records", passed=False, validation_method="text_fallback", error="No SAM header or alignment lines found")
        if data_lines and len(data_lines[0].split("\t")) < 11:
            return refused("validate.sam_bad_fields", passed=False, validation_method="text_fallback", error=f"SAM line has only {len(data_lines[0].split(chr(9)))} fields")
        return proven("validate.sam_text_ok", passed=True, validation_method="text_fallback", has_header=has_header, note="samtools unavailable — text check only")

    def _check_fastq(self, path: Path) -> dict:
        """FASTQ — seqkit stats for rich metadata, 4-line text fallback."""
        ret = self._run_tool(["seqkit", "stats", "-T", str(path)], timeout=60)
        if ret.returncode == 0:
            result = self._parse_seqkit_stats(ret.stdout) or {"passed": True, "note": "seqkit stats passed"}
            result["validation_method"] = "tool"
            return result
        if getattr(ret, "tool_found", False):
            # seqkit RAN and rejected the file (malformed FASTQ) — don't launder
            # a real rejection into a text-fallback pass (H1).
            return broke("validate.fastq_tool_rejected", passed=False, validation_method="tool",
                    error=f"seqkit stats rejected the file: {ret.stderr.strip()[:200]}")
        # Fallback: manual 4-line check
        lines = self._head_lines(path, 8)
        if len(lines) < 4:
            return refused("validate.fastq_too_few_lines", passed=False, validation_method="text_fallback", error="Fewer than 4 lines in FASTQ")
        if not lines[0].startswith("@"):
            return refused("validate.fastq_bad_header", passed=False, validation_method="text_fallback", error="FASTQ line 1 should start with '@'")
        if not lines[2].startswith("+"):
            return refused("validate.fastq_bad_sep", passed=False, validation_method="text_fallback", error="FASTQ line 3 should start with '+'")
        if len(lines[1]) != len(lines[3]):
            return refused("validate.fastq_len_mismatch", passed=False, validation_method="text_fallback", error="Sequence and quality length mismatch")
        return proven("validate.fastq_text_ok", passed=True, validation_method="text_fallback", read_length=self._max_fastq_read_length(path), note="seqkit unavailable — text check only")

    def _check_fasta(self, path: Path) -> dict:
        """FASTA — seqkit stats, header text fallback."""
        ret = self._run_tool(["seqkit", "stats", "-T", str(path)], timeout=60)
        if ret.returncode == 0:
            result = self._parse_seqkit_stats(ret.stdout) or {"passed": True, "note": "seqkit stats passed"}
            result["validation_method"] = "tool"
            return result
        if getattr(ret, "tool_found", False):
            # seqkit RAN and rejected the file (malformed FASTA) — don't launder
            # a real rejection into a text-fallback pass (H1).
            return broke("validate.fasta_tool_rejected", passed=False, validation_method="tool",
                    error=f"seqkit stats rejected the file: {ret.stderr.strip()[:200]}")
        lines = self._head_lines(path, 5)
        if not lines:
            return refused("validate.fasta_empty", passed=False, validation_method="text_fallback", error="Empty FASTA")
        if not lines[0].startswith(">"):
            return refused("validate.fasta_bad_header", passed=False, validation_method="text_fallback", error="FASTA does not start with '>'")
        return proven("validate.fasta_text_ok", passed=True, validation_method="text_fallback", first_header=lines[0][:80], note="seqkit unavailable — text check only")

    def _check_vcf(self, path: Path) -> dict:
        """VCF and BCF — bcftools stats, text fallback for plain VCF."""
        ret = self._run_tool(["bcftools", "stats", str(path)], timeout=60)
        if ret.returncode == 0:
            return proven("validate.vcf_ok", passed=True, validation_method="tool", bcftools_stats=self._parse_bcftools_sn(ret.stdout))
        if getattr(ret, "tool_found", False):
            # bcftools RAN and rejected the file (malformed VCF/BCF) — don't
            # launder a real rejection into a text-fallback pass (H1).
            return broke("validate.vcf_tool_rejected", passed=False, validation_method="tool",
                    error=f"bcftools stats rejected the file: {ret.stderr.strip()[:200]}")
        # Fallback: text check (plain VCF, bcftools not available)
        lines = self._head_lines(path, 30)
        if not any(l.startswith("##") for l in lines):
            return refused("validate.vcf_no_meta", passed=False, validation_method="text_fallback", error="VCF missing ## meta lines")
        data_lines = [l for l in lines if l and not l.startswith("#")]
        if data_lines and len(data_lines[0].split("\t")) < 8:
            return refused("validate.vcf_bad_fields", passed=False, validation_method="text_fallback", error=f"VCF data line has only {len(data_lines[0].split(chr(9)))} fields (need ≥8)")
        return proven(
            "validate.vcf_text_ok",
            passed=True,
            validation_method="text_fallback",
            has_column_header=any(l.startswith("#CHROM") for l in lines),
            data_lines_in_sample=len(data_lines),
            note="bcftools unavailable — text check only",
        )

    def _check_bed(self, path: Path) -> dict:
        lines = self._head_lines(path, 5)
        data = [l for l in lines if l and not l.startswith(("#", "track", "browser"))]
        if not data:
            return refused("validate.bed_no_data", passed=False, validation_method="text_fallback", error="No BED data lines found")
        fields = data[0].split("\t")
        if len(fields) < 3:
            return refused("validate.bed_bad_fields", passed=False, validation_method="text_fallback", error=f"BED line has only {len(fields)} fields (need ≥3)")
        try:
            int(fields[1]); int(fields[2])
        except ValueError:
            return refused("validate.bed_non_int_coords", passed=False, validation_method="text_fallback", error="BED start/end are not integers")
        return proven("validate.bed_ok", passed=True, validation_method="text_fallback", fields_per_line=len(fields))

    def _check_bai(self, path: Path) -> dict:
        """BAM index — check magic bytes (BAI\1 = 0x42 0x41 0x49 0x01)."""
        with open(path, "rb") as f:
            magic = f.read(4)
        if magic == b"\x42\x41\x49\x01":
            return proven("validate.bai_ok", passed=True, validation_method="magic_bytes")
        return refused("validate.bai_bad_magic", passed=False, validation_method="magic_bytes", error="BAI magic bytes not found")

    def _check_bigwig(self, path: Path) -> dict:
        with open(path, "rb") as f:
            magic = f.read(4)
        if magic in (b"\x26\xfc\x8f\x88", b"\x88\x8f\xfc\x26"):
            return proven("validate.bigwig_ok", passed=True, validation_method="magic_bytes")
        return refused("validate.bigwig_bad_magic", passed=False, validation_method="magic_bytes", error="BigWig magic bytes not found")

    def _check_counts_matrix(self, path: Path) -> dict:
        lines = self._head_lines(path, 5)
        data = [l for l in lines if l and not l.startswith("#")]
        if not data:
            return refused("validate.counts_no_data", passed=False, validation_method="text_fallback", error="No non-comment lines found")
        fields = data[0].split("\t")
        if len(fields) < 2:
            return refused("validate.counts_too_few_cols", passed=False, validation_method="text_fallback", error=f"Counts file has only {len(fields)} columns")
        return proven("validate.counts_ok", passed=True, validation_method="text_fallback", columns=len(fields), sample_header=data[0][:100])

    def _check_gtf(self, path: Path) -> dict:
        lines = self._head_lines(path, 10)
        data = [l for l in lines if l and not l.startswith("#")]
        if not data:
            return refused("validate.gtf_no_data", passed=False, validation_method="text_fallback", error="No non-comment lines in GTF/GFF")
        fields = data[0].split("\t")
        if len(fields) < 8:
            return refused("validate.gtf_bad_fields", passed=False, validation_method="text_fallback", error=f"GTF/GFF line has {len(fields)} fields (need ≥8)")
        return proven("validate.gtf_ok", passed=True, validation_method="text_fallback", sample_feature=fields[2] if len(fields) > 2 else "")

    def _check_gfa(self, path: Path) -> dict:
        """GFA / GAF — Graphical Fragment Assembly format."""
        lines = self._head_lines(path, 50)
        if not lines:
            return refused("validate.gfa_empty", passed=False, validation_method="text_fallback", error="Empty GFA/GAF file")
        valid_tags = {"H", "S", "L", "P", "W", "A", "J", "#"}
        data_lines = [l for l in lines if l.strip()]
        bad = [l[:30] for l in data_lines if l and l[0] not in valid_tags]
        if bad:
            return refused(
                "validate.gfa_bad_record",
                passed=False, validation_method="text_fallback",
                error=f"Unrecognised GFA record type(s): {bad[:3]}",
            )
        segment_count = sum(1 for l in data_lines if l.startswith("S"))
        return proven(
            "validate.gfa_ok",
            passed=True, validation_method="text_fallback",
            segment_count_in_sample=segment_count,
            has_header=any(l.startswith("H") for l in data_lines),
        )

    def _check_log(self, path: Path) -> dict:
        lines = self._head_lines(path, 5)
        if lines:
            return proven("validate.log_ok", passed=True,
                          validation_method="text_fallback", lines=len(lines))
        return refused("validate.log_empty", passed=False,
                       validation_method="text_fallback", lines=0)

    def _check_json(self, path: Path) -> dict:
        """Parse the file as JSON. Fails loudly if it isn't valid JSON."""
        import json
        try:
            with open(path) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            return refused("validate.json_parse_error", passed=False, validation_method="json_parse", error=str(e))
        except Exception as e:
            return refused("validate.json_read_error", passed=False, validation_method="json_parse", error=str(e))
        top_type = type(data).__name__
        return proven(
            "validate.json_ok",
            passed=True, validation_method="json_parse",
            top_type=top_type,
            top_keys=list(data.keys())[:10] if isinstance(data, dict) else None,
        )

    def _check_jsonl(self, path: Path) -> dict:
        """Parse the first N lines as JSON; each must be a valid JSON object."""
        import json
        n_ok = 0
        n_bad = 0
        first_error = None
        with open(path) as f:
            for i, line in enumerate(f):
                if i >= 20:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                    n_ok += 1
                except json.JSONDecodeError as e:
                    n_bad += 1
                    if first_error is None:
                        first_error = f"line {i+1}: {e}"
        if n_bad > 0:
            return refused("validate.jsonl_bad_lines", passed=False, validation_method="jsonl_parse",
                    lines_ok=n_ok, lines_bad=n_bad, first_error=first_error)
        if n_ok > 0:
            return proven("validate.jsonl_ok", passed=True,
                          validation_method="jsonl_parse", lines_ok=n_ok)
        return refused("validate.jsonl_empty", passed=False,
                       validation_method="jsonl_parse", lines_ok=n_ok)

    def _check_html(self, path: Path) -> dict:
        """Header probe — file must begin with <!DOCTYPE html or <html (case-insensitive)
        within the first 200 bytes. Catches `touch foo.html` (passes exists_nonzero
        but isn't HTML)."""
        try:
            head = path.open("rb").read(200).lstrip().lower()
        except Exception as e:
            return refused("validate.html_read_error", passed=False, validation_method="html_header", error=str(e))
        if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
            return proven("validate.html_ok", passed=True, validation_method="html_header")
        return refused("validate.html_no_prefix", passed=False, validation_method="html_header",
                error=f"no <!DOCTYPE html / <html prefix; first bytes: {head[:40]!r}")

    def _check_tabular(self, path: Path, sep: str, label: str) -> dict:
        """Generic tabular sanity: at least one row, consistent column count
        across the first 20 sampled rows."""
        rows = self._head_lines(path, 20)
        if not rows:
            return refused("validate.tabular_empty", passed=False, validation_method=f"{label}_parse", error="empty")
        cols = [len(r.split(sep)) for r in rows if r and not r.startswith("#")]
        if not cols:
            return refused("validate.tabular_no_rows", passed=False, validation_method=f"{label}_parse",
                    error="no data rows")
        if len(set(cols)) > 1:
            return refused("validate.tabular_ragged", passed=False, validation_method=f"{label}_parse",
                    error=f"inconsistent column counts across rows: {sorted(set(cols))[:5]}")
        return proven("validate.tabular_ok", passed=True, validation_method=f"{label}_parse",
                rows_sampled=len(rows), columns=cols[0])

    def _check_tsv(self, path: Path) -> dict:
        return self._check_tabular(path, "\t", "tsv")

    def _check_csv(self, path: Path) -> dict:
        return self._check_tabular(path, ",", "csv")

    def _check_txt(self, path: Path) -> dict:
        """Sanity: non-empty, mostly-printable text (catches a binary blob
        renamed to .txt)."""
        try:
            sample = path.open("rb").read(4096)
        except Exception as e:
            return refused("validate.txt_read_error", passed=False, validation_method="txt_probe", error=str(e))
        if not sample:
            return refused("validate.txt_empty", passed=False, validation_method="txt_probe", error="empty")
        if b"\x00" in sample:
            return refused("validate.txt_binary", passed=False, validation_method="txt_probe",
                    error="contains NUL bytes — looks binary, not text")
        return proven("validate.txt_ok", passed=True, validation_method="txt_probe", bytes_sampled=len(sample))

    def _check_any(self, path: Path) -> dict:
        return proven("validate.any_ok", passed=True, validation_method="exists_nonzero", note="Generic check — file exists and non-empty")

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _resolve_binary(self, tool: str) -> str | None:
        """Resolve a validator binary: pipeline env → core_tools env → system
        PATH. Returns the resolved path, or None if the tool is absent
        everywhere. The None case is what distinguishes "validator not
        installed" from "validator ran and rejected the file" (see _run_tool)."""
        for env in [self._env_name, self._core_tools_env]:
            if env:
                bin_path = self._envs_dir / env / "bin" / tool
                if bin_path.exists():
                    return str(bin_path)
        return shutil.which(tool)

    def _run_tool(self, cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
        """Resolve binary: pipeline env → core_tools env → system PATH.

        The returned CompletedProcess carries a `tool_found` attribute (fix
        H1): callers MUST distinguish a nonzero rc because the validator is
        ABSENT (fall back to a structural text check) from a nonzero rc
        because the validator RAN and REJECTED the file (a real failure — must
        NOT be laundered into a pass by the lenient fallback)."""
        tool = cmd[0]
        resolved = self._resolve_binary(tool)
        run_cmd = [resolved] + cmd[1:] if resolved else cmd
        try:
            # errors="replace": a validator tool can emit non-UTF-8 bytes (a
            # filename with invalid bytes, a locale-mangled message). Strict
            # decoding would raise UnicodeDecodeError and crash the validator on
            # what is really a passing/failing check — the returncode is the
            # verdict, the text is diagnostic.
            cp = subprocess.run(run_cmd, capture_output=True, text=True,
                                errors="replace", timeout=timeout)
            cp.tool_found = resolved is not None
            return cp
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            cp = subprocess.CompletedProcess(run_cmd, returncode=1, stdout="", stderr=str(e))
            # FileNotFoundError ⇒ genuinely absent; TimeoutExpired ⇒ the tool
            # exists but hung. Only the latter counts as "found".
            cp.tool_found = resolved is not None and isinstance(e, subprocess.TimeoutExpired)
            return cp

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
        if name.endswith(".gfa"):       return "gfa"
        if name.endswith(".gaf"):       return "gaf"
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
            return proven(
                "validate.seqkit_stats_ok",
                passed=True,
                num_seqs=int(fields[3]),
                sum_len=int(fields[4]),
                min_len=int(fields[5]),
                avg_len=float(fields[6]),
                max_len=int(fields[7]),
            )
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
