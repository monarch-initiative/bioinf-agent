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
# The wiring: _resolve_binary/_run_tool actually set tool_found honestly for a
# tool that exists nowhere.
# ---------------------------------------------------------------------------

def test_run_tool_sets_tool_found_false_for_absent_binary():
    v = _validator()
    cp = v._run_tool(["definitely_not_a_real_tool_zzz", "--version"])
    assert cp.tool_found is False
    assert cp.returncode != 0
