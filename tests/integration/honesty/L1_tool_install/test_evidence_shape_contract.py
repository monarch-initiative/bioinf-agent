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
    ("seqkit stats /data/reads.fq", "seqkit", "functional"),
    ("mytool", "mytool", "smoke"),
    ("", "mytool", "unknown"),          # decline to guess rather than assume 'smoke'
    ("tool --frobnicate", "other", "unknown"),
])
def test_evidence_depth_classifies(ev, tool, expected):
    assert evidence_depth(ev, tool) == expected


@pytest.mark.parametrize("ev,tool", [
    ("command -v samtools", "samtools"),          # PATH lookup — never runs the tool
    ("which bwa", "bwa"),
    ('python -c "import importlib.metadata as _m; _m.version(\'cyvcf2\')"', "cyvcf2"),
])
def test_presence_probes_are_named_presence_not_promoted(ev, tool):
    """A PATH/metadata lookup is the WEAKEST evidence there is and must say so.

    `command -v samtools` used to classify as 'version' — the regex matched the `-v` of
    `command -v` — and `_conda_presence_check(...)`, which is freeze's own auto-generated
    probe and therefore the evidence on nearly every real record, classified as
    'functional' because the path `/opt/conda/envs/*/conda-meta/*.json` matched "reads a
    file". The weakest evidence in the system was reporting as the strongest.
    """
    assert evidence_depth(ev, tool) == "presence"
    assert is_shallow_evidence(ev, tool) is True


@pytest.mark.parametrize("ev,tool,expected", [
    ("samtools --version | head -1", "samtools", "version"),
    ("samtools --version 2>&1", "samtools", "version"),
    ("bwa 2>&1 | head", "bwa", "smoke"),          # a bare invocation + plumbing is a smoke
    ("mytool --help | cat", "mytool", "help"),
    ("pigz --version > /dev/null", "pigz", "version"),
])
def test_output_plumbing_does_not_promote_depth(ev, tool, expected):
    """`| head`, `| cat`, `2>&1`, `> /dev/null` shape a command's OUTPUT and say nothing
    about what it does. They used to promote anything to 'functional' — one pipe
    outranking the thing being piped, and a one-character bypass for any depth rule."""
    assert evidence_depth(ev, tool) == expected


def test_shallow_flags_presence_only_not_functional():
    # presence/version/import/help prove the tool is there; smoke/functional prove it RUNS.
    assert is_shallow_evidence("samtools --version", "samtools") is True
    assert is_shallow_evidence('python -c "import x"', "x") is True
    assert is_shallow_evidence("tool --help", "tool") is True
    assert is_shallow_evidence("samtools view -c /data/in.bam", "samtools") is False
    # 'unknown' is NOT shallow — declining to classify is not evidence of shallowness.
    assert is_shallow_evidence("", "mytool") is False


def test_depth_is_disclosure_not_a_gate():
    # A shallow proof is NOT a shape violation — it still validly references the tool.
    # The two checks are orthogonal: shape rejects cheats; depth discloses thinness.
    assert evidence_shape_violation("samtools --version", "samtools") is None
    assert is_shallow_evidence("samtools --version", "samtools") is True


def test_freezes_own_auto_generated_evidence_is_classified_honestly():
    """The depth of freeze's OWN probes decides whether the disclosure means anything —
    they are the evidence on nearly every real record, so getting them wrong makes the
    report wrong for almost every env.

    This also pins WHY depth must never become a gate: the conda probe reports 'presence'
    (weakest) and the pip probe reports 'presence' too. A gate on shallow would refuse
    freeze's own defaults on pip while passing conda — no benefit where it matters,
    breakage where it doesn't.
    """
    from agent.skills.env_freeze import _conda_presence_check, _pip_presence_check
    assert evidence_depth(_conda_presence_check("pigz"), "pigz") == "presence"
    assert evidence_depth(_pip_presence_check("cyvcf2"), "cyvcf2") == "presence"


def test_nothing_in_the_contract_gates_on_depth():
    """check_build must NOT refuse on shallow evidence. Measured (audit 2026-07-16 Tier 2):
    a depth gate refuses the flagship-CORRECT artifact and the known-BROKEN one
    identically — `talos_authors` (which really does carry the bcftools fork) is
    [help, version, version] and the broken `talos_v11` reconstruction is [import]: all
    shallow. Zero discriminating power on the exact war story depth was proposed to catch.
    """
    from agent.skills.env_honesty import check_build
    record = {
        "image": "x:1", "image_digest": "sha256:abc",
        # the real talos_authors evidence set — all shallow, and CORRECT
        "verifications": [
            {"label": "talos", "tool": "talos", "check": "python -m talos.validate_moi --help", "passed": True},
            {"label": "bcftools", "tool": "bcftools", "check": "bcftools --version", "passed": True},
            {"label": "echtvar", "tool": "echtvar", "check": "echtvar --version", "passed": True},
        ],
    }
    assert all(is_shallow_evidence(v["check"], v["tool"]) for v in record["verifications"])
    assert check_build(record) == [], "shallow evidence must not refuse a build"
