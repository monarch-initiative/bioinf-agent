"""C1 — false-green certification of the OutputValidator `_ok` terminals.

The validators are the honesty-critical core of Layer 2: seal's I3/I4 rest on
"this output is a real file of the expected type". A false green here means an
autonomous agent trusts corrupt/empty output and proceeds — the exact failure
full-auto must never hit. So every `_ok` proven gets TWO adversarial anchors:

  1. VALID fixture   → outcome=proven, passed=True, the specific `_ok` code
     (certifies the proven line — executed AND named)
  2. MALFORMED fixture → outcome != proven, passed=False
     (the C1 defense: a plausible-but-wrong file cannot fake the green)

The tool-backed validators (samtools/bcftools/seqkit) are driven with a mocked
`_run_tool` so the test is deterministic regardless of what's installed — both
the tool-absent text fallback AND the tool-success path are exercised.
"""
from __future__ import annotations

import subprocess

import pytest

from agent.validators.output_validator import OutputValidator


def _validator() -> OutputValidator:
    return OutputValidator({
        "paths": {"conda_envs_prefix": "envs/"},
        "conda": {"env_prefix": "bioinf_"},
        "core_tools": {"env_name": "bioinf_core_tools"},
    })


def _fake_run(returncode: int, *, tool_found: bool, stdout: str = "", stderr: str = ""):
    def _run(cmd, timeout=60):
        cp = subprocess.CompletedProcess(cmd, returncode=returncode, stdout=stdout, stderr=stderr)
        cp.tool_found = tool_found
        return cp
    return _run


def _seq_run(results):
    """A sequenced _run_tool: yields (returncode, stdout) per call, tool_found=True."""
    it = iter(results)

    def _run(cmd, timeout=60):
        rc, out = next(it)
        cp = subprocess.CompletedProcess(cmd, returncode=rc, stdout=out, stderr="")
        cp.tool_found = True
        return cp
    return _run


# ---------------------------------------------------------------------------
# The table: (id, expected_type, valid_bytes, ok_code, malformed_bytes|None).
# `_run_tool` is forced ABSENT for every row, so tool-backed types (sam/fastq/
# fasta/vcf) take their structural text fallback deterministically; pure-python
# validators ignore it.
# ---------------------------------------------------------------------------
_CASES = [
    ("sam_text",  "sam",
     b"@HD\tVN:1.6\nr1\t0\tchr1\t1\t60\t10M\t*\t0\t0\tACGTACGTAC\tIIIIIIIIII\n",
     "validate.sam_text_ok",
     b"this is not a sam file at all\n"),
    ("fastq_text", "fastq",
     b"@r1\nACGT\n+\nIIII\n", "validate.fastq_text_ok",
     b"@r1\nACGT\n+\nII\n"),                          # seq/qual length mismatch
    ("fasta_text", "fasta",
     b">seq1\nACGTACGT\n", "validate.fasta_text_ok",
     b"no header here\nACGT\n"),                      # missing '>'
    ("vcf_text", "vcf",
     b"##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
     b"chr1\t1\t.\tA\tT\t.\t.\t.\n", "validate.vcf_text_ok",
     b"chr1\t1\t.\tA\tT\n"),                          # no ## meta
    ("bed", "bed",
     b"chr1\t0\t100\n", "validate.bed_ok",
     b"chr1\tstart\tend\n"),                          # non-int coords
    ("bai", "bai",
     b"\x42\x41\x49\x01and-the-rest", "validate.bai_ok",
     b"XXXXand-the-rest"),                            # wrong magic
    ("bigwig", "bigwig",
     b"\x26\xfc\x8f\x88payload", "validate.bigwig_ok",
     b"XXXXpayload"),                                 # wrong magic
    ("counts", "counts_matrix",
     b"gene\ts1\ts2\nG1\t5\t9\n", "validate.counts_ok",
     b"singlecolumn\n"),                              # <2 columns
    ("gtf", "gtf",
     b"chr1\tsrc\texon\t1\t100\t.\t+\t.\tgene_id \"g1\";\n", "validate.gtf_ok",
     b"chr1\tsrc\texon\n"),                           # <8 fields
    ("gfa", "gfa",
     b"H\tVN:Z:1.0\nS\t1\tACGT\n", "validate.gfa_ok",
     b"Z\tnot a gfa record\n"),                       # unknown record type
    ("log", "log",
     b"some log line\n", "validate.log_ok", None),    # empty is caught before dispatch
    ("json", "json",
     b'{"a": 1, "b": 2}', "validate.json_ok",
     b"{not valid json"),
    ("jsonl", "jsonl",
     b'{"a":1}\n{"b":2}\n', "validate.jsonl_ok",
     b'{"a":1}\n{bad}\n'),
    ("html", "html",
     b"<!DOCTYPE html><html><body>hi</body></html>", "validate.html_ok",
     b"just some text, definitely not html"),         # the `touch foo.html` attack
    ("tabular", "tsv",
     b"a\tb\tc\nd\te\tf\n", "validate.tabular_ok",
     b"a\tb\tc\nd\te\n"),                             # ragged columns
    ("txt", "txt",
     b"hello world, printable text\n", "validate.txt_ok",
     b"binary\x00blob\x00here"),                      # NUL bytes → looks binary
    ("any", "any",
     b"anything at all", "validate.any_ok", None),    # the deliberate escape hatch
]


@pytest.mark.parametrize("cid,etype,valid,ok_code,_bad",
                         _CASES, ids=[c[0] for c in _CASES])
def test_validator_ok_on_valid_file(tmp_path, monkeypatch, cid, etype, valid, ok_code, _bad):
    """A VALID file of the type must return proven with the specific _ok code —
    the green is real (executed + named), not a rubber stamp."""
    v = _validator()
    monkeypatch.setattr(v, "_run_tool", _fake_run(1, tool_found=False))  # force text path
    p = tmp_path / f"{cid}.out"
    p.write_bytes(valid)
    res = v.validate(str(p), etype)
    assert res.get("outcome") == "proven", res
    assert res.get("passed") is True, res
    assert res.get("code") == ok_code, res


@pytest.mark.parametrize("cid,etype,_valid,ok_code,bad",
                         [c for c in _CASES if c[4] is not None],
                         ids=[c[0] for c in _CASES if c[4] is not None])
def test_validator_rejects_malformed_file(tmp_path, monkeypatch, cid, etype, _valid, ok_code, bad):
    """C1 false-green defense: a plausible-but-wrong file of the SAME type must
    NOT return the proven green — the validator rejects (refused/broke), passed
    is False. This is what stops a corrupt output from being trusted under
    full-auto."""
    v = _validator()
    monkeypatch.setattr(v, "_run_tool", _fake_run(1, tool_found=False))
    p = tmp_path / f"{cid}.bad"
    p.write_bytes(bad)
    res = v.validate(str(p), etype)
    assert res.get("outcome") != "proven", f"malformed {etype} faked a green: {res}"
    assert res.get("passed") is False, res
    assert res.get("code") != ok_code, res


# ---------------------------------------------------------------------------
# Tool-SUCCESS paths — the proven terminals reached only when the real tool
# (samtools/bcftools/seqkit) runs and accepts the file. Mocked so deterministic.
# ---------------------------------------------------------------------------

def test_sam_ok_via_samtools_success(tmp_path, monkeypatch):
    """samtools quickcheck AND flagstat both green → validate.sam_ok."""
    v = _validator()
    monkeypatch.setattr(v, "_run_tool",
                        _seq_run([(0, ""), (0, "100 + 0 in total\n")]))  # quickcheck, flagstat
    p = tmp_path / "aln.bam"
    p.write_bytes(b"BAM\x01 pretend bam bytes")
    res = v.validate(str(p), "bam")
    assert res.get("outcome") == "proven" and res.get("code") == "validate.sam_ok", res


def test_sam_quickcheck_ok_when_flagstat_fails(tmp_path, monkeypatch):
    """quickcheck passes but flagstat fails → the weaker-but-honest
    validate.sam_quickcheck_ok green (still a real tool pass, not a fallback)."""
    v = _validator()
    monkeypatch.setattr(v, "_run_tool",
                        _seq_run([(0, ""), (1, "")]))   # quickcheck ok, flagstat fails
    p = tmp_path / "aln.bam"
    p.write_bytes(b"BAM\x01 pretend bam bytes")
    res = v.validate(str(p), "bam")
    assert res.get("outcome") == "proven" and res.get("code") == "validate.sam_quickcheck_ok", res


def test_vcf_ok_via_bcftools_success(tmp_path, monkeypatch):
    """bcftools stats green → validate.vcf_ok (the tool path, not text fallback)."""
    v = _validator()
    monkeypatch.setattr(v, "_run_tool",
                        _fake_run(0, tool_found=True,
                                  stdout="SN\t0\tnumber of records:\t5\n"))
    p = tmp_path / "calls.vcf"
    p.write_bytes(b"##fileformat=VCFv4.2\n#CHROM\tPOS\n")
    res = v.validate(str(p), "vcf")
    assert res.get("outcome") == "proven" and res.get("code") == "validate.vcf_ok", res


def test_seqkit_stats_ok_via_seqkit_success(tmp_path, monkeypatch):
    """seqkit stats -T green with a parseable TSV → validate.seqkit_stats_ok
    (the rich-metadata tool path for FASTQ/FASTA)."""
    v = _validator()
    tsv = ("file\tformat\ttype\tnum_seqs\tsum_len\tmin_len\tavg_len\tmax_len\n"
           "r.fastq\tFASTQ\tDNA\t10\t1000\t100\t100.0\t100\n")
    monkeypatch.setattr(v, "_run_tool", _fake_run(0, tool_found=True, stdout=tsv))
    p = tmp_path / "r.fastq"
    p.write_bytes(b"@r1\nACGT\n+\nIIII\n")
    res = v.validate(str(p), "fastq")
    assert res.get("outcome") == "proven" and res.get("code") == "validate.seqkit_stats_ok", res


# ---------------------------------------------------------------------------
# empty_allowed — the one place an empty file is a legitimate success signal
# (a tool that writes nothing to a .log on success). Must be OPT-IN.
# ---------------------------------------------------------------------------

def test_empty_allowed_only_with_flag(tmp_path):
    """An empty file passes ONLY when allow_empty=True (validate.empty_allowed);
    without the flag the same empty file is refused (validate.file_empty). The
    green must be opt-in, never the default for a silent-empty output."""
    v = _validator()
    p = tmp_path / "run.log"
    p.write_bytes(b"")
    ok = v.validate(str(p), "log", allow_empty=True)
    assert ok.get("outcome") == "proven" and ok.get("code") == "validate.empty_allowed", ok
    refused = v.validate(str(p), "log")
    assert refused.get("outcome") == "refused" and refused.get("code") == "validate.file_empty", refused


# ---------------------------------------------------------------------------
# Long comment/meta header (VEP-probe finding #3): a tabular validator must
# stream PAST a comment header of any length. VEP --tab emits ~30 '##' lines
# before data; the old head(20) window sampled only comments and FALSELY
# rejected a valid annotated file as 'no data rows' (a validator false-negative
# — a real output the agent then can't trust). Regression-locked here.
# ---------------------------------------------------------------------------
def test_tabular_streams_past_long_comment_header(tmp_path):
    v = _validator()
    p = tmp_path / "vep_out.tsv"
    header = "\n".join(f"## meta line {i}" for i in range(29))          # 29 '##' lines
    colhdr = "#Uploaded_variation\tLocation\tAllele\tConsequence"        # '#' column header
    data = "\n".join(f"v{i}\tchr22:{1000+i}\tG\tmissense_variant" for i in range(12))
    p.write_text(header + "\n" + colhdr + "\n" + data + "\n")
    r = v.validate(str(p), "tsv")
    assert r.get("outcome") == "proven" and r.get("passed") is True, r
    assert r.get("code") == "validate.tabular_ok"
    assert r.get("columns") == 4 and r.get("rows_sampled") == 12, r


def test_tabular_comment_only_still_fails(tmp_path):
    """A file that is ALL comment/meta and no data rows must NOT pass — the fix
    widens the data search, it must not turn a genuinely empty-of-data file green."""
    v = _validator()
    p = tmp_path / "hdr_only.tsv"
    p.write_text("\n".join(f"## only meta {i}" for i in range(40)) + "\n")
    r = v.validate(str(p), "tsv")
    assert r.get("outcome") != "proven" and r.get("passed") is False, r
    assert r.get("code") == "validate.tabular_no_rows", r
