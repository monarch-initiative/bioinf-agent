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
    mentioned = {f"I{n}" for n in re.findall(r"\bI(\d{1,2})\b", CLAUDE_MD.read_text())}
    unknown = sorted(mentioned - set(reg.REGISTRY), key=lambda s: int(s[1:]))
    assert not unknown, (
        f"CLAUDE.md refers to {unknown}, which the registry does not declare. Either "
        f"register them or stop citing them — an id a reader cannot look up is worse "
        f"than no id.")


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


@pytest.mark.parametrize("live_id", ["I5", "I10"])
def test_claude_md_does_not_call_a_live_invariant_retired(live_id):
    """The specific rot this workstream found: the prose asserted twice that I5 and I10
    were 'retired'/'subsumed' — the two live clauses its own table had dropped."""
    text = CLAUDE_MD.read_text()
    for m in re.finditer(r"[^.\n]*\b(retired|subsumed)\b[^.\n]*", text, re.I):
        sentence = m.group(0)
        if re.search(rf"\b{live_id}\b", sentence):
            # Allowed only when the sentence is explicitly about its history at Layer 1.
            assert "Layer 2" in sentence or "restored" in sentence.lower(), (
                f"CLAUDE.md calls {live_id} retired/subsumed, but it is ACTIVE and refuses "
                f"real seals today:\n    {sentence.strip()}")
