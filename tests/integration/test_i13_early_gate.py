"""
F2 (batch-2 Apollo3 stress): I13 license firewall must REFUSE a gated build
with empty licenses[] BEFORE any docker work — at the freeze() entry point,
not after a 10-30 minute build finishes and env_honesty re-checks. The
diagnostic shape is structurally indistinguishable from env_honesty's
post-build I13 violation, so callers handle one error path either way.

Why integration, not unit: the refusal lives in the real freeze() tool body,
not in a separable helper. A unit test of `env_honesty._check_license`
verifies the post-build check; that does NOT prove freeze() refuses up
front. The bug class — paying for a build the contract will refuse — is
exactly what an integration test of the entry point is for.

This test does NOT touch Docker (the gate fires before any daemon call).
We also confirm — as a corollary — that a request that doesn't trigger the
gate (gated=False) passes through the gate, even if it then fails further
down for unrelated reasons (no docker, no env, etc.). That separates 'I13
gate fired' from 'something else failed'.
"""
from __future__ import annotations

import os

import pytest

from agent import mcp_server as m


@pytest.fixture(autouse=True)
def _disable_disk_failsafe(monkeypatch):
    """A1 disk-failsafe fires at freeze() entry — before I13. Bypass it so
    this test asserts on the I13 gate, not the disk gate."""
    monkeypatch.setenv("BIOINF_FREEZE_MIN_DISK_GB", "0")


@pytest.mark.integration
def test_gated_freeze_with_empty_licenses_refuses_at_i13_early_gate():
    """The canonical failure shape — gated=True, licenses=None — must
    refuse with stage='i13_early_gate' before any docker invocation."""
    res = m.freeze(
        env_name="bioinf_test_gated",
        tools=["samtools=1.21"],
        platform="linux-64",
        gated=True,
        licenses=None,
    )
    assert res.get("success") is False, f"gated+empty-licenses must refuse: {res}"
    assert res.get("stage") == "i13_early_gate", \
        f"expected stage=i13_early_gate, got {res.get('stage')!r}: {res}"
    violations = res.get("honesty_violations") or []
    assert any(v.get("invariant") == "I13.gated_license_recorded" for v in violations), \
        f"missing I13 invariant in response: {res}"
    # Diagnostic names the remediation (pass licenses=[…] or patch_pipeline first).
    msg = " ".join(v.get("message", "") for v in violations)
    assert "licenses" in msg.lower()


@pytest.mark.integration
def test_gated_freeze_with_empty_list_also_refuses():
    """An empty list — distinct from None at the type level — must still
    fail the gate. The gate checks truthiness, not None-ness."""
    res = m.freeze(
        env_name="bioinf_test_gated2",
        tools=["samtools=1.21"],
        platform="linux-64",
        gated=True,
        licenses=[],
    )
    assert res.get("success") is False
    assert res.get("stage") == "i13_early_gate"


@pytest.mark.integration
def test_gated_freeze_with_licenses_does_not_fire_the_gate(monkeypatch):
    """A gated build that DECLARES licenses must NOT trip the early gate.
    We short-circuit immediately after the gate (mock parse_tools to raise a
    sentinel) so the test stays integration-fast — it proves the gate
    DIDN'T intercept, without paying for the rest of freeze() (network
    biocontainer probe, conda solve, build)."""
    class _Marker(Exception):
        pass
    monkeypatch.setattr(m._freeze, "parse_tools",
                        lambda _tools: (_ for _ in ()).throw(_Marker("past_the_gate")))
    with pytest.raises(_Marker):
        m.freeze(
            env_name="bioinf_test_gated_ok",
            tools=["samtools=1.21"],
            platform="linux-64",
            gated=True,
            licenses=["MIT"],
        )


@pytest.mark.integration
def test_ungated_freeze_skips_the_i13_gate(monkeypatch):
    """An ungated build has no licenses requirement at all. Same fast-path
    sentinel as above — assert the gate didn't fire, don't pay for the rest."""
    class _Marker(Exception):
        pass
    monkeypatch.setattr(m._freeze, "parse_tools",
                        lambda _tools: (_ for _ in ()).throw(_Marker("past_the_gate")))
    with pytest.raises(_Marker):
        m.freeze(
            env_name="bioinf_test_ungated",
            tools=["samtools=1.21"],
            platform="linux-64",
            gated=False,
            licenses=None,
        )
