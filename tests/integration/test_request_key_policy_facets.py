"""
D5 (batch-1 stress): the request_key MUST be sensitive to every policy
facet that produces a materially different artifact. Pre-fix, the key was
`{tools}|{platform}|{accel}` — POLICY-BLIND. Two callers with the same
tools/platform/accel but different gated/license/accelerator-toolkit
policy collided on one cache slot, and the SECOND caller got the FIRST's
artifact back across a policy boundary. The fix adds `gated`, `accel:<hash>`,
and `lic:<hash>` segments so policy-distinct artifacts get policy-distinct
cache slots.

Why integration, not unit: the request_key IS the cache scale-unlock. A
regression that collapses the policy facets back to a single slot is
silent — the only symptom is "agent X gets agent Y's artifact". The
contract is "two distinct intents → two distinct keys" and it must hold
along every policy axis independently AND together.
"""
from __future__ import annotations

import pytest

from agent.skills.freeze import request_key


_TOOLS = [("samtools", "1.21"), ("bwa", "0.7.17")]


@pytest.mark.integration
def test_identical_inputs_produce_identical_key():
    """The base contract: same intent → same cache slot. Order-independent
    in tools, canonical in platform."""
    k1 = request_key(_TOOLS, "linux-64")
    k2 = request_key(list(reversed(_TOOLS)), "linux-64")
    assert k1 == k2, "tool order changed the key (must be order-independent)"


@pytest.mark.integration
def test_gated_flag_shifts_key():
    """A gated build of the same tools is a DIFFERENT artifact (it's
    license-bound). Must not collide on the un-gated cache slot."""
    plain = request_key(_TOOLS, "linux-64")
    gated = request_key(_TOOLS, "linux-64", gated=True)
    assert plain != gated, "gated=True did not differentiate the request_key"


@pytest.mark.integration
def test_licenses_shift_key():
    """Different declared licenses → different artifacts (the firewall
    annotation differs). Order-independent in the licenses list."""
    k_mit = request_key(_TOOLS, "linux-64", gated=True, licenses=["MIT"])
    k_gpl = request_key(_TOOLS, "linux-64", gated=True, licenses=["GPL-3.0"])
    assert k_mit != k_gpl, "licenses set did not differentiate the request_key"
    # Order independent
    k1 = request_key(_TOOLS, "linux-64", gated=True, licenses=["MIT", "GPL-3.0"])
    k2 = request_key(_TOOLS, "linux-64", gated=True, licenses=["GPL-3.0", "MIT"])
    assert k1 == k2, "licenses list ORDER changed the key (must be sorted)"


@pytest.mark.integration
def test_accel_policy_shifts_key():
    """Cuda 12.1 vs cuda 12.8 yield DIFFERENT images (the runtime ABI
    differs). The accel_policy hash must reflect every gating metadata
    field."""
    pol_121 = {"toolkit_version": "12.1", "runtime": "build_only",
               "min_driver_version": "525.60", "compute_capability": "7.5"}
    pol_128 = {"toolkit_version": "12.8", "runtime": "build_only",
               "min_driver_version": "525.60", "compute_capability": "7.5"}
    k_121 = request_key(_TOOLS, "linux-64", accel="cuda", accel_policy=pol_121)
    k_128 = request_key(_TOOLS, "linux-64", accel="cuda", accel_policy=pol_128)
    assert k_121 != k_128, "different cuda toolkit versions collapsed onto one key"

    # runtime_verified vs build_only is also a meaningful difference
    pol_ver = dict(pol_121, runtime="runtime_verified")
    k_ver = request_key(_TOOLS, "linux-64", accel="cuda", accel_policy=pol_ver)
    assert k_121 != k_ver, "build_only vs runtime_verified collapsed onto one key"


@pytest.mark.integration
def test_platform_canonicalization_collapses_aliases():
    """linux-64 and linux/amd64 are the SAME platform spelled two ways
    (D6: spelling drift). The canonicalizer must collapse them onto one key."""
    k1 = request_key(_TOOLS, "linux-64")
    k2 = request_key(_TOOLS, "linux/amd64")
    assert k1 == k2, "platform spelling alias produced two cache slots " \
                     "(D6: spelling drift was supposed to be collapsed)"


@pytest.mark.integration
def test_full_policy_stack_distinct_keys():
    """Stress: every axis at once must differentiate."""
    base = request_key(_TOOLS, "linux-64", accel="none")
    polished = request_key(_TOOLS, "linux-64", accel="cuda",
                           gated=True, licenses=["NVIDIA-SLA"],
                           accel_policy={"toolkit_version": "12.1",
                                         "runtime": "runtime_verified",
                                         "min_driver_version": "525.60"})
    assert base != polished
    # And the policy-stacked key is stable across re-evaluations
    again = request_key(_TOOLS, "linux-64", accel="cuda",
                        gated=True, licenses=["NVIDIA-SLA"],
                        accel_policy={"toolkit_version": "12.1",
                                      "runtime": "runtime_verified",
                                      "min_driver_version": "525.60"})
    assert polished == again, "same policy stack produced two different keys"
