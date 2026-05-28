"""
attestation — emit a standard in-toto / SLSA provenance Statement from the verified
freeze record, rather than inventing our own attestation format.

Our Layer-1 honesty contract (BUILT / VALIDATED_IN_IMAGE / POLICY_CLEAN) *is* build
provenance. This expresses it in the in-toto Statement v1 envelope with a SLSA
Provenance v1 predicate, so the artifact plugs into the existing verifier/signing
ecosystem (cosign / sigstore / slsa-verifier) instead of being a private island.

Pure: `build_attestation(record) -> dict`, assembled from the runtime-captured
record, so it can't be faked. We emit the UNSIGNED Statement (the "signed-ready"
payload). Signing is a deployment step we don't hold keys for here — a user runs
`cosign attest --predicate <this> <image>` (or wraps it in a DSSE envelope) to
produce the signed attestation.
"""

from __future__ import annotations

from typing import Any

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
SLSA_PREDICATE = "https://slsa.dev/provenance/v1"
BUILD_TYPE = "https://github.com/bioinf-agent/container-native-build/v1"
BUILDER_ID = "https://github.com/bioinf-agent"


def _digest_dict(image_digest: str) -> dict:
    """`sha256:abc…` → {"sha256": "abc…"} (the in-toto DigestSet shape)."""
    s = (image_digest or "").strip()
    if ":" in s:
        algo, _, val = s.partition(":")
        return {algo: val}
    return {"sha256": s} if s else {}


def _purl(pkg: dict) -> str:
    """A package-URL for a resolved dependency (conda / pypi / apt-deb)."""
    name, ver = pkg.get("name", ""), pkg.get("version", "")
    kind = pkg.get("kind")
    if kind == "pypi":
        eco = "pypi"
    elif kind == "apt":
        eco = "deb/debian"           # pkg:deb/debian/<name>@<version>
    else:
        eco = "conda"
    return f"pkg:{eco}/{name}@{ver}" if ver else f"pkg:{eco}/{name}"


def build_attestation(record: dict, *, base_image: str = "") -> dict[str, Any]:
    """The in-toto Statement v1 (SLSA Provenance v1 predicate) for a freeze record.

    subject               = the shipped image + its digest (what the attestation is ABOUT)
    resolvedDependencies  = the full resolved package closure (purls) + the base image
    internalParameters    = engine/base/locus + the validated==shipped evidence + the
                            honesty-contract guarantees that gate the build
    """
    r = record or {}
    subject = [{"name": r.get("image", ""), "digest": _digest_dict(r.get("image_digest", ""))}]

    # the full SBOM: conda/pip closure + the apt/OS layer, all as purls
    closure = (r.get("resolved_packages") or []) + (r.get("system_packages") or [])
    resolved = [{"uri": _purl(p), "annotations": {"kind": p.get("kind", "")}}
                for p in closure if isinstance(p, dict) and p.get("name")]
    if base_image:
        resolved.append({"uri": f"docker:{base_image}",
                         "digest": _digest_dict(base_image.split("@")[-1]) if "@sha256:" in base_image else {},
                         "annotations": {"role": "base-image"}})

    validated = [{"tool": v.get("tool") or v.get("label"), "check": v.get("check"),
                  "passed": bool(v.get("passed"))}
                 for v in (r.get("verifications") or []) if isinstance(v, dict)]

    # The honesty guarantees are MODE-DEPENDENT and must not over-claim: a
    # container-native BUILD ran the contract (BUILT/VALIDATED_IN_IMAGE/POLICY_CLEAN);
    # an ADOPTED public biocontainer is TRUSTED BY ITS PUBLISHED DIGEST — it is NOT
    # built or validated in-locus, so claiming VALIDATED_IN_IMAGE (with an empty
    # evidence list) would be a false provenance assertion.
    if r.get("mode") == "adopt":
        guarantees = ["ADOPTED_BY_DIGEST", "POLICY_CLEAN"]
    else:
        guarantees = ["BUILT", "VALIDATED_IN_IMAGE", "POLICY_CLEAN"]

    predicate = {
        "buildDefinition": {
            "buildType": BUILD_TYPE,
            "externalParameters": {
                "requested_tools": r.get("requested_tools", []),
                "platform": r.get("platform", ""),
                "conda_specs": r.get("conda_specs", []),
            },
            "internalParameters": {
                "engine": r.get("engine", ""),
                "base_image": base_image,
                "build_method": r.get("build_method", ""),
                "mode": r.get("mode", ""),
                "validation_locus": r.get("validation_locus", ""),
                "honesty_contract": guarantees,
                "validated_in_image": validated,        # validated == shipped, per tool (build only)
                "redistributable": r.get("redistributable", not r.get("gated")),
                # F4 fix (Batch 2): the gated/license firewall (I13) is verified
                # at build time; the attestation must CARRY the declared licenses
                # forward so a downstream verifier / cosign-attest consumer can
                # see WHICH license terms gated this artifact. Without this, the
                # attestation says "POLICY_CLEAN" without saying "POLICY_CLEAN
                # against what". Recorded alongside `redistributable` (the
                # boolean firewall flag) since they're a unit — gated=True means
                # redistributable=False AND licenses[] names the terms.
                "license_gated": bool(r.get("license_gated", r.get("gated", False))),
                "licenses": list(r.get("licenses") or []),
                # The accelerator policy that gated POLICY_CLEAN (I12). Pure
                # metadata pass-through — the contract already enforces shape.
                "accelerator": r.get("accelerator") or {},
            },
            "resolvedDependencies": resolved,
        },
        "runDetails": {
            "builder": {"id": BUILDER_ID,
                        "builderDependencies": [{"uri": f"docker:{base_image}"}] if base_image else []},
            "metadata": {
                "invocationId": r.get("content_digest", ""),   # content-addressed run id
                "startedOn": r.get("created_at", ""),
            },
            "byproducts": [b for b in [
                # The Layer-1 deliverable is the HTML env report. The .md sibling
                # was retired in batch-3 (redundant view of the same pure-over-
                # record content); .html stays as the canonical human surface.
                {"name": "env-report", "mediaType": "text/html",
                 "uri": f"env_reports/{r.get('name','env')}.ENV.html"},
                {"name": "conda-lock", "uri": r.get("conda_lock")} if r.get("conda_lock") else None,
            ] if b],
        },
    }
    return {"_type": STATEMENT_TYPE, "subject": subject,
            "predicateType": SLSA_PREDICATE, "predicate": predicate}
