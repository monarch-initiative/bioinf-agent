"""
The invariant roster is DATA, and the prose tracks it.

The roster was an un-generated, un-linted second copy of itself and drifted in
every copy at once. Measured 2026-07-31, before `agent/skills/invariants.py`:

  * `check_workflow_invariants` refuses on I0 I3 I5 I6 I7 I8 I10.
  * CLAUDE.md's Layer-2 table listed I0 I3 I4 I6 I7 I8 — missing I5 and I10
    entirely, and listing I4, which that function does not emit.
  * CLAUDE.md's prose asserted TWICE that I5 and I10 were "retired"/"subsumed" —
    the two live clauses it forgot to list.
  * spec_writer's own docstring said I0/I3/I6/I7/I8 in four places while the
    constant six lines below said seven ids.

CLAUDE.md is the agent's PROMPT, so this is a runtime defect, not a docs one: the
agent plans against invariants it was told do not exist and gets refused by a gate
it had no reason to expect.

These tests are the ratchet. Adding a clause without registering it, removing one
without deregistering it, or editing the roster in the prose without editing the
data — each is a build break.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from agent.skills import invariants as reg

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD = ROOT / "CLAUDE.md"

#: Prose the agent is steered by, wherever it lives. `docs/primitives.md` is the
#: long-form primitive reference that was lifted OUT of CLAUDE.md on 2026-07-31 to stop
#: paying 8,806 tokens for it every session.
#:
#: It is listed here because moving governed text out of a linted file is the quietest
#: way to lose a lint. Every "is this claim still true" check below reads CLAUDE.md; had
#: the relocation not brought the new file with it, a stale "I5 is retired" sentence
#: could ride along in docs/primitives.md and the suite would stay green having stopped
#: looking. That is the vacuous-pass shape this repo keeps rediscovering — a check that
#: silently narrows its own scope reads exactly like a check that passes.
STEERING_DOCS = (CLAUDE_MD, ROOT / "docs" / "primitives.md")


def _steering_text() -> str:
    return "\n".join(p.read_text() for p in STEERING_DOCS if p.is_file())


def _emitted_ids(module_rel: str) -> set[str]:
    """Every `I<n>` prefix appearing in an `"invariant": "..."` dict literal in the module.

    MODULE-WIDE, deliberately. The first cut of this lint listed the checker functions by
    name — and immediately missed I5, whose clause lives in a helper the list forgot. A
    hand-maintained list of where-to-look is the same defect as a hand-maintained list of
    what-exists, one level up; it would have to be updated by exactly the change it is
    supposed to catch. Reading the whole module has nothing to fall out of date.

    From the AST rather than by calling: the point is what the SOURCE can emit, including
    branches no fixture reaches. Only real dict literals count, so prose in a docstring or
    a comment cannot fake an entry."""
    tree = ast.parse((ROOT / module_rel).read_text())
    found: set[str] = set()
    for sub in ast.walk(tree):
        if isinstance(sub, ast.Dict):
            for k, v in zip(sub.keys, sub.values):
                if (isinstance(k, ast.Constant) and k.value == "invariant"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    m = re.match(r"(I\d+)", v.value)
                    if m:
                        found.add(m.group(1))
    return found


def test_every_emitted_invariant_id_is_registered():
    """A clause that raises an id nobody wrote down is an invariant the roster — and
    therefore the agent's prompt — does not know about."""
    emitted = (_emitted_ids("agent/skills/spec_writer.py")
               | _emitted_ids("agent/skills/env_honesty.py"))
    assert emitted, "harvested no invariant ids at all — the AST walk has drifted"
    unknown = sorted(emitted - set(reg.REGISTRY))
    assert not unknown, (
        f"{unknown} are emitted by a gate but absent from agent/skills/invariants.py. "
        f"Register them (id, layer, statement, enforced_by) so the roster and the prose "
        f"can track them.")


def test_every_registered_active_invariant_is_actually_enforced():
    """The other direction: a roster that advertises a gate nothing runs is exactly the
    claim this codebase exists not to make."""
    emitted_l2 = _emitted_ids("agent/skills/spec_writer.py")
    emitted_l1 = _emitted_ids("agent/skills/env_honesty.py")

    declared_l2 = reg.enforced_by("agent.skills.spec_writer.check_workflow_invariants")
    assert declared_l2 == emitted_l2, (
        f"registry says check_workflow_invariants enforces {sorted(declared_l2)}, "
        f"the source emits {sorted(emitted_l2)}")

    declared_l1 = reg.enforced_by("agent.skills.env_honesty.check_build")
    active_l1 = {i.id for i in reg.active(reg.LAYER_ENV)}
    assert declared_l1 == active_l1 == emitted_l1, (
        f"registry {sorted(declared_l1)} vs source {sorted(emitted_l1)}")


def test_i4_is_registered_as_enforced_somewhere_other_than_the_structural_walk():
    """I4 needs a RUNNER, so it is not part of the structural walk and emits no
    I4-prefixed violation — which is precisely why it kept drifting in and out of
    hand-written rosters. The registry states where it really lives."""
    i4 = reg.REGISTRY["I4"]
    assert i4.status == reg.ACTIVE
    assert i4.enforced_by.endswith("self_test_usage")
    assert "I4" not in _emitted_ids("agent/skills/spec_writer.py")
    # ...and the seam that refuses on it really exists
    wf = (ROOT / "agent" / "mcp_tools" / "workflow_tools.py").read_text()
    assert "seal.usage_self_test_failed" in wf


def test_registry_entries_are_well_formed():
    for inv in reg.REGISTRY.values():
        assert re.fullmatch(r"I\d+", inv.id), inv.id
        assert inv.layer in (reg.LAYER_ENV, reg.LAYER_WORKFLOW), inv.id
        assert inv.status in (reg.ACTIVE, reg.RETIRED), inv.id
        assert inv.statement and not inv.statement.endswith("."), \
            f"{inv.id}: statement should be one line, no trailing period"
        assert inv.enforced_by.startswith("agent."), inv.id
        if inv.status == reg.RETIRED:
            assert inv.note, f"{inv.id} is retired without saying what absorbed it"


# ---------------------------------------------------------------------------
# CLAUDE.md — the agent's prompt — must track the data.
# ---------------------------------------------------------------------------

def _claude_table_ids() -> list[str]:
    """The ids in CLAUDE.md's Layer-2 invariant table (rows beginning `| I<n> |`)."""
    return re.findall(r"^\| (I\d+) \|", CLAUDE_MD.read_text(), re.M)


def test_claude_md_layer2_table_matches_the_registry():
    """THE ONE THAT MATTERS. This table is what the agent reads to know which gates it
    must satisfy; omitting a live one costs a refused seal and a wasted turn."""
    table = _claude_table_ids()
    expected = [i.id for i in reg.active(reg.LAYER_WORKFLOW)
                if i.enforced_by.endswith("check_workflow_invariants")
                or i.id == "I4"]
    assert sorted(set(table), key=lambda s: int(s[1:])) == expected, (
        f"CLAUDE.md's Layer-2 table lists {table}, the registry says {expected}. "
        f"Update the table (or the registry, if the code really changed).")


def test_claude_md_mentions_no_invariant_the_registry_does_not_know():
    mentioned = {f"I{n}" for n in re.findall(r"\bI(\d{1,2})\b", _steering_text())}
    unknown = sorted(mentioned - set(reg.REGISTRY), key=lambda s: int(s[1:]))
    assert not unknown, (
        f"The steering docs refer to {unknown}, which the registry does not declare. "
        f"Either register them or stop citing them — an id a reader cannot look up is "
        f"worse than no id.")


def test_every_steering_doc_the_lint_claims_to_read_is_actually_there():
    """A path in STEERING_DOCS that does not exist is silently skipped by
    `_steering_text`, so the roster would go on passing while the file it was supposed
    to police had been renamed or deleted. Name the gap instead."""
    missing = [str(p.relative_to(ROOT)) for p in STEERING_DOCS if not p.is_file()]
    assert not missing, (
        f"STEERING_DOCS lists {missing}, which are not on disk. Either restore them or "
        f"drop them from the tuple — a lint that reads nothing reports no problems.")


#: A hand-written roster: three or more invariant ids run together with `/`, `,` or `·`.
#: `I0/I3/I6/I7/I8` was the shape that appeared in six places and was wrong in all six.
_ROSTER_RUN = re.compile(r"\bI\d+(?:\s*[/,·]\s*I\d+){2,}\b")

#: Files where a roster run is a HISTORICAL statement about what was retired, not a claim
#: about what is enforced now. Kept explicit and short: each is a sentence about the
#: respine, and the registry's RETIRED entries are the authority for those.
_ROSTER_HISTORY_OK = {
    "the retired host writer's",
    "this subsumes the old",
    "no host re-anchoring needed",
}


def _roster_runs(text: str) -> list[str]:
    out = []
    for m in _ROSTER_RUN.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line = text[line_start: line_end if line_end != -1 else len(text)]
        if any(marker in line.lower() for marker in _ROSTER_HISTORY_OK):
            continue
        out.append(m.group(0))
    return out


@pytest.mark.parametrize("path", [
    "CLAUDE.md",
    "agent/skills/spec_writer.py",
    "agent/skills/env_honesty.py",
    "agent/mcp_tools/workflow_tools.py",
    "agent/mcp_tools/data_tools.py",
    "scripts/seaworthy_scope.py",
])
def test_no_hand_written_invariant_roster_survives(path):
    """THE GENERAL RATCHET — the reason this file exists rather than five one-off fixes.

    Six places spelled the Layer-2 roster out by hand as `I0/I3/I6/I7/I8`, and all six
    were wrong the same way (I5 and I10 missing) because each was copied from another
    copy rather than from the code. Any run of three-or-more ids is now a build break:
    cite the registry, or name the specific invariants you actually mean.

    Runs that are explicitly HISTORICAL ("the retired host writer's I1/I2/…") are allowed
    — they are statements about what was removed, and the registry's RETIRED entries back
    them up."""
    runs = _roster_runs((ROOT / path).read_text())
    assert not runs, (
        f"{path} spells out an invariant roster by hand: {runs}. Point at "
        f"agent/skills/invariants.py instead — a roster copied from a copy is how I5 and "
        f"I10 went missing from every list at once.")


#: A roster spelled ONE ID PER LIST ITEM — `["I0: …", "I3: …", "I6: …"]` — which the
#: single-line `_ROSTER_RUN` regex cannot see, because no line holds more than one id.
_ROSTER_ITEM = re.compile(r"^\s*(I\d+)\s*[:—-]")


def _per_item_rosters(module_rel: str) -> list[tuple[int, set[str]]]:
    """Every list literal in a module that enumerates 3+ invariants, one per string.

    THE BLIND SPOT THIS CLOSES. `_ROSTER_RUN` catches `I0/I3/I6/I7/I8` — ids run together
    on one line, the shape that was wrong in six places. It does NOT catch the same roster
    written as a list of sentences, and that shape was live and already wrong:
    `install_pipeline_brief` enumerated I0, I3, I4, I6, I7, I8 as six separate strings,
    omitting I5 and I10, and this suite passed over it. A lint that only recognises the
    formatting of the bug it was written for is a lint that will miss the next one.

    Returns (lineno, ids) per roster found, from the AST so a docstring cannot fake one."""
    tree = ast.parse((ROOT / module_rel).read_text())
    out: list[tuple[int, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        ids = {m.group(1)
               for el in node.elts
               if isinstance(el, ast.Constant) and isinstance(el.value, str)
               for m in [_ROSTER_ITEM.match(el.value)] if m}
        if len(ids) >= 3:
            out.append((node.lineno, ids))
    return out


@pytest.mark.parametrize("path", [
    "agent/mcp_tools/data_tools.py",
    "agent/mcp_tools/workflow_tools.py",
    "agent/skills/spec_writer.py",
    "agent/skills/env_honesty.py",
])
def test_a_per_item_roster_is_complete_for_its_layer(path):
    """A hand-enumerated roster must not be MISSING a live invariant of the layer it
    covers. Deriving it from the registry (what data_tools now does) makes it invisible
    here, which is the intended way to pass: a roster that cannot drift is not linted."""
    layer2 = {i.id for i in reg.active(layer=reg.LAYER_WORKFLOW)}
    layer1 = {i.id for i in reg.active(layer=reg.LAYER_ENV)}
    for lineno, ids in _per_item_rosters(path):
        # Attribute the roster to whichever layer it overlaps more, then require it whole.
        target = layer2 if len(ids & layer2) >= len(ids & layer1) else layer1
        missing = sorted(target - ids, key=lambda s: int(s[1:]))
        assert not missing, (
            f"{path}:{lineno} enumerates invariants one per item but omits {missing}. "
            f"Build the list from agent.skills.invariants.active(layer=…) instead — this "
            f"is the exact shape that let install_pipeline_brief hand an autonomous agent "
            f"a roster with I5 and I10 missing while the whole suite stayed green.")


def test_the_single_line_lint_alone_would_have_missed_the_per_item_shape():
    """The reason the check above exists, pinned so nobody 'simplifies' it back.

    Feeds both lints the exact defective shape — a six-entry list omitting I5 and I10 —
    and asserts the old regex is blind to it while the new walk catches it."""
    defective = ('invariants = [\n'
                 + "".join(f'    "{i}: something must be true",\n'
                           for i in ("I0", "I3", "I4", "I6", "I7", "I8"))
                 + ']\n')
    assert _roster_runs(defective) == [], (
        "the single-line roster regex now matches the per-item shape; if that is "
        "intentional this test should be rewritten, not deleted")

    tree = ast.parse(defective)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.List))
    ids = {m.group(1) for el in node.elts
           if isinstance(el, ast.Constant)
           for m in [_ROSTER_ITEM.match(el.value)] if m}
    assert len(ids) >= 3, "the per-item walk failed to recognise a roster at all"
    layer2 = {i.id for i in reg.active(layer=reg.LAYER_WORKFLOW)}
    assert sorted(layer2 - ids) == ["I5", "I10"] or set(layer2 - ids) == {"I5", "I10"}, (
        f"expected the per-item walk to find I5 and I10 missing; got {sorted(layer2 - ids)}")


def test_brief_hints_never_outlive_their_invariant():
    """`_BRIEF_HINTS` adds the practical how-to the registry deliberately omits. A hint
    for an id that no longer exists is the same stale-copy problem one level down."""
    from agent.mcp_tools.data_tools import _BRIEF_HINTS
    unknown = sorted(set(_BRIEF_HINTS) - set(reg.REGISTRY))
    assert not unknown, f"_BRIEF_HINTS explains {unknown}, which the registry does not declare"
    retired = sorted(k for k in _BRIEF_HINTS if reg.REGISTRY[k].status != reg.ACTIVE)
    assert not retired, f"_BRIEF_HINTS still explains retired invariant(s) {retired}"


def test_install_pipeline_brief_roster_matches_the_registry():
    """The brief handed to an autonomous subagent must name every live Layer-2 invariant.

    It named six of eight — I5 and I10 missing — so an agent following it would omit the
    reference-DB and service records those clauses refuse on, and be rejected by a gate
    the brief told it did not exist."""
    from agent.mcp_tools import data_tools
    brief = data_tools.install_pipeline_brief("samtools")
    listed = {m.group(1) for line in brief["invariants"]
              for m in [re.match(r"(I\d+)\s*:", line)] if m}
    expected = {i.id for i in reg.active(layer=reg.LAYER_WORKFLOW)}
    assert listed == expected, (
        f"the brief's Layer-2 roster is {sorted(listed)} but the registry's is "
        f"{sorted(expected)} (missing {sorted(expected - listed)}, "
        f"extra {sorted(listed - expected)})")


@pytest.mark.parametrize("live_id", ["I5", "I10"])
def test_claude_md_does_not_call_a_live_invariant_retired(live_id):
    """The specific rot this workstream found: the prose asserted twice that I5 and I10
    were 'retired'/'subsumed' — the two live clauses its own table had dropped.

    Reads every steering doc, not just CLAUDE.md. When the long-form primitive reference
    was lifted into docs/primitives.md, a false 'retired' sentence riding along in the
    moved text would have escaped this check entirely — and the suite would have gone
    green on the strength of having stopped looking.
    """
    text = _steering_text()
    for m in re.finditer(r"[^.\n]*\b(retired|subsumed)\b[^.\n]*", text, re.I):
        sentence = m.group(0)
        if re.search(rf"\b{live_id}\b", sentence):
            # Allowed only when the sentence is explicitly about its history at Layer 1.
            assert "Layer 2" in sentence or "restored" in sentence.lower(), (
                f"CLAUDE.md calls {live_id} retired/subsumed, but it is ACTIVE and refuses "
                f"real seals today:\n    {sentence.strip()}")


# ---------------------------------------------------------------------------------------
# Layer 1 has a roster too, and it went stale in exactly the same way
# ---------------------------------------------------------------------------------------
#
# The test above pins the brief's LAYER-2 roster, which was fixed by deriving it from the
# registry. The LAYER-1 list sitting directly beneath the comment that explains that fix
# was still hand-written, and by 2026-08-07 it was wrong the same way: three guarantees
# listed, four enforced. A record with a malformed `shipped_binaries[]` is refused by
# `WELL_FORMED.shipped_binaries` — a live clause the brief handed to an UNSUPERVISED
# subagent never mentioned.

def _emitted_layer1_clauses() -> set[str]:
    """The clause names `check_build` can actually put on a coverage row.

    Read from the source rather than by running a build: every `ClauseCoverage("X", ...)`
    literal in env_honesty, reduced to its top-level guarantee (`POLICY_CLEAN.license` ->
    `POLICY_CLEAN`). An f-string clause name (`f"WELL_FORMED.{key}"`) still yields its
    prefix, which is the part the roster is responsible for naming.
    """
    src = (ROOT / "agent" / "skills" / "env_honesty.py").read_text()
    names = re.findall(r'ClauseCoverage\(\s*f?["\']([A-Z_]+)', src)
    return set(names)


def test_the_layer1_roster_names_every_guarantee_check_build_enforces():
    from agent.skills import env_honesty as honesty
    declared = {name for name, _ in honesty.LAYER1_GUARANTEES}
    emitted = _emitted_layer1_clauses()
    missing = sorted(emitted - declared)
    assert not missing, (
        f"check_build emits the clause(s) {missing}, which LAYER1_GUARANTEES does not "
        "declare. Anyone told what the Layer-1 contract is — the ENV report, the "
        "autonomous install brief — will understate it, and an agent will be refused by a "
        "gate it was told did not exist. This is the I5/I10 rot, at Layer 1."
    )
    ghosts = sorted(declared - emitted)
    assert not ghosts, (
        f"LAYER1_GUARANTEES advertises {ghosts}, which check_build never emits."
    )


def test_the_brief_states_all_four_layer1_guarantees():
    """The consumer that made the staleness cost something: this brief is executed by a
    subagent without human supervision."""
    from agent.mcp_tools import data_tools
    from agent.skills import env_honesty as honesty
    brief = data_tools.install_pipeline_brief("samtools")
    for name, _ in honesty.LAYER1_GUARANTEES:
        assert any(line.startswith(f"{name}:") for line in brief["invariants"]), (
            f"the install brief never names the Layer-1 guarantee {name}")


def test_the_brief_can_reach_every_primitive():
    """It listed ELEVEN primitives against 47 registered ones, and the 36 it omitted were
    not marginal: `resolve_tool` ('start here when unsure which tier'), `freeze_from_image`
    and `build_env_from_authors_recipe` (the use-the-authors'-own-machinery path), and four
    whole install tiers — release_binary, perl, cargo, go. A subagent following the brief
    could not reach any of them: the can't-see-the-menu defect `tool_surface` exists to
    end, reproduced one level down."""
    from agent.mcp_tools import data_tools
    from agent.skills import tool_surface as ts
    listed = {line.split(":", 1)[0].strip() for line in data_tools.install_pipeline_brief("x")["primitives"]}
    missing = sorted(set(ts.positioned(ts.PRIMITIVE)) - listed)
    assert not missing, f"the brief cannot reach {missing}"


def test_primitive_notes_never_outlive_their_tool():
    """Same rule `_BRIEF_HINTS` lives by: membership is derived, the practical note is
    hand-written, and a note for a tool that is no longer a primitive is a stale copy."""
    from agent.mcp_tools.data_tools import _PRIMITIVE_NOTES
    from agent.skills import tool_surface as ts
    unknown = sorted(set(_PRIMITIVE_NOTES) - set(ts.positioned(ts.PRIMITIVE)))
    assert not unknown, (
        f"_PRIMITIVE_NOTES explains {unknown}, which tool_surface does not position as a "
        "PRIMITIVE — the note is either for a deleted tool or for one that moved below "
        "the table.")
