"""
invariants — the ONE declaration of what each numbered invariant is and where it lives.

WHY THIS EXISTS. The invariant roster was an un-generated, un-linted second copy of
itself, and it drifted in every copy at once. Measured 2026-07-31:

  * `check_workflow_invariants` refuses on I0, I3, I5, I6, I7, I8, I10.
  * CLAUDE.md's Layer-2 table listed I0, I3, I4, I6, I7, I8 — missing I5 and I10
    ENTIRELY, and listing I4, which that function does not emit (I4 is real, but it
    is enforced by the usage self-test, one seam over).
  * CLAUDE.md's prose asserted, twice, that I5 and I10 had been "retired" and
    "subsumed" — of the two live clauses it forgot to list.
  * spec_writer's own module docstring said I0/I3/I6/I7/I8 in four places while the
    constant six lines below it said seven ids.

CLAUDE.md IS THE AGENT'S PROMPT. A stale roster there is not a documentation defect,
it is a runtime one: the agent plans against invariants that do not exist, omits
fields for invariants it was told were retired, and is then refused by a gate it had
no reason to expect. This costs a real turn every session it happens.

So the roster is DATA, in one place, and `tests/test_invariant_registry.py` fails the
build when:
  * a clause emits an id this registry does not declare (a new invariant appeared and
    was never written down), or
  * this registry declares an ACTIVE id nothing emits (an invariant was removed and
    the roster kept advertising it), or
  * CLAUDE.md mentions an id the registry does not know, or its Layer-2 table does not
    match the registry exactly.

The point is NOT to be a second description of the checks. Each `statement` is one
line, the clause bodies stay authoritative about their own logic, and `enforced_by`
names the function that actually refuses — so a reader who wants the truth is one
grep from it rather than one paragraph from a paraphrase of it.
"""
from __future__ import annotations

from dataclasses import dataclass

ACTIVE = "active"
RETIRED = "retired"

#: Layer 1 = the env image (`env_honesty.check_build`). Layer 2 = the workflow run
#: (`spec_writer.check_workflow_invariants` + the usage self-test).
LAYER_ENV = 1
LAYER_WORKFLOW = 2


@dataclass(frozen=True)
class Invariant:
    id: str
    """`I3`, `I12`, … The prefix every violation this invariant raises begins with."""
    layer: int
    """LAYER_ENV or LAYER_WORKFLOW. RETIRED entries keep the layer they were retired FROM."""
    status: str
    """ACTIVE or RETIRED."""
    statement: str
    """ONE line: what must be true. Not a description of the implementation."""
    enforced_by: str
    """Dotted path of the function that REFUSES on it — the authority, so a reader who
    doubts this line can go read the code instead of a longer version of this line."""
    note: str = ""
    """For RETIRED: what absorbed it, and why that is not a loss of coverage."""


def _inv(**kw) -> Invariant:
    return Invariant(**kw)


REGISTRY: dict[str, Invariant] = {inv.id: inv for inv in [
    # ---- Layer 2: the workflow run -------------------------------------------------
    _inv(id="I0", layer=LAYER_WORKFLOW, status=ACTIVE,
         statement="every top-level list-of-records holds only dicts (shape sanity)",
         enforced_by="agent.skills.spec_writer.check_workflow_invariants"),
    _inv(id="I3", layer=LAYER_WORKFLOW, status=ACTIVE,
         statement="every rc=0 pipeline_step has validated detected_outputs, none typed "
                   "'any', and no validation record says passed=False",
         enforced_by="agent.skills.spec_writer.check_workflow_invariants"),
    _inv(id="I4", layer=LAYER_WORKFLOW, status=ACTIVE,
         statement="usage.command_template executes against every declared trial and each "
                   "produced file passes type-aware validation",
         # NOT check_workflow_invariants: I4 needs a RUNNER (a frozen image or a host env),
         # so it is a separate execution rather than a structural walk, and seal refuses on
         # it with `seal.usage_self_test_failed`. Being enforced elsewhere is exactly why
         # it kept appearing in and disappearing from hand-written rosters.
         enforced_by="agent.skills.spec_writer.self_test_usage",
         note="run by seal_workflow; sets usage_verified. Emits no I4-prefixed violation."),
    _inv(id="I5", layer=LAYER_WORKFLOW, status=ACTIVE,
         statement="every declared reference_database still exists, is non-empty, and "
                   "hashes to what was recorded (locus-aware: a cluster DB is checked over ssh)",
         enforced_by="agent.skills.spec_writer.check_workflow_invariants",
         note="RESTORED at Layer 2 after the respine retired it as an env-build invariant. "
              "CLAUDE.md went on calling it retired for months while it refused real seals."),
    _inv(id="I6", layer=LAYER_WORKFLOW, status=ACTIVE,
         statement="every input/output path is absolute and every {PLACEHOLDER} in "
                   "usage.command_template is declared",
         enforced_by="agent.skills.spec_writer.check_workflow_invariants"),
    _inv(id="I7", layer=LAYER_WORKFLOW, status=ACTIVE,
         statement="every rc=0 pipeline_step records real resource_usage (wall, peak RSS, "
                   "peak CPU) — not absent, not all zeros, not a capture error",
         enforced_by="agent.skills.spec_writer.check_workflow_invariants"),
    _inv(id="I8", layer=LAYER_WORKFLOW, status=ACTIVE,
         statement="every pipeline_step input traces to a prior step's output or a declared "
                   "external source, and every traced artifact still hashes to what was recorded",
         enforced_by="agent.skills.spec_writer.check_workflow_invariants"),
    _inv(id="I10", layer=LAYER_WORKFLOW, status=ACTIVE,
         statement="every declared service_dependency has at least one HEALTHY probe in its "
                   "health_check_log",
         enforced_by="agent.skills.spec_writer.check_workflow_invariants",
         note="RESTORED at Layer 2, same story as I5: retired as an env-build invariant, "
              "brought back as a run-side one, never re-listed in the roster. "
              "KNOWN VACUOUS, and no signal exists to fix it: the clause iterates "
              "service_dependencies, so it examines nothing on all 5 sealed specs and all "
              "3 drafts on disk. Nothing in a draft or a spec distinguishes 'this run "
              "needed no service' from 'nobody declared one' — a search of every recorded "
              "command, usage template and runtime_config for localhost/port/daemon tokens "
              "returns zero hits, and the env SBOM is both absent from the spec and noisy "
              "(the talos env ships pyspark). Investigated 2026-07-31 and recorded rather "
              "than papered over with a coverage state: inventing UNOBSERVED here would "
              "degrade 5/5 correct workflows, and inventing NOT_APPLICABLE would be a label "
              "on silence. The absence is real; it is just not currently distinguishable "
              "from an omission."),

    # ---- Layer 1: the env image ----------------------------------------------------
    _inv(id="I12", layer=LAYER_ENV, status=ACTIVE,
         statement="accelerator claims are structurally honest — cuda/rocm need a "
                   "toolkit_version, runtime_verified needs a probe + min_driver_version, "
                   "mps must be dev_only",
         enforced_by="agent.skills.env_honesty.check_build",
         note="the POLICY_CLEAN.accelerator coverage clause."),
    _inv(id="I13", layer=LAYER_ENV, status=ACTIVE,
         statement="a license-gated artifact is marked non-redistributable and names its "
                   "license(s)",
         enforced_by="agent.skills.env_honesty.check_build",
         note="the POLICY_CLEAN.license coverage clause. Reads gatedness via "
              "core_data.record_is_gated so the legacy `gated` key cannot slip past."),

    # ---- Retired: absorbed by the container-native locus ---------------------------
    # These were env-build invariants in the HOST model, where install and ship were two
    # events that could drift, so every claim had to be re-anchored at finalize. Building
    # and validating INSIDE the shipped image collapsed the two events into one; there is
    # no second event to drift from, so the re-anchoring walks have nothing left to do.
    # They are listed because CLAUDE.md still refers to them by number and a reader must
    # be able to look one up and find out it is gone, rather than find nothing.
    _inv(id="I1", layer=LAYER_ENV, status=RETIRED,
         statement="every declared package is present in the built env",
         enforced_by="agent.skills.env_honesty.check_build",
         note="absorbed by BUILT + VALIDATED_IN_IMAGE — a clean image cannot resolve a "
              "tool it does not carry."),
    _inv(id="I2", layer=LAYER_ENV, status=RETIRED,
         statement="every tool's verify command passed",
         enforced_by="agent.skills.env_honesty.check_build",
         note="became VALIDATED_IN_IMAGE, in a strictly stronger form: the evidence re-runs "
              "in the SHIPPED image, and its anti-cheat shape rule is what survives of I2."),
    _inv(id="I9", layer=LAYER_ENV, status=RETIRED,
         statement="a source install records the commit sha it was built from",
         enforced_by="agent.skills.env_honesty.check_build",
         note="the `git checkout <ref>` runs inside the RUN that becomes the image; a bad "
              "ref fails the build and there is no image to ship."),
    _inv(id="I11", layer=LAYER_ENV, status=RETIRED,
         statement="an authored file's sha256 matches what was recorded",
         enforced_by="agent.skills.env_honesty.check_build",
         note="the bytes are baked into the image by the same RUN that hashes them."),
    _inv(id="I14", layer=LAYER_ENV, status=RETIRED,
         statement="a release binary's sha256 matches what was recorded",
         enforced_by="agent.skills.env_honesty.check_build",
         note="the download's `sha256sum -c` runs inside the RUN; a mismatch fails the build."),
]}


def active(layer: int = 0) -> list[Invariant]:
    """Active invariants, optionally for one layer, in numeric id order."""
    out = [i for i in REGISTRY.values()
           if i.status == ACTIVE and (layer == 0 or i.layer == layer)]
    return sorted(out, key=lambda i: int(i.id[1:]))


def retired() -> list[Invariant]:
    return sorted((i for i in REGISTRY.values() if i.status == RETIRED),
                  key=lambda i: int(i.id[1:]))


def enforced_by(fn_path: str) -> set[str]:
    """The ids a given enforcer is responsible for — what the lint compares against the
    ids that function actually emits."""
    return {i.id for i in REGISTRY.values()
            if i.status == ACTIVE and i.enforced_by == fn_path}


def describe(invariant_id: str) -> str:
    """One line for a violation message / a report cell, or a clear miss."""
    inv = REGISTRY.get(invariant_id.split(".", 1)[0])
    if inv is None:
        return f"{invariant_id} (not in the invariant registry)"
    tag = "" if inv.status == ACTIVE else " [RETIRED]"
    return f"{inv.id}{tag}: {inv.statement}"
