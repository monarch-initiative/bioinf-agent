"""
The honesty contract's POLICY_CLEAN tier — runs against BOTH a built and
an adopted record. Two invariants gate every freeze:

  I12 (accelerator honesty): a record claiming hardware acceleration must
  carry the metadata that makes the claim verifiable.
    - type=mps         requires dev_only=true (MPS doesn't survive containerization)
    - type=cuda/rocm   requires toolkit_version
    - runtime=runtime_verified  requires runtime_probe AND min_driver_version

  I13 (license firewall): a license-gated artifact must declare its
  licenses and NOT be marked redistributable.
    - license_gated=true  requires redistributable=false
    - license_gated=true  requires non-empty licenses[]

Pre-D2/D3 fix (batch-1 dorado stress), `freeze(accel="cuda")` silently
shipped without ever running I12 — the artifact carried a POLICY_CLEAN
badge that nothing had checked. Same shape for D3 in the ADOPT branch:
adopting a biocontainer didn't run POLICY_CLEAN at all, so an adopt of a
gated tool with `licenses=None` would have shipped clean. adopt
exists specifically to keep the policy gates symmetric with check_build.

Why integration, not unit: the contract is composed of independent
clauses (I12 + I13) and the bug class is when one is silently skipped
(the dorado pattern). Integration runs the WHOLE function and asserts the
right clauses fire for each policy violation — a regression that drops a
clause is caught by the test that pins that clause.
"""
from __future__ import annotations

import pytest

from agent.skills.env_honesty import check_build


def _passing_build_record(**overrides) -> dict:
    base = {
        "image": "test_env:1.0", "image_digest": "sha256:deadbeef",
        "verifications": [
            {"label": "samtools", "tool": "samtools", "check": "samtools --version",
             "passed": True, "rc": 0},
        ],
        "license_gated": False, "redistributable": True, "accelerator": None,
        "licenses": [], "longtail_steps": [],
    }
    base.update(overrides)
    return base


def _passing_adopt_record(**overrides) -> dict:
    base = {
        "image": "quay.io/biocontainers/samtools:1.21", "image_digest": "sha256:adopted",
        "mode": "adopt", "license_gated": False, "redistributable": True,
        "accelerator": None, "licenses": [],
        # An adopt record now carries in-image evidence like any other: check_adopt (which
        # skipped VALIDATED_IN_IMAGE) is deleted, and adopt answers check_build.
        "verifications": [{"label": "samtools", "tool": "samtools",
                           "check": "command -v samtools", "passed": True}],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# I13 license firewall                                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.integration
def test_i13_gated_must_set_redistributable_false():
    """A gated artifact with redistributable=true must be refused."""
    r = _passing_build_record(license_gated=True, licenses=["Cellranger-NCBE"],
                              redistributable=True)
    v = check_build(r)
    assert any(x["invariant"] == "I13.gated_not_redistributable" for x in v), \
        f"I13 redistributable=true was not refused: {v}"


@pytest.mark.integration
def test_i13_gated_must_name_licenses():
    """A gated artifact without licenses[] populated must be refused (mirror
    of the freeze() early-gate F2; this is the post-build check)."""
    r = _passing_build_record(license_gated=True, redistributable=False, licenses=[])
    v = check_build(r)
    assert any(x["invariant"] == "I13.gated_license_recorded" for x in v), \
        f"I13 missing licenses[] was not refused: {v}"


@pytest.mark.integration
def test_i13_adopt_runs_the_same_firewall():
    """The dorado-stress D3 fix: adopt mode runs I13 too. A gated adopt
    without licenses is still a lie."""
    r = _passing_adopt_record(license_gated=True, redistributable=False, licenses=[])
    v = check_build(r)
    assert any(x["invariant"] == "I13.gated_license_recorded" for x in v), \
        f"adopt-mode I13 missing licenses was not refused: {v}"


# --------------------------------------------------------------------------- #
# I12 accelerator honesty                                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.integration
def test_i12_cuda_requires_toolkit_version():
    """type=cuda without toolkit_version is unverifiable — the host driver
    floor depends on the toolkit version. Refuse."""
    r = _passing_build_record(accelerator={"type": "cuda"})
    v = check_build(r)
    assert any(x["invariant"] == "I12.accel_toolkit_version_required" for x in v), \
        f"I12 cuda+no-toolkit was not refused: {v}"


@pytest.mark.integration
def test_i12_mps_requires_dev_only_flag():
    """MPS doesn't survive containerization to a linux image. type=mps
    must declare dev_only=true (a development-only signal)."""
    r = _passing_build_record(accelerator={"type": "mps"})
    v = check_build(r)
    assert any(x["invariant"] == "I12.mps_dev_only" for x in v), \
        f"I12 mps without dev_only=true was not refused: {v}"


@pytest.mark.integration
def test_i12_runtime_verified_requires_probe_and_driver():
    """runtime=runtime_verified is the strongest accelerator claim — it
    says 'a kernel actually ran on a real device'. Must record both the
    runtime_probe (the recorded invocation) AND min_driver_version (the
    host floor). Without either, downgrade to runtime=build_only."""
    r = _passing_build_record(accelerator={
        "type": "cuda", "toolkit_version": "12.1",
        "runtime": "runtime_verified",
        # missing runtime_probe + min_driver_version
    })
    v = check_build(r)
    inv = {x["invariant"] for x in v}
    assert "I12.runtime_verified_needs_probe" in inv, \
        f"I12 runtime_verified without probe was not refused: {v}"
    assert "I12.runtime_verified_needs_driver" in inv, \
        f"I12 runtime_verified without min_driver_version was not refused: {v}"


@pytest.mark.integration
def test_i12_adopt_runs_the_same_accelerator_gate():
    """Adopt mode runs I12 too — claiming cuda on an adopted biocontainer
    without toolkit_version is the same lie as build mode."""
    r = _passing_adopt_record(accelerator={"type": "cuda"})
    v = check_build(r)
    assert any(x["invariant"] == "I12.accel_toolkit_version_required" for x in v), \
        f"adopt-mode I12 cuda+no-toolkit was not refused: {v}"


@pytest.mark.integration
def test_passing_records_clear_the_contract():
    """Sanity: a record that meets every clause produces no violations.
    (If this fails, the test fixture above drifted, not the contract.)"""
    assert check_build(_passing_build_record()) == []
    assert check_build(_passing_adopt_record()) == []
