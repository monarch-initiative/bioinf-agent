"""
The honesty contract's first line of defense: evidence that doesn't EXERCISE
the tool must be refused. Carried verbatim from evidence.py / env_manager.verify
across the re-spine.

Cheats this catches:
  - empty string                   (nothing was checked)
  - `true`, `:`, plain numbers     (constant-true; passes without invoking
                                    the tool)
  - bare `echo ...`                (printed a string; never invoked the tool)
  - any evidence that doesn't reference the tool token as a word-boundary
    invocation                     (cannot prove the named tool was exercised)

N3 (batch-3) extended this spirit into the wrapper-tier: `command -v {wrap}`
passes when the wrapper FILE exists even if the wrapped script crashes. That
class is covered by test_wrapper_smoke_evidence.py; this file pins the
underlying shape rule that the wrapper-tier extends.

Why integration, not unit: the function IS the unit. The integration value
is exercising the FULL chain of shape rules from the public surface — empty
→ const-true → bare echo → tool-token boundary — so a regression in any
clause is caught by name. Each test maps to a stress-campaign category
agents have actually attempted (echo cheats, `true` cheats, missing tool
token).
"""
from __future__ import annotations

import pytest

from agent.skills.env_honesty import evidence_shape_violation


@pytest.mark.integration
def test_empty_evidence_refused():
    v = evidence_shape_violation("", tool="samtools")
    assert v is not None and "empty" in v.lower()


@pytest.mark.integration
def test_constant_true_cheat_refused():
    """`true` / `:` / `exit 0` / `[ 1 = 1 ]` pass without ever invoking the
    tool. All caught by the explicit const-true classifier."""
    for cheat in ("true", ":", "  true  ", "exit 0", "[ 1 = 1 ]", "test 1 = 1"):
        v = evidence_shape_violation(cheat, tool="samtools")
        assert v is not None, f"const-true cheat {cheat!r} accepted"
        assert "cheat" in v.lower() or "constant" in v.lower(), \
            f"{cheat!r} rejected but not as a const-true cheat: {v!r}"


@pytest.mark.integration
def test_bare_echo_cheat_refused():
    """`echo something` prints a string; never invokes the tool."""
    v = evidence_shape_violation("echo samtools 1.21", tool="samtools")
    assert v is not None
    assert "echo" in v.lower() or "string" in v.lower()


@pytest.mark.integration
def test_echo_with_subshell_invocation_accepted():
    """`echo $(samtools --version)` DOES invoke samtools (in the command
    substitution). The contract should NOT refuse this — the print is a
    side-effect of a real invocation."""
    ev = "echo $(samtools --version)"
    assert evidence_shape_violation(ev, tool="samtools") is None, \
        "evidence with $(...) tool invocation was wrongly refused"


@pytest.mark.integration
def test_evidence_missing_tool_token_refused():
    """`bcftools --version` cannot prove `samtools` was exercised. The shape
    rule rejects evidence that doesn't reference the named tool."""
    v = evidence_shape_violation("bcftools --version", tool="samtools")
    assert v is not None
    assert "samtools" in v
    assert "token" in v.lower() or "boundary" in v.lower() or "reference" in v.lower()


@pytest.mark.integration
def test_evidence_with_tool_substring_but_not_token_refused():
    """The rule is WORD-BOUNDARY: a tool token must appear as a whole word,
    not as part of a larger identifier. `samtoolshelper --version` does NOT
    prove `samtools` was invoked."""
    v = evidence_shape_violation("samtoolshelper --version", tool="samtools")
    assert v is not None, \
        "substring-only match (not word-boundary) was wrongly accepted"


@pytest.mark.integration
def test_legitimate_invocation_accepted():
    """`samtools --version` is a real probe — must pass."""
    assert evidence_shape_violation("samtools --version", tool="samtools") is None
    assert evidence_shape_violation("samtools view -h /tmp/x.bam",
                                    tool="samtools") is None


# ---------------------------------------------------------------------------
# Evidence DEPTH — the disclosure sibling of the shape rule. Not a gate: it
# classifies how deeply evidence exercises the tool so a shallow proof (presence
# only) can't read as a functional run — the honesty lever the Talos
# reconstruction slipped past (imported clean, but didn't RUN).
# ---------------------------------------------------------------------------
from agent.skills.env_honesty import evidence_depth, is_shallow_evidence  # noqa: E402


@pytest.mark.parametrize("ev,tool,expected", [
    ("samtools --version", "samtools", "version"),
    ("bcftools --version", "bcftools", "version"),
    ('python -c "import talos"', "talos", "import"),
    ("perl -MBio::DB::HTS -e1", "x", "import"),
    ("mytool --help", "mytool", "help"),
    ("python -m talos.validate_moi --help", "talos", "help"),
    ("samtools sort -o /tmp/out.bam /data/in.bam", "samtools", "functional"),
    ("bwa 2>&1 | head", "bwa", "functional"),
    ("seqkit stats /data/reads.fq", "seqkit", "functional"),
    ("mytool", "mytool", "smoke"),
])
def test_evidence_depth_classifies(ev, tool, expected):
    assert evidence_depth(ev, tool) == expected


def test_shallow_flags_presence_only_not_functional():
    # version/import/help prove PRESENCE; smoke/functional prove it RUNS.
    assert is_shallow_evidence("samtools --version", "samtools") is True
    assert is_shallow_evidence('python -c "import x"', "x") is True
    assert is_shallow_evidence("tool --help", "tool") is True
    assert is_shallow_evidence("samtools view -c /data/in.bam", "samtools") is False
    assert is_shallow_evidence("bwa 2>&1 | head", "bwa") is False


def test_depth_is_disclosure_not_a_gate():
    # A shallow proof is NOT a shape violation — it still validly references the tool.
    # The two checks are orthogonal: shape rejects cheats; depth discloses thinness.
    assert evidence_shape_violation("samtools --version", "samtools") is None
    assert is_shallow_evidence("samtools --version", "samtools") is True
