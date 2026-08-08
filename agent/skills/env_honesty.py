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

# For BUILT.platform's ONE reading of a platform spelling. locus is a leaf too
# (stdlib + subprocess), and only its PURE `target_arch` is used here — the contract
# compares two recorded fields and never talks to Docker itself.
from agent.skills import locus as _locus

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
#: PIPED — a TOP-LEVEL `|` (never `||`). A shell pipeline exits with its LAST stage's
#: status, so anything piped into `head`/`grep`/`tail` reports THAT command's rc and
#: discards the tool's. Unlike the three shapes above this is not a cheat anyone writes
#: on purpose, which is exactly why it got through: it is what you write to keep the
#: output short. Measured 2026-08-02: `exomiser --help 2>&1 | head -20` exits 0 in a
#: stock debian that has no exomiser, and a freeze registered `proven` on it.
#:
#: TOP-LEVEL is the whole difficulty, and the first version of this rule got it wrong.
#: A pipe inside `$( )`, backticks, quotes or a `( )` subshell does NOT launder the
#: outer status — and this codebase's OWN generated conda probe contains three of them
#: (`$(sed -nE 's|...|\1|p' "$f" | sort -u)`). Blanket-matching `|` refused the system's
#: own honest evidence, which is a neat demonstration of the module docstring's thesis:
#: a string rule cannot win this game. So this stays deliberately narrow — strip every
#: nested span first, then look at what is left — and the load-bearing check remains
#: the control-image experiment, which needs to parse nothing.
_PIPED = re.compile(r"(?<!\|)\|(?!\|)")
#: Spans whose contents are not top-level: quoted strings, command substitutions,
#: backticks, and parenthesised subshells. Innermost-first, applied repeatedly.
_NESTED_SPAN = re.compile(r"'[^']*'|\"[^\"]*\"|\$\([^()]*\)|`[^`]*`|\([^()]*\)")


def _top_level(ex: str) -> str:
    """`ex` with every nested span blanked out, so only top-level operators remain."""
    prev = None
    while prev != ex:
        prev, ex = ex, _NESTED_SPAN.sub(" ", ex)
    return ex
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
    # `-m\w*\s*[a-z]` used to sit at the end of this alternation, meaning to catch
    # `python -m module`. It caught ordinary CLI flags instead, and this branch runs BEFORE
    # the functional check below, so it DEMOTED commands that plainly move data:
    #
    #     bwa mem -M ref.fa                    -> import   (a file operand!)
    #     bcftools call -mv -Oz -o out.vcf.gz  -> import   (an -o operand!)
    #     kraken2 --memory-mapping             -> import
    #     /opt/tool-manager/bin/run --check    -> import   (a hyphenated PATH segment)
    #
    # Narrowed to an actual python module invocation. Measured across the 35 real evidence
    # commands in the corpus: ZERO classifications change, so this removes a trap without
    # touching a single shipped artifact.
    #
    # Worth stating precisely, because it was reported as worse than it is: the misfire was
    # LATENT (no record on disk was affected) and its direction was SAFE — `import` is in
    # _SHALLOW_DEPTHS, so a misfire always rendered the ⚠ "presence only" badge. It could
    # only under-claim, never dress a shallow proof as a functional run. `samtools view -m 4`
    # was never affected at all: the trailing `[a-z]` cannot match a digit.
    if re.search(r"\bimport\b|requirenamespace|library\s*\(|perl\s+-m"
                 r"|\bpython[\d.]*\s+-m\s", low):
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
    if _PIPED.search(_top_level(ex)) and "pipefail" not in ex:
        # The FOURTH laundering shape, and the one a careful agent reaches for by
        # accident: a shell pipeline exits with its LAST stage's status, so
        # `exomiser --help | head -20` is rc=0 even when exomiser is not installed
        # at all. Measured 2026-08-02 in a stock debian image: piped rc=0,
        # unpiped rc=127 — and a freeze had already registered `proven` on it.
        # Trimming output is a completely reasonable thing to want; `head` is not
        # a cheat, it is a convenience that silently disables the check. So name
        # the two honest ways to keep it.
        return (f"evidence {ev!r} pipes into another command, and a pipeline exits with "
                f"its LAST stage's status — so the recorded `passed` reports `head`/`grep`, "
                f"not the tool. It would pass in an image without the tool at all. "
                f"Either drop the pipe (redirect instead: `... > /dev/null`) or make the "
                f"pipeline honest: `set -o pipefail; ...`")
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

#: I12 is answered by TWO clauses, so each must name the EXACT ids it owns rather than
#: claim the whole `I12.` prefix — the `_VII_COVERS` discipline. A broad prefix on both
#: makes every accelerator violation claimed twice, and the anti-drift lint reads a
#: doubly-owned invariant as ownership by nobody in particular.
_ACCEL_STRUCTURAL_COVERS = (
    "I12.mps_dev_only",
    "I12.accel_toolkit_version_required",
    "I12.runtime_verified_needs_probe",
    "I12.runtime_verified_needs_driver",
    "I12.runtime_probe_not_captured",
    "I12.runtime_probe_incomplete",
    "I12.runtime_probe_failed",
)
_ACCEL_OBSERVED_COVERS = (
    "I12.accel_absent_from_image",
    "I12.accel_type_mismatch",
    "I12.accel_version_mismatch",
)


def _clause_accelerator(acc: Any) -> tuple[ClauseCoverage, list[dict]]:
    """I12, first half — is the accelerator claim internally well-formed?

    Everything this clause reads is written by the same agent that writes the claim,
    so it cannot tell a true block from a well-typed false one. That is not a flaw in
    it — a structural check is worth having — but it is the whole check I12 used to be.
    `_clause_accelerator_observed` is the half that opens the image."""
    v: list[dict] = []
    if not isinstance(acc, dict) or (acc.get("type") or "none") == "none":
        return (ClauseCoverage("POLICY_CLEAN.accelerator", _ACCEL_STRUCTURAL_COVERS, NOT_APPLICABLE, 0,
                               "no accelerator claimed — I12 has nothing to check, "
                               "and the artifact claims no GPU capability"), v)
    atype = acc.get("type")
    cov = ClauseCoverage("POLICY_CLEAN.accelerator", _ACCEL_STRUCTURAL_COVERS, CHECKED, 1,
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
        probe = acc.get("runtime_probe")
        if not probe or (isinstance(probe, str) and not probe.strip()):
            v.append({"invariant": "I12.runtime_verified_needs_probe", "where": "accelerator",
                      "message": f"accelerator.runtime=runtime_verified claims a kernel ran on a real "
                                 f"{atype} device, but no runtime_probe is recorded — without a GPU "
                                 f"runner this must stay runtime=build_only."})
        elif not isinstance(probe, dict):
            # A STRING here is the whole defect. `runtime_probe` is documented as "the
            # proof", has no producer anywhere in this codebase, and is reachable only
            # by typing it into patch_pipeline — so the strongest claim in the
            # accelerator vocabulary was satisfiable by writing a sentence. Worse, the
            # system cannot even produce the thing being claimed: `--gpus` and
            # apptainer's `--nv` appear nowhere in it, so no container this system
            # starts has ever had a device visible to it.
            v.append({
                "invariant": "I12.runtime_probe_not_captured", "where": "accelerator",
                "message": f"accelerator.runtime_probe is a hand-written string "
                           f"({str(probe)[:60]!r}), and a sentence is not evidence that a kernel "
                           f"ran on a {atype} device. It must be a CAPTURED record — "
                           f"{{command, returncode, output, locus}} — produced by actually "
                           f"running the probe where the device is. No producer of one exists "
                           f"yet, so today the honest setting is runtime=build_only: the image "
                           f"carries the toolkit, and execution on hardware was not verified."})
        else:
            missing = [k for k in ("command", "returncode", "locus") if k not in probe]
            if missing:
                v.append({
                    "invariant": "I12.runtime_probe_incomplete", "where": "accelerator",
                    "message": f"accelerator.runtime_probe is a captured record but omits "
                               f"{', '.join(missing)} — a probe that does not say what ran, "
                               f"whether it succeeded, and WHERE cannot distinguish a real "
                               f"{atype} device from a CPU fallback."})
            elif probe.get("returncode") not in (0, "0"):
                v.append({
                    "invariant": "I12.runtime_probe_failed", "where": "accelerator",
                    "message": f"accelerator.runtime=runtime_verified, but the recorded probe "
                               f"exited {probe.get('returncode')!r} — evidence that exists and "
                               f"says FAILED cannot be read as verification."})
        if not (acc.get("min_driver_version") or "").strip():
            v.append({"invariant": "I12.runtime_verified_needs_driver", "where": "accelerator",
                      "message": f"accelerator.runtime=runtime_verified for {atype} must record "
                                 f"min_driver_version (the host-driver floor the image needs)."})
    return (cov, v)


def _accel_version_agrees(claimed: str, observed: str) -> bool:
    """Does a claimed toolkit version agree with the one read off the image?

    Deliberately a PREFIX test on dot-components, not equality. The image states its
    version at whatever precision its packaging chose — nvidia's ENV says `12.4.1`,
    the toolkit directory it ships alongside says `12.4`, conda says `12.4` — and all
    three describe one toolkit. A record claiming `12.4` over an image reporting
    `12.4.1` is accurate, and refusing it would train callers to copy whatever string
    silences the gate, which is how a check stops being read.

    What it must still catch is a DIFFERENT toolkit: 11.8 vs 12.4, or 99.9 vs
    anything. Component-wise prefix does exactly that, and `12.4` vs `12.40` stays a
    mismatch because the comparison is per-component, never per-character.
    """
    c = [x for x in (claimed or "").strip().split(".") if x]
    o = [x for x in (observed or "").strip().split(".") if x]
    if not c or not o:
        return True                       # nothing to disagree about; handled by caller
    n = min(len(c), len(o))
    return c[:n] == o[:n]


def _clause_accelerator_observed(acc: Any, observed: Any) -> tuple[ClauseCoverage, list[dict]]:
    """I12, second half — the accelerator CLAIM against the shipped image.

    `_clause_accelerator` above checks that an accelerator block is internally
    well-formed: a cuda claim carries some toolkit_version, a runtime_verified claim
    carries some probe string. Every one of those fields is written by the same agent
    that writes the claim, so the whole clause compares a record against itself. On an
    arm64 Mac with no NVIDIA hardware in the building, a record reading
    `type: cuda, toolkit_version: 99.9, runtime: runtime_verified,
    runtime_probe: "yes there is definitely a gpu"` cleared the contract over a
    pyfaidx image that has never contained a line of CUDA — and the coverage line said
    "checked for toolkit/driver/runtime honesty", one observation. It had observed
    nothing; it had re-read the claim.

    This clause is the observation. `image_accelerator` is captured by the producer at
    freeze (locus.image_accelerator, which opens the image) and compared here — two
    fields, separate provenance, exactly as BUILT.platform compares `image_arch`
    against `platform`. Re-deriving it inside the contract would be the I5 laundering
    shape, a value checked against itself, and would put a `docker run` per env inside
    `list_installed_pipelines`.

    Kept SEPARATE from the structural clause rather than folded into it because the
    two can be true and false independently, and a single status could not say which
    half was missing: a record can be perfectly well-formed and describe an image that
    does not exist as described.
    """
    v: list[dict] = []
    claimed_type = (acc or {}).get("type") if isinstance(acc, dict) else None
    claimed_type = (claimed_type or "none").strip().lower()

    if claimed_type in ("", "none"):
        # F17. "No claim to compare" is true of the COMPARISON and was allowed to
        # stand in for "nothing to say about this image", which is a different
        # sentence. Measured on the real `ontresearch/dorado` adopt: the clause read
        # NOT_APPLICABLE, `image_accelerator` was never captured, the ENV report said
        # "the artifact claims no GPU capability" — and the apt SBOM on the same page
        # listed `cuda-libraries-12-8`. NOT_APPLICABLE means "the precondition is
        # genuinely absent AND that absence is itself a fact"; here the absence was a
        # fact about our INPUTS, and the page rendered it as one about the artifact.
        #
        # Under-claiming is NOT a violation and must never become one — the harmful
        # direction is claiming a GPU you do not have, which is what the allocation is
        # spent on. So this reports and does not refuse.
        if isinstance(observed, dict) and observed.get("resolved"):
            obs_t = (observed.get("type") or "").strip().lower()
            if obs_t and obs_t != "none":
                obs_v = (observed.get("version") or "").strip()
                return (ClauseCoverage(
                    "POLICY_CLEAN.accelerator_observed", _ACCEL_OBSERVED_COVERS, CHECKED, 1,
                    f"no accelerator claimed, but the shipped image carries "
                    f"{obs_t}{(' ' + obs_v) if obs_v else ''} "
                    f"({observed.get('source') or 'the image'}). Not a violation — "
                    f"under-claiming costs nobody an allocation — but the toolkit is "
                    f"there and the record now says so"), v)
            return (ClauseCoverage(
                "POLICY_CLEAN.accelerator_observed", _ACCEL_OBSERVED_COVERS, CHECKED, 1,
                "no accelerator claimed, and the shipped image was opened and carries "
                "no accelerator toolkit — claim and image agree"), v)
        return (ClauseCoverage(
            "POLICY_CLEAN.accelerator_observed", _ACCEL_OBSERVED_COVERS, NOT_APPLICABLE, 0,
            "no accelerator claimed and nothing opened the image — there is neither a "
            "claim to check nor an observation to report"), v)

    if claimed_type == "mps":
        # Metal never survives containerization; _clause_accelerator forces dev_only
        # for it. There is nothing to find in a linux image and its absence is not a
        # finding — looking would manufacture a violation out of the expected case.
        return (ClauseCoverage(
            "POLICY_CLEAN.accelerator_observed", _ACCEL_OBSERVED_COVERS, NOT_APPLICABLE, 0,
            "accelerator.type=mps is dev-only by I12 and is not a property of the "
            "shipped linux image — there is nothing in the image to observe"), v)

    if not isinstance(observed, dict) or not observed.get("resolved"):
        return (ClauseCoverage(
            "POLICY_CLEAN.accelerator_observed", _ACCEL_OBSERVED_COVERS, UNOBSERVED, 0,
            f"no image_accelerator recorded — nothing opened the image to check whether "
            f"the claimed {claimed_type} toolkit is actually in it, so "
            f"`toolkit_version: {(acc or {}).get('toolkit_version') or '?'}` is the "
            f"caller's word repeated back rather than a fact about the artifact"), v)

    obs_type = (observed.get("type") or "").strip().lower()
    obs_ver = (observed.get("version") or "").strip()
    claimed_ver = ((acc or {}).get("toolkit_version") or "").strip()
    src = observed.get("source") or "the image"

    if obs_type == "none":
        v.append({
            "invariant": "I12.accel_absent_from_image", "where": "accelerator",
            "message": f"the record claims accelerator.type={claimed_type}"
                       f"{f' (toolkit {claimed_ver})' if claimed_ver else ''}, but the shipped "
                       f"image contains no accelerator toolkit at all — {src}. A GPU claim is "
                       f"the one thing a user cannot check before committing the allocation, so "
                       f"it may not rest on the caller's say-so. Three ways out, and which one "
                       f"is right depends on a fact only you can check: (1) if the image has no "
                       f"GPU support, declare accelerator.type=none — that is then TRUE; (2) if "
                       f"it should have and doesn't, ship one that does (build FROM a cuda/rocm "
                       f"base, or install the toolkit into the env); (3) if the toolkit IS in "
                       f"the image but somewhere the probe does not search — vendor tarballs "
                       f"routinely bundle libcuda*/libcublas* in a TOOL-LOCAL lib/ dir rather "
                       f"than a standard prefix — then this refusal is a gap in the probe, not "
                       f"a defect in your record. Say so; do NOT resolve it by declaring "
                       f"type=none, which would put a false statement in the artifact to get "
                       f"past a check."})
        return (ClauseCoverage(
            "POLICY_CLEAN.accelerator_observed", _ACCEL_OBSERVED_COVERS, CHECKED, 1,
            f"the image was opened and carries no accelerator toolkit, against a "
            f"{claimed_type} claim"), v)

    if obs_type != claimed_type:
        v.append({
            "invariant": "I12.accel_type_mismatch", "where": "accelerator",
            "message": f"the record claims accelerator.type={claimed_type} but the shipped "
                       f"image carries {obs_type} ({src}). These are different vendors' "
                       f"toolchains and a binary built against one does not run on the other."})
        return (ClauseCoverage(
            "POLICY_CLEAN.accelerator_observed", _ACCEL_OBSERVED_COVERS, CHECKED, 1,
            f"the image carries {obs_type}, the record claims {claimed_type}"), v)

    if claimed_ver and obs_ver and not _accel_version_agrees(claimed_ver, obs_ver):
        v.append({
            "invariant": "I12.accel_version_mismatch", "where": "accelerator",
            "message": f"the record claims {claimed_type} toolkit {claimed_ver} but the shipped "
                       f"image carries {obs_ver} ({src}). Host-driver compatibility is decided "
                       f"by the toolkit actually in the image, so the wrong number here sends a "
                       f"user to provision the wrong driver floor."})
        return (ClauseCoverage(
            "POLICY_CLEAN.accelerator_observed", _ACCEL_OBSERVED_COVERS, CHECKED, 1,
            f"the image carries {claimed_type} {obs_ver}, the record claims {claimed_ver}"), v)

    if claimed_ver and not obs_ver:
        # The toolkit is there and is the right vendor, but no source in the image
        # states a version without being interpreted (nvcc-only images). The type is
        # confirmed; the number is not. Saying so beats both a silent pass and a
        # refusal of something that is probably right.
        return (ClauseCoverage(
            "POLICY_CLEAN.accelerator_observed", _ACCEL_OBSERVED_COVERS, CHECKED, 1,
            f"the image really does carry a {claimed_type} toolkit ({src}), but states no "
            f"version that can be read without interpreting prose — the claimed "
            f"{claimed_ver} is unconfirmed"), v)

    return (ClauseCoverage(
        "POLICY_CLEAN.accelerator_observed", _ACCEL_OBSERVED_COVERS, CHECKED, 1,
        f"the image really does carry {obs_type}"
        f"{f' {obs_ver}' if obs_ver else ''}, the accelerator the record claims ({src})"), v)


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
    unexamined.

    TWO WAYS TO BE GATED, and only one of them used to exist. `license_gated` is the
    agent's DECLARATION, and `redistributable` is derived from it (`not gated`,
    freeze_tools.py) — so an agent that simply never mentions a licence gets
    `redistributable: true` and this clause early-returned NOT_APPLICABLE without looking
    at anything. It did that on 13 of 13 registered envs: I13 had never once been CHECKED
    on a real record. Self-certification arming its own firewall is the same shape as
    `asset_authenticated = bool(sha256)` (2026-08-04, the operator hand-off) — a claim
    standing in for an observation.

    The second way is OBSERVED: the SBOM now carries the licence read out of each
    package's `conda-meta/*.json` IN THE SHIPPED IMAGE, and a package whose licence
    asserts a restriction makes the artifact gated as a matter of fact, whatever the
    record declares. bioconda ships `novoalign` ("Commercial (requires license for use)")
    and `sentieon`, both with a BioContainer the resolver will happily recommend adopting
    by digest. An observation cannot be un-said by an assertion — the same rule I3 applies
    to a failed validation — so it ARMS the clause rather than adding a parallel one, and
    the two requirements below are then identical either way.

    Coverage is reported honestly: `unrecognized` licences are counted and named in the
    detail line, because this clause can only speak for the packages whose licence it
    could read AND recognise. pip/dist-info packages carry no licence in the scan at all."""
    v: list[dict] = []
    observed = _core_data.restricted_packages(result)
    declared = _core_data.record_is_gated(result)
    pkgs = [p for p in (result.get("resolved_packages") or []) if isinstance(p, dict)]
    unknown = _core_data.unrecognized_packages(result)
    # what this clause can and cannot speak for — appended to whichever verdict follows,
    # so "nothing restricted" is never read as "every licence was understood".
    reach = (f"{len(pkgs) - len(unknown)}/{len(pkgs)} package licences recognised"
             + (f"; {len(unknown)} unrecognised ("
                + ", ".join(p["name"] for p in unknown[:5]) + ")"
                if unknown else "")) if pkgs else "no conda/pip closure to read licences from"

    if not (declared or observed):
        return (ClauseCoverage("POLICY_CLEAN.license", ("I13",), NOT_APPLICABLE, len(pkgs),
                               "not license-gated: nothing declared it and no package's own "
                               f"licence asserts a restriction — {reach}"), v)

    why = ("declared license_gated=true" if declared and not observed else
           "OBSERVED in the shipped image: " + ", ".join(
               f"{p['name']} {p['version']} — {p['license']!r}" for p in observed[:4]))
    cov = ClauseCoverage("POLICY_CLEAN.license", ("I13",), CHECKED, max(1, len(observed)),
                         f"license-gated ({why}) — checked that it is marked "
                         f"non-redistributable and names its license(s); {reach}")
    if result.get("redistributable", True):
        v.append({"invariant": "I13.gated_not_redistributable", "where": "redistributable",
                  "message": (
                      "license_gated=true requires redistributable=false — a gated tool's "
                      "image must never be marked redistributable."
                      if declared and not observed else
                      f"redistributable=true contradicts the shipped image's own package "
                      f"metadata ({why}). Re-freeze with gated=True + licenses=[…]; that "
                      f"sets redistributable=false and keeps the image out of any registry "
                      f"push. If the licence genuinely permits redistribution, it still has "
                      f"to be DECLARED — this artifact cannot claim it by default.")})
    if not (result.get("licenses") or []):
        v.append({"invariant": "I13.gated_license_recorded", "where": "licenses",
                  "message": "license_gated=true requires at least one entry in licenses[] naming "
                             "the license/terms the artifact is bound by."
                             + ("" if declared and not observed else
                                f" Gatedness here was OBSERVED, not declared ({why}).")})
    return (cov, v)


# ---------------------------------------------------------------------------
# The contract.
# ---------------------------------------------------------------------------

#: The `control` verdicts a producer writes onto a verification record — the result of
#: re-running that evidence in a digest-pinned image that LACKS the tool. Named here
#: because the serve side reads them and the two producers write them
#: (`env_build.verify_in_image`, `freeze_from_image._probe_tools`); three spellings of
#: "discriminating" across three files is how this codebase's defects start.
CONTROL_DISCRIMINATING = "discriminating"   # failed without the tool — the pass means something
CONTROL_VACUOUS = "vacuous"                 # passed without the tool — it proves nothing
CONTROL_UNCHECKED = "unchecked"             # the experiment could not be run — absence, not a pass

#: The invariant ids the base VALIDATED_IN_IMAGE clause accounts for. Spelled out rather
#: than left as the `VALIDATED_IN_IMAGE` prefix because `.discriminates` below is a
#: SEPARATE clause under the same prefix, and the coverage lint requires every emitted id
#: to be claimed by exactly one clause.
_VII_COVERS = ("VALIDATED_IN_IMAGE.no_evidence",
               "VALIDATED_IN_IMAGE.evidence_shape",
               "VALIDATED_IN_IMAGE.evidence_passed")


#: THE LAYER-1 ROSTER, AS DATA — the exact counterpart of `agent/skills/invariants.py`
#: for Layer 2, and created for the same reason.
#:
#: `check_build` emits its clause names at runtime; nothing declared them, so anyone
#: needing to TELL someone what the contract is had to hand-write the list. Two places
#: did, and both went stale in the same direction. `install_pipeline_brief` — the brief
#: handed to a subagent that installs WITHOUT SUPERVISION — listed three guarantees while
#: this module enforced four, so a record with a malformed `shipped_binaries[]` is refused
#: by a clause that brief never mentions. The irony was load-bearing: the comment beside
#: that list explains that the hand-written LAYER-2 roster went stale and was replaced by
#: registry derivation, and the LAYER-1 list immediately next to it was still hand-written
#: and wrong in exactly the same way.
#:
#: `tests/test_invariant_registry.py` asserts every clause `check_build` actually emits is
#: claimed by an entry here, so a fifth guarantee cannot appear without this roster
#: learning about it.
LAYER1_GUARANTEES: tuple[tuple[str, str], ...] = (
    ("BUILT",
     "the env image + image_digest resolve in the local Docker daemon, and the image is "
     "the architecture the record claims (a digest naming NO architecture is a manifest "
     "list — two machines pulling it ship different bytes — and is refused)"),
    ("VALIDATED_IN_IMAGE",
     "every tool's evidence command re-runs green INSIDE the shipped image AND "
     "discriminates — it references the tool as a real token, is not a constant-true "
     "short-circuit or rc-laundered, and on the agent-authored path must FAIL in a "
     "digest-pinned control image that lacks the tool. Because install==ship, the bytes "
     "validated are the bytes that run on HPC"),
    ("POLICY_CLEAN",
     "I12 accelerator honesty (cuda/rocm need toolkit_version; mps is dev_only; "
     "runtime_verified needs a CAPTURED probe + min_driver_version) checked BOTH for "
     "well-formedness and against the toolkit actually read off the shipped image, plus "
     "I13 license firewall (gated => redistributable:false AND licenses[] recorded)"),
    ("PROVENANCE_CLEAN",
     "the firewall around the SYNTHESIS tier (agent-as-generator): a synthesized install "
     "must carry its audit trail into the recipe — every sub-command tagged `extracted` "
     "(lifted verbatim from a named repo file, anchored by that file's sha256) or "
     "`agent_authored`. NOT_APPLICABLE when no step was synthesized"),
    ("WELL_FORMED",
     "every sub-record with a declared model parses — today `shipped_binaries[]` against "
     "ShippedBinary. Asserted at BOTH ends: on write (EnvCache.register) and on serve "
     "(check_build), because a gate only at the producer grandfathers in everything "
     "frozen before it existed"),
)
#: PROVENANCE_CLEAN is the entry this roster was worth writing for. It refuses on three
#: violations (synthesized_empty / untagged_command / extraction_unanchored) and, on
#: 2026-08-07, was named in ZERO steering documents — not CLAUDE.md's honesty-contract
#: table, not `install_pipeline_brief`, not docs/. Nobody had removed it; it was simply
#: never written down, and every hand-written summary of "the Layer-1 contract" therefore
#: described four guarantees where the code enforces five. That is the I5/I10 rot exactly,
#: and the lint above it now makes the same omission impossible to repeat.


# --- What a guarantee is worth over ONE record ------------------------------
# The roster above says what each guarantee MEANS. That is a property of the
# contract, identical for every env. This says what it came to over a PARTICULAR
# record, which is the only thing a reader actually wants to know, and the two
# were never joined until 2026-08-07.
#
# Before that join the ENV report carried a hand-written paragraph per build
# method, emitted unconditionally, asserting "every requested tool re-ran green"
# and "POLICY_CLEAN — I12 and I13 passed". Directly beneath it the generated
# coverage table marked those same clauses `unobserved` or `n/a`. Three records
# on disk shipped that contradiction, and the prose is the half a human reads
# first. Rounding an absent observation up into "passed" is the one thing this
# module exists to prevent, so a page doing it under the heading "How this was
# verified" was the defect at its most expensive.
#
# The fix is not better prose. It is that there IS no prose: a renderer asks
# here, and the answer comes off the same walk the gate ran.
ESTABLISHED = "established"                  # every contributing clause checked, none failed
PARTLY_ESTABLISHED = "partly_established"    # some checked, some had nothing to look at
NOT_ESTABLISHED = "not_established"          # nothing was observed — silence, not evidence
GUARANTEE_NOT_APPLICABLE = "not_applicable"  # precondition genuinely absent; itself a fact
FAILED = "failed"                            # a contributing clause objected

GUARANTEE_VERDICTS = (ESTABLISHED, PARTLY_ESTABLISHED, NOT_ESTABLISHED,
                      GUARANTEE_NOT_APPLICABLE, FAILED)


def _violation_hits(clause: ClauseCoverage, failed_ids: set) -> bool:
    """True when one of `failed_ids` is a violation this clause accounts for.

    Matched through `covers`, never by name similarity: the accelerator clause
    emits `I12.*` and the license clause `I13.*`, so a name-prefix convention
    would be a rule with silent exceptions. Both prefix directions are tried
    because a violation may be recorded at the family (`I12`) or at the specific
    failure (`I12.accel_absent_from_image`)."""
    for fid in failed_ids:
        for c in (*(clause.covers or ()), clause.clause):
            if fid == c or fid.startswith(c + ".") or c.startswith(fid + "."):
                return True
    return False


def guarantee_verdicts(contract: BuildContract) -> list[dict]:
    """One row per Layer-1 guarantee: what it means, and what this record earned.

    THE ONLY reading of "did guarantee X hold for this env". Renderers call it;
    none of them re-derives it, because two derivations of one question is how
    the ENV report's prose and the coverage table beneath it came to disagree.

    Each row: `{guarantee, statement, verdict, clauses[], failed_clauses[],
    observations, checked[], unobserved[], not_applicable[]}`. `verdict` is one
    of GUARANTEE_VERDICTS — five states, because the four that are not
    `established` are four genuinely different things and a boolean flattens all
    of them into the wrong one.
    """
    failed_ids = {v.get("invariant", "") for v in contract.violations
                  if isinstance(v, dict) and v.get("invariant")}
    rows: list[dict] = []
    for name, statement in LAYER1_GUARANTEES:
        mine = [c for c in contract.coverage
                if c.clause == name or c.clause.startswith(name + ".")]
        failed = [c.clause for c in mine if _violation_hits(c, failed_ids)]
        checked = [c for c in mine if c.status == CHECKED]
        unobs = [c for c in mine if c.status == UNOBSERVED]
        na = [c for c in mine if c.status == NOT_APPLICABLE]
        if failed:
            verdict = FAILED
        elif not mine:
            # The registry lint makes this near-impossible, but a guarantee with
            # no clause must READ as unestablished rather than quietly vanish
            # from the list of things the reader was promised.
            verdict = NOT_ESTABLISHED
        elif checked and not unobs:
            verdict = ESTABLISHED
        elif checked:
            verdict = PARTLY_ESTABLISHED
        elif unobs:
            verdict = NOT_ESTABLISHED
        else:
            verdict = GUARANTEE_NOT_APPLICABLE
        rows.append({
            "guarantee": name,
            "statement": statement,
            "verdict": verdict,
            "clauses": [c.clause for c in mine],
            "failed_clauses": failed,
            "observations": sum(c.observations for c in mine),
            "checked": [c.clause for c in checked],
            "unobserved": [c.clause for c in unobs],
            "not_applicable": [c.clause for c in na],
            # WHY it is not established, in the clause's own words. Without this a
            # reader gets a verdict and no reason, and has to go find the coverage
            # table to learn that (say) no evidence was run in the image at all —
            # which is the single most important thing that page can tell them.
            "notes": [c.detail for c in mine
                      if c.status != CHECKED or c.clause in failed],
        })
    return rows


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

    # -- BUILT.platform ---------------------------------------------------
    # Does the artifact actually HAVE the architecture the record says it shipped?
    #
    # `platform` is the caller's REQUEST, echoed onto the record, and until this clause
    # nothing ever compared it to the artifact. Neither did anything else that looks
    # like it did: `validation_locus` is `detect_locus(REQUEST)`, i.e. the request
    # compared against the local daemon — so a record could state its architecture
    # three times over without one of those statements being an observation. Measured
    # on a real arm64 freeze (G3 phase 6): `resolve_biocontainer` takes no platform
    # argument at all, so an arm64 adopt binds bioconda's amd64-only image, and the
    # refusal that followed named the WRONG CAUSE — "the tool is not provably
    # present/runnable in what we ship", when samtools is present and fine and the
    # only thing wrong is that no arm64 build of that image exists.
    #
    # THE OBSERVATION IS THE PRODUCER'S; this clause only COMPARES. freeze reads
    # `.Architecture` off the shipped image and writes `image_arch`; here we hold it
    # against `platform`. Those are two different fields — one an observation of the
    # artifact, one the request — so the comparison means something. (Contrast I5's
    # laundering bug, where an anchor written at seal was compared against itself
    # moments later.) Re-deriving the arch here would also put a `docker image
    # inspect` per env inside `list_installed_pipelines`, which re-earns every
    # record's contract on every call.
    recorded_arch = _locus.target_arch(result.get("platform") or "")
    observed = result.get("image_arch")
    if observed is None:
        # Three-state, like every other clause here: a record written before freeze
        # captured this proves nothing about its architecture either way, and saying
        # so is not the same as failing it. Re-earned by a re-freeze, never a backfill
        # ([[feedback-existing-installs-not-precious]]).
        coverage.append(ClauseCoverage(
            "BUILT.platform", ("BUILT.platform",), UNOBSERVED, 0,
            f"no image_arch recorded — nothing read the architecture off the shipped "
            f"image, so `platform: {result.get('platform') or '?'}` is the request "
            f"repeated back rather than a fact about the artifact"))
    elif not observed:
        # NOT "we could not tell". An empty `.Architecture` on an image that inspects
        # fine means the digest resolves to a MANIFEST LIST / OCI index — a menu of
        # per-arch images, not one image's bytes. `multiqc` in this corpus pins one:
        # a single digest serving an amd64 child and an arm64 child, recorded as
        # `platform: linux/amd64`. Two machines pulling that digest ship different
        # binaries, which is the one thing a content-addressed artifact may not do.
        violations.append({
            "invariant": "BUILT.platform_pinned", "where": "image_digest",
            "message": "the recorded image_digest names no architecture — it resolves to a "
                       "manifest list / OCI index, which addresses a MENU of per-arch images "
                       "rather than one image's bytes, so two machines pulling this digest can "
                       "ship different binaries. Record the per-architecture child digest for "
                       "the platform actually validated (docker manifest inspect <ref> lists "
                       "them) instead of the index digest."})
        coverage.append(ClauseCoverage(
            "BUILT.platform", ("BUILT.platform",), CHECKED, 1,
            "the recorded digest resolves to a manifest index, so it names no architecture"))
    elif recorded_arch and observed != recorded_arch:
        violations.append({
            "invariant": "BUILT.platform_matches", "where": "platform",
            "message": f"record claims platform '{result.get('platform')}' ({recorded_arch}) but the "
                       f"shipped image is {observed} — the artifact does not have the architecture "
                       f"this record advertises, and an HPC target that honours it will get bytes it "
                       f"cannot run. On the adopt path this is the usual shape: bioconda publishes "
                       f"linux-64 biocontainers only, so an arm64 request binds an amd64 image. Ask "
                       f"for the architecture the artifact really is, or take the container-native "
                       f"build path, which builds FOR the requested platform and does produce a "
                       f"genuine arm64 image."})
        coverage.append(ClauseCoverage(
            "BUILT.platform", ("BUILT.platform",), CHECKED, 1,
            f"the shipped image is {observed}; the record claims {recorded_arch}"))
    elif not recorded_arch:
        violations.append({
            "invariant": "BUILT.platform_matches", "where": "platform",
            "message": f"the shipped image is {observed}, but the record's platform "
                       f"'{result.get('platform')}' is not a spelling this system recognises "
                       f"(agent/skills/locus.py:_ARCH_TOKENS), so the claim cannot be checked "
                       f"against the artifact at all."})
        coverage.append(ClauseCoverage(
            "BUILT.platform", ("BUILT.platform",), CHECKED, 1,
            f"the shipped image is {observed}; the recorded platform is unreadable"))
    else:
        coverage.append(ClauseCoverage(
            "BUILT.platform", ("BUILT.platform",), CHECKED, 1,
            f"the shipped image really is {observed}, the architecture the record claims"))

    # -- VALIDATED_IN_IMAGE ----------------------------------------------
    # Every declared tool's evidence must (a) have a non-cheat shape and (b) have
    # PASSED when re-run in the shipped image. (a) is the carried I2 knowledge;
    # (b) is validated==shipped — strictly stronger than the host verify.
    verifications = [v for v in (result.get("verifications") or []) if isinstance(v, dict)]
    if not verifications:
        coverage.append(ClauseCoverage("VALIDATED_IN_IMAGE", _VII_COVERS, UNOBSERVED, 0,
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
        # STATE WHAT THE HEURISTIC SAW, NOT WHAT IS TRUE. This read
        # "{deep} of them RUN the tool (the rest are presence/version/help probes)" — an
        # assertion of fact about the commands, sourced from a classifier whose own
        # docstring says "a string cannot tell you what a command does at runtime; this
        # reads structure and is wrong often enough that gating on it was measured to
        # refuse the CORRECT artifact".
        #
        # Measured 2026-08-07 over the 35 real evidence commands in the corpus: 7 classify
        # as `import`, and 5 of those 7 demonstrably run the tool —
        #   `perl -MStatistics::Descriptive … $s->add_data(1,2,3,4,100); die unless …`
        #   `Rscript -e "library(BiocGenerics); x <- BiocGenerics::union(c(1,2,3), c(3,4)) …"`
        #   `Rscript -e 'library(DESeq2); … DESeq(dds); res <- results(dds); stopifnot(nrow(res)==200)'`
        # — because the load pattern (`library(`, `perl -M`, `import`) is tested BEFORE
        # the functional signals and claims the command first. So `rnaseq_deseq2`'s page
        # said "0 of them RUN the tool" about a command that runs DESeq() and asserts on
        # its result. A flat falsehood, stated with the confidence of a measurement.
        #
        # Correctly SEPARATING those five from the two real imports
        # (`python -c 'import pyfaidx; assert pyfaidx.Fasta is not None'`) needs a
        # classifier that reasons about residue rather than patterns, which is a change
        # with its own blast radius and is not made here. What is fixed is the CLAIM: the
        # line now reports a structural reading as a structural reading. Depth is
        # disclosure and never gates, so under-claiming costs a reader nothing; asserting
        # it as fact cost them the truth.
        coverage.append(ClauseCoverage(
            "VALIDATED_IN_IMAGE", _VII_COVERS, CHECKED, len(verifications),
            f"{len(verifications)} evidence command(s) re-run in the shipped image; "
            f"{deep} of them LOOK like a functional run to a structural reading of the "
            f"command text (the rest read as presence/version/help/load probes). That "
            f"reading is a heuristic over strings, not an observation of what ran — it "
            f"under-reports commands that load a library and then use it "
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

    # -- VALIDATED_IN_IMAGE.discriminates --------------------------------
    # THE SERVE-SIDE HALF of the control experiment. The two producers re-run each
    # PASSING evidence in a digest-pinned image that lacks the tool and refuse to
    # register when it passes there too. That gate lives at the WRITER, and tier 5's
    # lesson is that a writer-only gate grandfathers every record written before it
    # existed. Measured when this landed: 9 of the 10 records in the EnvCache carried
    # no `control` verdict at all, and every one of them served `contract_ok: true`
    # with nothing behind it but the string rule the audit had already walked through
    # (`true || samtools`, `true # samtools`, and the trailing pipe that started this).
    #
    # This function is PURE — it cannot run a container, so it cannot re-run the
    # experiment. It can only report who asked. That asymmetry decides both verdicts:
    #
    #   vacuous            VIOLATION. The record states IN ITS OWN BYTES that its
    #                      evidence passes without the tool. Refusing that needs no
    #                      container, and the writer's refusal is not a reason for the
    #                      reader to stay silent — that is the whole tier-5 lesson.
    #   absent/unchecked   UNOBSERVED, never a violation. "Nobody asked" is not "it
    #                      failed": refusing here would condemn correct records over
    #                      missing data, and passing silently is the defect being
    #                      fixed. The third state is the only honest answer, and it
    #                      costs the record its `proven` tag and prints the gap.
    #
    # A re-freeze earns it back ([[feedback-existing-installs-not-precious]]). There is
    # deliberately NO backfill: a `control` field written without running the control
    # is the anchor-laundering I5 already paid for once.
    # `unasked` is the COMPLEMENT of a closed set of verdicts, not a match on the two
    # spellings we happen to know ("unchecked", absent). An unrecognized verdict from a
    # future producer must land in the gap, not sail through as if it had been asked.
    _ASKED = (CONTROL_DISCRIMINATING, CONTROL_VACUOUS)
    passing = [v for v in verifications if v.get("passed")]
    vacuous = [v for v in passing if v.get("control") == CONTROL_VACUOUS]
    asked = [v for v in passing if v.get("control") in _ASKED]
    unasked = [v for v in passing if v.get("control") not in _ASKED]
    for ver in vacuous:
        label = ver.get("label", "?")
        violations.append({
            "invariant": "VALIDATED_IN_IMAGE.vacuous_evidence",
            "where": f"verifications[{label}]",
            "message": f"{label}: evidence {ver.get('check','')!r} is recorded as VACUOUS — it "
                       f"exits 0 in a control image that does NOT contain the tool "
                       f"({ver.get('control_note') or 'no note recorded'}), so its pass in the "
                       f"shipped image proves nothing about the shipped image. Re-freeze with "
                       f"evidence that runs the tool."})
    _disc = "VALIDATED_IN_IMAGE.discriminates"
    _disc_covers = ("VALIDATED_IN_IMAGE.vacuous_evidence",)
    if vacuous:
        # It looked and it OBJECTED — CHECKED, even if other evidence went unasked. A
        # clause that raised a violation must never report that it declined to look.
        coverage.append(ClauseCoverage(
            _disc, _disc_covers, CHECKED, len(asked),
            f"{len(asked)} of {len(passing)} passing evidence command(s) were re-run in a "
            f"control image without the tool, and {len(vacuous)} passed there too (VACUOUS)"))
    elif passing and not unasked:
        coverage.append(ClauseCoverage(
            _disc, _disc_covers, CHECKED, len(asked),
            f"all {len(asked)} passing evidence command(s) FAILED in a control image without "
            f"the tool, so each pass in the shipped image is about the shipped image"))
    elif not passing:
        # NOT `NOT_APPLICABLE`, though the producers do write `control: "not_applicable"`
        # on an individual failing verification. That is a different question: theirs is
        # "was a control run owed for THIS evidence?" (no — paying to learn why a failure
        # failed buys nothing); this clause's is "did we establish that this IMAGE's
        # evidence discriminates?" — and the answer is no, we established nothing.
        #
        # NOT_APPLICABLE would mean the absence is itself a FACT about the artifact, the
        # way "claims no GPU" is. An artifact with no passing evidence is not a fact, it
        # is a broken artifact, and the attestation proves the difference matters: a
        # NOT_APPLICABLE clause earns a place in `honesty_contract`, so an adopt record
        # with no evidence at all would have claimed a validation guarantee — the exact
        # thing L4's roundtrip test exists to forbid.
        coverage.append(ClauseCoverage(
            _disc, _disc_covers, UNOBSERVED, 0,
            "no evidence passed in the shipped image, so nothing here establishes that "
            "its evidence can tell this image from one without the tool"))
    else:
        coverage.append(ClauseCoverage(
            _disc, _disc_covers, UNOBSERVED, 0,
            f"{len(unasked)} of {len(passing)} passing evidence command(s) were never re-run in "
            f"a control image lacking the tool "
            f"({', '.join(sorted((v.get('label') or '?') for v in unasked))}) — so nothing here "
            f"distinguishes this image from an image without the tool. Re-freeze to earn it"))

    # -- POLICY_CLEAN + PROVENANCE_CLEAN ---------------------------------
    # PROVENANCE_CLEAN is the firewall around the synthesis tier: a synthesized
    # (agent-as-generator) install must carry its provenance into the recipe, so
    # nothing untraceable to the tool's own files ships.
    for cov, viol in (_clause_accelerator(result.get("accelerator")),
                      _clause_accelerator_observed(result.get("accelerator"),
                                                   result.get("image_accelerator")),
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
