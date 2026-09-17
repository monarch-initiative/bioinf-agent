"""
typed_nouns — the ONE declaration of which record nouns are construction-typed, and how far.

WHY THIS EXISTS. The 2026-08 architecture review named the structural debt in one
sentence: records are hand-built dicts merged into a draft, and correctness is enforced
by five compensating mechanisms instead of one property — records that cannot be
constructed wrong. The fix is a per-noun program, not a rewrite: the runtime constructs
the typed model at the write funnel, and the walk clauses the type absorbs are DELETED,
not duplicated. `ShippedBinary` walked this road first (`EnvCache._validate_shape`) and
its defect class — N producers writing N key-dialects that M readers mis-read
differently — has not recurred for that noun.

A program that advances noun-by-noun over months needs its state written down as DATA,
or it rots the way every hand-written roster here has rotted (invariants.py's module
docstring is the measured version of that story). This registry is that state:

  * mode=SHADOW — the funnel validates the noun against its model and LOGS mismatches
    (never refuses, never blocks a write). The log is the bill for enforcing the noun:
    an empty log across a real freeze→seal drive means the producers already emit the
    shape, and the noun is ready to flip.
  * mode=ENFORCED — the write funnel RAISES on a malformed record (`enforced_at` names
    the function that does), and the walk clauses in `retires` are deleted once EVERY
    noun claiming them is enforced. Enforce-and-retire happen in the SAME change —
    separate PRs are the grandfathering trap (a gate at only one end, the tier-5
    lesson), and the lint below holds the line.

`tests/test_typed_noun_registry.py` fails the build when this table and reality drift:
a walk clause deleted while a claimant noun is still SHADOW, a clause still present
when every claimant is ENFORCED, an enforcement point that doesn't resolve, or a funnel
that stopped calling `check_draft` — the "gate present in code, absent in effect"
shape this codebase is defined by.

SHADOW MODE NEVER RAISES. It is observation, priced at one model_validate per record
per draft write, and a failure anywhere inside it (including the log write) is
swallowed — a diagnostic must not be able to break the thing it diagnoses.

Layer-1 note: both Layer-1 nouns are already ENFORCED at `EnvCache._validate_shape`,
so there is no EnvCache shadow hook today. Add one the day a Layer-1 noun enters this
registry in SHADOW mode — not before.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from agent.skills.invariants import LAYER_ENV, LAYER_WORKFLOW

SHADOW = "shadow"
ENFORCED = "enforced"


@dataclass(frozen=True)
class TypedNoun:
    noun: str
    """Top-level key of the record in its store (the pipeline draft, or the EnvCache
    record for layer-1 nouns)."""
    model: str
    """Class name in `agent.models.core_data` that types it."""
    layer: int
    """LAYER_ENV or LAYER_WORKFLOW — which store the noun lives in."""
    mode: str
    """SHADOW (validate + log at the funnel) or ENFORCED (the funnel raises)."""
    element: bool = True
    """True: the noun is a LIST and the model types one element. False: the model
    types the whole value."""
    retires: tuple[str, ...] = ()
    """Violation ids whose walk clause may be deleted once EVERY noun claiming the id
    is ENFORCED. A shared id (I0.shape_sanity walks seven lists) is claimed by every
    noun it walks, so no single seam can delete it early."""
    enforced_at: str = ""
    """ENFORCED only: dotted path of the function that raises on a malformed record."""


def _tn(**kw) -> TypedNoun:
    return TypedNoun(**kw)


REGISTRY: dict[str, TypedNoun] = {tn.noun: tn for tn in [
    # ---- Layer 2: the pipeline draft / WorkflowSpec ---------------------------------
    # `retires` attribution is from measurement at spec_writer HEAD: I0.shape_sanity
    # walks the seven list nouns below; I6.absolute_paths and I7.resource_usage_recorded
    # read only pipeline_steps. I7.resource_usage_captured (all-zeros / sacct_error) and
    # I6.template_placeholders_declared are value/world checks and are claimed by nobody.
    #
    # pipeline_steps ENFORCED (Seam A): the five producers construct through
    # PipelineStep.produce, check_draft raises at the write funnel, and seal re-validates
    # via WorkflowSpec. Its two solely-claimed clauses (I6.absolute_paths,
    # I7.resource_usage_recorded) were deleted from the walk in the same change; the
    # I0.shape_sanity claim waits on the other six list nouns.
    _tn(noun="pipeline_steps", model="PipelineStep", layer=LAYER_WORKFLOW, mode=ENFORCED,
        enforced_at="agent.skills.typed_nouns.check_draft",
        retires=("I0.shape_sanity", "I6.absolute_paths", "I7.resource_usage_recorded")),
    _tn(noun="install_steps", model="InstallStep", layer=LAYER_WORKFLOW, mode=SHADOW,
        retires=("I0.shape_sanity",)),
    _tn(noun="packages", model="PackageRecord", layer=LAYER_WORKFLOW, mode=SHADOW,
        retires=("I0.shape_sanity",)),
    _tn(noun="reference_databases", model="ReferenceDatabase", layer=LAYER_WORKFLOW,
        mode=SHADOW, retires=("I0.shape_sanity",)),
    _tn(noun="runtime_configs", model="RuntimeConfig", layer=LAYER_WORKFLOW,
        mode=SHADOW, retires=("I0.shape_sanity",)),
    _tn(noun="service_dependencies", model="ServiceDependency", layer=LAYER_WORKFLOW,
        mode=SHADOW, retires=("I0.shape_sanity",)),
    _tn(noun="authored_artifacts", model="AuthoredArtifact", layer=LAYER_WORKFLOW,
        mode=SHADOW, retires=("I0.shape_sanity",)),
    _tn(noun="test_data", model="TestDataRef", layer=LAYER_WORKFLOW, mode=SHADOW,
        element=False),
    _tn(noun="usage", model="UsageTemplate", layer=LAYER_WORKFLOW, mode=SHADOW,
        element=False),

    # ---- Layer 1: the EnvCache record — the precedent, already enforced -------------
    _tn(noun="shipped_binaries", model="ShippedBinary", layer=LAYER_ENV, mode=ENFORCED,
        enforced_at="agent.skills.freeze.EnvCache._validate_shape"),
    _tn(noun="tool_identities", model="ToolIdentity", layer=LAYER_ENV, mode=ENFORCED,
        enforced_at="agent.skills.freeze.EnvCache._validate_shape"),
]}


def shadow_nouns(layer: int = 0) -> list[TypedNoun]:
    return [tn for tn in REGISTRY.values()
            if tn.mode == SHADOW and (layer == 0 or tn.layer == layer)]


def enforced_nouns(layer: int = 0) -> list[TypedNoun]:
    return [tn for tn in REGISTRY.values()
            if tn.mode == ENFORCED and (layer == 0 or tn.layer == layer)]


def claimants(violation_id: str) -> list[TypedNoun]:
    """Every noun whose enforcement the given walk clause is waiting on."""
    return [tn for tn in REGISTRY.values() if violation_id in tn.retires]


# ---------------------------------------------------------------------------
# The write-funnel check — raises for ENFORCED nouns, observes for SHADOW ones
# ---------------------------------------------------------------------------

class TypedNounViolation(ValueError):
    """A record failed its noun's model at the write funnel while the noun is
    ENFORCED. The message carries the pydantic error list — the gate is the
    guide: the producer that built the record gets told exactly which field,
    at the write, not at seal."""


def mismatch_log_path():
    """Where shadow mismatches land. ONE answer, so the flip-gate check ("is the log
    empty for this noun across a real drive?") and the funnel write read the same file.
    Resolved per call, not at import — the workspace is env-resolved and tests point it
    elsewhere."""
    from agent.skills import workspace
    return workspace.scratch_dir("typed_records") / "shadow_mismatches.jsonl"


#: (log_dir, noun, error-fingerprint) already logged this process — a draft is
#: re-written on every mutation, so without this one malformed record would log
#: once per subsequent write of the same draft.
_seen: set[tuple] = set()


def check_draft(draft: dict, *, source: str) -> None:
    """The typed-record gate at the draft write funnel.

    Called by `PipelineState._write_draft_file` — the one funnel every draft mutation
    exits through — so no per-mutator wiring exists to forget. Two modes, from the
    registry:

    ENFORCED nouns RAISE `TypedNounViolation` (the write never lands). Deliberately
    outside the fence below: an enforced gate that can be swallowed is a shadow gate
    with a misleading name.

    SHADOW nouns log mismatches and NEVER raise: shadow mode is priced observation,
    and its whole body is fenced so a validator bug or an unwritable log cannot fail
    the write it is watching."""
    for tn in enforced_nouns(LAYER_WORKFLOW):
        value = draft.get(tn.noun)
        if value is None:
            continue
        model = _model(tn.model)
        if tn.element:
            if not isinstance(value, list):
                raise TypedNounViolation(
                    f"{source}: draft field '{tn.noun}' is "
                    f"{type(value).__name__}, expected a list of {tn.model}")
            for i, entry in enumerate(value):
                _enforce_one(model, tn, entry, f"{tn.noun}[{i}]", source)
        else:
            _enforce_one(model, tn, value, tn.noun, source)

    try:
        for tn in shadow_nouns(LAYER_WORKFLOW):
            value = draft.get(tn.noun)
            if value is None:
                continue
            model = _model(tn.model)
            if tn.element:
                if not isinstance(value, list):
                    _log_mismatch(tn, source, tn.noun, [{
                        "loc": "", "type": "not_a_list",
                        "msg": f"expected list, got {type(value).__name__}",
                    }])
                    continue
                for i, entry in enumerate(value):
                    _validate_one(model, tn, entry, f"{tn.noun}[{i}]", source)
            else:
                _validate_one(model, tn, value, tn.noun, source)
    except Exception:
        return


def _enforce_one(model, tn: TypedNoun, entry, where: str, source: str) -> None:
    try:
        model.model_validate(entry)
    except Exception as e:
        raise TypedNounViolation(
            f"{source}: {where} does not satisfy {tn.model} (noun '{tn.noun}' is "
            f"ENFORCED). This gate validates the WHOLE draft on every write, so the "
            f"violating record may be one ALREADY IN THE DRAFT (hand-edited, or written "
            f"before enforcement) rather than the one this mutation adds — check {where} "
            f"against the error below. '{tn.noun}' is not patchable: re-run the offending "
            f"record's producer (replace_step=N overwrites a failed slot), or "
            f"discard_pipeline_draft and rebuild.\n{e}"
        ) from e


def _model(name: str):
    from agent.models import core_data
    return getattr(core_data, name)


def _validate_one(model, tn: TypedNoun, entry, where: str, source: str) -> None:
    try:
        model.model_validate(entry)
        return
    except Exception as e:
        errors = getattr(e, "errors", None)
        if callable(errors):
            try:
                errs = [{"loc": ".".join(str(p) for p in er.get("loc", ())),
                         "type": er.get("type", ""), "msg": er.get("msg", "")}
                        for er in errors()]
            except Exception:
                errs = [{"loc": "", "type": type(e).__name__, "msg": str(e)}]
        else:
            errs = [{"loc": "", "type": type(e).__name__, "msg": str(e)}]
    _log_mismatch(tn, source, where, errs)


def _log_mismatch(tn: TypedNoun, source: str, where: str, errors: list[dict]) -> None:
    try:
        path = mismatch_log_path()
        fingerprint = (str(path.parent), tn.noun,
                       tuple((er.get("loc", ""), er.get("type", "")) for er in errors))
        if fingerprint in _seen:
            return
        _seen.add(fingerprint)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "noun": tn.noun,
            "model": tn.model,
            "where": where,
            "source": source,
            "errors": errors[:10],
        }
        with open(path, "a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        return
