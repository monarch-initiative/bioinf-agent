"""
env_honesty — the container-native Layer-1 honesty contract.

This REPLACES spec_writer's env-build invariants (I1/I2/I5/I9/I10/I11/I12/I13/I14)
for the container-native build locus, learning from what those invariants earned
but shedding the machinery the locus makes redundant. The honesty model collapses
because the locus collapsed install-and-ship into ONE event:

    host model:   install on host → record sha256/commit → REPLAY in a linux image
                  → re-hash / re-clone / re-verify at finalize (drift could happen
                  between the two events, so every claim is re-anchored).

    container-native: install == ship is one event. The download's `sha256sum -c`,
                  the `git checkout {ref}`, the base64-baked file all live INSIDE the
                  RUN that becomes the image. A failed or mismatched anchor fails
                  `docker build` — there is no image to ship. Nothing is re-anchored
                  because there is no second event to drift from.

So nine env-build invariants + three re-anchoring walks (binary re-hash, source
re-clone, authored re-hash) collapse into THREE structural guarantees:

  BUILT              — the image exists (image + image_digest resolve). Every RUN,
                       each carrying its own inline anchor, returned 0; else no
                       image. Absorbs I1 + the structural core of I9/I11/I14.
  VALIDATED_IN_IMAGE — every declared tool's evidence passes when re-run in the
                       SHIPPED image, AND each evidence genuinely exercises its tool
                       (the anti-echo-cheat shape rule — the one piece of I2 that
                       still has work to do, since the presence anchor is now free:
                       a clean image can't `command -v X` a tool it doesn't carry).
                       This is I2 in its strongest form — validated == shipped.
  POLICY_CLEAN       — accelerator honesty (I12) + the license firewall (I13),
                       carried verbatim. These are policy (what we may ship), not
                       anchoring, so the locus shift doesn't touch them.

`content_digest` (the EnvCache key) is a REPRODUCIBILITY property, not an honesty
gate, and lives in freeze.py — it answers "same request → same bytes?", a different
question from "did we ship what we validated?".

Pure: check_build(build_result: dict) -> list[violation]. Mirrors spec_writer's
check_invariants shape so the mental model transfers and it's unit-testable with a
hand-built BuildResult (no container needed).
"""

from __future__ import annotations

import re
from typing import Any, Optional

# The declared shape of the sub-records WELL_FORMED asserts. core_data is a leaf
# (pydantic/yaml only) so this keeps the module import-cycle-free; it is no longer
# stdlib-pure, which is the price of the contract knowing what a record IS.
from agent.models import core_data as _core_data

# ---------------------------------------------------------------------------
# The anti-echo-cheat shape rule (carried from evidence.py / env_manager.verify).
#
# In the host model the cheat was the "library-only echo cheat": agent-supplied
# stdout like `echo "samtools 1.21"` faking a verify. The defense layered a word-
# boundary token check WITH an independent presence anchor (which / conda-list).
# In the container-native model the presence anchor is FREE — evidence runs in a
# clean image, so `command -v X` can only pass if X is genuinely installed; no
# agent stdout is trusted. What remains to guard is the LAZY evidence: a constant-
# true (`true`, `:`, `exit 0`) or a bare `echo` that passes without touching the
# tool. So we keep I2's word-boundary token rule (evidence must reference the tool)
# and reject constant-true / bare-echo shapes.
# ---------------------------------------------------------------------------

_CONST_TRUE = re.compile(
    r"^\s*(true|:|exit\s+0|\[\s*1\s*=\s*1\s*\]|test\s+1\s*=\s*1)\s*$", re.I)
_BARE_ECHO = re.compile(r"^\s*(echo|printf)\b", re.I)
# probe verbs that prove SOMETHING even when no tool token is known (e.g. an
# authored file's `test -f {path}`), so the shape rule has a positive anchor.
_PROBE_HINTS = ("command -v", "which ", "--version", "-version", "version",
                "test -f", "test -e", "import ", "-e1", "rscript", "perl -m")


def _tool_tokens(tool: str) -> set[str]:
    """The tool plus its conda-prefix-stripped forms — mirrors evidence.r_namespace
    (r-ape→ape, bioconductor-deseq2→deseq2, python-foo→foo, perl-bio-x→bio-x)."""
    toks = {tool}
    for pre in ("r-", "bioconductor-", "python-", "perl-"):
        if tool.lower().startswith(pre):
            toks.add(tool[len(pre):])
    return {t for t in toks if t}


def _references_tool(evidence: str, tool: str) -> bool:
    """Does `evidence` invoke `tool` as a word-boundary token? Boundary = the
    adjacent char is not an alphanumeric continuation (so `cat` does NOT match
    inside `concatenate`), with a perl `-M` exception (`-MBio::DB::HTS` glues the
    module to a capital letter, which is legitimate)."""
    low = evidence.lower()
    for t in _tool_tokens(tool):
        tl = t.lower()
        idx = low.find(tl)
        while idx != -1:
            before = low[idx - 1] if idx > 0 else " "
            after = low[idx + len(tl)] if idx + len(tl) < len(low) else " "
            left_ok = (not before.isalnum()) or low[max(0, idx - 2):idx] == "-m"
            right_ok = not after.isalnum()
            if left_ok and right_ok:
                return True
            idx = low.find(tl, idx + 1)
    return False


#: evidence depths, weakest → strongest. 'presence' proves only that the tool is INSTALLED
#: (a PATH/metadata lookup that never executes it); 'version'/'import'/'help' prove it
#: loads/answers; 'smoke'/'functional' prove it RUNS. Everything up to 'help' is "shallow".
#: 'unknown' is not on the scale — it means the classifier declined to guess.
EVIDENCE_DEPTHS = ("presence", "version", "import", "help", "smoke", "functional")
_SHALLOW_DEPTHS = frozenset({"presence", "version", "import", "help"})

#: Output plumbing — pipes/redirects that shape a command's OUTPUT and say nothing about
#: what it does. These must be stripped before the "reads/writes → functional" rule, or a
#: single `| cat` promotes any probe to 'functional'.
_PLUMBING = re.compile(
    r"\s*(\|\s*(cat|head|tail|grep|sed|awk|tr|cut|wc|sort|uniq)\b[^|]*"
    r"|\d?>>?\s*/dev/null|\d?>&\d|2>&1)", re.I)


def _strip_plumbing(ev: str) -> str:
    prev = None
    out = ev
    while prev != out:
        prev, out = out, _PLUMBING.sub(" ", out)
    return out.strip()


def evidence_depth(evidence: str, tool: str = "") -> str:
    """Classify how deeply an evidence command exercises the tool — DISCLOSURE ONLY.

    NOTHING GATES ON THIS, and nothing should. A string cannot tell you what a command
    does at runtime; this reads structure and is wrong often enough that gating on it was
    measured (audit 2026-07-16 Tier 2) to refuse the CORRECT artifact and the known-broken
    one identically — `talos_authors` (which really does carry the bcftools fork) is
    [help, version, version] and the broken `talos_v11` reconstruction is [import]: all
    shallow, zero discriminating power on the very war story depth was proposed to catch.
    Its job is to make a report honest, not to refuse a build.

    Returns one of EVIDENCE_DEPTHS, or 'unknown' when the shape is not recognizable —
    guessing 'smoke' for anything unparsed was itself a small lie.

    Three defects this rule set fixes, each of which made the DISCLOSURE wrong:
      - `command -v samtools` read as 'version' (the regex matched the `-v` of `command -v`)
        when it is the weakest evidence there is: a PATH lookup that never runs the tool.
        It now has its own, honest name: 'presence'.
      - `_conda_presence_check(...)` — freeze's own auto-generated probe, and therefore the
        evidence on nearly every real record — read as 'functional' because the path
        `/opt/conda/envs/*/conda-meta/pigz-*.json` matched "reads a file". A presence probe
        was reporting as the strongest possible proof.
      - `samtools --version | head -1` read as 'functional': one pipe outranked the thing
        being piped. Plumbing is stripped first, and version/help are decided before the
        reads-a-path rule rather than after it.
    """
    raw = (evidence or "").strip()
    if not raw:
        return "unknown"          # nothing to classify; the shape check refuses it anyway
    ev = _strip_plumbing(raw)
    low = ev.lower()

    # -- presence: resolves the tool on PATH / in package metadata, never runs it. The
    #    weakest evidence, and (via _conda_presence_check) by far the commonest.
    if re.search(r"\b(command\s+-v|which|type\s+-p|hash)\b", low) or \
            re.search(r"conda-meta|importlib\.metadata|_m\.version\(|installed\.packages\(", low):
        return "presence"
    # -- version / help: the tool EXECUTES and answers. Decided BEFORE both the import rule
    #    and the path rule: `python -m talos.validate_moi --help` RUNS the module's
    #    entrypoint (→ 'help'), which is a strictly stronger claim than "it imported", and
    #    `tool --version /etc/x.cfg` must not be promoted by an incidental path operand.
    if re.search(r"(--version|-version|\bversion\b|\s-V\b)", ev):
        return "version"
    if re.search(r"(--help|\s-h\b|\busage\b)", ev):
        return "help"
    # -- import / load-only: the module loads. Proves more than presence, still not a run.
    if re.search(r"\bimport\b|requirenamespace|library\s*\(|perl\s+-m|-m\w*\s*[a-z]", low):
        return "import"
    # -- functional: moves real data — a genuine pipe/redirect (plumbing already stripped),
    #    a file path operand, or an explicit -i/-o.
    if re.search(r"[<>|]", ev) or re.search(r"/\w[\w./-]*\.\w+", ev) or " -o " in ev or " -i " in ev:
        return "functional"
    # -- a bare invocation of the tool with no recognizable shape.
    if tool and _references_tool(ev, tool):
        return "smoke"
    return "unknown"


def is_shallow_evidence(evidence: str, tool: str = "") -> bool:
    """True if the evidence only proves the tool is present/loads, not that it RUNS.

    DISCLOSURE ONLY — never a refusal, and never a gate. See evidence_depth: gating on
    this was measured to refuse the correct artifact and the broken one identically.
    'unknown' is NOT shallow — declining to classify is not evidence of shallowness."""
    return evidence_depth(evidence, tool) in _SHALLOW_DEPTHS


def evidence_shape_violation(evidence: str, tool: str = "") -> Optional[str]:
    """Return a reason string if `evidence` is a cheat shape, else None. Public so
    a face (MCP/CLI) can pre-flight an agent-authored evidence before a build."""
    ev = (evidence or "").strip()
    if not ev:
        return "evidence is empty — nothing was checked"
    if _CONST_TRUE.match(ev):
        return f"evidence {ev!r} is a constant-true cheat — it passes without exercising the tool"
    if _BARE_ECHO.match(ev) and "$(" not in ev and "`" not in ev:
        return f"evidence {ev!r} only echoes a string — it never invokes the tool"
    if tool:
        if not _references_tool(ev, tool):
            return (f"evidence {ev!r} never references the tool token {tool!r} as a "
                    f"word-boundary invocation — it cannot prove {tool!r} is present")
        return None
    # no tool token (e.g. an authored file): require a recognizable presence probe
    if not any(h in ev.lower() for h in _PROBE_HINTS):
        return f"evidence {ev!r} has no recognizable presence probe and no tool token to anchor on"
    return None


# ---------------------------------------------------------------------------
# POLICY (carried verbatim from spec_writer._check_accelerator / _check_license).
# These are decoupled from spec_writer on purpose — env_honesty is the surviving
# contract; the host moat is slated for teardown. Same invariant IDs + messages so
# they stay recognizable.
# ---------------------------------------------------------------------------

def _check_accelerator(acc: Any) -> list[dict]:
    """I12 — hardware-acceleration claims must be structurally honest."""
    v: list[dict] = []
    if not isinstance(acc, dict):
        return v
    atype = acc.get("type") or "none"
    if atype == "none":
        return v
    if atype == "mps":
        if not acc.get("dev_only"):
            v.append({"invariant": "I12.mps_dev_only", "where": "accelerator",
                      "message": "accelerator.type=mps must set dev_only=true — Metal/MPS does "
                                 "not survive containerization to the shipped linux image, so it "
                                 "can never be a property of the deliverable."})
        return v
    if not (acc.get("toolkit_version") or "").strip():
        v.append({"invariant": "I12.accel_toolkit_version_required", "where": "accelerator",
                  "message": f"accelerator.type={atype} requires toolkit_version (the CUDA/ROCm "
                             f"toolkit baked into the image) — host-driver compatibility depends on it."})
    if (acc.get("runtime") or "build_only") == "runtime_verified":
        if not (acc.get("runtime_probe") or "").strip():
            v.append({"invariant": "I12.runtime_verified_needs_probe", "where": "accelerator",
                      "message": f"accelerator.runtime=runtime_verified claims a kernel ran on a real "
                                 f"{atype} device, but no runtime_probe is recorded — without a GPU "
                                 f"runner this must stay runtime=build_only."})
        if not (acc.get("min_driver_version") or "").strip():
            v.append({"invariant": "I12.runtime_verified_needs_driver", "where": "accelerator",
                      "message": f"accelerator.runtime=runtime_verified for {atype} must record "
                                 f"min_driver_version (the host-driver floor the image needs)."})
    return v


def _check_provenance(result: dict) -> list[dict]:
    """PROVENANCE_CLEAN — the firewall around the synthesis tier (agent-as-generator).
    A synthesized install must carry a full audit trail INTO the recipe: every
    sub-command tagged EXTRACTED (lifted verbatim from a named repo file, with that
    file's sha256) or AGENT_AUTHORED. The authoritative grounding ran at authoring
    (synth_build re-verified every command against the live repo fetch); this is the
    defense-in-depth that refuses a synthesized recipe which reached the build
    WITHOUT its provenance — a hand-injected or bug-dropped record — so nothing
    untraceable to the tool's own files can ship. Structural (no network)."""
    v: list[dict] = []
    for st in result.get("longtail_steps") or []:
        if not isinstance(st, dict):
            continue
        prov = st.get("provenance") or {}
        if prov.get("source") != "synthesized":
            continue
        cmds = prov.get("commands") or []
        if not cmds:
            v.append({"invariant": "PROVENANCE_CLEAN.synthesized_empty",
                      "where": f"longtail[{st.get('purpose','?')}]",
                      "message": "a synthesized install carries no command provenance — its audit "
                                 "trail was lost; refusing to ship an untraceable recipe."})
            continue
        for i, c in enumerate(cmds):
            cp = (c or {}).get("provenance") or {}
            src = cp.get("source")
            if src not in ("extracted", "agent_authored"):
                v.append({"invariant": "PROVENANCE_CLEAN.untagged_command",
                          "where": f"longtail[{st.get('purpose','?')}].commands[{i}]",
                          "message": f"synthesized command {i} has provenance source {src!r} — only "
                                     f"'extracted' or 'agent_authored' may ship (it is untraceable)."})
            elif src == "extracted" and not (cp.get("origin_file") and cp.get("origin_sha256")):
                v.append({"invariant": "PROVENANCE_CLEAN.extraction_unanchored",
                          "where": f"longtail[{st.get('purpose','?')}].commands[{i}]",
                          "message": f"synthesized command {i} claims EXTRACTED but names no "
                                     f"origin_file + origin_sha256 — an extraction must anchor to the "
                                     f"file it was lifted from."})
    return v


def _check_license(result: dict) -> list[dict]:
    """I13 — license-gated artifacts must not be marked redistributable and must
    name their license(s). The procedural firewall against republishing a gated
    artifact (cellranger, dorado, AlphaFold params, …)."""
    v: list[dict] = []
    if not result.get("license_gated"):
        return v
    if result.get("redistributable", True):
        v.append({"invariant": "I13.gated_not_redistributable", "where": "redistributable",
                  "message": "license_gated=true requires redistributable=false — a gated tool's "
                             "image must never be marked redistributable."})
    if not (result.get("licenses") or []):
        v.append({"invariant": "I13.gated_license_recorded", "where": "licenses",
                  "message": "license_gated=true requires at least one entry in licenses[] naming "
                             "the license/terms the artifact is bound by."})
    return v


# ---------------------------------------------------------------------------
# The contract.
# ---------------------------------------------------------------------------

def check_build(result: dict) -> list[dict]:
    """The container-native Layer-1 honesty contract over a BuildResult dict.
    Returns a list of violations (empty == honest). EnvBuild.run() calls this and
    refuses (success=False) on any violation.

    Expects:
      image, image_digest        — the shipped artifact handles (BUILT)
      verifications: list of      — the in-image evidence outcomes + their shapes
        {label, tool, check, passed}  (VALIDATED_IN_IMAGE)
      accelerator, license_gated, — policy fields (POLICY_CLEAN)
        licenses, redistributable
    """
    violations: list[dict] = []

    # -- WELL_FORMED -----------------------------------------------------
    # Layer-1's shape-sanity clause — the analog of Layer-2's I0, and asserted here
    # (the SERVING question) as well as at EnvCache.register (the WRITING question),
    # because tier 5's lesson is that a gate only at the producer leaves every record
    # frozen before it existed grandfathered in. A record whose sub-records don't
    # conform cannot be READ, so it cannot be honestly rendered or served: freeze /
    # run / stage / seal all refuse it and name this clause, and it is re-earned by a
    # re-freeze rather than a backfill ([[feedback-existing-installs-not-precious]]).
    try:
        _core_data.shipped_binaries(result)
    except Exception as e:
        violations.append({"invariant": "WELL_FORMED.shipped_binaries",
                           "where": "shipped_binaries",
                           "message": f"shipped_binaries does not conform to the declared "
                                      f"ShippedBinary shape, so its contents cannot be read "
                                      f"without guessing: {e}"})

    # -- BUILT -----------------------------------------------------------
    # The image existing is the structural anchor for I1/I9/I11/I14: every RUN
    # (each with its inline sha256/commit/bake anchor) returned 0, else there is
    # no image. We assert the handles resolve.
    if not (result.get("image") or "").strip():
        violations.append({"invariant": "BUILT.image_present", "where": "image",
                           "message": "no shipped image tag — the build did not produce an artifact "
                                      "(a failed RUN fails `docker build`; nothing to ship)."})
    if not (result.get("image_digest") or "").strip():
        violations.append({"invariant": "BUILT.image_digest_resolved", "where": "image_digest",
                           "message": "image has no content id (docker image inspect returned no Id) — "
                                      "the artifact isn't present in the local daemon."})

    # -- VALIDATED_IN_IMAGE ----------------------------------------------
    # Every declared tool's evidence must (a) have a non-cheat shape and (b) have
    # PASSED when re-run in the shipped image. (a) is the carried I2 knowledge;
    # (b) is validated==shipped — strictly stronger than the host verify.
    verifications = result.get("verifications") or []
    if not verifications:
        violations.append({"invariant": "VALIDATED_IN_IMAGE.no_evidence", "where": "verifications",
                           "message": "the build declared no tool evidence — an env with nothing "
                                      "proven in the shipped image is an unverified claim."})
    for ver in verifications:
        if not isinstance(ver, dict):
            continue
        label = ver.get("label", "?")
        shape = evidence_shape_violation(ver.get("check", ""), ver.get("tool", ""))
        if shape:
            violations.append({"invariant": "VALIDATED_IN_IMAGE.evidence_shape",
                               "where": f"verifications[{label}]",
                               "message": f"{label}: {shape}"})
        if not ver.get("passed"):
            violations.append({"invariant": "VALIDATED_IN_IMAGE.evidence_passed",
                               "where": f"verifications[{label}]",
                               "message": f"{label}: evidence {ver.get('check','')!r} did not pass "
                                          f"(rc={ver.get('rc')}) in the shipped image — the tool is "
                                          f"not provably present/runnable in what we ship."})

    # -- POLICY_CLEAN ----------------------------------------------------
    violations.extend(_check_accelerator(result.get("accelerator")))
    violations.extend(_check_license(result))

    # -- PROVENANCE_CLEAN (synthesis tier) -------------------------------
    # A synthesized (agent-as-generator) install must carry its provenance into the
    # recipe — nothing untraceable to the tool's own files ships.
    violations.extend(_check_provenance(result))

    return violations


# check_adopt is DELETED (audit 2026-07-16 Tier 2).
#
# It was the mode-aware Layer-1 contract for an adopted BioContainer: BUILT (as
# ADOPTED_BY_DIGEST) + POLICY_CLEAN, with VALIDATED_IN_IMAGE deliberately skipped because
# "the biocontainer's contents are trusted by their published manifest digest".
#
# That reasoning answered the wrong question. Nobody suspected bioconda of lying about its
# own bytes; the real risk is that WE bind the WRONG image — a mulled tag resolving to a
# package set that doesn't contain the tool — and only running the tool can catch it. The
# cost of finding out was ~0.25s per tool against an image freeze had already pulled to
# read its SBOM. Meanwhile adopt is the DEFAULT for pure-conda envs, so the single
# unvalidated path was also the busiest: `samtools=1.21` registered with
# `verifications: []` and two sealed workflows rest on it, while its ENV report and
# attestation presented it as a solved component.
#
# freeze() now generates the same presence evidence the build path uses, runs it in the
# adopted image, and answers `check_build` — one contract for both modes. Its I13 clause
# was dead regardless: `can_adopt` requires `not gated`, so a gated artifact never reached
# it. Anything that needs "is this record an adopt?" should read `record["mode"]`.
