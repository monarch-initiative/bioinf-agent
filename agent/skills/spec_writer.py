"""
spec_writer — Layer-2 (workflow) spec + provenance persistence.

The pre-respine combined env+workflow writer (save_pipeline_spec) and its
env-build invariants have been retired (WHICH ones is data, in
agent/skills/invariants.py — the list spelled out here named four still-active
invariants as retired, two of them Layer-2 clauses that refuse real seals): an
env is now solved once by freeze() and verified IN the shipped image by
env_honesty.check_build (install==ship). What survives here is the Layer-2
surface that consumes a frozen env:

    write_workflow_spec(workflow, config)   -> {workflow_spec_path}
    check_workflow_invariants(spec)         -> run-side violations (roster:
                                               agent/skills/invariants.py)
    self_test_usage(spec, env_manager, ...) -> executes usage.command_template (I4)

check_invariants is now the run-side checker (roster in agent/skills/invariants.py); the
env-build tiers moved to agent/skills/env_honesty.py.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

from agent.models import core_data as _core_data
from agent.models.core_data import step_is_validated, usage_commands
from agent.skills import invariants as _invariants
from agent.skills.outcomes import refused
# The READER for a step's validation dict. Imported (not re-implemented) so the invariant
# and the store agree on how a record is keyed — pipeline_state imports only stdlib + yaml
# + outcomes, so this stays cycle-free.
from agent.skills.pipeline_state import validation_covers as _validation_covers


# ---------------------------------------------------------------------------
# I8 external-source vocabulary — SINGLE-SOURCED.
#
# The Layer-2 composition-coherence law (I8): every pipeline_step input must
# trace to a prior step's output OR one of these external-source categories.
# Defined ONCE, here, so the runtime seal-time check (_check_composition_coherence
# below) and the Phase-4 plan-authoring gate (agent/skills/plan.py — the SAME law
# lifted to authoring time) read the identical set and the identical sentence,
# instead of two copies that drift (this repo's signature disease). Adding a 5th
# category here flows to BOTH checks automatically. The set is exactly the fields
# _check_composition_coherence seeds its external universe from (test_data /
# reference_databases.local_path / runtime_configs.path / authored_artifacts.path).
# ---------------------------------------------------------------------------
I8_STATEMENT = "every input traces to a prior step's output OR an external source"

#: Companion-file suffixes a step may legitimately consume beside a prior step's output:
#: the index/checksum/compression siblings that bioinformatics tools write next to the
#: real artifact. Order matters only in that the longest sensible suffix should win; each
#: is stripped from the FULL path and the parent looked up in the universe by full path,
#: never by basename (see the I8 loop for what basename matching cost).
_SIDECAR_SUFFIXES = (".bai", ".csi", ".crai", ".tbi", ".fai", ".gzi", ".dict",
                     ".idx", ".md5", ".sha256", ".gz", ".bgz")


def _sidecar_parent(path: str) -> Optional[str]:
    """The artifact `path` is a companion OF, or None if it is not a companion.

    `/run/x.bam.bai` → `/run/x.bam`; `/run/x.bam` → None."""
    if not isinstance(path, str):
        return None
    low = path.lower()
    for suf in _SIDECAR_SUFFIXES:
        if low.endswith(suf) and len(path) > len(suf):
            return path[: -len(suf)]
    return None


def _dir_contains_a_prior_output(path: str, universe: set) -> bool:
    """Is `path` the directory a prior step wrote its outputs INTO?

    True only when some artifact already in the universe has `path` as its EXACT
    parent. The exactness is the whole safety property:

      * prefix matching would let `/work/run1` capture `/work/run10`;
      * ancestor matching would let an input of `/work` trace to every output
        anything ever produced, which is not provenance, it is a wildcard.

    Scoped this way it is not a loosening of I8 but an expression of a relation
    the full-path rule cannot state: a step whose declared input is the directory
    holding a prior step's recorded outputs really does consume them. That is the
    normal shape of a cluster chain — htseq-count writes six `.counts.tsv` into
    the shared remote workflow_dir and DESeq2 takes the directory."""
    if not isinstance(path, str) or not path:
        return False
    target = path.rstrip("/") or "/"
    for produced in universe:
        if isinstance(produced, str) and produced:
            if str(Path(produced).parent) == target:
                return True
    return False


def _ran_off_host(step: dict) -> bool:
    """Did this step execute on a filesystem other than ours?

    Cluster steps consume paths under the compute env's scratch, which share no
    namespace with the local paths every declared external source is recorded as. I8
    needs to know that so it can tell "incomparable paths" from "unrelated paths" —
    the first deserves a fallback, the second is the orphan it exists to catch.

    Read from the step's own runtime evidence (`validation_locus`, the sacct/cluster
    fields `run_step_on_cluster` stamps), never from a caller-supplied flag: a step
    could otherwise claim cross-locus staging to excuse a genuine orphan."""
    if not isinstance(step, dict):
        return False
    locus = (step.get("validation_locus")
             or (step.get("resource_usage") or {}).get("locus") or "")
    return str(locus).lower() not in ("", "host", "local", "container")

EXTERNAL_SOURCE_KINDS = frozenset({
    "test_data", "reference_databases", "runtime_configs", "authored_artifacts",
})


# ---------------------------------------------------------------------------
# Pipeline spec persistence
# ---------------------------------------------------------------------------

def derive_pipeline_status(steps: list) -> str:
    """The real run status of a set of pipeline_steps, per the PipelineStatus
    Literal (core_data.py). ONE definition — seal STORES it on the WorkflowSpec
    and the renderer READS the stored value, so there is no forked derivation.

    Replaces the fabricated `pipeline_status = "in_progress"` default that seal
    used to stamp into every spec regardless of the run (the draft's dead nominal
    stamp propagated straight through). Now the producer STATES the truth:
        failed              — any step exited non-zero
        fully_validated     — every step's outputs passed validate_output
        partially_validated — some validated, some ran-but-unvalidated
        complete            — all ran cleanly, none validated
        in_progress         — no steps yet
    """
    steps = [s for s in (steps or []) if isinstance(s, dict)]
    if not steps:
        return "in_progress"
    if any(s.get("returncode") not in (None, 0) for s in steps):
        return "failed"
    validated = [s for s in steps if step_is_validated(s)]
    if len(validated) == len(steps):
        return "fully_validated"
    if validated:
        return "partially_validated"
    return "complete"


# Fields whose ABSENCE is a known late-seal trap: the invariants all pass, then the
# model refuses at write time. Pydantic names the field but not the remedy, and by then
# every expensive step is already done. THE GATE IS THE GUIDE — the knowledge about how
# to satisfy a refusal belongs AT the refusal, not in prose the agent must pre-load every
# session to avoid tripping it. A refusal that only says "no" gets worked around; one
# that says "here" gets fixed.
_SHAPE_FIX = {
    "usage.description":
        "a one-line human sentence saying what the how-to does. Required whenever a "
        "`usage` block is present, and NO invariant checks it — so it surfaces only "
        "here, after everything else passed.",
    "usage.command_template":
        "a str, or a list[str] for a multi-phase how-to (one command per entry, run in "
        "order in one shared working dir).",
    "usage.inputs":  "one entry per input, each with a `format`.",
    "usage.outputs": "one entry per output, each with a `files` glob. An EMPTY outputs "
                     "list makes the self-test `not_attempted`, never `verified` — with "
                     "nothing declared a trial can only prove the command exits 0, which "
                     "`touch` also does.",
}


def _shape_fix_hint(err: Exception) -> str:
    """Turn a pydantic ValidationError into a remedy the caller can act on.

    Never replaces the raw error — the evidence stays. This only adds the 'and here is
    how to supply it' half, plus the primitive that writes these fields, because every
    agent-authored key on a draft goes through exactly one door (`patch_pipeline`) and
    an agent that does not know that reaches for the wrong one."""
    errors = getattr(err, "errors", None)
    if not callable(errors):
        return ""
    lines: list[str] = []
    try:
        for e in errors():
            path = ".".join(str(p) for p in e.get("loc", ()) if not isinstance(p, int))
            remedy = _SHAPE_FIX.get(path)
            lines.append(f"{path}: {remedy}" if remedy
                         else f"{path}: {e.get('msg', 'invalid')}")
    except Exception:
        return ""
    if not lines:
        return ""
    return ("Supply these on the draft with "
            "patch_pipeline(pipeline_id, {...}) and seal again — " + " | ".join(lines))


def write_workflow_spec(workflow: dict, config: dict) -> dict:
    """Validate + write a Layer-2 WorkflowSpec as YAML.

    The human-facing how-to is rendered as HTML into {name}.RUN.html by the seal
    (run_dashboard_html), NOT a markdown guide — the markdown GUIDE.md is retired.
    Any legacy `user_guide` markdown on the workflow dict is dropped here (never
    inlined in the YAML, never written to disk)."""
    # (helper below is module-level; see _shape_fix_hint)
    from agent.models.core_data import WorkflowSpec

    project_root = Path(__file__).parent.parent.parent.resolve()
    out_dir = project_root / config["paths"]["pipelines_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        wf = WorkflowSpec.model_validate(workflow)
    except Exception as e:
        return refused("spec_writer.validation_failed",
                       error=f"WorkflowSpec validation failed: {e}",
                       fix=_shape_fix_hint(e))

    name = wf.workflow_name
    yaml_path = out_dir / f"{name}.workflow.yaml"
    # NOT `exclude_none=True`. That made this function a one-way door: the dict
    # validated on the way IN, then every None was stripped on the way OUT, so a
    # model field that is REQUIRED-but-nullable came back MISSING and
    # `load_workflow_spec` — which advertises itself as the exact inverse of this
    # function — refused the very spec this function had just written.
    #
    # Caught by sealing the first workflow to carry a directory content-anchor:
    # `core_data_dir: {kind: directory, sha256: None, size_bytes: None}` landed on
    # disk as `{kind: directory}`, and `describe_sealed_step` then rejected a
    # freshly-sealed, fully-validated workflow. Nothing was wrong with the run —
    # only with the spelling on disk.
    #
    # The typed-record rule this broke is the repo's own (see CLAUDE.md, the
    # typed-record seam): every field required, `None` a value a producer must
    # STATE. `exclude_none` un-states it. Explicit `null`s cost some YAML noise and
    # buy a spec that round-trips, which is the whole point of a sealed artifact.
    data = wf.model_dump()
    data.pop("user_guide", None)        # retired markdown guide — never persist it
    data.pop("user_guide_path", None)
    yaml_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return {"workflow_spec_path": str(yaml_path)}


def load_workflow_spec(path: Any) -> Any:
    """THE typed reader for a sealed ``{name}.workflow.yaml`` — the read-back seam,
    the exact inverse of ``write_workflow_spec`` (and the WorkflowSpec analog of
    ``intent.parse_intent`` / ``plan.parse_plan``).

    A sealed spec is validated ONCE, at write (``write_workflow_spec`` →
    ``WorkflowSpec.model_validate``). Thereafter every read path in this codebase
    ``yaml.safe_load``-s it into a plain dict and ``.get()``-scrapes summary fields
    (``agent_status``, ``resources.list_pipelines``, ``pipeline_state.spec_sealed``).
    That scrape is harmless for an orientation SUMMARY — but re-running a RECORDED
    step means pulling a command we are about to EXECUTE in the shipped image out of
    that dict, which is exactly the read where a scrape is dangerous: the
    bcftools-1.23.1 lesson is that a ``.get``/regex over a record returns the wrong
    field under a confident name, and "the wrong command in the right container" is a
    silent, ground-truthed lie. So any consumer that ACTS on a sealed spec reads it
    back through THIS typed seam — a malformed artifact fails HERE, loudly
    (``pydantic.ValidationError``), rather than surfacing as a bad command deep in a
    container run.

    Returns a validated ``WorkflowSpec``. Raises: ``FileNotFoundError`` if ``path``
    is absent, ``yaml.YAMLError`` on unparseable YAML, ``ValueError`` if the document
    is not a mapping, ``pydantic.ValidationError`` if the record no longer satisfies
    the model. (``WorkflowSpec`` is ``extra="allow"``, so the runtime-authored extra
    keys — ``detected_outputs``, ``container_image_digest``, … — ride back through
    untouched.)
    """
    from agent.models.core_data import WorkflowSpec

    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(
            f"sealed workflow at {path} is not a mapping (got {type(raw).__name__}); "
            "a WorkflowSpec YAML must be a top-level object")
    return WorkflowSpec.model_validate(raw)


def select_pipeline_step(spec: Any, step_number: int) -> Any:
    """Pick ONE recorded step out of a typed ``WorkflowSpec`` by its (1-based) ``step``
    number. Pure — no side effects, no disk. Raises ``ValueError`` naming the
    available step numbers when ``step_number`` is not present, so a wrong guess
    self-corrects against the real roster instead of silently selecting nothing (the
    silent-empty trap the honesty contract exists to close)."""
    for st in spec.pipeline_steps:
        if st.step == step_number:
            return st
    available = [st.step for st in spec.pipeline_steps]
    raise ValueError(
        f"workflow '{spec.workflow_name}' has no pipeline_step number {step_number}; "
        f"recorded step numbers are {available or '(none)'}")


def self_test_usage(spec: dict, env_manager: Any, validator: Optional[Any] = None) -> dict:
    """Execute usage.command_template with real test inputs and verify outputs.

    Keystone honesty check (I4): for each declared input shape, substitute the
    {PLACEHOLDER} slots, run the resulting command in a fresh scratch dir, and
    confirm files declared in usage.outputs are produced. usage_verified=True
    requires every declared trial to pass.

    Trial selection:
      - If usage.trials is non-empty: run one trial per entry. Each trial's
        `substitutions` dict directly fills the placeholders. {OUTPUT_DIR}
        (and any *output* slot) is auto-overridden to a fresh per-trial
        scratch dir. This is the multi-shape contract — multiple input
        shapes prove the machinery isn't a one-trick.
      - If usage.trials is empty: fall back to a single auto-inferred trial
        sourced from pipeline_steps[*].inputs (backward-compatible).

    Returns:
      {ok: bool, trials: [<per-trial result>...], reason?: str}
      Each per-trial result has: name, ok, command_run, substitutions,
      produced_files, scratch_dir, [reason, missing_outputs, stderr_tail].
    """
    # THREE-STATE, not a bool. `ok: False` used to mean two utterly different things —
    # "the how-to was tested and it FAILED" and "the how-to was never tested at all" —
    # and seal recorded both as usage_verified=False, which the dashboard then rendered as
    # a verdict. Since seal REFUSES on a genuine failure, every usage_verified=False that
    # reached disk provably meant "never attempted": a fabricated verdict, absence of data
    # rendering as data (audit 2026-07-16). `status` says which:
    #   verified      — every declared trial ran and produced validated outputs
    #   failed        — a trial ran and did not (seal refuses; never reaches disk)
    #   not_attempted — we had no way to run it; `reason` says why. NOT a judgement of the
    #                   how-to, and must never be rendered as one.
    def _not_attempted(reason: str) -> dict:
        return {"ok": False, "status": "not_attempted", "reason": reason, "trials": []}

    usage = spec.get("usage")
    if not usage or not isinstance(usage, dict):
        return _not_attempted("no usage block to self-test")
    # ONE reading of command_template (str or list[str]) — see core_data.usage_commands.
    commands = usage_commands(usage)
    if not commands:
        return _not_attempted("usage.command_template is empty")
    template = "\n".join(commands)   # placeholder scanning spans every command

    # NOTHING DECLARED ⇒ NOTHING VERIFIED. A trial is judged on its outputs: every
    # declared pattern must match a produced file and every match must pass type-aware
    # validation. With `usage.outputs == []` both loops iterate zero times, so the trial
    # returned ok=True having established only that the command exited 0 — and
    # `usage_verified: True` went onto the sealed spec and the run dashboard over a
    # how-to nobody checked the results of. `touch` would have passed.
    #
    # `not_attempted` is the honest state and the one that already exists for exactly
    # this ("we had no way to run it; NOT a judgement of the how-to"). It does not refuse
    # the seal — it records `usage_verified: False` with a reason, which is the truth.
    if not usage.get("outputs"):
        return _not_attempted(
            "usage.outputs is empty, so a trial could only prove the command exits 0 — "
            "not that it produces anything. Declare usage.outputs[*].files (a glob per "
            "output, written through a recognized OUTPUT slot such as {OUTPUT_DIR}) to "
            "make this how-to self-testable.")

    # A runner is either injected (the frozen image — validated == shipped) or taken from
    # the host conda env. The host env is the PRE-freeze iteration path; requiring it was
    # what silently disabled I4 for every container-native env.
    env_name = spec.get("conda_env")
    if env_manager is None:
        # Say only what is KNOWN here: the seal was handed no runner. The old
        # sentence asserted two probes ("neither a frozen env image nor a host
        # conda env could execute") that this function cannot see and that were
        # not both taken — sea-trial F19 hit it with the image present by digest
        # and a working host env on disk. Causes are named as candidates.
        return _not_attempted("no runner available — the seal could not build an in-image "
                              "runner (the pinned image did not resolve in the local "
                              "daemon, or a trial input exists only at another locus) and "
                              "the spec names no conda_env for a host-env fallback, so "
                              "nothing here executed the how-to")
    if getattr(env_manager, "is_image_runner", False):
        env_name = env_name or "<frozen image>"
    elif not env_name:
        return _not_attempted(
            "no conda_env on spec and no frozen env image supplied — nothing to run the "
            "how-to in. Freeze the env and re-seal to self-test against the shipped bytes.")

    placeholders = set(re.findall(r"\{([A-Z][A-Z0-9_]*)\}", template))
    if not placeholders:
        return _not_attempted("command_template has no {PLACEHOLDER} slots")

    declared_trials = usage.get("trials") or []
    inputs_spec     = usage.get("inputs", [])  or []
    outputs_spec    = usage.get("outputs", []) or []

    if declared_trials:
        trial_plans = [
            {
                "name":          (t.get("name") or f"trial_{i+1}"),
                "substitutions": dict(t.get("substitutions") or {}),
                "description":   t.get("description"),
            }
            for i, t in enumerate(declared_trials) if isinstance(t, dict)
        ]
    else:
        # Backward-compatible single inferred trial.
        inferred = _infer_substitutions(spec, placeholders, inputs_spec)
        # An INCOMPLETE inference cannot be run — the command would still carry literal
        # `{PEDIGREE}` text. Running it anyway reports "failed", which reads as a verdict
        # on the how-to when the truth is we never managed to build a trial for it. Real
        # case: talos_validate_moi declares no trials and inference fills 2 of its 5 slots,
        # so it self-tested as FAILED and (once I4 gates the seal) became unsealable — the
        # how-to was fine; our trial was not.
        missing = sorted(p for p in placeholders
                         if not _is_output_slot(p) and p not in inferred)
        if missing:
            return _not_attempted(
                f"could not infer a value for {', '.join('{'+m+'}' for m in missing)} from "
                f"pipeline_steps[*].inputs, so no runnable trial could be built. Declare "
                f"usage.trials with explicit substitutions to self-test this how-to.")
        trial_plans = [{
            "name":          "auto",
            "substitutions": inferred,
            "description":   "inferred from pipeline_steps[*].inputs (no usage.trials declared)",
        }]

    trial_results = [
        _run_one_trial(plan, commands, placeholders, outputs_spec, env_manager, env_name, spec, validator)
        for plan in trial_plans
    ]

    overall_ok = bool(trial_results) and all(t.get("ok") for t in trial_results)
    return {
        "ok":     overall_ok,
        "status": "verified" if overall_ok else "failed",
        "locus":  "image" if getattr(env_manager, "is_image_runner", False) else "host",
        "trials": trial_results,
        "trial_count": len(trial_results),
        "passed":      sum(1 for t in trial_results if t.get("ok")),
        "source":      "trials" if declared_trials else "inferred",
    }


def _is_output_slot(slot: str) -> bool:
    """The I4 self-test convention, in ONE place (was duplicated inline).

    The self-test runs each trial in a fresh scratch dir and scans ONLY that dir
    for usage.outputs[*].files. So it must know which template placeholders are
    OUTPUT targets — those get the scratch path substituted so produced files land
    where the scan looks. A placeholder is an output slot when its name is
    {OUTPUT_DIR}/{OUT_DIR} or contains 'output'. Any other slot keeps the trial's
    declared value; an output written through an UNRECOGNIZED slot lands outside
    the scratch dir and is invisible to the scan (the sea-trial `{OUT_TSV}` trap).
    Referenced by the I4 failure hint so the refusal explains this convention."""
    s = slot.lower()
    return "output" in s or "out_dir" in s


def _infer_substitutions(spec: dict, placeholders: set, inputs_spec: list) -> dict:
    """Fallback single-trial inference from pipeline_steps[*].inputs.

    Same policy as before the multi-shape extension: extension-match by
    declared format, then positional fallback. {OUTPUT_*} slots are left
    unfilled — _run_one_trial substitutes the per-trial scratch dir.
    """
    candidate_paths: list[str] = []
    for s in spec.get("pipeline_steps", []) or []:
        for inp in (s.get("inputs") or []):
            p = inp.get("path") if isinstance(inp, dict) else inp
            if isinstance(p, str) and p and Path(p).exists():
                candidate_paths.append(p)
            if isinstance(inp, dict):
                for ref in inp.get("references", []) or []:
                    if isinstance(ref, str) and Path(ref).exists():
                        candidate_paths.append(ref)
        for o in (s.get("detected_outputs") or []):
            if isinstance(o, str) and Path(o).exists():
                candidate_paths.append(o)
    seen = set(); cps = []
    for p in candidate_paths:
        if p not in seen:
            cps.append(p); seen.add(p)
    candidate_paths = cps

    substitutions: dict = {}
    for slot in placeholders:
        if _is_output_slot(slot):
            continue   # _run_one_trial fills these with scratch
        slot_spec = next((i for i in inputs_spec if i.get("name") == slot), None)
        fmt = (slot_spec.get("format") or "").lower() if slot_spec else ""

        chosen = None
        if fmt:
            for cp in candidate_paths:
                low = cp.lower()
                if fmt in low or low.endswith(f".{fmt}") or low.endswith(f".{fmt}.gz"):
                    chosen = cp
                    break
        if not chosen and candidate_paths:
            required_slots = [s for s in inputs_spec if s.get("required") is not False]
            try:
                idx = next(i for i, s in enumerate(required_slots) if s.get("name") == slot)
                if idx < len(candidate_paths):
                    chosen = candidate_paths[idx]
            except StopIteration:
                pass
        if chosen:
            substitutions[slot] = chosen
    return substitutions


def _run_one_trial(
    plan: dict, commands: list[str], placeholders: set, outputs_spec: list,
    env_manager: Any, env_name: str, spec: dict, validator: Optional[Any],
) -> dict:
    """Execute one trial: substitute, run EVERY command in order in one fresh
    scratch dir, verify outputs.

    `commands` is the normalized usage.command_template (see core_data.usage_commands):
    a multi-command how-to runs in sequence, sharing the scratch dir, so command 2 can
    consume command 1's output — which is what makes a Phase A→B→C pipeline self-testable
    at all. The FIRST non-zero rc fails the trial and stops: continuing past a failure
    would let a later command produce the declared outputs and paper over a broken step.

    The trial passes only when EVERY pattern in usage.outputs[*].files matched
    a produced file AND every matched file passes type-aware validate_output.
    Filename-match alone (the original I4) was satisfiable by `touch foo.bam`;
    requiring the validator to confirm the bytes are a real BAM, VCF, JSON,
    etc. kills the cheap-fake path.
    """
    import fnmatch, shutil, tempfile

    trial_name = plan["name"]
    subs       = dict(plan.get("substitutions") or {})
    scratch    = Path(tempfile.mkdtemp(prefix=f"selftest_{spec.get('pipeline_name','x')}_{trial_name}_"))

    # Always override output slots with this trial's scratch dir.
    # `output_slots` travels WITH the substitution map because the two halves of that
    # map have different lifetimes and a reader must be able to tell them apart: an
    # input slot holds a durable path to real test data, an output slot holds a temp
    # directory that is deleted before anyone reads the report. Without this the run
    # dashboard would have to re-derive the distinction by re-implementing
    # `_is_output_slot` at the render site — a second reading of the convention that
    # decides where the self-test looks for outputs, which is exactly the shape of
    # drift this codebase keeps paying for.
    output_slots = sorted(s for s in placeholders if _is_output_slot(s))
    for slot in output_slots:
        subs[slot] = str(scratch)

    missing = [s for s in placeholders if s not in subs or not subs[s]]
    if missing:
        shutil.rmtree(scratch, ignore_errors=True)
        return {
            "name": trial_name, "ok": False,
            "reason": f"could not resolve placeholders: {missing}",
            "substitutions": subs, "output_slots": output_slots,
        }

    def _fill(cmd: str) -> str:
        for slot, val in subs.items():
            cmd = cmd.replace("{" + slot + "}", str(val))
        return cmd

    ran: list[str] = []
    for i, raw in enumerate(commands, start=1):
        command = _fill(raw)
        ran.append(command)
        result = env_manager.run_in_env(env_name, command, timeout=600, watch_dir=str(scratch))
        rc = result.get("returncode")
        if rc != 0:
            # Stop at the first failure — a later command must never be allowed to
            # produce the declared outputs on top of a broken earlier one.
            step = (f"command {i} of {len(commands)}" if len(commands) > 1
                    else "command_template")
            return {
                "name": trial_name, "ok": False,
                "reason": f"{step} execution failed (rc={rc})",
                "command_run": command, "commands_run": ran,
                "failed_index": i, "scratch_dir": str(scratch),
                "stderr_tail": (result.get("stderr") or "")[-500:],
                "substitutions": subs, "output_slots": output_slots,
            }

    # Every command succeeded. From here on the trial is judged on its OUTPUTS, and
    # what "ran" means is the whole sequence — reporting only the last command would
    # misname the thing that was tested the moment a how-to has more than one step.
    command_run = "\n".join(ran)

    produced = []
    for p in scratch.rglob("*"):
        if p.is_file():
            produced.append(str(p.relative_to(scratch)))

    # Step 1: every declared pattern must match at least one produced file.
    missing_outputs = []
    matched_files: dict[str, list[tuple[str, str]]] = {}   # pattern → [(produced_rel, declared_type)]
    for o in outputs_spec:
        patterns = o.get("files") or []
        declared_type = (o.get("type") or "").lower()
        for pat in patterns:
            matches = [
                f for f in produced
                if fnmatch.fnmatch(f, pat) or fnmatch.fnmatch(Path(f).name, pat)
            ]
            if not matches:
                missing_outputs.append({"slot": o.get("name"), "pattern": pat})
            else:
                matched_files.setdefault(pat, []).extend((m, declared_type) for m in matches)

    if missing_outputs:
        # Legibility (sea-trial finding): the raw "produced_files: []" gives an
        # unattended agent no way to see WHY nothing landed. The usual cause is an
        # output written through a placeholder the self-test didn't recognize as an
        # output slot (so it kept the agent's own absolute path instead of the
        # scratch dir). Disclose the convention + which slots WERE recognized so the
        # fix is obvious — without weakening the check.
        recognized = sorted(s for s in placeholders if _is_output_slot(s))
        out = {
            "name": trial_name, "ok": False,
            "reason": "command ran but expected outputs missing",
            "missing_outputs": missing_outputs,
            "produced_files": produced[:20],
            "recognized_output_slots": recognized,
            "command_run": command_run, "commands_run": ran, "scratch_dir": str(scratch),
            "substitutions": subs, "output_slots": output_slots,
        }
        if not produced:
            out["hint"] = (
                "Nothing landed in the self-test scratch dir. The self-test scans "
                "ONLY a fresh scratch dir and fills that path into OUTPUT slots — "
                "placeholders named {OUTPUT_DIR}/{OUT_DIR} or containing 'output' "
                f"(recognized here: {recognized or 'NONE'}). An output written via "
                "e.g. `-o {OUT_TSV}` uses an unrecognized slot, so the file lands "
                "outside the scratch dir and is invisible. Fix: write outputs under "
                "a recognized slot, e.g. `-o {OUTPUT_DIR}/stats.tsv`, and declare its "
                "glob in usage.outputs[*].files."
            )
        return out

    # Step 2: type-aware validation on each matched file. Skips when the
    # validator wasn't passed in (legacy path / unit tests), but with the
    # MCP server wired up, validator is always supplied so this is the
    # mainline.
    validation_results: list[dict] = []
    if validator is not None:
        for pat, matches in matched_files.items():
            for produced_rel, declared_type in matches:
                abs_path = str(scratch / produced_rel)
                # Resolve expected type: declared > extension inference > "any".
                etype = declared_type or _infer_type_from_basename(Path(produced_rel).name)
                v = validator.validate(abs_path, etype, env_name=env_name)
                validation_results.append({
                    "pattern":       pat,
                    "file":          produced_rel,
                    "expected_type": etype,
                    "passed":        bool(v.get("passed")),
                    "method":        v.get("validation_method") or v.get("method"),
                    "error":         v.get("error"),
                })

        failed = [v for v in validation_results if not v["passed"]]
        if failed:
            return {
                "name": trial_name, "ok": False,
                "reason": "command ran and produced files but type-validation failed",
                "failed_validations": failed[:10],
                "validation_results": validation_results[:20],
                "produced_files": produced[:20],
                "command_run": command_run, "commands_run": ran, "scratch_dir": str(scratch),
                "substitutions": subs, "output_slots": output_slots,
            }

    return {
        "name": trial_name, "ok": True,
        "command_run": command_run, "commands_run": ran, "substitutions": subs,
        "output_slots": output_slots,
        "produced_files": produced[:20], "scratch_dir": str(scratch),
        "validation_results": validation_results[:20],
        "description": plan.get("description"),
    }


def _infer_type_from_basename(basename: str) -> str:
    """Map a basename to a validator type. Mirrors _infer_validator_type in
    mcp_server.py; kept here to avoid the import cycle."""
    name = basename.lower()
    if name.endswith(".gz"):
        name = name[:-3]
    last = name.rsplit(".", 1)[-1]
    for known in ("bam", "sam", "bai", "vcf", "bcf", "fasta", "fastq",
                  "bed", "bigwig", "gtf", "gff", "gfa", "tsv", "csv", "txt",
                  "html", "json", "jsonl", "fq"):
        if last == known:
            # validator dispatches on "fasta" not "fa", "fastq" not "fq"
            return "fastq" if known == "fq" else known
    return "any"


def check_invariants(spec: dict) -> list[dict]:
    """Return the run-side (Layer-2) invariant violations. Empty list = the
    workflow's recorded run is machine-verifiable as honest — every claimed
    output was observed + type-validated and every input traces to a source.

    Each violation: {invariant: <id>, message: <str>, where: <optional path>}.

    Scope is I0 (shape) · I3 (validated outputs) · I6 (absolute paths +
    declared placeholders) · I7 (resource_usage) · I8 (input provenance). The
    env-build invariants are NOT here (see agent/skills/invariants.py) — an env
    is verified IN its shipped image by env_honesty.check_build (install==ship).
    seal_workflow runs this (via check_workflow_invariants) over the validated
    run and refuses to write a WorkflowSpec if anything fails.
    """
    violations: list[dict] = []

    # ------------------------------------------------------------------
    # I0: shape sanity. Every top-level list-of-records must contain only
    # dict entries — anything else means an upstream primitive misbehaved
    # (or someone bypassed the writer-API). The downstream invariant clauses
    # defensively skip non-dicts to avoid AttributeError; this gate makes
    # sure such skips are LOUD, not silent.
    # ------------------------------------------------------------------
    for list_key in (
        "install_steps", "packages", "pipeline_steps",
        "reference_databases", "runtime_configs",
        "service_dependencies", "authored_artifacts",
    ):
        items = spec.get(list_key)
        if items is None:
            continue
        if not isinstance(items, list):
            violations.append({
                "invariant": "I0.shape_sanity",
                "message":   f"top-level field '{list_key}' is {type(items).__name__}, expected list",
                "where":     list_key,
            })
            continue
        for i, entry in enumerate(items):
            if not isinstance(entry, dict):
                violations.append({
                    "invariant": "I0.shape_sanity",
                    "message":   f"{list_key}[{i}] is {type(entry).__name__}, expected dict — "
                                 f"a malformed record skipped subsequent invariant checks",
                    "where":     f"{list_key}[{i}]",
                })

    # ------------------------------------------------------------------
    # I3: every pipeline_step has detected_outputs (observed by the runtime
    # via filesystem snapshot — agent cannot fake) AND each output has a
    # validate_output record OR the step has been explicitly marked passed
    # because its outputs were checked elsewhere (mark_step_validated, which
    # itself refuses to mark a no-output step as passed).
    # ------------------------------------------------------------------
    for s in spec.get("pipeline_steps", []) or []:
        if not isinstance(s, dict):
            continue
        step_n = s.get("step")
        outs = s.get("detected_outputs") or s.get("outputs") or []
        if s.get("returncode") not in (None, 0):
            continue   # failed steps are honestly marked elsewhere
        if not outs:
            if s.get("validation_status") != "passed":
                violations.append({
                    "invariant": "I3.pipeline_step_has_outputs",
                    "message":   f"pipeline_step {step_n} produced no detected_outputs "
                                 f"and is not explicitly mark_step_validated — "
                                 f"there is no evidence the step did anything.",
                    "where":     f"pipeline_steps[step={step_n}]",
                })
            continue
        validation = s.get("validation") or {}
        # Coverage is asked PER OUTPUT PATH, through the one reader that knows how a
        # validation record is keyed. It used to compare `Path(o).name` against the key
        # set, which meant `/out/a/result.bam` counted as validated because a DIFFERENT
        # file called `result.bam` had a record — the read side of the same basename
        # collision that let the write side erase a failure.
        unvalidated = [o for o in outs if not _validation_covers(validation, o)]
        # An explicit mark_step_validated=passed substitutes for per-file
        # validate_output records (use case: outputs aren't validate_output-able
        # but the agent verified them by other means — mark_step_validated
        # already refuses to pass for outputs-empty steps).
        if unvalidated and s.get("validation_status") != "passed":
            violations.append({
                "invariant": "I3.outputs_validated",
                "message":   f"pipeline_step {step_n} has {len(unvalidated)} detected_outputs "
                             f"with no validate_output result and no mark_step_validated — "
                             f"the spec claims outputs were produced but no validation was run",
                "where":     f"pipeline_steps[step={step_n}].detected_outputs",
                "unvalidated_files": [Path(o).name for o in unvalidated[:5]],
            })

        # I3 amendment (C1): a validate_output record existing is NOT the same
        # as it PASSING. The runtime records passed:False for a malformed BAM /
        # empty VCF / bad JSON — but seal used to accept any record. That let a
        # spec claim "outputs checked" over a step whose outputs demonstrably
        # failed their type-aware check.
        #
        # THIS CLAUSE IS NOT OVERRIDABLE (audit 2026-07-16, re-audit). It used to honour
        # `mark_step_validated=passed`, which made the agent's assertion outrank the
        # runtime's own measurement — the exact thing CLAUDE.md's opening promise rules
        # out ("nothing is taken on faith from the agent"). The other I3 clauses are about
        # ABSENT evidence, where an agent saying "I checked it another way" adds
        # information. This one is about evidence that EXISTS and says FAILED; an
        # assertion cannot un-fail a measurement, it can only hide it. If the validator is
        # wrong, fix the validator or re-run the step — don't let the spec outrank the run.
        failed_validations = [
            fn for fn, v in validation.items()
            if isinstance(v, dict) and v.get("passed") is False
        ]
        if failed_validations:
            violations.append({
                "invariant": "I3.validation_passed",
                "message":   f"pipeline_step {step_n} has {len(failed_validations)} output(s) "
                             f"whose validate_output result is passed=False — the run recorded "
                             f"that these outputs FAILED type-aware validation. A spec cannot seal "
                             f"over a failed output check, and mark_step_validated cannot override "
                             f"this: re-run the step, or fix the validator if it is wrong.",
                "where":     f"pipeline_steps[step={step_n}].validation",
                "failed_files": failed_validations[:5],
            })

        # I3 amendment: expected_type="any" is the lazy fallback that only
        # checks file-exists-and-nonzero. For biomedical-grade specs, every
        # validation must declare a real type so OutputValidator dispatches
        # to a type-aware checker (samtools view for BAM, bcftools for VCF,
        # json.loads for JSON, etc.). Forces agents to pass output_types in
        # run_pipeline_step rather than leaning on extension-inference
        # falling through to "any".
        any_typed = [
            (fn, v) for fn, v in validation.items()
            if isinstance(v, dict) and (v.get("expected_type") or "").lower() == "any"
        ]
        if any_typed:
            violations.append({
                "invariant": "I3.declared_output_type",
                "message":   f"pipeline_step {step_n} has {len(any_typed)} output(s) "
                             f"validated as expected_type='any' (exists-nonzero only). "
                             f"Declare a real type via run_pipeline_step's output_types "
                             f"so the validator does type-aware checks. Lazy 'any' fails "
                             f"the honesty contract — `touch foo.bar` would pass.",
                "where":     f"pipeline_steps[step={step_n}].validation",
                "any_typed_files": [fn for fn, _ in any_typed[:5]],
            })

    # ------------------------------------------------------------------
    # I6: paths in known-path fields are absolute. Relative paths in a
    # spec are reproducibility landmines (depend on the agent's CWD at
    # finalize time). Skip slot placeholders like {INPUT_VCF}.
    # ------------------------------------------------------------------
    def _is_path_like(s: str) -> bool:
        if not isinstance(s, str) or not s:
            return False
        if s.startswith(("{", "$", "<")):
            return False   # placeholder
        if "/" not in s:
            return False   # bare token, not a path
        return True

    for s in spec.get("pipeline_steps", []) or []:
        if not isinstance(s, dict):
            continue
        step_n = s.get("step")
        for inp in s.get("inputs", []) or []:
            p = inp.get("path") if isinstance(inp, dict) else inp
            if _is_path_like(p) and not Path(p).is_absolute():
                violations.append({
                    "invariant": "I6.absolute_paths",
                    "message":   f"pipeline_step {step_n} has a relative input path: {p}",
                    "where":     f"pipeline_steps[step={step_n}].inputs",
                })
        for o in s.get("detected_outputs", []) or []:
            if _is_path_like(o) and not Path(o).is_absolute():
                violations.append({
                    "invariant": "I6.absolute_paths",
                    "message":   f"pipeline_step {step_n} has a relative output path: {o}",
                    "where":     f"pipeline_steps[step={step_n}].detected_outputs",
                })
        # remote_outputs feeds the I8 universe, so it is a path channel into the
        # lineage graph and gets the same absoluteness rule. A field that other
        # invariants trust and this one does not examine is how an unchecked
        # channel opens.
        for o in s.get("remote_outputs", []) or []:
            if _is_path_like(o) and not Path(o).is_absolute():
                violations.append({
                    "invariant": "I6.absolute_paths",
                    "message":   f"pipeline_step {step_n} has a relative remote output "
                                 f"path: {o}",
                    "where":     f"pipeline_steps[step={step_n}].remote_outputs",
                })

    # ------------------------------------------------------------------
    # I6 (extended): every {PLACEHOLDER} in usage.command_template must
    # resolve to a declared usage.inputs[*].name OR be one of the auto-
    # allocated scratch slots (OUTPUT_DIR / OUT_DIR). A typo like
    # {OUPUT_DIR} silently passes _is_path_like (it starts with `{`) and
    # the dynamic self-test only catches it when a trial actually runs —
    # this static check surfaces it sooner.
    # ------------------------------------------------------------------
    usage = spec.get("usage") if isinstance(spec.get("usage"), dict) else None
    if usage:
        # Scan EVERY command (str or list[str]) through the one reader — an undeclared
        # placeholder in command 3 of a multi-step how-to is exactly as broken as one in
        # command 1, and re-reading the raw field here would have seen only the first.
        template = "\n".join(usage_commands(usage))
        if template:
            template_placeholders = set(re.findall(r"\{([A-Z][A-Z0-9_]*)\}", template))
            declared_input_names = {
                (inp.get("name") or "").strip()
                for inp in (usage.get("inputs") or [])
                if isinstance(inp, dict) and inp.get("name")
            }
            scratch_slots = {"OUTPUT_DIR", "OUT_DIR"}
            legal = declared_input_names | scratch_slots
            undeclared = sorted(template_placeholders - legal)
            if undeclared:
                violations.append({
                    "invariant": "I6.template_placeholders_declared",
                    "message":   f"usage.command_template references placeholders "
                                 f"{undeclared} that are not declared in usage.inputs[*].name "
                                 f"(and aren't OUTPUT_DIR/OUT_DIR). Either add them to "
                                 f"usage.inputs or fix the typo. Declared inputs: "
                                 f"{sorted(declared_input_names) or '(none)'}.",
                    "where":     "usage.command_template",
                    "undeclared_placeholders": undeclared,
                })

    # ------------------------------------------------------------------
    # I7: every rc=0 pipeline_step has resource_usage populated by the
    # runtime psutil monitor. wall_seconds, peak_rss_mb, max_cpu_percent
    # are observations of a real execution — an agent can't synthesize
    # them without bypassing run_pipeline_step / run_in_env entirely.
    # Downstream HPC users need honest cost data to size jobs.
    # ------------------------------------------------------------------
    for s in spec.get("pipeline_steps", []) or []:
        if not isinstance(s, dict):
            continue
        step_n = s.get("step")
        if s.get("returncode") not in (None, 0):
            continue   # failed / not-run steps don't need resource data
        ru = s.get("resource_usage")
        if not isinstance(ru, dict) or "wall_seconds" not in ru or "peak_rss_mb" not in ru:
            violations.append({
                "invariant": "I7.resource_usage_recorded",
                "message":   f"pipeline_step {step_n} has no resource_usage — "
                             f"the runtime monitor never observed it run. "
                             f"Use run_pipeline_step or run_in_env (which populate this).",
                "where":     f"pipeline_steps[step={step_n}]",
            })
            continue

        # I7 amendment (C3): the KEYS existing is not enough — the VALUES must
        # be a real observation. A cluster step whose sacct query hiccuped
        # records an all-zeros sentinel with a `sacct_error` marker
        # (run_cluster_step.py). Any process that actually ran has a nonzero
        # peak RSS and nonzero wall — an all-zeros record means we have NO
        # honest cost data, so the badge (and the HPC job-sizing numbers the
        # guide publishes) would be fabricated. Reject rather than seal zeros.
        if ru.get("sacct_error"):
            violations.append({
                "invariant": "I7.resource_usage_captured",
                "message":   f"pipeline_step {step_n} resource_usage carries a sacct_error "
                             f"({ru.get('sacct_error')!r}) — the cluster accounting query "
                             f"failed, so wall/RSS/CPU are placeholder zeros, not a real "
                             f"observation. Re-run the step so sacct returns MaxRSS/Elapsed, "
                             f"or resolve the accounting delay before sealing.",
                "where":     f"pipeline_steps[step={step_n}].resource_usage",
            })
        elif (float(ru.get("peak_rss_mb") or 0) <= 0
              and float(ru.get("wall_seconds") or 0) <= 0):
            # DELIBERATELY `and` — an ALL-zeros record is the "monitor captured nothing"
            # shape. It is tempting to reject either-zero (the audit-2026-07-16 draft did,
            # since this rule's message says a real process has nonzero RSS *and* wall),
            # but a value-level check CANNOT tell fabrication from a sampling limit:
            #   - host locus: _run_monitored polls the process tree every 0.3s, so any step
            #     faster than that (`samtools --version` → rc=0, wall=0.01, peak_rss_mb=0.0)
            #     legitimately reports zero RSS. Rejecting it would refuse a real, green run.
            #   - cluster locus: a zero from sacct genuinely IS fabrication.
            # Only the PRODUCER knows which it is, so that is where the distinction lives:
            # cluster_jobs._parse_max_rss_mb returns None for "sacct accounted nothing" and
            # cluster_job_resources attaches an explicit `sacct_error`, which the branch
            # above already refuses. Fix the producer that fabricates; don't blind-tighten
            # a checker that can't see the difference.
            violations.append({
                "invariant": "I7.resource_usage_captured",
                "message":   f"pipeline_step {step_n} resource_usage is all zeros "
                             f"(wall={ru.get('wall_seconds')}, peak_rss_mb={ru.get('peak_rss_mb')}) "
                             f"— a process that actually ran has a nonzero peak RSS and wall "
                             f"time. Zeros mean the monitor captured nothing; the cost data "
                             f"would be fabricated. Re-run so the monitor observes a real run.",
                "where":     f"pipeline_steps[step={step_n}].resource_usage",
            })

    # ------------------------------------------------------------------
    # I8: composition coherence. Every step's inputs must trace to either
    # an external source (test_data, reference_databases, runtime_configs,
    # authored_artifacts) or a prior step's outputs. An orphan input — a path
    # no upstream source produces — means the pipeline doesn't actually
    # compose: step N consumed something step N-1 didn't make.
    # ------------------------------------------------------------------
    violations.extend(_check_composition_coherence(spec))

    # ------------------------------------------------------------------
    # I8 (authored-artifact integrity): re-hash every staged artifact at
    # seal time. The respine retired the dedicated I9 because Layer 1 bakes
    # the artifact into the image bytes — but Layer 2 references artifacts
    # OUTSIDE the image on disk, so 'install==ship' doesn't cover them. The
    # downstream consumer reads the file, not the spec, so the spec's claim
    # about the file (sha256) must keep matching disk up to the seal moment.
    # ------------------------------------------------------------------
    violations.extend(_check_authored_artifact_integrity(spec))

    # ------------------------------------------------------------------
    # I8 (test-data integrity): the same re-anchoring for the external source
    # the documented protocol actually tells the agent to produce. See
    # `verify_test_data` — until this existed, test_data was the one input
    # source whose paths were read only as strings to widen I8's universe.
    # ------------------------------------------------------------------
    violations.extend(_check_test_data_integrity(spec))

    # ------------------------------------------------------------------
    # I8 (lineage integrity): file-type-agnostic universal lineage. When a
    # step's input path matches a prior step's output, the on-disk bytes must
    # match the producer's recorded sha256 from run time. Catches an agent
    # silently mutating a file between steps; the path-only I8 walk would not.
    # ------------------------------------------------------------------
    violations.extend(_check_lineage_integrity(spec))

    # ------------------------------------------------------------------
    # I10 (service health, restored at Layer 2): every declared service_dependency
    # must have proven healthy. The respine retired I10 as an ENV-BUILD invariant
    # (an env doesn't need the service to BUILD) — but VALIDATED_IN_IMAGE says
    # nothing about whether a RUNTIME service a workflow depends on ever came up,
    # so it fell through the crack. This restores it at the layer where service
    # dependencies actually live (the Layer-2 draft), same move as the I9→I8
    # authored-artifact restoration above.
    # ------------------------------------------------------------------
    violations.extend(_check_service_health(spec))

    # ------------------------------------------------------------------
    # I5 (reference-database availability, restored at Layer 2): every declared
    # reference_database.local_path must exist on disk (and, when the record
    # carries integrity anchors, still match them). The respine retired I5 as an
    # env-build invariant claiming 'install==ship' subsumes it — but a reference
    # DB is, by the model's own contract, "mounted at runtime rather than baked
    # into the Docker image" (tens-to-hundreds of GB), so VALIDATED_IN_IMAGE
    # provably says nothing about it. It fell through the exact crack I9/I10 did:
    # a runtime-external artifact a workflow depends on. I8.composition_coherence
    # even treats a ref-DB's local_path as a valid input source WITHOUT checking
    # the file is there — so a step could seal green consuming a DB that is absent
    # or truncated. This restores existence + strengthens it with the sha256 anchor
    # the model already carries for downstream re-run pinning.
    # ------------------------------------------------------------------
    violations.extend(_check_reference_database_availability(spec))

    return violations


# Layer-2 (workflow) invariant subset. A WorkflowSpec consumes a frozen env by digest,
# so only the RUN-side invariants apply — the env-build ones are Layer 1's, verified IN
# the shipped image by env_honesty.check_build.
#
# DERIVED FROM THE REGISTRY, never typed out. The literal set that used to live here
# said seven ids while this module's own docstring said five, four lines apart, and
# CLAUDE.md's table said six — a different six. Reading it from `invariants` means a new
# clause is registered once and every consumer follows.
_WORKFLOW_INVARIANT_TIERS = frozenset(
    i.id for i in _invariants.active(_invariants.LAYER_WORKFLOW)
    if i.enforced_by.endswith("check_workflow_invariants"))


def check_workflow_invariants(spec: dict) -> list[dict]:
    """Run only the workflow-relevant invariants — see `agent/skills/invariants.py` for
    the roster and each one's statement (deliberately NOT restated here; this docstring
    used to name five of the seven).

    Pass the FULL draft so I8 sees the complete universe of prior outputs + external
    sources, but only the run-side violations are returned."""
    return [v for v in check_invariants(spec)
            if v.get("invariant", "").split(".")[0] in _WORKFLOW_INVARIANT_TIERS]


def _check_service_health(spec: dict) -> list[dict]:
    """I10 (restored at Layer 2): every declared service_dependency must have at
    least one HEALTHY probe in its health_check_log — proof the service actually
    came up while the workflow ran.

    The respine retired I10 among the env-build invariants, but it does NOT belong
    to Layer 1 (an env doesn't need the service running to BUILD) and it was never
    added to Layer 2 — so a WorkflowSpec could seal green while depending on a
    service that never became healthy (a status=failed / zero-healthy record). The
    ServiceDependency docstring even promises 'health_check_log: … I10 requires ≥1
    healthy', so the firewall was advertised but a no-op. A probe counts healthy
    when its `healthy` is truthy OR returncode == 0 — the predicate now lives in
    `core_data.service_healthy_probes`, because the RUN dashboard needs the same answer
    to badge the service and a hand-copy there would have put the strict form in this
    gate and a looser one in front of the reader. The log is runtime-populated
    (start_service / verify_service_dependency), so the agent can't fabricate it. A
    service that was healthy then cleanly stopped (status=stopped, ≥1 healthy probe)
    PASSES — the check is 'did it ever come up', not 'is it up now'."""
    violations: list[dict] = []
    for i, sd in enumerate(spec.get("service_dependencies", []) or []):
        if not isinstance(sd, dict):
            continue
        name = sd.get("name") or sd.get("service_name") or f"service[{i}]"
        log = _core_data.service_probe_log(sd)
        healthy = _core_data.service_healthy_probes(sd)
        if not healthy:
            violations.append({
                "invariant": "I10.service_never_healthy",
                "where": f"service_dependencies[{i}]",
                "message": (f"service '{name}' has no healthy probe in its "
                            f"health_check_log (status={sd.get('status')!r}, "
                            f"{len(log)} probe(s), 0 healthy) — a workflow cannot be "
                            f"sealed depending on a service that never came up. "
                            f"start_service must reach healthy, or drop the dependency."),
            })
    return violations


def _check_reference_database_availability(spec: dict) -> list[dict]:
    """I5 (restored at Layer 2): every declared reference_database with a
    local_path must actually be present — and, when the record carries integrity
    anchors, still match them.

    The retired host-writer I5 checked existence only ('reference DBs without
    their data don't run'). The respine folded I5 into the env-build collapse on
    the theory that VALIDATED_IN_IMAGE subsumes it — but the ReferenceDatabase
    model itself says these are 'mounted at runtime rather than baked into the
    Docker image' (they can be hundreds of GB), so the image validation cannot
    speak to them at all. Same crack as the I9->I8 authored-artifact and I10
    service restorations: a runtime-external dependency a workflow relies on.

    Three tiers of check, each cheap-first so a 200 GB VEP cache never forces a
    pathological seal-time hash:
      - EXISTENCE (always): local_path missing on disk -> I5.reference_database_missing.
        Works for a file OR a directory (VEP cache / exomiser bundle are trees).
      - NON-EMPTY (always, cheap): a zero-byte file or empty directory is a
        failed/partial download masquerading as present -> I5.reference_database_empty.
      - SIZE match (cheap stat, when size_bytes recorded + local_path is a file):
        a truncated or swapped artifact -> I5.reference_database_size_mismatch.
        This is the always-affordable integrity signal for huge DBs we won't hash.
      - SHA256 drift (only when sha256 recorded, local_path is a file, AND it is
        under the hash cap): the model records sha256 explicitly so 'downstream
        re-runs pin the DB by this hash' — enforce it -> I5.reference_database_mutated
        / I5.reference_database_unreadable. Over the cap we rely on size + existence.

    Entries without a local_path are skipped (a declared-but-not-downloaded DB
    that no step consumes; if a step DID consume it, I8.composition_coherence
    already fails because a None local_path is never added to the source universe)."""
    import hashlib
    HASH_CAP_BYTES = 2 * 1024 * 1024 * 1024   # 2 GiB — hash below, trust size+existence above
    violations: list[dict] = []
    for i, rdb in enumerate(spec.get("reference_databases", []) or []):
        if not isinstance(rdb, dict):
            continue
        lp = rdb.get("local_path")
        if not lp or not isinstance(lp, str):
            continue   # not-yet-downloaded declaration; unused ones are harmless
        name = rdb.get("name") or f"reference_databases[{i}]"
        where = f"reference_databases[name={rdb.get('name')}]"

        # Locus-aware: a cluster-resident DB (downloaded via a SLURM job into the
        # env's common_data zone) can't be re-hashed locally — its local_path is a
        # CLUSTER path. Verify it OVER SSH instead (existence + non-empty), the same
        # "observe at the locus" posture as the C2 .sif round-trip. Lazy-import to
        # keep spec_writer's base import cheap + avoid any import-order coupling.
        if rdb.get("locus") == "cluster":
            from agent.skills import acquire_data
            violations.extend(acquire_data.check_cluster_reference_db(rdb))
            continue

        p = Path(lp)
        if not p.exists():
            violations.append({
                "invariant": "I5.reference_database_missing",
                "message":   f"reference_database '{name}' has local_path that does not "
                             f"exist on disk: {lp} — a workflow cannot seal depending on "
                             f"reference data that isn't there (mounted at runtime, so the "
                             f"image validation cannot cover it)",
                "where":     where, "path": lp,
            })
            continue

        # Non-empty: a partial/failed download can leave a 0-byte file or empty dir.
        try:
            if p.is_dir():
                empty = not any(p.iterdir())
            else:
                empty = p.stat().st_size == 0
        except Exception:
            empty = False
        if empty:
            violations.append({
                "invariant": "I5.reference_database_empty",
                "message":   f"reference_database '{name}' local_path exists but is empty "
                             f"({'directory has no entries' if p.is_dir() else 'zero-byte file'}): "
                             f"{lp} — an empty DB is as unusable as a missing one (partial download?)",
                "where":     where, "path": lp,
            })
            continue

        if p.is_dir():
            continue   # size_bytes / sha256 anchor a single artifact, not a tree

        recorded_size = rdb.get("size_bytes")
        actual_size = None
        try:
            actual_size = p.stat().st_size
        except Exception:
            actual_size = None
        if isinstance(recorded_size, int) and recorded_size > 0 and actual_size is not None \
                and actual_size != recorded_size:
            violations.append({
                "invariant": "I5.reference_database_size_mismatch",
                "message":   f"reference_database '{name}' size changed since it was recorded: "
                             f"recorded {recorded_size} bytes, on disk {actual_size} bytes — "
                             f"the artifact was truncated, replaced, or re-downloaded to "
                             f"different content",
                "where":     where, "path": lp,
                "recorded_size_bytes": recorded_size, "disk_size_bytes": actual_size,
            })
            continue

        recorded_sha = rdb.get("sha256")
        if recorded_sha and (actual_size is None or actual_size <= HASH_CAP_BYTES):
            try:
                h = hashlib.sha256()
                with p.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
                disk_sha = h.hexdigest()
            except Exception as e:
                violations.append({
                    "invariant": "I5.reference_database_unreadable",
                    "message":   f"reference_database '{name}' could not be re-hashed at "
                                 f"seal time: {e!r}",
                    "where":     where, "path": lp,
                })
                continue
            if disk_sha != recorded_sha:
                violations.append({
                    "invariant": "I5.reference_database_mutated",
                    "message":   f"reference_database '{name}' bytes changed since download: "
                                 f"recorded sha256={recorded_sha[:12]}..., on disk={disk_sha[:12]}... "
                                 f"— downstream re-runs pin this DB by hash, so the spec's claim "
                                 f"about it no longer matches reality",
                    "where":     where, "path": lp,
                    "recorded_sha256": recorded_sha, "disk_sha256": disk_sha,
                })
    return violations


#: `test_data_integrity` states which of the three it is, so a reader never has to infer
#: "no complaint" from an absent field. Mirrors the three-state usage verdict.
TEST_DATA_VERIFIED = "verified"
TEST_DATA_UNANCHORED = "unanchored"
TEST_DATA_DIVERGED = "diverged"
TEST_DATA_NOT_ATTEMPTED = "not_attempted"


def verify_test_data(spec: dict) -> dict:
    """Re-anchor every `test_data` path against disk. THE reader for "is this run's
    test data the test data it was selected against?" (Named `verify_…` rather than
    `test_data_…`: pytest COLLECTS any imported callable whose name starts with `test_`,
    so the obvious name turns every test module that imports it into a broken fixture
    request. The field it produces on the spec is `test_data_integrity`.)

    The last external input source with no content check. Every other one is pinned —
    reference_databases by I5, authored_artifacts by the re-hash above, runtime_configs
    by their recorded sha256 — while `test_data`, which step 3 of the documented protocol
    tells the agent to produce, was accepted purely as a set of strings that made I8's
    universe bigger. Verified by reproduction before this was written: a spec whose
    `test_data.r1` pointed at `/nope/ghost.fastq.gz` sealed green, and so did one whose
    input file was rewritten with entirely different reads between two seal calls.

    Returns {status, checked, anchored, violations[]}:
      - EXISTENCE + NON-EMPTY are unconditional. They need no recorded anchor — the
        artifact is either there or it is not — so they close the hole for legacy blocks
        too, and they are the half that catches the common real failure (a scratch dir
        cleaned up between the run and the seal).
      - SIZE / SHA256 drift are checked only against an anchor `select_test_data`
        recorded. Seal NEVER writes one: an anchor first observed here would be compared
        against itself moments later and manufacture its own agreement, which is exactly
        how I5's cluster branch spent months proving nothing.
      - A path with no anchor is `unanchored` — reported as a third state, never rounded
        up to a pass. That is the honest landing for the specs sealed before anchors
        existed and for any block that reached the draft outside the primitive.

    TWO SCOPE LIMITS, both stated rather than implied, because a checker silent about
    what it does not look at reads as one that looked at everything:
      - Every path resolves against the LOCAL filesystem (`core_data.resolve_data_path`).
        A path in another namespace — a cluster path — is not found here and lands as
        `I8.test_data_missing`, i.e. a FALSE REFUSAL, not a false pass. That is the safe
        direction and it is deliberate; no MCP primitive writes a non-local path into
        `test_data` today (`select_test_data` reads local disk and is the sole producer).
      - Only values `core_data.test_data_paths` recognizes as paths are checked at all.
        A value under an unrecognized key, or one starting with a scheme/variable
        (`s3://…`, `$SCRATCH/…`), is not a violation — it is simply not a path we claim
        to have checked, and a block of only such values is `not_attempted`. The backstop
        is that the same reader seeds I8's traceable universe, so a path this filter drops
        cannot be silently CONSUMED by a step either: it is refused as an orphan input."""
    td = spec.get("test_data")
    paths = _core_data.test_data_paths(td)
    if not paths:
        return {"status": TEST_DATA_NOT_ATTEMPTED, "checked": 0, "anchored": 0,
                "violations": []}

    recorded_anchors = _core_data.test_data_anchors(td)

    violations: list[dict] = []
    anchored = 0
    for key, raw in sorted(paths.items()):
        where = f"test_data.{key}"
        p = _core_data.resolve_data_path(raw)
        if not p.exists():
            violations.append({
                "invariant": "I8.test_data_missing",
                "message":   f"test_data.{key} points at {raw}, which is not on the LOCAL "
                             f"filesystem (resolved to {p}) — so nothing about this workflow's "
                             f"inputs can be reproduced or re-checked. Either the input is gone, "
                             f"or it is a path in another namespace (a cluster path is not "
                             f"resolvable here; see the I8 note in agent/skills/invariants.py)",
                "where": where, "path": str(p),
            })
            continue
        try:
            empty = (not any(p.iterdir())) if p.is_dir() else p.stat().st_size == 0
        except OSError:
            empty = False
        if empty:
            violations.append({
                "invariant": "I8.test_data_empty",
                "message":   f"test_data.{key} exists but is empty "
                             f"({'no entries' if p.is_dir() else 'zero bytes'}): {raw} — an "
                             f"empty input is as unusable as a missing one",
                "where": where, "path": str(p),
            })
            continue

        # ANCHORED means "the producer recorded what this artifact WAS", which for a
        # directory is its kind and nothing else — a tree has no single hash. Gating on
        # "has a digest" instead would make a block containing `core_data_dir` (which
        # `select_test_data` emits on every match) permanently un-verifiable, so the
        # happy path could never reach `verified` and the third state would mean nothing.
        rec = recorded_anchors.get(key) or {}
        kind = rec.get("kind")
        if kind == "directory":
            anchored += 1
            continue
        if kind != "file" or not (rec.get("sha256") or rec.get("size_bytes")):
            continue                       # unanchored — counted below, never a violation
        anchored += 1
        if p.is_dir():
            # Recorded as a file, now a directory: not a hash mismatch, a swap.
            violations.append({
                "invariant": "I8.test_data_kind_changed",
                "message":   f"test_data.{key} was anchored as a file and is now a "
                             f"directory: {raw}",
                "where": where, "path": str(p),
            })
            continue

        now = _core_data.anchor_for_path(p)
        if now is None:
            violations.append({
                "invariant": "I8.test_data_unreadable",
                "message":   f"test_data.{key} could not be re-read at seal time: {raw}",
                "where": where, "path": str(p),
            })
            continue
        rec_size, rec_sha = rec.get("size_bytes"), rec.get("sha256")
        if isinstance(rec_size, int) and now.size_bytes is not None and now.size_bytes != rec_size:
            violations.append({
                "invariant": "I8.test_data_size_mismatch",
                "message":   f"test_data.{key} size changed since it was selected: recorded "
                             f"{rec_size} bytes, on disk {now.size_bytes} — the input was "
                             f"truncated, replaced, or re-downloaded to different content",
                "where": where, "path": str(p),
                "recorded_size_bytes": rec_size, "disk_size_bytes": now.size_bytes,
            })
            continue
        # `now.sha256` is None only above the hash cap, where the size check above is the
        # anchor. Comparing None to a recorded digest would read as a mutation.
        if rec_sha and now.sha256 and now.sha256 != rec_sha:
            violations.append({
                "invariant": "I8.test_data_mutated",
                "message":   f"test_data.{key} bytes changed since it was selected: recorded "
                             f"sha256={rec_sha[:12]}..., on disk={now.sha256[:12]}... — the run "
                             f"was validated against different data than this path now holds",
                "where": where, "path": str(p),
                "recorded_sha256": rec_sha, "disk_sha256": now.sha256,
            })

    # `diverged` never reaches disk — seal refuses on the violations — but this function
    # is a public reader, and returning "unanchored" for an input that WAS anchored and
    # then changed underneath us would be the wrong word for the worst case.
    if violations:
        status = TEST_DATA_DIVERGED
    elif anchored == len(paths):
        status = TEST_DATA_VERIFIED
    else:
        status = TEST_DATA_UNANCHORED
    return {"status": status, "checked": len(paths), "anchored": anchored,
            "violations": violations}


def _check_test_data_integrity(spec: dict) -> list[dict]:
    return verify_test_data(spec)["violations"]


def _check_authored_artifact_integrity(spec: dict) -> list[dict]:
    """Re-anchor every authored_artifact against its on-disk bytes at seal
    time. Each entry was sha256-anchored by stage_authored_artifact when it
    landed (the runtime hashed the bytes; the agent never supplied the value).
    A mutation between stage and seal — file overwritten, replaced with a
    fake, or simply deleted — would otherwise let an agent claim an artifact
    has identity X while disk holds Y. Downstream consumers read the FILE,
    not the spec, so the spec's claim about it has to keep matching reality
    up to the moment we freeze the record.

    Pre-respine I9 enforced this. The respine collapsed I9 because in Layer
    1 the artifact is BAKED INTO the image and the image bytes are the truth.
    Layer-2 authored artifacts live OUTSIDE the image on disk, so the
    'install==ship' collapse does NOT cover them — this check is the Layer-2
    restoration. Invariant ID is I8.* because the concern is external-source
    integrity (same family as I8.composition_coherence).

    Returns violations for: (a) on-disk path missing, (b) sha256 drift. Skips
    entries without a recorded sha256 (legacy / not produced by
    stage_authored_artifact)."""
    import hashlib
    violations: list[dict] = []
    for i, a in enumerate(spec.get("authored_artifacts", []) or []):
        if not isinstance(a, dict):
            continue
        path = a.get("path")
        recorded = a.get("sha256")
        if not path or not isinstance(path, str):
            continue
        if not recorded:
            continue   # legacy entries — nothing to re-anchor against
        p = Path(path)
        if not p.is_file():
            violations.append({
                "invariant": "I8.authored_artifact_missing",
                "message":   f"authored_artifact '{path}' was recorded but is no "
                             f"longer on disk — staged content is gone, downstream "
                             f"steps that depend on it have no real source",
                "where":     f"authored_artifacts[{i}]",
                "path":      path,
            })
            continue
        try:
            disk_sha = hashlib.sha256(p.read_bytes()).hexdigest()
        except Exception as e:
            violations.append({
                "invariant": "I8.authored_artifact_unreadable",
                "message":   f"authored_artifact '{path}' could not be re-hashed "
                             f"at seal time: {e!r}",
                "where":     f"authored_artifacts[{i}]",
                "path":      path,
            })
            continue
        if disk_sha != recorded:
            violations.append({
                "invariant": "I8.authored_artifact_mutated",
                "message":   f"authored_artifact '{path}' bytes changed since stage: "
                             f"recorded sha256={recorded[:12]}..., on disk={disk_sha[:12]}... "
                             f"— the spec's claim about this artifact no longer matches reality",
                "where":     f"authored_artifacts[{i}]",
                "path":      path,
                "recorded_sha256": recorded,
                "disk_sha256":     disk_sha,
            })
    return violations


def _check_lineage_integrity(spec: dict) -> list[dict]:
    """Universal file-type-agnostic lineage (L11 cheat-guard level).

    The workflow graph already declares each pipeline_step's inputs and
    outputs as paths. A path that appears as both a producer step's output
    AND a downstream step's input must hold the SAME bytes throughout the
    workflow's lifetime — otherwise an agent could silently mutate the file
    between steps and the I8 PATH-lineage check (composition_coherence)
    would not catch it.

    This is file-type-agnostic: it works for BAM, VCF, parquet, .weird, or
    any extension the agent hasn't met before. The contract is just 'same
    path → same bytes' at the workflow level. Per-file-type lineage (BAM
    @PG chain, VCF sample IDs) stays optional, opt-in per validator.

    The producer step must have `output_sha256: {path: hex}` recorded at run
    time (env_manager.hash_outputs does this for run_pipeline_step /
    run_step_in_container). If the producer has no output_sha256, the
    lineage clause for paths it owns is SKIPPED — back-compat with older
    recorded runs; the check is an honesty guard, not a coverage gate.

    Returns I8.lineage_mutated / I8.lineage_missing / I8.lineage_unreadable
    violations. Lives in the I8 tier (composition-coherence sibling) so
    check_workflow_invariants picks it up without _WORKFLOW_INVARIANT_TIERS
    edits."""
    import hashlib as _h
    violations: list[dict] = []
    steps = sorted(
        [s for s in spec.get("pipeline_steps", []) or [] if isinstance(s, dict)],
        key=lambda s: s.get("step") or 0,
    )

    # Build {output_path → (producer_step_n, recorded_sha)} for paths whose
    # producer recorded a hash. When a path appears in multiple producers
    # (rare; e.g. step 3 overwrites step 1's reads.bam), the LAST in step
    # order wins, matching the natural-overwrite semantics — step 4's
    # input reads.bam IS what step 3 wrote, not what step 1 wrote.
    producer_index: dict[str, tuple[int, str]] = {}
    for s in steps:
        sn = s.get("step") or 0
        recorded = s.get("output_sha256") or {}
        if not isinstance(recorded, dict):
            continue
        for path, sha in recorded.items():
            if isinstance(path, str) and isinstance(sha, str) and sha:
                producer_index[path] = (sn, sha)

    # For each consumer step, check inputs against the producer index.
    for s in steps:
        consumer_n = s.get("step") or 0
        for inp in s.get("inputs", []) or []:
            ipath = inp.get("path") if isinstance(inp, dict) else inp
            if not isinstance(ipath, str) or ipath not in producer_index:
                continue
            producer_n, recorded_sha = producer_index[ipath]
            if producer_n >= consumer_n:
                # Self-reference or backward edge — not a forward lineage clause.
                continue
            p = Path(ipath)
            if not p.is_file():
                violations.append({
                    "invariant": "I8.lineage_missing",
                    "message":   f"pipeline_step {consumer_n} consumes '{ipath}' as input "
                                 f"(produced by step {producer_n}) but the file is no "
                                 f"longer on disk at seal time — the consumer's input is "
                                 f"a dangling reference",
                    "where":     f"pipeline_steps[step={consumer_n}].inputs",
                    "path":      ipath,
                    "producer_step": producer_n,
                })
                continue
            try:
                h = _h.sha256()
                with p.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
                disk_sha = h.hexdigest()
            except Exception as e:
                violations.append({
                    "invariant": "I8.lineage_unreadable",
                    "message":   f"could not re-hash '{ipath}' for lineage check "
                                 f"(producer step {producer_n}): {e!r}",
                    "where":     f"pipeline_steps[step={consumer_n}].inputs",
                    "path":      ipath,
                })
                continue
            if disk_sha != recorded_sha:
                violations.append({
                    "invariant": "I8.lineage_mutated",
                    "message":   f"pipeline_step {consumer_n} consumes '{ipath}' but its "
                                 f"bytes changed since step {producer_n} produced it: "
                                 f"producer recorded sha256={recorded_sha[:12]}..., "
                                 f"on disk={disk_sha[:12]}... — the workflow's "
                                 f"same-path-same-bytes contract is broken",
                    "where":     f"pipeline_steps[step={consumer_n}].inputs",
                    "path":      ipath,
                    "producer_step":  producer_n,
                    "producer_sha256": recorded_sha,
                    "disk_sha256":     disk_sha,
                })
    return violations


def _check_composition_coherence(spec: dict) -> list[dict]:
    """Walk pipeline_steps in step-order; verify each input is producible by
    something upstream. See I8 description.

    Skips these (legitimate orphans):
      - placeholders ({X}, $X, <X>)
      - bare tokens with no '/' (not paths)
      - script files (.sh/.py/.R/.pl/.nf/.smk/.bash) — agent-managed, not data
      - paths under any conda env directory (envs/{...}/) — bundled tool data
      - URLs (http:// https:// ftp://)
    """
    # Project root for resolving relative test_data paths into absolute
    # form — select_test_data records paths relative to project root, but
    # pipeline_steps store absolute paths. Both shapes must compare equal.
    project_root = Path(__file__).parent.parent.parent.resolve()

    def _add_external(s: str) -> None:
        if not isinstance(s, str) or not s:
            return
        external_paths.add(s)
        p = Path(s)
        if not p.is_absolute():
            joined = project_root / p
            external_paths.add(str(joined))            # absolute, unresolved
            try:
                external_paths.add(str(joined.resolve()))   # symlink-resolved
            except Exception:
                pass
        else:
            try:
                external_paths.add(str(p.resolve()))
            except Exception:
                pass

    external_paths: set[str] = set()

    # THE one reading of "which test_data values are paths" — this used to be a fixed
    # key tuple here and a "starts with /" scan in data_pins, and they disagreed on both
    # axes. See core_data.test_data_paths.
    for _path in _core_data.test_data_paths(spec.get("test_data")).values():
        _add_external(_path)

    for rdb in spec.get("reference_databases", []) or []:
        if isinstance(rdb, dict):
            _add_external(rdb.get("local_path"))

    for rc in spec.get("runtime_configs", []) or []:
        if isinstance(rc, dict):
            _add_external(rc.get("path"))

    # Authored artifacts — agent-written scripts, synthetic test inputs,
    # staged transformations. Their provenance (content or genesis command +
    # sha256) is captured by stage_authored_artifact; downstream pipeline
    # steps may legitimately reference them as inputs.
    for a in spec.get("authored_artifacts", []) or []:
        if isinstance(a, dict):
            _add_external(a.get("path"))

    def _is_orphan_exempt(p: str) -> bool:
        if not p or not isinstance(p, str):
            return True
        if p.startswith(("{", "$", "<")):
            return True
        if p.startswith(("http://", "https://", "ftp://")):
            return True
        if "/" not in p:
            return True
        low = p.lower()
        for ext in (".sh", ".py", ".r", ".pl", ".nf", ".smk", ".bash", ".rscript"):
            if low.endswith(ext):
                return True
        if "/envs/" in p:
            return True
        return False

    violations: list[dict] = []
    universe: set[str] = set(external_paths)
    # Basenames of DECLARED EXTERNAL SOURCES only — the cross-locus staging handle (see
    # the loop). Deliberately not seeded from prior outputs: steps sharing a filesystem
    # compare by path, so widening this to outputs would reopen the hole it replaced.
    external_basenames: set[str] = {Path(x).name for x in external_paths if x}

    steps = sorted(
        [s for s in spec.get("pipeline_steps", []) or [] if isinstance(s, dict)],
        key=lambda s: s.get("step") or 0,
    )

    for s in steps:
        step_n = s.get("step")
        for inp in s.get("inputs", []) or []:
            p = inp.get("path") if isinstance(inp, dict) else inp
            if _is_orphan_exempt(p):
                continue
            if p in universe:
                continue
            # SIDECAR TOLERANCE — a step may consume the companion of a file a prior step
            # produced (`{p}.bai` beside `{p}`, `{p}.gz` after an in-step bgzip). The
            # PARENT must be in the universe, matched on the FULL path.
            parent = _sidecar_parent(p)
            if parent and parent in universe:
                continue
            # CROSS-LOCUS STAGING — a step that ran somewhere else consumes the UPLOADED
            # COPY of a declared external source. Its path is on the cluster filesystem;
            # every external source we know is a local path. Those two absolute paths
            # live in different namespaces and comparing them is a category error, so
            # here — and only here — the basename is the strongest handle the record
            # carries, and it is matched against DECLARED EXTERNAL SOURCES only, never
            # against prior outputs (two steps on the same cluster share a filesystem, so
            # their paths compare directly and need no fallback).
            #
            # This is what the old `any(Path(u).name == base for u in universe)` was
            # really load-bearing for — samtools_cluster_rung3 seals on exactly this
            # shape. But it was written as an unscoped rule and did two wrong things:
            # it REFUSED the sidecar case its own comment claimed it existed for
            # (`sample.bam.bai` != `sample.bam` as basenames), and it ACCEPTED any input
            # anywhere on the local filesystem that merely shared a name with some prior
            # output — `/somewhere/else/entirely/sample.bam` traced cleanly to
            # `/run1/sample.bam`, which is the entire class of orphan I8 exists to catch.
            # Both verified against the live checker; scoping it to the cross-locus case
            # keeps the real capability and drops the hole.
            if _ran_off_host(s) and Path(p).name in external_basenames:
                continue
            # DIRECTORY CONSUMPTION — a step consumes the DIRECTORY a prior step
            # wrote into, not the individual files. This is the normal shape for a
            # cluster chain: htseq-count writes six `.counts.tsv` into
            # <workflow_dir>, and DESeq2's input is <workflow_dir> itself.
            #
            # Matched by exact PARENT, never by prefix. `/work/run1` must not
            # capture `/work/run10`, and a grandparent must not capture a whole
            # subtree — an input of `/work` would otherwise trace to every output
            # anything ever produced. `_dir_contains_a_prior_output` is that rule,
            # and it is a real provenance relationship rather than a loosening:
            # if a prior step's recorded output lives directly in this directory,
            # then consuming the directory IS consuming that output.
            if _dir_contains_a_prior_output(p, universe):
                continue
            violations.append({
                "invariant": "I8.composition_coherence",
                "message":   f"pipeline_step {step_n} input '{p}' has no producing source — "
                             f"{I8_STATEMENT}; recognized external kinds are "
                             f"{', '.join(sorted(EXTERNAL_SOURCE_KINDS))}",
                "where":     f"pipeline_steps[step={step_n}].inputs",
                "orphan_path": p,
            })

        for o in s.get("outputs", []) or []:
            if isinstance(o, str) and o:
                universe.add(o)
        for o in s.get("detected_outputs", []) or []:
            if isinstance(o, str) and o:
                universe.add(o)
        # REMOTE OUTPUTS — where an off-host step's outputs live AT THE LOCUS
        # THAT PRODUCED THEM. `detected_outputs` holds the downloaded LOCAL
        # copies, which is right for validation (we hashed and typed those very
        # bytes) but leaves a cluster chain unprovable: step 2 consumes
        # `<remote workflow_dir>/counts.tsv`, and no local path can ever match it.
        #
        # A DEDICATED field rather than reusing `outputs`, deliberately. `outputs`
        # is read first-wins as `detected_outputs or outputs`, so writing remote
        # paths there would make a step whose downloads ALL failed — the
        # silent-empty-success trap I3 exists to catch — look like it produced
        # something. It also already has a second producer (`run_in_env`) writing
        # a different dialect into it. One producer, one consumer, no collision.
        for o in s.get("remote_outputs", []) or []:
            if isinstance(o, str) and o:
                universe.add(o)

    return violations
