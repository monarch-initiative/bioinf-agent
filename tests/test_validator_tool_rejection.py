"""H1 — the validator must distinguish "tool ABSENT" from "tool RAN and
REJECTED the file".

Before the fix, `_run_tool` returned rc=1 in both cases, so a type-aware
checker (samtools/bcftools/seqkit) that actively REJECTED a corrupt file
fell through to the lenient text fallback — which could then PASS it. That
laundered a real rejection into a "passed" record wearing the honesty
badge. The fix tags each `_run_tool` result with `tool_found`: a nonzero rc
with the tool present is a genuine failure (passed=False, no fallback); only
a genuinely-absent tool falls back to the structural text check.
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


def _fake_run(returncode: int, *, tool_found: bool, stderr: str = ""):
    def _run(cmd, timeout=60):
        cp = subprocess.CompletedProcess(cmd, returncode=returncode,
                                         stdout="", stderr=stderr)
        cp.tool_found = tool_found
        return cp
    return _run


# ---------------------------------------------------------------------------
# The headline: tool present + rejected ⇒ passed=False, NO text fallback.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("checker,badfile", [
    ("_check_sam", "x.bam"),
    ("_check_vcf", "x.vcf"),
    ("_check_fastq", "x.fastq"),
    ("_check_fasta", "x.fasta"),
])
def test_tool_ran_and_rejected_does_not_fall_back(tmp_path, monkeypatch, checker, badfile):
    v = _validator()
    monkeypatch.setattr(v, "_run_tool",
                        _fake_run(1, tool_found=True, stderr="corrupt / truncated"))
    p = tmp_path / badfile
    # Content that WOULD pass a lenient text check, to prove the fallback is
    # not what's deciding the verdict here.
    p.write_text("@SQ\tSN:chr1\tLN:1000\nr1\t0\tchr1\t1\t60\t10M\t*\t0\t0\tAAAAAAAAAA\tIIIIIIIIII\n")
    result = getattr(v, checker)(p)
    assert result["passed"] is False, \
        f"{checker}: a tool that RAN and rejected the file must fail, not fall back: {result}"
    assert result["validation_method"] == "tool", \
        f"{checker}: rejection must be attributed to the tool, not the text fallback: {result}"


# ---------------------------------------------------------------------------
# The complement: tool genuinely absent ⇒ structural text fallback still runs.
# ---------------------------------------------------------------------------

def test_tool_absent_falls_back_to_text_check(tmp_path, monkeypatch):
    v = _validator()
    monkeypatch.setattr(v, "_run_tool", _fake_run(1, tool_found=False))
    sam = tmp_path / "ok.sam"
    sam.write_text("@HD\tVN:1.6\nr1\t0\tchr1\t1\t60\t10M\t*\t0\t0\tACGTACGTAC\tIIIIIIIIII\n")
    result = v._check_sam(sam)
    assert result["passed"] is True, f"valid SAM should pass the text fallback: {result}"
    assert result["validation_method"] == "text_fallback"


def test_tool_absent_text_fallback_still_rejects_garbage(tmp_path, monkeypatch):
    """The fallback is a real check, not a rubber stamp: a file with neither a
    SAM header nor alignment columns fails even when samtools is absent."""
    v = _validator()
    monkeypatch.setattr(v, "_run_tool", _fake_run(1, tool_found=False))
    junk = tmp_path / "junk.sam"
    junk.write_text("this is not a sam file at all\n")
    result = v._check_sam(junk)
    assert result["passed"] is False, f"garbage should fail even without samtools: {result}"


# ---------------------------------------------------------------------------
# The one quickcheck complaint that is NOT a defect: "no targets in header".
#
# `picard FastqToSam` and `samtools import` produce an UNALIGNED bam, which by
# definition declares no @SQ targets. quickcheck scores that 8 and exits
# nonzero, so the clause above rejected every uBAM ever produced — a real,
# validated, 20k-record file recorded as `passed: False`, which I3 then refuses
# to seal. Found by running picard on a cluster; uBAM is the raw-read format
# GATK best practices are built on, so this was not an exotic corner.
#
# The bits COMPOSE, and that is what makes accepting 8 safe: a truncated uBAM
# scores 8|16 = 24, never 8.
# ---------------------------------------------------------------------------

def _fake_run_sequence(*results):
    """Return a _run_tool stub that answers successive calls from `results`,
    each a (returncode, stdout) pair. Lets a test drive quickcheck and the
    view -H / view -c readback independently."""
    calls = {"n": 0}

    def _run(cmd, timeout=60):
        rc, out = results[min(calls["n"], len(results) - 1)]
        calls["n"] += 1
        cp = subprocess.CompletedProcess(cmd, returncode=rc, stdout=out, stderr="")
        cp.tool_found = True
        return cp
    return _run


def test_unaligned_bam_passes_when_header_and_records_read_back(tmp_path, monkeypatch):
    """quickcheck says only "no targets"; the readback proves it is real."""
    v = _validator()
    monkeypatch.setattr(v, "_run_tool", _fake_run_sequence(
        (8, ""),                       # quickcheck: no targets in header
        (0, "@HD\tVN:1.6\n@RG\tID:A"),  # samtools view -H
        (0, "20000\n"),                # samtools view -c
    ))
    result = v._check_sam(tmp_path / "unaligned.bam")
    assert result["passed"] is True, f"a valid unaligned BAM must validate: {result}"
    assert result["code"] == "validate.sam_unaligned_ok", result
    assert result["records"] == 20000, result


def test_truncated_unaligned_bam_is_rejected_at_the_bitmask_not_the_readback(
        tmp_path, monkeypatch):
    """THE safety property, isolated. A truncated uBAM scores 8|16 = 24, and the
    `== 8` gate must reject it OUTRIGHT rather than sending it to the lenient
    path.

    The readback here is stubbed to SUCCEED, which a real truncated file's
    record count would not. That is deliberate and is the whole point of the
    test: it strips away the second line of defence so the bitmask gate is the
    only thing that can produce the verdict. Loosen the gate to a bitwise
    `rc & 8` and this test fails — whereas a test that let the readback do the
    rejecting would pass either way and pin nothing.
    """
    v = _validator()
    monkeypatch.setattr(v, "_run_tool", _fake_run_sequence(
        (24, ""),                      # quickcheck: no targets AND missing EOF
        (0, "@HD\tVN:1.6\n@RG\tID:A"),  # header would read back...
        (0, "20000\n"),                # ...and so would a count
    ))
    result = v._check_sam(tmp_path / "truncated_unaligned.bam")
    assert result["passed"] is False, \
        f"a TRUNCATED unaligned BAM must be rejected by the bitmask gate: {result}"
    assert result["code"] == "validate.sam_tool_rejected", result


def test_unaligned_bam_with_unreadable_header_is_rejected(tmp_path, monkeypatch):
    """The readback is a real check, not a formality: quickcheck's silence on
    the other bits is not taken as proof the file is readable."""
    v = _validator()
    monkeypatch.setattr(v, "_run_tool", _fake_run_sequence(
        (8, ""),      # quickcheck: no targets
        (1, ""),      # samtools view -H fails
    ))
    result = v._check_sam(tmp_path / "unaligned.bam")
    assert result["passed"] is False, result
    assert result["code"] == "validate.sam_tool_rejected", result


def test_unaligned_bam_with_zero_records_is_refused(tmp_path, monkeypatch):
    """An empty output that exits 0 is the silent-empty-success trap; `touch`
    would clear any bar lower than this."""
    v = _validator()
    monkeypatch.setattr(v, "_run_tool", _fake_run_sequence(
        (8, ""),                 # quickcheck: no targets
        (0, "@HD\tVN:1.6"),      # header reads back
        (0, "0\n"),              # ...but there are no records
    ))
    result = v._check_sam(tmp_path / "empty_unaligned.bam")
    assert result["passed"] is False, result
    assert result["code"] == "validate.sam_no_records", result


def _samtools_available() -> bool:
    return bool(getattr(_validator()._run_tool(["samtools", "--version"]),
                        "tool_found", False))


@pytest.mark.skipif(not _samtools_available(),
                    reason="needs the real samtools from the core_tools env")
def test_real_unaligned_bam_validates_and_real_truncation_does_not(tmp_path):
    """The mocked tests above pin the BRANCHING; this one pins the PREMISE —
    that a real samtools really does score an unaligned BAM 8 and a truncated
    one 24. If a future samtools renumbers those bits, the mocks would all
    still pass while production silently rejected every uBAM again."""
    v = _validator()
    sam = tmp_path / "mini.sam"
    sam.write_text(
        "@HD\tVN:1.6\tSO:queryname\n"
        "@RG\tID:A\tSM:s1\n"
        "r1\t77\t*\t0\t0\t*\t*\t0\t0\tACGTACGTAC\tIIIIIIIIII\tRG:Z:A\n"
        "r1\t141\t*\t0\t0\t*\t*\t0\t0\tTGCATGCATG\tIIIIIIIIII\tRG:Z:A\n")
    bam = tmp_path / "mini_unaligned.bam"
    conv = v._run_tool(["samtools", "view", "-b", "-o", str(bam), str(sam)])
    assert conv.returncode == 0 and bam.exists(), \
        f"could not build the fixture uBAM: {conv.stderr[:300]}"

    ok = v._check_sam(bam)
    assert ok["passed"] is True, f"a REAL unaligned BAM must validate: {ok}"
    assert ok["code"] == "validate.sam_unaligned_ok", ok
    assert ok["records"] == 2, ok

    truncated = tmp_path / "mini_truncated.bam"
    truncated.write_bytes(bam.read_bytes()[:len(bam.read_bytes()) // 2])
    bad = v._check_sam(truncated)
    assert bad["passed"] is False, \
        f"a REAL truncated unaligned BAM must NOT validate: {bad}"


# ---------------------------------------------------------------------------
# The wiring: _resolve_binary/_run_tool actually set tool_found honestly for a
# tool that exists nowhere.
# ---------------------------------------------------------------------------

def test_run_tool_sets_tool_found_false_for_absent_binary():
    v = _validator()
    cp = v._run_tool(["definitely_not_a_real_tool_zzz", "--version"])
    assert cp.tool_found is False
    assert cp.returncode != 0
