"""
F4 (batch-2 Apollo3 stress): the attestation predicate must carry the
declared license/accelerator policy forward so a cosign-attest verifier
can see the policy this artifact is bound by. Pre-fix, the predicate said
'POLICY_CLEAN' without saying 'POLICY_CLEAN against WHAT' — the firewall
fields (license_gated, licenses, accelerator) were nowhere in the
attestation.

The contract: build_attestation MUST round-trip:
  - license_gated  → predicate.buildDefinition.internalParameters.license_gated
  - licenses[]     → predicate.buildDefinition.internalParameters.licenses
  - accelerator    → predicate.buildDefinition.internalParameters.accelerator
  - mode=adopt     → honesty_contract = ['ADOPTED_BY_DIGEST', 'POLICY_CLEAN']
                     (NOT 'VALIDATED_IN_IMAGE' — audit#2 mode-awareness)
  - mode=build     → honesty_contract = ['BUILT', 'VALIDATED_IN_IMAGE', 'POLICY_CLEAN']
  - base_image     → resolvedDependencies as {uri, digest, annotations.role:base-image}
                     (audit#2: content_digest folds in the base image)

Why integration, not unit: the attestation predicate is the provenance
contract a downstream verifier consumes. A regression that drops one of
these fields ships an artifact whose attestation looks valid (it's signed
JSON) but is missing the policy the artifact claims. Mode-awareness is
ESPECIALLY fragile — adopt must NOT over-claim VALIDATED_IN_IMAGE.
"""
from __future__ import annotations

import pytest

from agent.skills.attestation import build_attestation


def _build_record(**overrides) -> dict:
    base = {
        "name": "test_env", "image": "test_env:1.0",
        "image_digest": "sha256:deadbeef",
        "content_digest": "sha256:cafef00d",
        "platform": "linux/amd64", "engine": "pixi",
        "mode": "build", "build_method": "container-native",
        "validation_locus": "native",
        "requested_tools": ["samtools"],
        "conda_specs": ["samtools=1.21"],
        "resolved_packages": [{"name": "samtools", "version": "1.21",
                                "kind": "conda"}],
        "system_packages": [],
        "verifications": [{"tool": "samtools", "check": "samtools --version",
                            "passed": True}],
        "license_gated": False, "licenses": [], "accelerator": {},
        "redistributable": True,
    }
    base.update(overrides)
    return base


def _adopt_record(**overrides) -> dict:
    base = _build_record()
    base.update({"mode": "adopt", "build_method": "biocontainer-adopt",
                 "verifications": []})
    base.update(overrides)
    return base


@pytest.mark.integration
def test_license_gated_and_licenses_roundtrip_to_predicate():
    """A gated build with licenses=['NVIDIA-SLA'] must surface BOTH the
    boolean firewall flag AND the license terms in the predicate."""
    r = _build_record(license_gated=True, licenses=["NVIDIA-SLA"],
                      redistributable=False)
    att = build_attestation(r)
    internal = att["predicate"]["buildDefinition"]["internalParameters"]
    assert internal["license_gated"] is True, internal
    assert internal["licenses"] == ["NVIDIA-SLA"], internal
    assert internal["redistributable"] is False, internal


@pytest.mark.integration
def test_accelerator_policy_roundtrip_to_predicate():
    """An accelerator-bound build must surface the FULL accelerator dict
    in the predicate (not just a boolean)."""
    accel = {"type": "cuda", "toolkit_version": "12.1",
             "runtime": "runtime_verified", "min_driver_version": "525.60",
             "compute_capability": "7.5", "runtime_probe": "nvidia-smi -L"}
    r = _build_record(accelerator=accel)
    att = build_attestation(r)
    internal = att["predicate"]["buildDefinition"]["internalParameters"]
    assert internal["accelerator"] == accel, \
        f"accelerator policy did not round-trip in full: {internal['accelerator']}"


@pytest.mark.integration
def test_build_mode_predicate_carries_built_validated_clean_triple():
    """The three-guarantee badge for build mode."""
    att = build_attestation(_build_record())
    internal = att["predicate"]["buildDefinition"]["internalParameters"]
    assert internal["honesty_contract"] == ["BUILT", "VALIDATED_IN_IMAGE", "POLICY_CLEAN"]


@pytest.mark.integration
def test_adopt_mode_predicate_does_not_overclaim_validated_in_image():
    """Adopt mode trusts the upstream digest — it does NOT validate in-
    locus, so the attestation must NOT claim VALIDATED_IN_IMAGE. (Audit
    #2 mode-awareness fix.)"""
    att = build_attestation(_adopt_record())
    internal = att["predicate"]["buildDefinition"]["internalParameters"]
    assert "VALIDATED_IN_IMAGE" not in internal["honesty_contract"], \
        f"adopt mode falsely claimed VALIDATED_IN_IMAGE: {internal['honesty_contract']}"
    assert internal["honesty_contract"] == ["ADOPTED_BY_DIGEST", "POLICY_CLEAN"]


@pytest.mark.integration
def test_base_image_appears_in_resolved_dependencies():
    """Audit#2: the base image is part of the artifact's identity and the
    attestation must carry it. Build with a digest-pinned base; the
    predicate's resolvedDependencies must include a base-image entry."""
    base = "ubuntu:22.04@sha256:abc123"
    att = build_attestation(_build_record(), base_image=base)
    resolved = att["predicate"]["buildDefinition"]["resolvedDependencies"]
    base_entries = [d for d in resolved if d.get("annotations", {}).get("role") == "base-image"]
    assert len(base_entries) == 1, \
        f"base_image not surfaced as resolvedDependency: {resolved}"
    assert base_entries[0]["uri"].endswith(base)
    # digest pinned → digest field populated
    assert base_entries[0].get("digest"), "base image digest not extracted into digest dict"


@pytest.mark.integration
def test_subject_pins_the_shipped_image_and_digest():
    """The attestation's `subject` is what the predicate is ABOUT. Pinning
    must use the image tag + digest dict shape that cosign-attest expects."""
    att = build_attestation(_build_record())
    assert len(att["subject"]) == 1
    assert att["subject"][0]["name"] == "test_env:1.0"
    assert att["subject"][0]["digest"]   # non-empty dict
