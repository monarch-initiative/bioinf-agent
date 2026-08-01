"""Once a field has a designated reader, nothing else may read it raw.

This codebase's most expensive recurring defect is one fact spelled out in N places and
drifting in N-1. It has been found and fixed at least four separate times:

  * `command_template` — str or list[str], read two different ways     -> usage_commands
  * `usage_verification` / `usage_verified` — a three-state fact behind a bool, where
    the dashboard grew a private derivation and the guide printed the raw bool, so two
    pages disagreed about one workflow                                 -> usage_status
  * `license_gated` / the legacy `gated` — the deduplication reached four rendering
    callers and stopped one import short of the contract, so records carrying only the
    legacy key passed I13 by default                                   -> record_is_gated
  * `validation` / `validation_status` — hand-spelled in SEVEN places; six had both
    clauses and the seventh, the row facing the user, kept only the agent's override.
    It reported `steps_validated: 0` for every workflow ever sealed, including a
    five-step run with a complete set of passing records               -> step_is_validated

Each time, the fix was the instance plus a paragraph. A paragraph does not find the
fifth one. This does.

The rule is mechanical and narrow: a field listed in OWNED_BY has one reader, and any
other module in agent/ that reads it directly must appear in ALLOWED with a stated
reason. Writing is untouched — producers must write these fields, and forbidding that
would be nonsense. Only READS are the drift surface, because a read is where a second
interpretation gets invented.

An entry in ALLOWED is not a defeat. Several are correct: passing a whole sub-record
through to a payload, or reading a sibling key like `reason` or `locus` that the leaf
does not answer. What the allowlist buys is that each one was LOOKED AT and its reason
written down, instead of being indistinguishable from the next silent copy.

Written after the lint found two live instances on its first run: `user_guide.py` gated
its "(self-tested)" label on the raw `usage_verified` bool in two places, and a spec on
disk already carries `usage_verified: true` beside `status: not_attempted` — the exact
combination that prints "self-tested" over a command nothing executed.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "agent"

#: field name -> the ONE function in agent/models/core_data.py that reads it.
OWNED_BY = {
    "command_template":   "usage_commands",
    "usage_verification": "usage_status",
    "usage_verified":     "usage_status",
    "validation_status":  "step_is_validated",
    "license_gated":      "record_is_gated",
    "gated":              "record_is_gated",
    # Registered WITH the field, not after it drifted — this one had two readers within
    # an hour of being introduced (the seal-side check and the production data-pin
    # check), which is how fast the class recurs when nothing is watching.
    "content_anchors":    "test_data_anchors",
}

#: "<path>:<field>" -> why reading it raw here is correct.
#: Every entry is a claim someone checked. Adding one without a reason defeats the lint.
ALLOWED = {
    "skills/spec_writer.py:validation_status":
        "I3's per-output coverage check needs the OVERRIDE specifically, not the "
        "combined predicate — it asks whether the agent's assertion is standing in for "
        "absent per-file records, which is a different question from 'is this step "
        "validated'. Routing it through the leaf would make the clause always true.",
    "mcp_tools/workflow_tools.py:usage_verification":
        "Passes the whole usage_verification sub-record through into the seal result "
        "payload. A pass-through of the record is not a second derivation of the "
        "verdict; the verdict beside it is read via usage_status.",
    "skills/resources.py:usage_verified":
        "Deliberately surfaces the legacy bool ALONGSIDE the three-state status in the "
        "same inventory row, for readers pinned to the old key. The row's actual "
        "verdict is usage_status; see the comment at the call site.",
    "skills/resources.py:usage_verification":
        "Reads the `reason` sibling, which the leaf does not answer — the leaf returns "
        "the status only.",
    "skills/run_dashboard_html.py:usage_verification":
        "Reads the `locus` sibling for display; the status two lines above comes from "
        "the leaf.",
    "mcp_tools/freeze_tools.py:license_gated":
        "Reads a pipeline DRAFT, not an EnvCache record. Drafts are written only by "
        "patch_pipeline, which writes the canonical key — the legacy `gated` spelling "
        "the leaf exists to absorb has never appeared in a draft.",
    "skills/env_recipe.py:license_gated":
        "Reads back a recipe dict this same module wrote ~20 lines earlier with the "
        "canonical key. A closed loop, so there is no second spelling to absorb.",
}


def _raw_reads(path: Path) -> list[tuple[int, str]]:
    """Every `x.get("field")` / `x["field"]` READ of an owned field in one file.

    Store context is skipped: `d["license_gated"] = True` is a producer writing, which
    is exactly what should keep happening.
    """
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:                                    # pragma: no cover
        return []
    found = []
    for n in ast.walk(tree):
        field = None
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get" and n.args
                and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)):
            field = n.args[0].value
        elif (isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load)
              and isinstance(n.slice, ast.Constant)
              and isinstance(n.slice.value, str)):
            field = n.slice.value
        if field in OWNED_BY:
            found.append((n.lineno, field))
    return found


def _files() -> list[Path]:
    # core_data.py is where the leaves live; models/ declares the fields.
    return sorted(p for p in AGENT.rglob("*.py")
                  if p.name != "core_data.py" and "__pycache__" not in p.parts)


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_no_module_reads_an_owned_field_without_going_through_its_leaf(path):
    rel = str(path.relative_to(AGENT))
    offenders = [(ln, f) for ln, f in _raw_reads(path)
                 if f"{rel}:{f}" not in ALLOWED]
    assert not offenders, (
        "\n".join(
            f"  {rel}:{ln} reads {f!r} directly — go through "
            f"core_data.{OWNED_BY[f]}()" for ln, f in offenders)
        + "\n\nThis field has ONE designated reader. Every time a second reading of a "
          "shared fact has been written in this codebase it eventually disagreed with "
          "the first, and the copy that disagreed was the one facing the user.\n"
          "If the raw read is genuinely right here (a pass-through, or a sibling key "
          "the leaf does not answer), add 'path:field' to ALLOWED in this file WITH "
          "the reason.")


def test_every_owned_field_actually_has_its_leaf():
    """A registry pointing at a function that no longer exists silently protects
    nothing — the same failure the invariant roster lint exists to catch."""
    from agent.models import core_data
    for field, leaf in sorted(OWNED_BY.items()):
        assert callable(getattr(core_data, leaf, None)), (
            f"OWNED_BY maps {field!r} to core_data.{leaf}(), which does not exist. "
            f"Either the leaf was renamed (update this map) or it was deleted (and "
            f"every caller has quietly gone back to its own reading).")


def test_the_allowlist_has_no_stale_entries():
    """An exemption for a read that no longer exists is a licence sitting open for the
    next person who writes one at that path."""
    live = {f"{p.relative_to(AGENT)}:{f}" for p in _files() for _, f in _raw_reads(p)}
    stale = sorted(set(ALLOWED) - live)
    assert not stale, (
        f"ALLOWED exempts reads that are no longer there: {stale}. Drop them — an "
        f"exemption nobody needs is one nobody re-examines.")


def test_the_lint_catches_a_planted_copy(tmp_path):
    """A lint nobody has seen fire is a lint nobody knows the shape of."""
    f = tmp_path / "sneaky.py"
    f.write_text('def verdict(spec):\n'
                 '    return "yes" if spec.get("usage_verified") else "no"\n')
    assert _raw_reads(f) == [(2, "usage_verified")]

    ok = tmp_path / "fine.py"
    ok.write_text('def verdict(spec):\n'
                  '    d = {}\n'
                  '    d["usage_verified"] = True      # a WRITE, not a read\n'
                  '    return usage_status(spec)\n')
    assert _raw_reads(ok) == [], "a producer writing the field must not be flagged"
