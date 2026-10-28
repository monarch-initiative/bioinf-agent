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
# Evidence KIND — the disclosure sibling of the shape rule. Not a gate: it says
# whether a check RAN the tool or only FOUND it present, so a presence probe can
# never read as a run on the ENV report (the honesty lever the Talos reconstruction
# slipped past: it imported clean, it never ran).
# ---------------------------------------------------------------------------
from agent.skills.env_honesty import EVIDENCE_FOUND, EVIDENCE_RAN, evidence_kind  # noqa: E402


@pytest.mark.parametrize("ev,tool,expected", [
    # the tool is the command word: it executed, whatever it was asked
    ("samtools --version", "samtools", EVIDENCE_RAN),
    ("bcftools --version", "bcftools", EVIDENCE_RAN),
    ("mytool --help", "mytool", EVIDENCE_RAN),
    ("mytool", "mytool", EVIDENCE_RAN),
    ("samtools sort -o /tmp/out.bam /data/in.bam", "samtools", EVIDENCE_RAN),
    ("seqkit stats /data/reads.fq", "seqkit", EVIDENCE_RAN),
    ("samtools --version | head -1", "samtools", EVIDENCE_RAN),
    ("samtools --version 2>&1", "samtools", EVIDENCE_RAN),
    ("bwa 2>&1 | head", "bwa", EVIDENCE_RAN),
    ("time bwa > /tmp/h.txt 2>&1", "bwa", EVIDENCE_RAN),
    ("gunzip < /data/x.gz > /tmp/y.txt", "gunzip", EVIDENCE_RAN),
    ("pigz --version > /dev/null", "pigz", EVIDENCE_RAN),
    ("run_deepvariant --version", "deepvariant", EVIDENCE_RAN),      # a name that carries the token
    # an interpreter runs the tool's module, script or jar
    ("python -m talos.validate_moi --help", "talos", EVIDENCE_RAN),
    ("python /opt/deepvariant/bin/run_deepvariant.py --version", "deepvariant", EVIDENCE_RAN),
    ("java -jar /opt/exomiser/exomiser-cli-15.1.0.jar --help", "exomiser", EVIDENCE_RAN),
    # a load with nothing after it, a load followed by a print, a metadata lookup: found
    ('python -c "import talos"', "talos", EVIDENCE_FOUND),
    ('python -c "import talos; print(\'talos ok\')"', "talos", EVIDENCE_FOUND),
    ('python -c "import spliceai; print(spliceai.__version__)"', "spliceai", EVIDENCE_FOUND),
    ("perl -MBio::DB::HTS -e1", "Bio::DB::HTS", EVIDENCE_FOUND),
    # a load, then the tool is called: ran
    ("Rscript -e 'library(DESeq2); dds <- DESeq(dds)'", "bioconductor-deseq2", EVIDENCE_RAN),
    ("Rscript -e 'library(DESeq2); sessionInfo()'", "bioconductor-deseq2", EVIDENCE_FOUND),
    # the tool's name only inside a string handed to a file read: found
    ('python3 -c "compile(open(\'/opt/deepvariant/bin/run_deepvariant.py\').read(), \'x\', \'exec\')"',
     "deepvariant", EVIDENCE_FOUND),
    # nothing to classify, or a tool that is not the one asked about
    ("", "mytool", EVIDENCE_FOUND),
    ("tool --frobnicate", "other", EVIDENCE_FOUND),
])
def test_evidence_kind_classifies(ev, tool, expected):
    assert evidence_kind(ev, tool) == expected


@pytest.mark.parametrize("ev,tool", [
    ("command -v samtools", "samtools"),          # PATH lookup — never runs the tool
    ("which bwa", "bwa"),
    ('python -c "import importlib.metadata as _m; _m.version(\'cyvcf2\')"', "cyvcf2"),
    ("grep -q apollo /etc/apollo.conf", "apollo"),
    ("unzip -p /apollo/apollo.war META-INF/MANIFEST.MF > /tmp/m.txt 2>&1 && grep -q apollo /tmp/m.txt", "apollo"),
])
def test_lookups_and_file_reads_only_find_the_tool(ev, tool):
    """A PATH or metadata lookup and a file read are the WEAKEST evidence there is and
    must say so: `command -v samtools` once classified as a version probe (the `-v`), and
    freeze's own conda probe as a functional run (its conda-meta path read as data)."""
    assert evidence_kind(ev, tool) == EVIDENCE_FOUND


def test_kind_is_disclosure_not_a_gate():
    # A check that only finds the tool is NOT a shape violation — it still validly
    # references the tool. The two checks are orthogonal: shape rejects cheats; kind
    # discloses what the check did.
    assert evidence_shape_violation("command -v samtools", "samtools") is None
    assert evidence_kind("command -v samtools", "samtools") == EVIDENCE_FOUND


def test_freezes_own_auto_generated_evidence_is_classified_honestly():
    """freeze's OWN probes are the evidence on nearly every real record, so getting them
    wrong makes the report wrong for almost every env: both only FIND the tool."""
    from agent.skills.env_freeze import _conda_presence_check, _pip_presence_check
    assert evidence_kind(_conda_presence_check("pigz"), "pigz") == EVIDENCE_FOUND
    assert evidence_kind(_pip_presence_check("cyvcf2"), "cyvcf2") == EVIDENCE_FOUND


def test_nothing_in_the_contract_gates_on_the_kind():
    """check_build must NOT refuse evidence that only finds the tool: a string reading
    cannot tell the correct artifact from the broken one, so it is disclosed on the ENV
    report ("found present · not exercised") and never load-bearing."""
    from agent.skills.env_honesty import check_build
    record = {
        "image": "x:1", "image_digest": "sha256:abc", "image_arch": "amd64", "platform": "linux/amd64",
        "verifications": [
            {"label": "samtools", "tool": "samtools", "check": "command -v samtools", "passed": True,
             "control": "discriminating"},
        ],
    }
    assert evidence_kind("command -v samtools", "samtools") == EVIDENCE_FOUND
    assert check_build(record) == [], "evidence that only finds the tool must not refuse a build"


# ---------------------------------------------------------------------------
# The tool's own exit status counts
# ---------------------------------------------------------------------------

def test_a_semicolon_between_the_tool_and_the_grep_is_refused_with_both_honest_forms():
    """`tool … > f 2>&1; grep P f`: the `;` discards the tool's exit status, so a tool that
    crashed after printing the pattern passes (DeepVariant's SIGILL banner matched the
    grep). The remedy names the `&&` form and the `rc=$?` form."""
    why = evidence_shape_violation("run_deepvariant --version > /tmp/dv.txt 2>&1; grep -q /opt/deepvariant /tmp/dv.txt",
                                   "deepvariant")
    assert why and "discards the tool's own exit status" in why
    assert "&& grep -q PATTERN" in why and 'rc=$?' in why
    # the recommended shapes pass
    assert evidence_shape_violation("tool --version > /tmp/out.txt 2>&1 && grep -q 1.0 /tmp/out.txt", "tool") is None
    assert evidence_shape_violation("tool > /tmp/out.txt 2>&1; rc=$?; grep -q Usage /tmp/out.txt && [ \"$rc\" -eq 1 ]",
                                    "tool") is None
    # a `;` that does not end in an inspector, or whose tool runs LAST, keeps its status
    assert evidence_shape_violation("cd /tmp; tool --version", "tool") is None
    assert evidence_shape_violation("set -o pipefail; tool --version | head -1", "tool") is None


def test_a_crash_banner_in_a_passing_check_fails_the_contract():
    """The gate's own capture-then-check idiom let a dead tool register: the grep matched
    the path inside `Illegal instruction … /opt/deepvariant/bin/run_deepvariant.py`."""
    from agent.skills.env_honesty import check_build
    record = {
        "image": "x:1", "image_digest": "sha256:abc", "image_arch": "amd64", "platform": "linux/amd64",
        "verifications": [{
            "label": "deepvariant", "tool": "deepvariant",
            "check": "run_deepvariant --version > /tmp/dv.txt 2>&1 && grep -q /opt/deepvariant /tmp/dv.txt",
            "passed": True, "rc": 0, "control": "discriminating",
            "out": "/opt/deepvariant/bin/run_deepvariant: line 2:     8 Illegal instruction  /usr/bin/python3 -u "
                   "/opt/deepvariant/bin/run_deepvariant.py --version"}],
    }
    ids = [v["invariant"] for v in check_build(record)]
    assert "VALIDATED_IN_IMAGE.tool_crashed" in ids, ids
    msg = next(v["message"] for v in check_build(record) if v["invariant"] == "VALIDATED_IN_IMAGE.tool_crashed")
    assert "Illegal instruction" in msg and "the tool died" in msg


def test_a_timed_out_check_is_diagnosed_as_a_timeout_and_names_the_knob():
    from agent.skills.env_honesty import check_build
    record = {
        "image": "x:1", "image_digest": "sha256:abc", "image_arch": "amd64", "platform": "linux/amd64",
        "verifications": [{
            "label": "talos", "tool": "talos", "check": "run_workflow --help",
            "passed": False, "rc": 124, "timeout_s": 300,
            "out": "[_proc] command timed out after 300s"}],
    }
    msg = next(v["message"] for v in check_build(record) if v["invariant"] == "VALIDATED_IN_IMAGE.evidence_passed")
    assert "TIMED OUT after 300 s" in msg and "evidence_timeout_s" in msg
    assert "redirect order" not in msg, "a timeout is not a wrong command"
