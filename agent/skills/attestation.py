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

from agent.skills.env_report_helpers import version_divergences as _version_divergences
from agent.models.core_data import record_is_gated as _record_is_gated

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

    # THE GUARANTEES ARE READ OFF THE CONTRACT, NOT ASSERTED BY MODE.
    #
    # The provenance distinction is real and survives: a container-native BUILD made the
    # bytes (BUILT); an ADOPTED public biocontainer's bytes are BioContainers', pinned by
    # their published manifest digest (ADOPTED_BY_DIGEST). Everything else — which clauses
    # this artifact actually earned — comes from `evaluate_build`, the same evaluation that
    # gated the freeze.
    #
    # This used to be a literal list on the build branch (`["BUILT", "VALIDATED_IN_IMAGE",
    # "POLICY_CLEAN"]`) and a hand-written re-derivation on the adopt branch ("append
    # VALIDATED_IN_IMAGE if there is any evidence"). Two problems, and the second is the
    # one that matters: the adopt branch was a SECOND implementation of "did this clause
    # examine anything", so the document a downstream verifier consumes decided honesty by
    # its own rules rather than by the contract's. A hardcoded guarantee is a claim nothing
    # can falsify — the strongest form of a report that cannot lie being a report that
    # never looked. Now a clause appears here only if it was CHECKED, and `not_applicable`
    # / `unobserved` clauses are carried explicitly beside it rather than dropped, because
    # a verifier that sees only the earned list cannot tell a short list from a full one.
    # A clause earns a place in `honesty_contract` only if it (a) establishes ASSURANCE
    # — WELL_FORMED records what shipped, it guarantees nothing, so it belongs in the
    # coverage block, not the guarantee list; (b) reached a VERDICT — `not_applicable`
    # counts (no accelerator claimed IS the policy answer), `unobserved` never does; and
    # (c) emitted NO violation. (c) is not hypothetical: `CHECKED` means the clause
    # LOOKED, not that it liked what it saw, and a first cut of this derivation listed a
    # record's malformed `shipped_binaries` as a guarantee it had just failed.
    from agent.skills.env_honesty import (ASSURANCE, CHECKED, NOT_APPLICABLE, UNOBSERVED,
                                          evaluate_build)
    _contract = evaluate_build(r)
    _failed = {c.clause for c in _contract.coverage for v in _contract.violations
               if any(v["invariant"] == p or v["invariant"].startswith(p + ".")
                      for p in c.covers)}
    earned = [c.clause for c in _contract.coverage
              if c.establishes == ASSURANCE and c.status != UNOBSERVED
              and c.clause not in _failed]
    # BUILT is the one clause whose NAME is mode-specific. The contract asks one question
    # ("do the image handles resolve?") and both modes answer it, but "BUILT" asserts we
    # made these bytes — false for an adopt, where the provenance is BioContainers' and
    # the anchor is their published manifest digest. So adopt renames rather than appends:
    # appending gave an adopted image a `BUILT` guarantee it had not earned.
    if r.get("mode") == "adopt":
        earned = ["ADOPTED_BY_DIGEST" if c == "BUILT" else c for c in earned]
        if "ADOPTED_BY_DIGEST" not in earned:
            earned.insert(0, "ADOPTED_BY_DIGEST")
    guarantees = earned
    contract_coverage = {
        "checked": [c.clause for c in _contract.coverage if c.status == CHECKED],
        "not_applicable": [{"clause": c.clause, "why": c.detail}
                           for c in _contract.coverage if c.status == NOT_APPLICABLE],
        "unobserved": [{"clause": c.clause, "establishes": c.establishes, "why": c.detail}
                       for c in _contract.unobserved],
        "failed": sorted(_failed),
    }

    # WHAT WAS THIS BUILT FROM? For the two authors' paths the answer used to be nowhere in
    # the document: externalParameters carried {requested_tools, platform, conda_specs}, and
    # an authors-dockerfile env has NO conda specs — so the provenance said "build_method:
    # authors-dockerfile" without saying WHOSE Dockerfile, at which commit. SLSA's
    # externalParameters is exactly the slot for inputs the requester controlled, and the
    # repo/commit/recipe/build-args are precisely that. Emitted only when present: a key
    # whose value is a fabricated blank is worse than an absent key (the ShippedBinary rule).
    ds = r.get("dockerfile_source") or {}
    source: dict = {}
    if ds:
        source["authors_recipe"] = {
            k: v for k, v in (("repo", ds.get("repo")), ("commit", ds.get("commit")),
                              ("tag", ds.get("tag")), ("recipe_path", ds.get("recipe_path")),
                              ("build_args", ds.get("build_args"))) if v}
    if r.get("image_by_digest"):
        source["adopted_image"] = r["image_by_digest"]

    predicate = {
        "buildDefinition": {
            "buildType": BUILD_TYPE,
            "externalParameters": {
                "requested_tools": r.get("requested_tools", []),
                "platform": r.get("platform", ""),
                "conda_specs": r.get("conda_specs", []),
                **source,
            },
            "internalParameters": {
                "engine": r.get("engine", ""),
                "base_image": base_image,
                "build_method": r.get("build_method", ""),
                "mode": r.get("mode", ""),
                "validation_locus": r.get("validation_locus", ""),
                "honesty_contract": guarantees,
                # ...and what the contract did NOT examine. A verifier reading only the
                # earned list cannot distinguish a short list from a complete one.
                "contract_coverage": contract_coverage,
                "validated_in_image": validated,        # validated == shipped, per tool (build only)
                "redistributable": r.get("redistributable", not _record_is_gated(r)),
                # F4 fix (Batch 2): the gated/license firewall (I13) is verified
                # at build time; the attestation must CARRY the declared licenses
                # forward so a downstream verifier / cosign-attest consumer can
                # see WHICH license terms gated this artifact. Without this, the
                # attestation says "POLICY_CLEAN" without saying "POLICY_CLEAN
                # against what". Recorded alongside `redistributable` (the
                # boolean firewall flag) since they're a unit — gated=True means
                # redistributable=False AND licenses[] names the terms.
                "license_gated": _record_is_gated(r),
                "licenses": list(r.get("licenses") or []),
                # The accelerator policy that gated POLICY_CLEAN (I12). Pure
                # metadata pass-through — the contract already enforces shape.
                "accelerator": r.get("accelerator") or {},
                # IDENTITY DISCLOSURE (audit #8): each requested tool's OWN self-
                # description, read at freeze from the registry the shipped package
                # came from. AGENT-ASSERTED, not a verified capability — it lives in
                # internalParameters beside the other declared (license/accelerator)
                # metadata, NOT in resolvedDependencies (which are verified). A
                # downstream verifier reads it to catch a wrong-domain adoption; it
                # gates nothing. Pass-through: register validated the ToolIdentity shape.
                "tool_identities": r.get("tool_identities") or [],
                # VERSION DIVERGENCE (audit 2026-07-19, W5): requested ≠ OBSERVED
                # installed, per tool. Derived from resolvedDependencies vs the request
                # via the ONE shared divergence check the ENV report + list_installed
                # also read, so a downstream verifier sees the same mismatch the human
                # report shows. Empty when every requested version matches (or can't be
                # compared) — a fabricated-blank key is worse than an absent one, so it
                # carries [] only when the record has requested tools at all.
                "versionMismatch": _version_divergences(r),
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
