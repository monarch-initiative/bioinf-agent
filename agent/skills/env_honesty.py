"""
env_honesty — the container-native Layer-1 honesty contract.

This REPLACES spec_writer's retired env-build invariants (WHICH ones is data, in
agent/skills/invariants.py; the list spelled out on this line named nine, and four of
them were never retired at all — two are the Layer-2 run-side clauses, two are the
POLICY_CLEAN clauses enforced right here in this module)
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
                       image. Absorbs I1 + the structural core of the other
                       re-anchoring invariants (see the registry).
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

#: A shell comment. Stripped BEFORE the token check, because `true # samtools` referenced
#: the tool as a perfectly good word-boundary token in text the shell never executes —
#: the token rule was reading a comment as an invocation.
_COMMENT = re.compile(r"(?:^|\s)#[^\n]*")

_TRUE_WORD = r"(?:true|:|exit\s+0)"
#: RC LAUNDERING — the command's exit status is forced to 0 whatever the tool did, so
#: `passed: True` stops meaning anything. `|| true` and `; true` launder; `&& true` does
#: NOT (a failing left side still propagates), so it is deliberately excluded.
_RC_LAUNDERED = re.compile(rf"(?:\|\||;)\s*{_TRUE_WORD}\s*$", re.I)
#: SHORT CIRCUIT — a constant-true on the LEFT of `||` means the right side, the part that
#: mentions the tool, may never run at all. This is `true || samtools`: the audit's headline
#: bypass, which passed every rule here and was rated `functional`, the strongest class.
_SHORT_CIRCUITED = re.compile(rf"^\s*{_TRUE_WORD}\s*\|\|", re.I)
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
    a face (MCP/CLI) can pre-flight an agent-authored evidence before a build.

    DEFENSE IN DEPTH, NOT THE DEFENSE. This reads a string, and a string rule cannot win
    against a string author — the audit walked through the previous version with four
    one-liners. The load-bearing check is the EXPERIMENT in
    `freeze_from_image._evidence_discriminates`: run the same evidence in a control image
    that lacks the tool and require it to FAIL there. What survives here is the cheap part
    — the shapes catchable with no container, before a build is attempted.

    Known and accepted residue: a token quoted as a mere string operand (`[ -n "samtools" ]`)
    still reads as an invocation. Distinguishing an executed token from a quoted one needs a
    shell parser, and the experiment catches it for free. Stating the gap beats pretending
    the regex closed it."""
    ev = (evidence or "").strip()
    if not ev:
        return "evidence is empty — nothing was checked"
    # Analyse the EXECUTED text: what the shell would actually run. (Only for analysis —
    # the command runs verbatim.) A `#` inside a quoted string is stripped too, which can
    # cost a false refusal on an unusual evidence; a loud, fixable refusal is the right
    # side to err on when the alternative is reading a comment as proof.
    ex = _COMMENT.sub(" ", ev).strip()
    if not ex:
        return f"evidence {ev!r} is entirely a comment — the shell would run nothing"
    if _CONST_TRUE.match(ex):
        return f"evidence {ev!r} is a constant-true cheat — it passes without exercising the tool"
    if sc := _SHORT_CIRCUITED.match(ex):
        return (f"evidence {ev!r} begins with a constant-true short-circuit "
                f"({sc.group(0).strip()!r}) — everything after `||` is skipped when it "
                f"succeeds, so the part naming the tool never runs")
    if _RC_LAUNDERED.search(ex):
        return (f"evidence {ev!r} ends by forcing exit 0 — the recorded `passed` would be "
                f"true whatever the tool did, so the check cannot fail and proves nothing")
    if _BARE_ECHO.match(ex) and "$(" not in ex and "`" not in ex:
        return f"evidence {ev!r} only echoes a string — it never invokes the tool"
    if tool:
        if not _references_tool(ex, tool):
            return (f"evidence {ev!r} never references the tool token {tool!r} as a "
                    f"word-boundary invocation — it cannot prove {tool!r} is present")
        return None
    # no tool token (e.g. an authored file): require a recognizable presence probe
    if not any(h in ex.lower() for h in _PROBE_HINTS):
        return f"evidence {ev!r} has no recognizable presence probe and no tool token to anchor on"
    return None


# ---------------------------------------------------------------------------
# CONTRACT COVERAGE — the third state.
#
# A gate answers a binary question (violations or none) over a domain with THREE
# states: checked-and-good, checked-and-bad, and NOTHING TO CHECK. With no way to
# say the third, every clause whose subject was absent returned "no violations" —
# so absence read as compliance, and a green badge could rest on clauses that never
# looked at anything (audit 2026-07-30, the whole-system finding: `outcomes.degraded`
# had zero call sites in 33k LOC because there was nothing to attach it to).
#
# So every clause now reports WHAT IT SAW alongside whether it objected:
#
#   CHECKED         the clause examined >= 1 real observation. Its silence is evidence.
#   NOT_APPLICABLE  the precondition is genuinely absent and that absence is ITSELF a
#                   fact — no accelerator was claimed, the artifact is not gated, no
#                   synthesized step exists. Nothing was skipped; there was nothing to
#                   check, and saying so is honest.
#   UNOBSERVED      the clause found nothing to look at but its subject SHOULD exist.
#                   The honesty hole: silence here is NOT evidence.
#
# This GATES NOTHING. It changes no build's pass/fail — a record that passed before
# passes now. What it changes is what a passing record is allowed to imply: freeze
# returns `degraded` instead of `proven` when the green rests on unobserved clauses,
# and ENV.html prints the coverage line under "How this was verified". The distinction
# between NOT_APPLICABLE and UNOBSERVED is the entire point; collapsing them back into
# one "didn't run" bucket would restore the defect this exists to fix.
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field  # noqa: E402  (kept beside its users)

CHECKED = "checked"
NOT_APPLICABLE = "not_applicable"
UNOBSERVED = "unobserved"

COVERAGE_STATUSES = (CHECKED, NOT_APPLICABLE, UNOBSERVED)

#: What a clause establishes when it passes — which tells a reader what an UNOBSERVED
#: verdict on it actually costs them.
ASSURANCE = "assurance"    # we PROVED something about the artifact (it built, it ran, policy holds)
DISCLOSURE = "disclosure"  # we RECORDED something about it (what shipped, what it calls itself)


@dataclass(frozen=True)
class ClauseCoverage:
    """What one contract clause actually looked at. Disclosure, never a gate."""
    clause: str
    """The clause's own name (`VALIDATED_IN_IMAGE`, `POLICY_CLEAN.accelerator`, …)."""
    covers: tuple
    """The invariant-id prefixes whose violations this clause accounts for — the JOIN
    between the two halves of a BuildContract, and the thing that keeps them in step.
    Mostly the clause name itself, but not always: the accelerator clause emits `I12.*`
    and the license clause `I13.*`, so a name-prefix convention would have been a rule
    with silent exceptions. Stated explicitly and LINTED (a violation whose invariant no
    clause covers fails the test suite), so adding a clause without its coverage entry —
    the exact way this disclosure would rot back into nothing — is a build break."""
    status: str
    """One of COVERAGE_STATUSES."""
    observations: int
    """How many real things the clause examined. 0 for NOT_APPLICABLE and UNOBSERVED."""
    detail: str
    """One human-readable line: what was examined, or why there was nothing to examine."""
    establishes: str = ASSURANCE
    """ASSURANCE or DISCLOSURE. An UNOBSERVED assurance clause means we proved less than
    the badge implies; an UNOBSERVED disclosure clause means the record SAYS less. Both
    matter, they are not the same complaint, and the difference is worth one field."""


@dataclass(frozen=True)
class BuildContract:
    """The full result of evaluating the Layer-1 contract: what it objected to AND
    what it managed to look at. `check_build` returns only the first half (its
    signature is load-bearing across ~8 call sites); anything that RENDERS or REPORTS
    the contract should take the whole thing, so the report can never claim more
    coverage than the check had."""
    violations: list[dict] = field(default_factory=list)
    coverage: list[ClauseCoverage] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing objected. NOT the same as "fully observed" — read
        `unobserved` for that. Keeping them separate is the point."""
        return not self.violations

    @property
    def unobserved(self) -> list[ClauseCoverage]:
        return [c for c in self.coverage if c.status == UNOBSERVED]

    @property
    def checked(self) -> list[ClauseCoverage]:
        return [c for c in self.coverage if c.status == CHECKED]

    @property
    def not_applicable(self) -> list[ClauseCoverage]:
        return [c for c in self.coverage if c.status == NOT_APPLICABLE]

    @property
    def observations(self) -> int:
        return sum(c.observations for c in self.coverage)

    def summary(self) -> str:
        """The one-line coverage disclosure that goes under a green badge, e.g.
        "3 clause(s) checked on 8 real observation(s) · 3 not applicable ·
         1 UNOBSERVED (WELL_FORMED.shipped_binaries)"."""
        parts = [f"{len(self.checked)} clause(s) checked on {self.observations} real observation(s)"]
        if self.not_applicable:
            parts.append(f"{len(self.not_applicable)} not applicable")
        if self.unobserved:
            parts.append(f"{len(self.unobserved)} UNOBSERVED "
                         f"({', '.join(c.clause for c in self.unobserved)})")
        return " · ".join(parts)

    def as_dict(self) -> dict:
        """JSON-able form for an outcome payload / a rendered report."""
        return {
            "summary": self.summary(),
            "fully_observed": not self.unobserved,
            "observations": self.observations,
            "clauses": [{"clause": c.clause, "status": c.status, "establishes": c.establishes,
                         "covers": list(c.covers),
                         "observations": c.observations, "detail": c.detail}
                        for c in self.coverage],
        }


def coverage_disclosure(contract: BuildContract) -> dict:
    """The coverage fields to merge into a freeze's outcome payload — one definition, so
    the two freeze paths (`freeze` and `freeze_from_image`) disclose identically.

    THE CALLER BRANCHES, THIS DOES NOT. A helper that returned the outcome function
    itself would be invisible to `scripts/extract_outcomes.py`, which harvests the
    decision surface by reading the literal helper NAME and its constant code at each
    call site — the exact anti-pattern `outcomes.py` documents. So the shared part is the
    payload; the call site writes an explicit `if contract.unobserved: degraded(...) else
    proven(...)` with literal codes, and the harvested model stays complete.

    THE DEGRADE RULE (`contract.unobserved`) IS STRUCTURAL, NOT HEURISTIC, deliberately.
    It fires on "this clause had nothing to look at" — a fact about the record. It does
    NOT fire on shallow evidence depth, even though shallow evidence is the commonest
    real weakness: `evidence_depth` is a string reader MEASURED (audit 2026-07-16 Tier 2)
    to classify the correct artifact and the known-broken one identically, so driving the
    primary outcome tag off it would put a known-unreliable signal in the load-bearing
    position. Depth stays an advisory and a line in the coverage detail."""
    if contract.violations:
        raise AssertionError(
            "coverage_disclosure called on a contract with violations — the caller must "
            "refuse first; a green tag must never be derived from a failing contract")
    payload = {"contract_coverage": contract.as_dict()}
    if contract.unobserved:
        gaps = ", ".join(f"{c.clause} ({c.establishes})" for c in contract.unobserved)
        payload["coverage_advisory"] = (
            f"the honesty contract passed, but {len(contract.unobserved)} clause(s) had nothing "
            f"to examine: {gaps}. Nothing here is wrong — but this record proves/discloses less "
            f"than a fully-observed one, and the difference is not visible in a pass/fail badge.")
    return payload


# ---------------------------------------------------------------------------
# POLICY (carried from spec_writer._check_accelerator / _check_license). These are
# decoupled from spec_writer on purpose — env_honesty is the surviving contract; the
# host moat is slated for teardown. Same invariant IDs + messages so they stay
# recognizable.
#
# Each clause returns (coverage, violations) from ONE walk. The coverage verdict is
# decided at the SAME early-return that decides whether to check — deliberately, so
# the two can't drift: a separate `coverage_of(result)` function would be a second
# copy of every precondition, which is the exact defect class this codebase keeps
# paying for (audit RC2: one truth in N places, drifting in N-1).
# ---------------------------------------------------------------------------

def _clause_accelerator(acc: Any) -> tuple[ClauseCoverage, list[dict]]:
    """I12 — hardware-acceleration claims must be structurally honest."""
    v: list[dict] = []
    if not isinstance(acc, dict) or (acc.get("type") or "none") == "none":
        return (ClauseCoverage("POLICY_CLEAN.accelerator", ("I12",), NOT_APPLICABLE, 0,
                               "no accelerator claimed — I12 has nothing to check, "
                               "and the artifact claims no GPU capability"), v)
    atype = acc.get("type")
    cov = ClauseCoverage("POLICY_CLEAN.accelerator", ("I12",), CHECKED, 1,
                         f"accelerator.type={atype} — checked for toolkit/driver/runtime honesty")
    if atype == "mps":
        if not acc.get("dev_only"):
            v.append({"invariant": "I12.mps_dev_only", "where": "accelerator",
                      "message": "accelerator.type=mps must set dev_only=true — Metal/MPS does "
                                 "not survive containerization to the shipped linux image, so it "
                                 "can never be a property of the deliverable."})
        return (cov, v)
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
    return (cov, v)


def _clause_provenance(result: dict) -> tuple[ClauseCoverage, list[dict]]:
    """PROVENANCE_CLEAN — the firewall around the synthesis tier (agent-as-generator).
    A synthesized install must carry a full audit trail INTO the recipe: every
    sub-command tagged EXTRACTED (lifted verbatim from a named repo file, with that
    file's sha256) or AGENT_AUTHORED. The authoritative grounding ran at authoring
    (synth_build re-verified every command against the live repo fetch); this is the
    defense-in-depth that refuses a synthesized recipe which reached the build
    WITHOUT its provenance — a hand-injected or bug-dropped record — so nothing
    untraceable to the tool's own files can ship. Structural (no network)."""
    v: list[dict] = []
    synthesized = 0
    examined = 0
    for st in result.get("longtail_steps") or []:
        if not isinstance(st, dict):
            continue
        prov = st.get("provenance") or {}
        if prov.get("source") != "synthesized":
            continue
        synthesized += 1
        cmds = prov.get("commands") or []
        examined += len(cmds)
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
    if not synthesized:
        return (ClauseCoverage("PROVENANCE_CLEAN", ("PROVENANCE_CLEAN",), NOT_APPLICABLE, 0,
                               "no synthesized (agent-generated) install step — every install "
                               "command came from a packaged tier, so there is no audit trail "
                               "to demand"), v)
    return (ClauseCoverage("PROVENANCE_CLEAN", ("PROVENANCE_CLEAN",), CHECKED, examined,
                           f"{examined} command(s) across {synthesized} synthesized step(s) "
                           f"checked for EXTRACTED/AGENT_AUTHORED provenance"), v)


def _clause_license(result: dict) -> tuple[ClauseCoverage, list[dict]]:
    """I13 — license-gated artifacts must not be marked redistributable and must
    name their license(s). The procedural firewall against republishing a gated
    artifact (cellranger, dorado, AlphaFold params, …).

    Reads gatedness through `core_data.record_is_gated`, NOT a local `.get("license_gated")`:
    records on disk carry the legacy `gated` key, and a one-key read let those pass I13
    unexamined."""
    v: list[dict] = []
    if not _core_data.record_is_gated(result):
        return (ClauseCoverage("POLICY_CLEAN.license", ("I13",), NOT_APPLICABLE, 0,
                               "not license-gated — the artifact is freely redistributable, "
                               "so I13's republication firewall has nothing to guard"), v)
    cov = ClauseCoverage("POLICY_CLEAN.license", ("I13",), CHECKED, 1,
                         "license-gated — checked that it is marked non-redistributable "
                         "and names its license(s)")
    if result.get("redistributable", True):
        v.append({"invariant": "I13.gated_not_redistributable", "where": "redistributable",
                  "message": "license_gated=true requires redistributable=false — a gated tool's "
                             "image must never be marked redistributable."})
    if not (result.get("licenses") or []):
        v.append({"invariant": "I13.gated_license_recorded", "where": "licenses",
                  "message": "license_gated=true requires at least one entry in licenses[] naming "
                             "the license/terms the artifact is bound by."})
    return (cov, v)


# ---------------------------------------------------------------------------
# The contract.
# ---------------------------------------------------------------------------

def check_build(result: dict) -> list[dict]:
    """The container-native Layer-1 honesty contract over a BuildResult dict.
    Returns a list of violations (empty == honest). EnvBuild.run() calls this and
    refuses (success=False) on any violation.

    THE GATE. Use `evaluate_build` instead when you are going to REPORT the result:
    this returns only what the contract objected to, and a reader that sees `[]` and
    prints "verified" cannot tell a clause that checked three things from a clause
    that checked nothing.
    """
    return evaluate_build(result).violations


def evaluate_build(result: dict) -> BuildContract:
    """The contract in full: violations AND per-clause coverage, from one walk.

    Expects:
      image, image_digest        — the shipped artifact handles (BUILT)
      verifications: list of      — the in-image evidence outcomes + their shapes
        {label, tool, check, passed}  (VALIDATED_IN_IMAGE)
      accelerator, license_gated, — policy fields (POLICY_CLEAN)
        licenses, redistributable
    """
    violations: list[dict] = []
    coverage: list[ClauseCoverage] = []

    # -- WELL_FORMED -----------------------------------------------------
    # Layer-1's shape-sanity clause — the analog of Layer-2's I0, and asserted here
    # (the SERVING question) as well as at EnvCache.register (the WRITING question),
    # because tier 5's lesson is that a gate only at the producer leaves every record
    # frozen before it existed grandfathered in. A record whose sub-records don't
    # conform cannot be READ, so it cannot be honestly rendered or served: freeze /
    # run / stage / seal all refuse it and name this clause, and it is re-earned by a
    # re-freeze rather than a backfill ([[feedback-existing-installs-not-precious]]).
    for key, reader, model in (("shipped_binaries", _core_data.shipped_binaries, "ShippedBinary"),
                               ("tool_identities", _core_data.tool_identities, "ToolIdentity")):
        try:
            parsed = reader(result)
        except Exception as e:
            coverage.append(ClauseCoverage(f"WELL_FORMED.{key}", (f"WELL_FORMED.{key}",), CHECKED,
                                           len(result.get(key) or []),
                                           f"{key} present but does not parse", DISCLOSURE))
            violations.append({"invariant": f"WELL_FORMED.{key}", "where": key,
                               "message": f"{key} does not conform to the declared {model} shape, "
                                          f"so its contents cannot be read without guessing: {e}"})
            continue
        if parsed:
            coverage.append(ClauseCoverage(f"WELL_FORMED.{key}", (f"WELL_FORMED.{key}",), CHECKED, len(parsed),
                                           f"{len(parsed)} {key} record(s) parsed against {model}",
                                           DISCLOSURE))
        else:
            # NOT a violation — a record may legitimately predate the field. But it is
            # NOT compliance either: nothing was validated, so nothing may be implied.
            coverage.append(ClauseCoverage(
                f"WELL_FORMED.{key}", (f"WELL_FORMED.{key}",), UNOBSERVED, 0,
                f"no {key} recorded — the shape check had nothing to validate, so this "
                f"record discloses nothing about {key}", DISCLOSURE))

    # -- BUILT -----------------------------------------------------------
    # The image existing is the structural anchor for the retired re-anchoring
    # invariants (agent/skills/invariants.py): every RUN
    # (each with its inline sha256/commit/bake anchor) returned 0, else there is
    # no image. We assert the handles resolve.
    #
    # BUILT can never be UNOBSERVED: both handles are mandatory, so their ABSENCE is
    # the violation rather than a gap in coverage. (This is the shape every clause
    # would ideally have — nothing to check is impossible when the subject is required.)
    built_seen = 0
    if not (result.get("image") or "").strip():
        violations.append({"invariant": "BUILT.image_present", "where": "image",
                           "message": "no shipped image tag — the build did not produce an artifact "
                                      "(a failed RUN fails `docker build`; nothing to ship)."})
    else:
        built_seen += 1
    if not (result.get("image_digest") or "").strip():
        violations.append({"invariant": "BUILT.image_digest_resolved", "where": "image_digest",
                           "message": "image has no content id (docker image inspect returned no Id) — "
                                      "the artifact isn't present in the local daemon."})
    else:
        built_seen += 1
    coverage.append(ClauseCoverage("BUILT", ("BUILT",), CHECKED, built_seen,
                                   f"{built_seen}/2 shipped-artifact handle(s) (image, image_digest) resolve"))

    # -- VALIDATED_IN_IMAGE ----------------------------------------------
    # Every declared tool's evidence must (a) have a non-cheat shape and (b) have
    # PASSED when re-run in the shipped image. (a) is the carried I2 knowledge;
    # (b) is validated==shipped — strictly stronger than the host verify.
    verifications = [v for v in (result.get("verifications") or []) if isinstance(v, dict)]
    if not verifications:
        coverage.append(ClauseCoverage("VALIDATED_IN_IMAGE", ("VALIDATED_IN_IMAGE",), UNOBSERVED, 0,
                                       "no evidence was run in the shipped image"))
        violations.append({"invariant": "VALIDATED_IN_IMAGE.no_evidence", "where": "verifications",
                           "message": "the build declared no tool evidence — an env with nothing "
                                      "proven in the shipped image is an unverified claim."})
    else:
        # Depth is DISCLOSURE, never a gate (see evidence_depth). Reporting it here is
        # what stops "5 tools validated" from reading as "5 tools were run" when all five
        # evidences are PATH lookups — the commonest real shape in this repo's records.
        depths = [evidence_depth(v.get("check", ""), v.get("tool", "")) for v in verifications]
        deep = sum(1 for d in depths if d not in _SHALLOW_DEPTHS and d != "unknown")
        coverage.append(ClauseCoverage(
            "VALIDATED_IN_IMAGE", ("VALIDATED_IN_IMAGE",), CHECKED, len(verifications),
            f"{len(verifications)} evidence command(s) re-run in the shipped image; "
            f"{deep} of them RUN the tool (the rest are presence/version/help probes) "
            f"[{', '.join(sorted(set(depths)))}]"))
    for ver in verifications:
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

    # -- POLICY_CLEAN + PROVENANCE_CLEAN ---------------------------------
    # PROVENANCE_CLEAN is the firewall around the synthesis tier: a synthesized
    # (agent-as-generator) install must carry its provenance into the recipe, so
    # nothing untraceable to the tool's own files ships.
    for cov, viol in (_clause_accelerator(result.get("accelerator")),
                      _clause_license(result),
                      _clause_provenance(result)):
        coverage.append(cov)
        violations.extend(viol)

    return BuildContract(violations=violations, coverage=coverage)


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
