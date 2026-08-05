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
def test_authors_dockerfile_provenance_names_the_source_it_was_built_from():
    """SLSA provenance for an authors-built env must say WHOSE Dockerfile, at which commit.

    externalParameters carried {requested_tools, platform, conda_specs} — and an
    authors-dockerfile env has NO conda specs, so the document asserted
    `build_method: authors-dockerfile` while naming nothing it was built from. A provenance
    attestation that omits the source is a signature over an anonymous artifact."""
    rec = _build_record(build_method="authors-dockerfile", conda_specs=[],
                        dockerfile_source={"repo": "https://github.com/o/r",
                                           "commit": "c" * 40, "tag": "v1",
                                           "recipe_path": "docker/Dockerfile.gpu",
                                           "build_args": {"BCFTOOLS_VERSION": "1.23.1"},
                                           "dockerfile": "FROM debian:bookworm-slim"})
    ext = build_attestation(rec)["predicate"]["buildDefinition"]["externalParameters"]
    src = ext["authors_recipe"]
    assert src["repo"] == "https://github.com/o/r"
    assert src["commit"] == "c" * 40
    assert src["recipe_path"] == "docker/Dockerfile.gpu"
    assert src["build_args"] == {"BCFTOOLS_VERSION": "1.23.1"}


@pytest.mark.integration
def test_provenance_omits_a_source_it_does_not_have_rather_than_blanking_it():
    """No fabricated defaults: a container-native env has no authors' source, so the key is
    ABSENT — not present-and-empty. An empty dict here would read as "we checked and there
    is no source", which is a different claim from "this build had none"."""
    ext = build_attestation(_build_record())["predicate"]["buildDefinition"]["externalParameters"]
    assert "authors_recipe" not in ext
    assert "adopted_image" not in ext


@pytest.mark.integration
def test_adopt_provenance_names_the_pullable_digest():
    rec = _adopt_record(image_by_digest="quay.io/biocontainers/samtools@sha256:" + "ef" * 32,
                        verifications=[{"tool": "samtools", "check": "samtools --version",
                                        "passed": True}])
    ext = build_attestation(rec)["predicate"]["buildDefinition"]["externalParameters"]
    assert ext["adopted_image"].endswith("@sha256:" + "ef" * 32)


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
    # POLICY_CLEAN is now listed as its INDEPENDENT clauses (I12 accelerator, I13
    # license). They reach separate verdicts on separate evidence — one name for
    # two answers is the same collapse the coverage work exists to undo — and
    # PROVENANCE_CLEAN (the synthesis firewall) was always a clause and was simply
    # missing from this list. The guarantees are read off env_honesty.evaluate_build,
    # so this assertion tracks the contract instead of restating it from memory.
    #
    # I12 is TWO clauses for the same reason: `.accelerator` asks whether the claim is
    # internally well-formed, `.accelerator_observed` asks whether the shipped image
    # actually carries what it claims. Both can be true or false independently — a
    # perfectly-formed block can describe an image that does not exist as described —
    # and a single name could not report which half was missing.
    assert internal["honesty_contract"] == [
        "BUILT", "VALIDATED_IN_IMAGE", "POLICY_CLEAN.accelerator",
        "POLICY_CLEAN.accelerator_observed", "POLICY_CLEAN.license",
        "PROVENANCE_CLEAN"]


@pytest.mark.integration
def test_adopt_predicate_claims_validation_only_when_evidence_exists():
    """The rule is unchanged — never claim a validation you didn't do — but the CONDITION
    is the evidence, not the mode.

    Adopt used to skip in-image validation by design, so "mode == adopt" implied "no
    VALIDATED_IN_IMAGE". Adopt now runs each tool's evidence inside the adopted image
    (audit 2026-07-16 Tier 2), so it EARNS the claim; keying off mode would now under-claim
    a real proof, and keying off mode alone would over-claim for a record with no evidence.
    Key off the evidence. ADOPTED_BY_DIGEST stays either way: it is a provenance statement
    ("we did not build these bytes"), and that never stopped being true.
    """
    rec = _adopt_record()
    rec.pop("verifications", None)
    internal = build_attestation(rec)["predicate"]["buildDefinition"]["internalParameters"]
    assert internal["honesty_contract"] == [
        "ADOPTED_BY_DIGEST", "POLICY_CLEAN.accelerator", "POLICY_CLEAN.accelerator_observed",
        "POLICY_CLEAN.license",
        "PROVENANCE_CLEAN"], \
        f"an adopt record with no evidence must not claim validation: {internal['honesty_contract']}"

    rec["verifications"] = [{"tool": "samtools", "check": "command -v samtools", "passed": True}]
    internal = build_attestation(rec)["predicate"]["buildDefinition"]["internalParameters"]
    assert internal["honesty_contract"] == [
        "ADOPTED_BY_DIGEST", "VALIDATED_IN_IMAGE", "POLICY_CLEAN.accelerator",
        "POLICY_CLEAN.accelerator_observed", "POLICY_CLEAN.license", "PROVENANCE_CLEAN"], internal["honesty_contract"]


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
