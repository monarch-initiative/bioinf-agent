"""
Contract coverage — the THIRD state, and the lint that keeps it honest.

`check_build` answers a binary question over a domain with three states:
checked-and-good, checked-and-bad, and NOTHING TO CHECK. Before coverage existed
the third collapsed into the first, so a clause whose subject was absent returned
"no violations" and a green badge could rest on clauses that never looked at
anything (audit 2026-07-30: `outcomes.degraded` had zero call sites in 33k LOC
because there was nothing to attach it to).

These tests defend three properties, in descending order of how much they matter:

  1. NOT_APPLICABLE and UNOBSERVED stay DISTINCT. Collapsing them restores the
     defect. This is the whole point of the module.
  2. Coverage and violations stay JOINED. Every invariant a clause can emit is
     claimed by some clause's `covers`, so adding a clause without its coverage
     entry — the way this would quietly rot back to nothing — breaks the build.
  3. The gate is UNCHANGED. Coverage is disclosure; it must not move any
     artifact's pass/fail.
"""
from __future__ import annotations

import pytest

from agent.skills import env_honesty as eh


def _clean_record(**over) -> dict:
    """A record that passes the contract with every clause observed."""
    rec = {
        "image": "x:1",
        "image_digest": "sha256:abc",
        # Same shape as `control` below, one clause over: `platform` alone is the
        # caller's REQUEST, and a record carrying only that has had nothing look at the
        # artifact's architecture, so BUILT.platform reads UNOBSERVED. `image_arch` is
        # the observation read off the shipped image, and a fully-observed record is by
        # definition one that HAS it.
        "platform": "linux-64",
        "image_arch": "amd64",
        # `control` is part of "every clause observed": without it the record cannot say
        # its evidence distinguishes this image from any image, and the discriminates
        # clause below reads UNOBSERVED. A fixture that omitted it would quietly stop
        # being the fully-observed record this helper promises.
        "verifications": [{"label": "samtools", "tool": "samtools",
                           "check": "samtools --version", "rc": 0, "passed": True,
                           "control": "discriminating",
                           "control_note": "fails in a control image without the tool (rc=127)"}],
        "shipped_binaries": [{"tool": "samtools", "version": "1.21", "provenance": "conda",
                              "install_command": None, "tier": None, "verified": None,
                              "assurance": None}],
        "tool_identities": [{"tool": "samtools", "self_description": "SAM tools",
                             "source": "conda", "package": "samtools", "version": "1.21"}],
    }
    rec.update(over)
    return rec


def _claims(clause: eh.ClauseCoverage, invariant: str) -> bool:
    """Does this clause account for that invariant id?

    ONE reading, used by both lints below. They used to differ: the ownership lint
    matched `covers` exactly-or-as-a-dotted-prefix, while the not-applicable lint
    compared only the leading segment. That was indistinguishable while every prefix had
    exactly one clause — and wrong the moment `VALIDATED_IN_IMAGE.discriminates` joined
    `VALIDATED_IN_IMAGE`, because a sibling's violation read as this clause's."""
    return any(invariant == p or invariant.startswith(p + ".") for p in clause.covers)


def _clause(contract, name: str) -> eh.ClauseCoverage:
    for c in contract.coverage:
        if c.clause == name:
            return c
    raise AssertionError(f"no coverage clause {name!r} in {[c.clause for c in contract.coverage]}")


# ---------------------------------------------------------------------------
# 1. The three states are genuinely three.
# ---------------------------------------------------------------------------

def test_not_applicable_and_unobserved_are_distinct_states():
    """THE point of this module. Both produce zero violations and zero observations;
    they mean opposite things. `not_applicable` = the precondition is absent and that
    absence is a FACT about the artifact (it claims no GPU). `unobserved` = the subject
    should exist and does not, so the clause's silence is NOT evidence. A reader who
    cannot tell them apart is back to reading absence as compliance."""
    rec = _clean_record()
    rec.pop("shipped_binaries")
    c = eh.evaluate_build(rec)

    assert c.ok, "neither state is a violation"
    assert _clause(c, "POLICY_CLEAN.accelerator").status == eh.NOT_APPLICABLE
    assert _clause(c, "WELL_FORMED.shipped_binaries").status == eh.UNOBSERVED
    assert [x.clause for x in c.unobserved] == ["WELL_FORMED.shipped_binaries"]
    # ...and the distinction survives into the human summary and the JSON payload.
    assert "UNOBSERVED" in c.summary() and "not applicable" in c.summary()
    assert c.as_dict()["fully_observed"] is False


def test_fully_observed_record_has_no_unobserved_clauses():
    c = eh.evaluate_build(_clean_record())
    assert c.ok and not c.unobserved
    assert c.as_dict()["fully_observed"] is True
    assert c.observations == sum(x.observations for x in c.checked)


def test_a_clause_that_objects_is_never_reported_as_not_applicable():
    """A clause cannot both decline to look and complain about what it saw. This is the
    consistency the single-walk design buys: the coverage verdict is decided at the SAME
    early-return that decides whether to check, so the two cannot drift apart."""
    for rec in (
        _clean_record(image=""),                                   # BUILT
        _clean_record(verifications=[]),                           # VALIDATED_IN_IMAGE
        _clean_record(license_gated=True, licenses=[], redistributable=True),   # I13
        _clean_record(accelerator={"type": "cuda"}),               # I12
        _clean_record(shipped_binaries=[{"nonsense": 1}]),         # WELL_FORMED
    ):
        c = eh.evaluate_build(rec)
        assert c.violations, rec
        for cl in c.coverage:
            if cl.status == eh.NOT_APPLICABLE:
                mine = [v["invariant"] for v in c.violations if _claims(cl, v["invariant"])]
                assert not mine, (
                    f"{cl.clause} declared itself not-applicable yet emitted {mine}")


# ---------------------------------------------------------------------------
# 2. The join between the two halves is enforced, not asserted in prose.
# ---------------------------------------------------------------------------

def test_every_violation_is_claimed_by_exactly_one_coverage_clause():
    """THE ANTI-DRIFT LINT. Coverage is only trustworthy if it accounts for the whole
    contract. Each violation's invariant id must be claimed by exactly one clause's
    `covers`; a new clause added without its coverage entry (or a coverage entry that
    stops matching its invariant ids) fails here rather than silently shrinking the
    disclosure back toward nothing."""
    provoke = [
        _clean_record(image="", image_digest=""),
        _clean_record(verifications=[]),
        _clean_record(verifications=[{"label": "x", "tool": "samtools",
                                      "check": "echo hi", "rc": 0, "passed": True}]),
        _clean_record(verifications=[{"label": "x", "tool": "samtools",
                                      "check": "samtools --version", "rc": 1, "passed": False}]),
        _clean_record(verifications=[{"label": "x", "tool": "samtools",
                                      "check": "samtools --version", "rc": 0, "passed": True,
                                      "control": "vacuous", "control_note": "passes anywhere"}]),
        _clean_record(accelerator={"type": "cuda"}),
        _clean_record(accelerator={"type": "mps"}),
        _clean_record(accelerator={"type": "cuda", "toolkit_version": "12.1",
                                   "runtime": "runtime_verified"}),
        _clean_record(license_gated=True, licenses=[], redistributable=True),
        _clean_record(shipped_binaries=[{"nonsense": 1}]),
        _clean_record(tool_identities=[{"nonsense": 1}]),
        _clean_record(longtail_steps=[{"purpose": "p", "provenance": {"source": "synthesized",
                                                                      "commands": []}}]),
        _clean_record(longtail_steps=[{"purpose": "p", "provenance": {
            "source": "synthesized", "commands": [{"provenance": {"source": "guessed"}}]}}]),
        _clean_record(longtail_steps=[{"purpose": "p", "provenance": {
            "source": "synthesized", "commands": [{"provenance": {"source": "extracted"}}]}}]),
    ]
    seen = set()
    for rec in provoke:
        c = eh.evaluate_build(rec)
        assert c.violations, f"fixture provoked nothing: {rec}"
        for v in c.violations:
            inv = v["invariant"]
            seen.add(inv)
            owners = [cl.clause for cl in c.coverage if _claims(cl, inv)]
            assert len(owners) == 1, (
                f"invariant {inv!r} is claimed by {owners} — every violation must be "
                f"accounted for by exactly one coverage clause")
    # A canary on the fixture set itself: if a clause is added and this list is not
    # extended, coverage of the LINT silently drops even though the lint still passes.
    assert len(seen) >= 17, f"only provoked {len(seen)} distinct invariants: {sorted(seen)}"


def test_coverage_fields_are_well_formed():
    c = eh.evaluate_build(_clean_record())
    names = [cl.clause for cl in c.coverage]
    assert len(names) == len(set(names)), f"duplicate coverage clause: {names}"
    for cl in c.coverage:
        assert cl.status in eh.COVERAGE_STATUSES
        assert cl.establishes in (eh.ASSURANCE, eh.DISCLOSURE)
        assert cl.covers, f"{cl.clause} claims no invariant ids"
        assert cl.detail.strip(), f"{cl.clause} has no human detail"
        if cl.status != eh.CHECKED:
            assert cl.observations == 0


# ---------------------------------------------------------------------------
# 3. The gate did not move.
# ---------------------------------------------------------------------------

def test_check_build_is_exactly_the_violations_half():
    """`check_build` keeps its signature and its verdict — coverage is additive
    disclosure, not a new refusal. Everything that gates reads this."""
    for rec in (_clean_record(), _clean_record(image=""), _clean_record(verifications=[])):
        assert eh.check_build(rec) == eh.evaluate_build(rec).violations


def test_an_unobserved_clause_is_never_a_violation():
    """Degrading is not refusing. A record missing an optional disclosure still freezes,
    still registers, still ships — it is just no longer allowed to imply it was checked."""
    rec = _clean_record()
    rec.pop("shipped_binaries")
    rec.pop("tool_identities")
    c = eh.evaluate_build(rec)
    assert c.ok and len(c.unobserved) == 2


# ---------------------------------------------------------------------------
# The disclosure payload + the degrade rule.
# ---------------------------------------------------------------------------

def test_coverage_disclosure_advises_only_when_something_is_unobserved():
    full = eh.coverage_disclosure(eh.evaluate_build(_clean_record()))
    assert "coverage_advisory" not in full
    assert full["contract_coverage"]["fully_observed"] is True

    rec = _clean_record()
    rec.pop("tool_identities")
    gap = eh.coverage_disclosure(eh.evaluate_build(rec))
    assert "WELL_FORMED.tool_identities" in gap["coverage_advisory"]
    assert "disclosure" in gap["coverage_advisory"]


def test_coverage_disclosure_refuses_to_dress_up_a_failing_contract():
    """A green tag must never be derived from a failing contract. The caller refuses
    first; reaching here with violations is a wiring bug, and it says so loudly rather
    than emitting a cheerful coverage summary over a broken artifact."""
    with pytest.raises(AssertionError):
        eh.coverage_disclosure(eh.evaluate_build(_clean_record(image="")))


def test_degrade_rule_ignores_evidence_depth():
    """Deliberate: shallow evidence does NOT degrade the outcome. `evidence_depth` was
    MEASURED (audit 2026-07-16 Tier 2) to classify the correct artifact and the known-
    broken one identically, so it must not drive the primary tag. It is disclosed in the
    coverage detail and nowhere load-bearing."""
    shallow = _clean_record(verifications=[{"label": "s", "tool": "samtools",
                                            "check": "command -v samtools",
                                            "rc": 0, "passed": True,
                                            "control": "discriminating"}])
    c = eh.evaluate_build(shallow)
    assert c.ok and not c.unobserved, "presence-only evidence must not degrade the tag"
    detail = _clause(c, "VALIDATED_IN_IMAGE").detail
    assert "0 of them RUN the tool" in detail, "...but it MUST be disclosed: " + detail


# ---------------------------------------------------------------------------
# 4. The SERVE-SIDE half of the control experiment (tier 5: a producer-only gate
#    grandfathers every record written before it existed).
# ---------------------------------------------------------------------------

def _ver(**over) -> dict:
    v = {"label": "samtools", "tool": "samtools", "check": "samtools --version",
         "rc": 0, "passed": True, "control": "discriminating"}
    v.update(over)
    return v


def test_a_record_whose_evidence_was_never_control_tested_is_not_fully_observed():
    """THE GRANDFATHERING FIX. When this landed, 9 of the 10 records in the real EnvCache
    carried no `control` verdict — the producers had started running the experiment, but
    every record written before that still served `contract_ok: true` on the strength of
    a string rule the audit had already walked through. Absence of the verdict now costs
    the record its fully-observed status, which is what turns `proven` into `degraded`."""
    rec = _clean_record(verifications=[_ver(control=None)])
    del rec["verifications"][0]["control"]
    c = eh.evaluate_build(rec)

    assert c.ok, "nobody asked is not a violation — it must not refuse a correct record"
    assert _clause(c, "VALIDATED_IN_IMAGE.discriminates").status == eh.UNOBSERVED
    assert c.as_dict()["fully_observed"] is False
    advisory = eh.coverage_disclosure(c)["coverage_advisory"]
    assert "VALIDATED_IN_IMAGE.discriminates" in advisory and "assurance" in advisory


def test_an_unchecked_control_is_absence_not_compliance():
    """`unchecked` means the experiment could not be run (no docker, no network). The
    producer already refuses to treat that as a pass; the reader must not either."""
    c = eh.evaluate_build(_clean_record(verifications=[_ver(control="unchecked")]))
    assert c.ok and _clause(c, "VALIDATED_IN_IMAGE.discriminates").status == eh.UNOBSERVED


def test_an_unrecognized_control_verdict_lands_in_the_gap_not_the_pass():
    """`unasked` is the COMPLEMENT of a closed set, so a verdict from a future producer
    that this reader does not understand degrades rather than sails through. The opposite
    default — match the spellings we know are bad — is how a new value becomes a silent
    pass."""
    c = eh.evaluate_build(_clean_record(verifications=[_ver(control="probably_fine")]))
    assert _clause(c, "VALIDATED_IN_IMAGE.discriminates").status == eh.UNOBSERVED


def test_a_record_that_admits_its_own_evidence_is_vacuous_is_REFUSED_at_serve_time():
    """The one case the pure reader can gate on with no container: the record states in
    its own bytes that its evidence passes in an image without the tool. The producer
    refuses to write this — and that is exactly why the reader must refuse it too, or
    the check only ever applies to records written after the check existed."""
    c = eh.evaluate_build(_clean_record(
        verifications=[_ver(control="vacuous",
                            control_note="also exits 0 in a stock debian without the tool")]))
    assert not c.ok
    v = [x for x in c.violations if x["invariant"] == "VALIDATED_IN_IMAGE.vacuous_evidence"]
    assert len(v) == 1 and "proves nothing" in v[0]["message"]
    # ...and a clause that objected never reports that it declined to look.
    assert _clause(c, "VALIDATED_IN_IMAGE.discriminates").status == eh.CHECKED


@pytest.mark.parametrize("vers", [
    [],                                                              # nothing declared
    [_ver(rc=1, passed=False, control="not_applicable")],            # declared, all failed
], ids=["no-evidence", "all-evidence-failed"])
def test_no_passing_evidence_establishes_nothing_rather_than_being_not_applicable(vers):
    """UNOBSERVED, NOT `NOT_APPLICABLE` — and the attestation is why the difference is
    load-bearing, not pedantry. A NOT_APPLICABLE clause EARNS a place in
    `honesty_contract`, so calling this inapplicable would have let an adopt record with
    no evidence at all ship an attestation claiming a validation guarantee. That is the
    precise claim L4's roundtrip test exists to forbid. The producers' per-verification
    `control: "not_applicable"` answers a different question (was a control owed for THIS
    evidence) and does not transfer to the clause."""
    c = eh.evaluate_build(_clean_record(verifications=vers))
    assert _clause(c, "VALIDATED_IN_IMAGE.discriminates").status == eh.UNOBSERVED

    from agent.skills.attestation import build_attestation
    guarantees = (build_attestation(_clean_record(verifications=vers, mode="adopt"))
                  ["predicate"]["buildDefinition"]["internalParameters"]["honesty_contract"])
    assert not any(g.startswith("VALIDATED_IN_IMAGE") for g in guarantees), guarantees


def test_the_serve_side_verdicts_are_the_ones_the_producers_actually_write():
    """The two halves must agree on the spelling. This reads the CONSTANTS the producers
    import, so a rename cannot leave the reader matching a string nobody writes."""
    from agent.skills import freeze_from_image as ffi
    assert ffi._evidence_discriminates.__doc__ is not None
    for verdict in (eh.CONTROL_DISCRIMINATING, eh.CONTROL_VACUOUS, eh.CONTROL_UNCHECKED):
        assert f'"{verdict}"' in ffi._evidence_discriminates.__doc__, (
            f"{verdict!r} is not among the verdicts the producer documents writing")


# ---------------------------------------------------------------------------
# I13's legacy key — the drift the canonical reader was written to prevent,
# which had never reached the contract itself.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The evidence-shape rule — the cheap half of the anti-cheat defense.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cheat,why", [
    ("true || samtools",            "constant-true short-circuits past the tool"),
    (": || samtools",               "`:` is the same short-circuit spelled differently"),
    ("true # samtools",             "the token is in a COMMENT the shell never runs"),
    ("test -f /etc/hosts # samtools", "a real probe of something else, tool named in a comment"),
    ("# samtools --version",        "the whole command is a comment"),
    ("samtools --version || true",  "rc laundered: `passed` is true whatever the tool did"),
    ("samtools --version ; true",   "`;` launders rc exactly as `||` does"),
    # MEASURED 2026-08-02, not reasoned: `exomiser --help 2>&1 | head -20` returned
    # rc=0 from a stock debian image containing no exomiser (unpiped: rc=127) — and a
    # freeze had already registered `proven` on exactly that evidence. This case used
    # to live in the ACCEPTS list below; it was moved on the strength of that run, not
    # on a change of opinion.
    ("samtools --version 2>&1 | head -1", "a pipeline exits with its LAST stage's rc, "
                                          "so `head` reports success for a missing tool"),
    ('echo "samtools 99.9.9"',      "the classic library-only echo cheat"),
    ("true",                        "bare constant-true"),
    ("",                            "empty"),
])
def test_shape_rule_rejects_evidence_that_cannot_fail(cheat, why):
    """Each of these referenced the tool as a clean word-boundary token, was not a bare
    echo, and PASSED the previous shape rule — the audit reproduced them by hand, and
    `evidence_depth` rated `true || samtools` as `functional`, the strongest class."""
    assert eh.evidence_shape_violation(cheat, "samtools") is not None, f"{cheat!r}: {why}"


@pytest.mark.parametrize("ok", [
    "samtools --version",
    "command -v samtools",
    "samtools view -H /data/x.bam",
    "samtools --version && true",          # `&&` does NOT launder — failure propagates
    "set -o pipefail; samtools --version 2>&1 | head -1",   # pipefail makes a pipe honest
    "samtools --version > /dev/null",                       # redirect, don't pipe
    "command -v samtools || ( for f in /opt/conda/envs/*/conda-meta/samtools-*.json; do :; done )",
    # A pipe NESTED inside $( ) / quotes / a subshell does not launder the outer rc.
    # freeze's own generated probe contains all three, and the first version of the
    # pipe rule refused it — the reason the rule strips nested spans before looking.
    """command -v samtools || ( for b in $(sed -nE 's|.*"bin/([^"/]+)".*|\\1|p' f | sort -u); do command -v "$b" && exit 0; done; exit 1 )""",
])
def test_shape_rule_accepts_real_evidence(ok):
    """The rules must not cost a single legitimate shape — including freeze's OWN
    auto-generated conda presence probe, which contains both `||` and `:`."""
    assert eh.evidence_shape_violation(ok, "samtools") is None, ok


def test_shape_rule_states_its_own_residue():
    """A quoted token that is never executed still reads as an invocation. Separating an
    executed token from a quoted one needs a shell parser; the differential control run in
    freeze_from_image catches it for free. The gap is REAL and this test pins it, so it is
    a known limitation rather than a surprise — if a future change closes it, this fails
    and gets promoted."""
    assert eh.evidence_shape_violation('[ -n "samtools" ]', "samtools") is None

    # ...and one shape that LOOKS like a cheat and is not: `:` then the tool. The tool
    # really runs and its exit status really is the result.
    assert eh.evidence_shape_violation(": ; samtools", "samtools") is None


# ---------------------------------------------------------------------------
# The differential control run — the LOAD-BEARING half.
# ---------------------------------------------------------------------------

def _control_harness(monkeypatch, *, control_rc: int):
    """Stub freeze_from_image's docker surface. `control_rc` is what the evidence does in
    the CONTROL image — the only variable that matters here."""
    import json as _json

    from agent.skills import container_build as CB
    from agent.skills import freeze_from_image as F

    monkeypatch.setattr(F, "_image_present", lambda image: True)
    monkeypatch.setattr(F, "_image_digest", lambda image: "sha256:" + "ab" * 32)
    monkeypatch.setattr(F, "_run_in_image",
                        lambda image, platform, command, timeout=300, maxlen=400:
                            {"rc": control_rc, "out": "control"} if image == F._CONTROL_IMAGE
                            else {"rc": 0, "out": _json.dumps([])} if "importlib.metadata" in command
                            else {"rc": 0, "out": "ran"})
    monkeypatch.setattr(CB.ContainerBuild, "conda_sbom_from_image", staticmethod(lambda *a, **k: []))
    monkeypatch.setattr(CB.ContainerBuild, "apt_sbom_from_image", staticmethod(lambda *a, **k: []))

    class _Cache:
        def __init__(self):
            self.registered = {}

        def register(self, key, record):
            self.registered[key] = record

    return F, _Cache()


def test_evidence_that_also_passes_without_the_tool_is_refused(tmp_path, monkeypatch):
    """THE LOAD-BEARING ANTI-CHEAT. `evidence` on this path is authored by the agent, and
    a string rule cannot police a string author. So the evidence is re-run in a control
    image that lacks the tool: if it passes there too, it would pass anywhere, and its
    pass in the shipped image proves nothing.

    Note this catches the cheats WITHOUT READING THE COMMAND — including the residue the
    shape rule openly cannot see (a quoted token like `[ -n "samtools" ]`)."""
    F, cache = _control_harness(monkeypatch, control_rc=0)   # passes in the control too
    out = F.freeze_from_image(
        image="img@sha256:abc", name="t", version="1",
        tools=[{"name": "samtools", "evidence": '[ -n "samtools" ]'}],
        build_method="adopt-image", env_cache=cache, reports_dir=tmp_path)

    assert out["outcome"] == "refused", out
    assert out["code"] == "freeze_from_image.vacuous_evidence"
    assert "samtools" in out["vacuous_evidence"][0]
    assert not cache.registered, "a vacuous-evidence image must never reach the cache"


def test_evidence_that_fails_without_the_tool_is_accepted(tmp_path, monkeypatch):
    """The control is a discriminator, not a blanket refusal: evidence that distinguishes
    the two images is exactly what we want, and is recorded as such."""
    F, cache = _control_harness(monkeypatch, control_rc=127)  # absent in the control
    out = F.freeze_from_image(
        image="img@sha256:abc", name="t", version="1",
        tools=[{"name": "samtools", "evidence": "samtools --version"}],
        build_method="adopt-image", env_cache=cache, reports_dir=tmp_path)

    assert out["outcome"] in ("proven", "degraded"), out
    assert cache.registered
    ver = list(cache.registered.values())[0]["verifications"][0]
    assert ver["control"] == "discriminating"


def test_control_that_cannot_run_is_recorded_as_unchecked_not_as_a_pass(monkeypatch):
    """If the control cannot run (no docker, no network), that is `unchecked` — recorded
    as the absence of a check. The one thing it must never become is a silent pass, which
    is the failure mode this whole workstream exists to remove."""
    from agent.skills import freeze_from_image as F

    monkeypatch.setattr(F, "_image_present", lambda image: False)
    monkeypatch.setattr(F, "_sh", lambda argv, timeout=300: {"rc": 1, "out": "", "err": "no network"})
    verdict, note = F._evidence_discriminates("linux/amd64", "samtools --version")
    assert verdict == "unchecked" and "unavailable" in note

    def _boom(*a, **k):
        raise OSError("docker daemon not running")
    monkeypatch.setattr(F, "_image_present", _boom)
    assert F._evidence_discriminates("linux/amd64", "samtools --version")[0] == "unchecked"


def test_attestation_guarantees_are_read_off_the_contract_not_the_mode():
    """The in-toto predicate is what an EXTERNAL verifier consumes, so it is the last
    place that may assert an unfalsifiable claim. Its guarantee list used to be a literal
    on the build branch and a hand-rolled re-derivation on the adopt branch — a second
    implementation of "did this clause examine anything", deciding honesty by its own
    rules rather than the contract's."""
    from agent.skills.attestation import build_attestation

    def _params(rec):
        return build_attestation(rec, base_image="")["predicate"]["buildDefinition"][
            "internalParameters"]

    ip = _params(_clean_record(mode="build"))
    assert "BUILT" in ip["honesty_contract"] and "VALIDATED_IN_IMAGE" in ip["honesty_contract"]
    # a not-applicable policy clause still REACHED A VERDICT, so it is genuinely guaranteed
    assert "POLICY_CLEAN.license" in ip["honesty_contract"]
    # ...but a DISCLOSURE clause is not a guarantee, however well it parsed
    assert not any(g.startswith("WELL_FORMED") for g in ip["honesty_contract"])

    ip_adopt = _params(_clean_record(mode="adopt"))
    assert ip_adopt["honesty_contract"][0] == "ADOPTED_BY_DIGEST"

    # A clause that LOOKED and OBJECTED must never be listed as a guarantee. `checked`
    # means the clause ran, not that it approved — conflating the two claimed a failing
    # WELL_FORMED as an earned guarantee in the first cut of this derivation.
    bad = _params(_clean_record(mode="build", shipped_binaries=[{"nonsense": 1}]))
    assert "WELL_FORMED.shipped_binaries" in bad["contract_coverage"]["failed"]
    assert "WELL_FORMED.shipped_binaries" not in bad["honesty_contract"]

    # An unobserved clause is disclosed, not silently dropped: a verifier reading only
    # the earned list cannot tell a short list from a complete one.
    rec = _clean_record(mode="build")
    rec.pop("tool_identities")
    gap = _params(rec)["contract_coverage"]
    assert [u["clause"] for u in gap["unobserved"]] == ["WELL_FORMED.tool_identities"]
    assert gap["not_applicable"] and all(u["why"] for u in gap["unobserved"])


def test_i13_fires_on_the_legacy_gated_key():
    """Records on disk carry `gated`, not `license_gated`. The contract read only the
    canonical name, so a legacy gated record passed I13 by default AND reported the
    clause as not-applicable. `core_data.record_is_gated` is the one reader; the contract
    now uses it, which is the only reason it is in a leaf module."""
    legacy = _clean_record(gated=True, redistributable=True, licenses=[])
    c = eh.evaluate_build(legacy)
    assert _clause(c, "POLICY_CLEAN.license").status == eh.CHECKED
    assert {v["invariant"] for v in c.violations} == {
        "I13.gated_not_redistributable", "I13.gated_license_recorded"}

    # ...and the canonical key still wins when both are present.
    both = _clean_record(license_gated=False, gated=True)
    assert _clause(eh.evaluate_build(both), "POLICY_CLEAN.license").status == eh.NOT_APPLICABLE
