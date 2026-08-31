"""F12 — a GPU toolkit the image really carries, in a place the probe never looks.

`locus.image_accelerator` searches five locations: the image's own ENV
(`CUDA_VERSION`), `/usr/local/cuda-*`, `/opt/rocm`, a conda prefix's
`conda-meta`, and `nvcc` on PATH. Vendor tarballs — the tier `resolve_tool`
picks for exactly this class of tool — ship their CUDA runtime in a TOOL-LOCAL
`lib/` dir instead: `dorado-2.1.1-linux-x64/lib/libcublasLt.so.12`, reached
through `LD_LIBRARY_PATH` rather than through any standard prefix. None of the
five sees it.

The consequence is the thing worth guarding: a record whose `accelerator.type:
cuda` claim is TRUE is refused with `I12.accel_absent_from_image`, and the
refusal used to end with *"or set accelerator.type=none"* — telling the author
to write a false statement into the artifact to get past a check. That is the
inverse of every other refusal in this codebase.

Measured 2026-08-07 against a real image built to a vendor tarball's shape
(`/opt/dorado/lib/libcublasLt.so.12`, `LD_LIBRARY_PATH` set):

    image_accelerator -> {'resolved': True, 'type': 'none',
                          'source': 'no CUDA/ROCm toolkit found in the image
                                     (env, /usr/local/cuda-*, /opt/rocm,
                                      conda-meta, nvcc)'}

Until then F12 was a COMPOSED finding — four measured links and an inferred
join, because closing it the obvious way meant a ~25 GB dorado freeze. It cost
~80 MB this way, and unlike the freeze it stays.

The clause-level tests below need no daemon and run on every push; the probe
test needs one and is opt-in. Both matter: the first pins what the contract does
with the observation, the second pins the reach of the observation itself, and
F12 lives in the join.
"""
from __future__ import annotations

import subprocess

import pytest

from agent.skills.env_honesty import (CHECKED, NOT_APPLICABLE, UNOBSERVED,
                                      evaluate_build)

_TOOL_LOCAL_DOCKERFILE = """
FROM debian:bookworm-slim
RUN mkdir -p /opt/vendortool/lib \
 && printf 'x' > /opt/vendortool/lib/libcublasLt.so.12.8.3.14 \
 && ln -s libcublasLt.so.12.8.3.14 /opt/vendortool/lib/libcublasLt.so.12 \
 && printf 'x' > /opt/vendortool/lib/libcudart.so.12.8.90 \
 && ln -s libcudart.so.12.8.90 /opt/vendortool/lib/libcudart.so.12
ENV LD_LIBRARY_PATH=/opt/vendortool/lib
"""

_BASE = {
    "name": "vendortool", "image": "x:1@sha256:aa", "image_digest": "sha256:aa",
    "image_arch": "amd64", "platform": "linux/amd64", "requested_tools": ["vendortool"],
    "verifications": [{"label": "vendortool", "tool": "vendortool",
                       "check": "vendortool --version", "passed": True,
                       "control": "discriminating"}],
}

#: What the probe returns for the tool-local layout — copied from the measured run
#: above rather than invented, so the clause tests below are exercised against the
#: real observation and not against a shape nobody has seen.
_PROBE_SAW_NOTHING = {
    "resolved": True, "type": "none", "version": "",
    "source": ("no CUDA/ROCm toolkit found in the image "
               "(env, /usr/local/cuda-*, /opt/rocm, conda-meta, nvcc)"),
    "driver_requirement": "",
}


def _clause(contract, name):
    return next(c for c in contract.coverage if c.clause == name)


def _has_docker() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=30).returncode == 0
    except Exception:
        return False


# -- the join, at the clause level (no daemon) -------------------------------

def test_a_true_cuda_claim_is_refused_when_the_probe_cannot_reach_the_toolkit():
    """F12's join, pinned. This is a CORRECT refusal given what was observed —
    the point is not to make it pass, it is that the refusal must not tell the
    author to fix it by lying."""
    rec = {**_BASE, "accelerator": {"type": "cuda", "toolkit_version": "12.8"},
           "image_accelerator": _PROBE_SAW_NOTHING}
    violations = evaluate_build(rec).violations
    ids = [v["invariant"] for v in violations]
    assert "I12.accel_absent_from_image" in ids, ids


def test_the_refusal_does_not_instruct_the_author_to_declare_a_falsehood():
    """THE finding. The remedy list must offer the honest third option — that
    the toolkit is present somewhere the probe does not search, which makes this
    a gap in our observation rather than a defect in their record."""
    rec = {**_BASE, "accelerator": {"type": "cuda", "toolkit_version": "12.8"},
           "image_accelerator": _PROBE_SAW_NOTHING}
    msg = next(v["message"] for v in evaluate_build(rec).violations
               if v["invariant"] == "I12.accel_absent_from_image")

    assert "TOOL-LOCAL" in msg or "tool-local" in msg, (
        "the refusal never mentions the layout that causes it, so an author "
        "hitting it has no way to tell a true claim from a false one")
    assert "do NOT resolve it by declaring" in msg, (
        "the refusal must say explicitly that declaring type=none is not the fix "
        "when the toolkit is really there")


def test_the_refusal_still_names_where_it_looked():
    """The property that made this message the template for the other four:
    it says what was examined, so the author can tell whether the search was
    the wrong one."""
    rec = {**_BASE, "accelerator": {"type": "cuda", "toolkit_version": "12.8"},
           "image_accelerator": _PROBE_SAW_NOTHING}
    msg = next(v["message"] for v in evaluate_build(rec).violations
               if v["invariant"] == "I12.accel_absent_from_image")
    for place in ("/usr/local/cuda-*", "/opt/rocm", "conda-meta", "nvcc"):
        assert place in msg, f"the refusal does not say it looked in {place}"


def test_an_unread_image_is_unobserved_rather_than_absent():
    """The adjacent trap. "we did not open the image" must not render as "the
    image has no toolkit" — the first is our gap, the second is a finding about
    their artifact."""
    rec = {**_BASE, "accelerator": {"type": "cuda", "toolkit_version": "12.8"}}
    contract = evaluate_build(rec)
    assert _clause(contract, "POLICY_CLEAN.accelerator_observed").status == UNOBSERVED
    assert not [v for v in contract.violations
                if v["invariant"] == "I12.accel_absent_from_image"]


def test_a_matching_observation_clears_the_clause():
    """The PASS direction, so these tests cannot all be satisfied by a clause
    that refuses everything."""
    rec = {**_BASE, "accelerator": {"type": "cuda", "toolkit_version": "12.8"},
           "image_accelerator": {"resolved": True, "type": "cuda", "version": "12.8",
                                 "source": "toolkit directory /usr/local/cuda-12.8"}}
    contract = evaluate_build(rec)
    assert _clause(contract, "POLICY_CLEAN.accelerator_observed").status == CHECKED
    assert not [v for v in contract.violations if v["invariant"].startswith("I12.")]


# -- F17: the observation that is never taken --------------------------------

_IMAGE_HAS_CUDA = {"resolved": True, "type": "cuda", "version": "12.8",
                   "source": "toolkit directory /usr/local/cuda-12.8"}


def test_an_unclaimed_accelerator_still_reports_what_the_image_carries():
    """F17. Measured on the real `ontresearch/dorado` adopt: no claim was made
    (that primitive has no `accelerator` parameter to make one with), so nothing
    opened the image, so the ENV report said *"the artifact claims no GPU
    capability"* while its own apt SBOM listed `cuda-libraries-12-8`.

    "No claim to compare" is a true statement about the COMPARISON that was
    allowed to stand in for "nothing to say about this image" — and
    NOT_APPLICABLE is defined as "the precondition is absent AND that absence is
    itself a fact". Here the absence was a fact about our inputs."""
    rec = {**_BASE, "image_accelerator": _IMAGE_HAS_CUDA}
    cov = _clause(evaluate_build(rec), "POLICY_CLEAN.accelerator_observed")
    assert cov.status == CHECKED, cov
    assert "cuda" in cov.detail and "12.8" in cov.detail


def test_under_claiming_an_accelerator_is_never_a_violation():
    """The direction that matters. Claiming a GPU you do not have costs someone
    an allocation; shipping one nobody claimed costs nothing. This clause reports
    and must not refuse, or every CUDA-based image frozen without an accelerator
    block would suddenly fail to register."""
    rec = {**_BASE, "image_accelerator": _IMAGE_HAS_CUDA}
    assert not [v for v in evaluate_build(rec).violations
                if v["invariant"].startswith("I12.")]


def test_agreement_between_no_claim_and_no_toolkit_is_stated_as_agreement():
    rec = {**_BASE, "image_accelerator": {"resolved": True, "type": "none",
                                          "version": "", "source": "…"}}
    cov = _clause(evaluate_build(rec), "POLICY_CLEAN.accelerator_observed")
    assert cov.status == CHECKED
    assert "agree" in cov.detail


def test_no_claim_and_no_observation_is_still_not_applicable():
    """The one case that genuinely has nothing on either side. It must not be
    dressed up as agreement — nobody looked."""
    cov = _clause(evaluate_build(dict(_BASE)), "POLICY_CLEAN.accelerator_observed")
    assert cov.status == NOT_APPLICABLE
    assert "nothing opened the image" in cov.detail


def test_the_env_report_says_the_image_carries_a_toolkit_nobody_claimed():
    """End to end, on the page the reader opens to decide whether to ask for a
    GPU node. `Accelerator — none` was the answer for a GPU basecaller."""
    from agent.skills.env_report_html import render_env_report_html
    html = render_env_report_html({**_BASE, "image_accelerator": _IMAGE_HAS_CUDA})
    assert "but the shipped image carries cuda 12.8" in html
    assert "no GPU capability is CLAIMED" in html


def test_the_probe_now_runs_whether_or_not_a_claim_was_made():
    """The producer half. Both freeze paths must capture the observation
    unconditionally, or the clause above has nothing to read."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("agent/mcp_tools/freeze_tools.py", "agent/skills/freeze_from_image.py"):
        src = (root / rel).read_text()
        i = src.index("image_accelerator(image)")
        window = src[i:i + 200]
        assert 'not in ("none"' not in window, (
            f"{rel} probes the accelerator only when a claim is made — that is F17")


# -- the probe's actual reach (needs a daemon) -------------------------------

@pytest.mark.integration_docker
@pytest.mark.skipif(not _has_docker(), reason="needs a live Docker daemon")
def test_the_probe_does_not_see_a_toolkit_in_a_vendor_tarballs_lib_dir(tmp_path):
    """The measurement itself, kept executable. If the probe is ever widened to
    follow `LD_LIBRARY_PATH` (one candidate fix for site #2), this test fails —
    which is the correct signal, not a nuisance."""
    from agent.skills import locus

    (tmp_path / "Dockerfile").write_text(_TOOL_LOCAL_DOCKERFILE)
    tag = "bioinf_test_accel_toollocal:latest"
    build = subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "-q", "-t", tag, str(tmp_path)],
        capture_output=True, text=True, timeout=600)
    if build.returncode != 0:
        pytest.skip(f"could not build the fixture image: {build.stderr[-300:]}")
    try:
        obs = locus.image_accelerator(tag)
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, timeout=120)

    assert obs["resolved"] is True, obs
    assert obs["type"] == "none", (
        f"the probe now reaches a tool-local toolkit ({obs}) — F12's mechanism is "
        "gone and this test, plus the refusal text it guards, should be revisited")


# ---------------------------------------------------------------------------
# F4 — presence-only by CONSTRUCTION on the adopt path.
#
# `_validate_tools_in_image` synthesized a conda-metadata presence probe and had
# no way to be given anything better, because `freeze` had no parameter through
# which a caller could author one. So "presence only" was a property of the
# ROUTE, not a choice — and the ENV report's remedy, "re-freeze to earn it",
# named an action that could not change the outcome no matter how many times it
# was taken.
#
# The parameter now exists. What matters is that it did not open a hole: text a
# caller types is agent-authored text, and it earns its standing the same way
# `freeze_from_image`'s does — by failing in an image that lacks the tool.
# ---------------------------------------------------------------------------

def test_freeze_accepts_caller_authored_evidence():
    import inspect
    from agent.mcp_tools.freeze_tools import freeze
    fn = getattr(freeze, "fn", freeze)
    assert "evidence" in inspect.signature(fn).parameters, (
        "freeze cannot be given evidence, so its adopt path is presence-only by "
        "construction and the report's 're-freeze to earn it' is unactionable")


def _fake_ms(monkeypatch, rc=0):
    import agent.mcp_tools.freeze_tools as ft

    class _Docker:
        def run_in_container(self, image, cmd, platform=None, timeout=None):
            return {"returncode": rc, "stdout": "ok", "stderr": ""}

    class _EF:
        @staticmethod
        def _conda_presence_check(tool):
            return f"ls /opt/conda/conda-meta/{tool}-*.json"

    monkeypatch.setattr(ft._ms, "_docker", _Docker(), raising=False)
    monkeypatch.setattr(ft._ms, "_env_freeze", _EF(), raising=False)
    return ft


def test_the_synthesized_probe_is_used_when_no_evidence_is_given(monkeypatch):
    ft = _fake_ms(monkeypatch)
    rows = ft._validate_tools_in_image("img", "linux/amd64", ["samtools"])
    assert rows[0]["check"].startswith("ls /opt/conda")
    # the synthesized probe is ours, so it carries no authored-evidence verdict
    assert "control" not in rows[0]


def test_authored_evidence_replaces_the_probe_and_is_control_tested(monkeypatch):
    ft = _fake_ms(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        "agent.skills.freeze_from_image._evidence_discriminates",
        lambda platform, ev: (seen.setdefault("ev", ev), ("discriminating", "failed in control"))[1])

    rows = ft._validate_tools_in_image(
        "img", "linux/amd64", ["samtools"],
        evidence={"samtools": "samtools sort /t.bam -o /o.bam && test -s /o.bam"})

    assert rows[0]["check"].startswith("samtools sort")
    assert rows[0]["control"] == "discriminating"
    assert seen["ev"].startswith("samtools sort"), "the control ran a different command"


def test_a_vacuous_authored_check_is_recorded_as_vacuous(monkeypatch):
    """`true || samtools` names the tool at a word boundary, is not a bare echo,
    and `evidence_depth` rates it the strongest class. The string rule cannot
    catch it; the control image can, because it passes there too."""
    ft = _fake_ms(monkeypatch)
    monkeypatch.setattr("agent.skills.freeze_from_image._evidence_discriminates",
                        lambda platform, ev: ("vacuous", "passed in a control image without the tool"))
    rows = ft._validate_tools_in_image("img", "linux/amd64", ["samtools"],
                                       evidence={"samtools": "true || samtools"})
    assert rows[0]["control"] == "vacuous"


def test_a_failing_authored_check_does_not_pay_for_a_control_run(monkeypatch):
    """A failing check is already refusing; a container spent learning why a
    failure failed buys nothing."""
    ft = _fake_ms(monkeypatch, rc=1)
    called = []
    monkeypatch.setattr("agent.skills.freeze_from_image._evidence_discriminates",
                        lambda platform, ev: (called.append(ev), ("discriminating", ""))[1])
    rows = ft._validate_tools_in_image("img", "linux/amd64", ["samtools"],
                                       evidence={"samtools": "samtools --nope"})
    assert rows[0]["control"] == "not_applicable"
    assert called == []
