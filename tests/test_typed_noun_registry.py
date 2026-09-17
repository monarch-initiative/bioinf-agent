"""The typed-noun registry lint — the anti-drift guard for the typed-records program.

`agent/skills/typed_nouns.py` declares which record nouns are construction-typed and
how far (SHADOW: validate + log at the funnel; ENFORCED: the funnel raises, walk
clauses retired). That table is only trustworthy if reality cannot drift away from it,
in either direction:

  * a walk clause DELETED while a noun claiming it is still SHADOW — coverage lost
    with nothing standing in for it;
  * a walk clause STILL PRESENT when every claimant is ENFORCED — two truths, the
    duplicated-coverage state the program exists to end;
  * an ENFORCED noun whose enforcement point doesn't resolve — a gate on paper;
  * a funnel that stopped calling the shadow check — gate present in code, absent
    in effect.

Each of those is a build failure here. The behavior tests at the bottom pin the two
properties shadow mode promises: it never raises, and it logs what it saw.
"""
from __future__ import annotations

import ast
import inspect
import json
from importlib import import_module
from pathlib import Path

import pytest
from pydantic import BaseModel

from agent.models import core_data
from agent.skills import pipeline_state, spec_writer, typed_nouns
from agent.skills.invariants import REGISTRY as INVARIANT_REGISTRY

SPEC_WRITER_SRC = Path(spec_writer.__file__).read_text()


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------

def test_every_model_resolves_to_a_core_data_model():
    for tn in typed_nouns.REGISTRY.values():
        cls = getattr(core_data, tn.model, None)
        assert cls is not None and issubclass(cls, BaseModel), (
            f"typed noun '{tn.noun}' names model '{tn.model}', which is not a "
            f"BaseModel in agent.models.core_data"
        )


def test_modes_are_valid_and_enforcement_points_resolve():
    for tn in typed_nouns.REGISTRY.values():
        assert tn.mode in (typed_nouns.SHADOW, typed_nouns.ENFORCED), tn.noun
        if tn.mode == typed_nouns.ENFORCED:
            assert tn.enforced_at, (
                f"'{tn.noun}' is ENFORCED but names no enforcement point — "
                f"an enforced noun with no named gate is a claim, not a property"
            )
            assert _resolve_dotted(tn.enforced_at) is not None, (
                f"'{tn.noun}' names enforcement point '{tn.enforced_at}', "
                f"which does not resolve — the gate this registry advertises "
                f"does not exist"
            )
        else:
            assert not tn.enforced_at, (
                f"'{tn.noun}' is SHADOW but names an enforcement point — either "
                f"flip the mode or drop the claim; a half-state reads as enforced"
            )


def _resolve_dotted(path: str):
    """Resolve `pkg.module.Class.method` by importing the longest importable prefix
    and walking attributes for the rest."""
    parts = path.split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj = import_module(".".join(parts[:i]))
        except ImportError:
            continue
        for attr in parts[i:]:
            obj = getattr(obj, attr, None)
            if obj is None:
                return None
        return obj
    return None


def test_every_retired_id_is_a_registered_invariant():
    for tn in typed_nouns.REGISTRY.values():
        for vid in tn.retires:
            prefix = vid.split(".", 1)[0]
            assert prefix in INVARIANT_REGISTRY, (
                f"'{tn.noun}' claims to retire '{vid}', whose invariant prefix "
                f"'{prefix}' is not in the invariant registry"
            )


# ---------------------------------------------------------------------------
# The two-direction retirement ratchet
# ---------------------------------------------------------------------------

def test_a_clause_is_not_deleted_while_a_claimant_noun_is_shadow():
    """Deleting a walk clause before its noun is enforced is a coverage HOLE, not a
    retirement — the type that was supposed to absorb the check isn't gating yet."""
    for tn in typed_nouns.shadow_nouns():
        for vid in tn.retires:
            assert f'"{vid}"' in SPEC_WRITER_SRC, (
                f"walk clause '{vid}' is gone from spec_writer, but '{tn.noun}' — "
                f"a noun claiming it — is still SHADOW. Either the clause was "
                f"deleted early (restore it) or the noun was enforced without "
                f"updating this registry (flip its mode in the same change)."
            )


def test_a_fully_enforced_claim_leaves_no_clause_behind():
    """Once EVERY claimant of a violation id is ENFORCED, the walk clause must be
    deleted — enforced type + surviving walk clause is two truths about one shape,
    and two readings of one field is how this codebase's defects start."""
    all_ids = {vid for tn in typed_nouns.REGISTRY.values() for vid in tn.retires}
    for vid in sorted(all_ids):
        claimant_nouns = typed_nouns.claimants(vid)
        assert claimant_nouns, vid
        if all(tn.mode == typed_nouns.ENFORCED for tn in claimant_nouns):
            assert f'"{vid}"' not in SPEC_WRITER_SRC, (
                f"every noun claiming '{vid}' is ENFORCED, but the walk clause "
                f"still emits it — retire the clause (and its tests) in the same "
                f"change that flipped the last noun."
            )


# ---------------------------------------------------------------------------
# The funnel is actually wired
# ---------------------------------------------------------------------------

def test_the_draft_funnel_calls_the_typed_check():
    """`_write_draft_file` is the one exit every draft mutation takes; the typed
    gate lives there or it lives nowhere. Checked against the AST, not a substring —
    a call moved into a comment must not count."""
    tree = ast.parse(inspect.getsource(pipeline_state.PipelineState))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_write_draft_file"),
              None)
    assert fn is not None
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "check_draft"]
    assert calls, (
        "PipelineState._write_draft_file no longer calls "
        "typed_nouns.check_draft — the typed gate is present in the "
        "registry and absent in effect."
    )


# ---------------------------------------------------------------------------
# Shadow-mode behavior: never raises, logs what it saw
# ---------------------------------------------------------------------------

def test_shadow_mode_never_raises_and_logs_the_mismatch(tmp_path):
    garbage = {
        "usage": {"description": "missing its command_template"},
        "install_steps": "not even a list",
    }
    typed_nouns.check_draft(garbage, source="test:garbage")   # must not raise
    log = typed_nouns.mismatch_log_path()
    assert log.exists(), "shadow mode saw malformed records and logged nothing"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    nouns_logged = {e["noun"] for e in entries}
    assert {"usage", "install_steps"} <= nouns_logged
    for e in entries:
        assert e["errors"] and e["source"] == "test:garbage"


def test_an_enforced_noun_raises_at_the_funnel():
    """pipeline_steps is ENFORCED (Seam A): a malformed record REFUSES the write
    instead of logging. The raise must carry the field so the producer can act
    on it — the gate is the guide."""
    with pytest.raises(typed_nouns.TypedNounViolation, match="command"):
        typed_nouns.check_draft(
            {"pipeline_steps": [{"step": 1, "tool": "samtools",
                                 "returncode": 0}]},   # no command, no resource_usage
            source="test:enforced")
    with pytest.raises(typed_nouns.TypedNounViolation, match="list"):
        typed_nouns.check_draft({"pipeline_steps": "not a list"},
                                source="test:enforced")


def test_the_enforced_raise_is_not_swallowed_by_the_shadow_fence():
    """The shadow fence exists so observation can never break a write; the
    enforced gate exists so a bad record can never land. One function serves
    both, so pin that the fence does not extend over the raise."""
    tree = ast.parse(inspect.getsource(typed_nouns.check_draft))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    trys = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
    assert trys, "the shadow half lost its fence"
    for t in trys:
        fenced_calls = {c.func.id for n in ast.walk(t) for c in ast.walk(n)
                        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert "_enforce_one" not in fenced_calls, (
            "_enforce_one runs inside a try/except fence — an enforced gate "
            "that can be swallowed is a shadow gate with a misleading name")


def test_the_typed_check_is_silent_on_model_built_records():
    """The workflow_records builders and the typed gate must agree — a builder
    that trips its own noun's validation is a fixture encoding a shape the
    model refuses, the exact defect both exist to end. pipeline_steps is
    ENFORCED here, so agreement means: no raise, and no log entry."""
    from workflow_records import install_step, package_record, pipeline_step
    draft = {
        "pipeline_name": "demo",
        "pipeline_steps": [pipeline_step()],
        "install_steps": [install_step()],
        "packages": [package_record()],
    }
    typed_nouns.check_draft(draft, source="test:clean")   # must not raise
    log = typed_nouns.mismatch_log_path()
    assert not log.exists() or not log.read_text().strip(), (
        f"model-built records tripped the typed gate: {log.read_text()}"
    )


def test_a_write_through_the_store_is_refused_when_enforced(tmp_path):
    """End to end through the real funnel: a mutator that lands a malformed
    pipeline_step is REFUSED (the write never reaches disk), and a well-formed
    one still lands. This is the write-side half of assert-at-both-ends; the
    serve-side half is WorkflowSpec.model_validate at seal."""
    from workflow_records import pipeline_step
    store = pipeline_state.PipelineState(config={})
    store.start("enforce_probe", "typed-records seam A funnel test")
    with pytest.raises(typed_nouns.TypedNounViolation, match="command"):
        store.add_step("enforce_probe", {"step": 1, "tool": "samtools"})
    draft = store.get_draft("enforce_probe")
    assert not draft.get("pipeline_steps"), (
        "the refused record reached the draft anyway — the gate ran after the write")
    idx = store.add_step("enforce_probe", pipeline_step())
    assert idx == 1
    assert store.get_draft("enforce_probe")["pipeline_steps"][0]["command"]
