"""
spec_writer — Layer-2 (workflow) spec + provenance persistence.

An env is solved once by freeze() and verified IN the shipped image by
env_honesty.check_build (install==ship); which invariants are env-build ones is
data, in agent/skills/invariants.py. This module is the Layer-2 surface that
consumes a frozen env:

    write_workflow_spec(workflow, config, env_name=None) -> {workflow_spec_path}
    check_workflow_invariants(spec)         -> run-side violations (roster:
                                               agent/skills/invariants.py)
    self_test_usage(spec, env_manager, ...) -> executes usage.command_template (I4)

check_invariants is now the run-side checker (roster in agent/skills/invariants.py); the
env-build tiers moved to agent/skills/env_honesty.py.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional

import yaml

from agent.models import core_data as _core_data
from agent.models.core_data import (OUTPUT_SLOT_RULE, is_output_slot, step_is_validated, usage_commands,
                                    usage_slots)
from agent.skills import invariants as _invariants
from agent.skills.outcomes import refused
# The READER for a step's validation dict. Imported (not re-implemented) so the invariant
# and the store agree on how a record is keyed — pipeline_state imports only stdlib + yaml
# + outcomes, so this stays cycle-free.
from agent.skills.pipeline_state import validation_covers as _validation_covers
from agent.skills import workspace

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


def _under_declared_dir(path: str, dirs: list[str]) -> bool:
    """Is `path` strictly beneath one of the declared external DIRECTORIES — component-wise,
    so `/ref/db10` is not under `/ref/db1` and a directory is not under itself? The
    resolved form of the path is tried too, as the directories were added in both forms."""
    if not dirs:
        return False
    forms = [path]
    try:
        forms.append(str(Path(path).resolve()))
    except Exception:
        pass
    for d in dirs:
        dp = PurePosixPath(d)
        for f in forms:
            fp = PurePosixPath(f)
            if fp != dp and fp.is_relative_to(dp):
                return True
    return False


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
    could otherwise claim cross-locus staging to excuse a genuine orphan.

    The cluster is the one locus whose paths live on another filesystem. `native`,
    `emulated` and `adopted` name where a container step validated on THIS machine,
    so they are on-host like `host` and `container`."""
    if not isinstance(step, dict):
        return False
    locus = (step.get("validation_locus")
             or (step.get("resource_usage") or {}).get("locus") or "")
    return str(locus).lower() == "cluster"

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

    The producer STATES the truth — never a nominal default stamped regardless
    of the run:
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


def write_workflow_spec(workflow: dict, config: dict, env_name: str | None = None) -> dict:
    """Validate + write a Layer-2 WorkflowSpec as YAML.

    The human-facing how-to is rendered as HTML into {name}.RUN.html by the seal
    (run_dashboard_html)."""
    # (helper below is module-level; see _shape_fix_hint)
    from agent.models.core_data import WorkflowSpec

    try:
        wf = WorkflowSpec.model_validate(workflow)
    except Exception as e:
        return refused("spec_writer.validation_failed",
                       error=f"WorkflowSpec validation failed: {e}",
                       fix=_shape_fix_hint(e))

    # The spec is filed under the env it runs on (environments/<env>/), beside the
    # ENV report and the image. The seal passes the env's name; any other caller
    # has the registry answer it from the request key the spec pins.
    if not env_name:
        env_name = _registered_env_name(wf.env_request_key)
        if not env_name:
            return refused("spec_writer.env_unknown",
                           error=(f"no frozen env is registered for env_request_key "
                                  f"{wf.env_request_key!r}, so there is no environment "
                                  f"directory to file this workflow under"),
                           fix="freeze the env first, or pass env_name=")
    name = wf.workflow_name
    yaml_path = workspace.env_dir(env_name) / f"{name}{workspace.WORKFLOW_SUFFIX}"
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
    yaml_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return {"workflow_spec_path": str(yaml_path)}


def _registered_env_name(request_key: str) -> str | None:
    from agent.skills.freeze import EnvCache
    try:
        rec = EnvCache(workspace.env_cache_path()).lookup(request_key) or {}
    except Exception:
        return None
    return rec.get("name") or None


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
    that dict, which is exactly the read where a scrape is dangerous: a
    ``.get``/regex over a record returns the wrong field under a confident name, and
    "the wrong command in the right container" is a
    silent, ground-truthed lie. So any consumer that ACTS on a sealed spec reads it
    back through THIS typed seam — a malformed artifact fails HERE, loudly
    (``pydantic.ValidationError``), rather than surfacing as a bad command deep in a
    container run.

    Returns a validated ``WorkflowSpec``. Raises: ``FileNotFoundError`` if ``path``
    is absent, ``yaml.YAMLError`` on unparseable YAML, ``ValueError`` if the document
    is not a mapping, ``pydantic.ValidationError`` if the record does not satisfy
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


# ---------------------------------------------------------------------------
# The how-to, DERIVED from the recorded steps (I4).
#
# The command the agent ran IS the how-to. Every run tool records, beside the literal
# command, the `template` it was given (the command with `{UPPER_SNAKE}` slots), the
# `bindings` each input slot took, the `formats` of the slots that are not paths, and the
# `work_dir` the step ran in and wrote into. The seal reads those and writes the same
# `usage` + `usage_verification` record an author used to type by hand — so every reader
# (the render, the RUN dashboard, the inventory) is unchanged, and a how-to that differs
# from what ran cannot be written at all.
# ---------------------------------------------------------------------------

def _step_bindings(step: Mapping) -> dict[str, str]:
    """A step's INPUT bindings: what it bound, minus the output slots (those are the
    step's own work_dir, not a value a sample supplies)."""
    return {str(k): str(v) for k, v in (step.get("bindings") or {}).items()
            if not is_output_slot(str(k))}


def _per_sample_data_paths(spec: Mapping) -> set[str]:
    """The paths in `test_data` that belong to ONE sample (reads, a phenopacket): a slot
    bound to one of them is per-sample whatever else the record says."""
    out: set[str] = set()
    for key, path in _core_data.test_data_paths(spec.get("test_data")).items():
        leaf = key.rsplit(".", 1)[-1]
        if leaf in ("r1", "r2", "phenopacket") or leaf.startswith(("r1_", "r2_")):
            out.add(path)
            try:
                out.add(str(_core_data.resolve_data_path(path)))
            except Exception:
                pass
    return out


def _per_sample_slots(steps: list[dict], spec: Mapping) -> set[str]:
    """The slots whose value names the sample: a sample-name slot, a slot bound to a
    sample's own data (reads, a phenopacket), or a slot bound to more than one value across
    the steps that share its template."""
    per_sample: set[str] = set()
    sample_paths = _per_sample_data_paths(spec)
    values: dict[tuple[str, str], set[str]] = {}
    for s in steps:
        for k, v in _step_bindings(s).items():
            if k in _core_data.SAMPLE_SLOT_NAMES or v in sample_paths:
                per_sample.add(k)
            values.setdefault((str(s.get("template") or ""), k), set()).add(v)
    for (_t, k), vs in values.items():
        if len(vs) > 1:
            per_sample.add(k)
    return per_sample


def _step_is_per_sample(step: Mapping, per_sample_slots: set[str]) -> bool:
    return any(k in per_sample_slots for k in _step_bindings(step))


def _under(path: str, root: str) -> bool:
    r = root.rstrip("/")
    return bool(r) and (path == r or path.startswith(r + "/"))


def _bound_from_step(value: str, step: Mapping) -> bool:
    """Is `value` something `step` produced: a file it detected, a path under its work_dir
    (a prefix naming an index family), or one of its remote outputs?"""
    outs = [str(o) for o in (step.get("detected_outputs") or []) + (step.get("remote_outputs") or [])]
    if value in outs or any(o.startswith(value) for o in outs):      # the exact file, or a prefix of a family
        return True
    wd = step.get("work_dir")
    return bool(wd) and _under(value, str(wd)) and value != str(wd).rstrip("/")


def binds_prior_sample_output(spec: Mapping, bindings: Mapping[str, Any]) -> list[str]:
    """The run-call half of the chain rule, said BEFORE the step runs. A per-sample chain
    reads an earlier step's file as `{OUTPUT_DIR}/<name>` in the same working directory;
    a slot bound to a file a PER-SAMPLE step wrote would make that file a samplesheet
    column the user has to supply, and the earlier stage's output an orphan. A file a
    fixed step wrote (an index build, an annotation slice) is the other case and is fine:
    that step is provenance, not a how-to command. One line per offending slot."""
    steps = [s for s in (spec.get("pipeline_steps") or [])
             if isinstance(s, dict) and s.get("returncode") == 0]
    if not steps:
        return []
    per_sample = _per_sample_slots(steps, spec)
    problems: list[str] = []
    for slot, value in (bindings or {}).items():
        v = str(value)
        if not v.startswith("/"):
            continue
        for s in steps:
            if _bound_from_step(v, s) and _step_is_per_sample(s, per_sample):
                wd = str(s.get("work_dir") or "")
                name = v[len(wd.rstrip("/")) + 1:] if wd and _under(v, wd) else Path(v).name
                problems.append(
                    f"{{{slot}}} is bound to {v}, which step {s.get('step')} wrote for this sample: a "
                    f"per-sample chain runs its steps in ONE working directory and reads an earlier "
                    f"step's file as `{{OUTPUT_DIR}}/{name}` (pass watch_dir={wd!r}) — a file bound "
                    f"here would become a samplesheet column the user has to supply")
                break
    return problems


def _relative_outputs(step: Mapping) -> list[str]:
    """A step's outputs by their path under its work_dir — the local files it detected,
    or, for an off-host step, where its outputs live at the locus that made them."""
    wd = str(step.get("work_dir") or "").rstrip("/")
    outs = [str(o) for o in (step.get("remote_outputs") or step.get("detected_outputs") or [])]
    rels: list[str] = []
    for o in outs:
        rel = o[len(wd) + 1:] if wd and o.startswith(wd + "/") else Path(o).name
        if rel and rel not in rels:
            rels.append(rel)
    return rels


def _templatize(name: str, bindings: Mapping[str, str], slots: set[str]) -> str:
    """Put a sample's values back into an observed name as its slots: `SRR1039508.counts.txt`
    with `SAMPLE=SRR1039508` is `{SAMPLE}.counts.txt`. Longest value first; a path-shaped
    value is never substituted into a name."""
    out = name
    for slot in sorted(slots, key=lambda s: -len(bindings.get(s, ""))):
        v = bindings.get(slot)
        if v and "/" not in v and v in out:
            out = out.replace(v, "{" + slot + "}")
    return out


#: A compression extension rides on the format it wraps: `vcf.gz`, never `gz`.
_COMPRESSION = ("gz", "bgz", "bz2", "xz", "zst")


def _input_format(slot: str, values: list[str], formats: Mapping[str, str]) -> Optional[str]:
    """The format a derived input states: the declared one, else what the bound values
    look like — a directory, a prefix naming a family of files (an index), or a file's
    extension with its compression (`gencode.v47.basic.chr22.gtf` is `gtf`, `x.vcf.gz` is
    `vcf.gz`) — else nothing."""
    if slot in formats:
        return formats[slot]
    for v in values:
        if not v.startswith("/"):
            continue
        p = Path(v)
        if p.is_dir():
            return "directory"
        if _core_data.prefix_family(v):
            return "prefix"
        exts = [s.lstrip(".").lower() for s in p.suffixes if s.lstrip(".")]
        if len(exts) > 1 and exts[-1] in _COMPRESSION:
            return ".".join(exts[-2:])
        if exts:
            return exts[-1]
    return None


def _step_locus(step: Mapping) -> str:
    if step.get("validation_locus") == "cluster":
        return "cluster"
    return "image" if step.get("ran_in_container") else "host"


def derive_usage(spec: Mapping) -> dict:
    """THE how-to of a run, read off its recorded steps. Returns `{usage,
    usage_verification}` in exactly the record shape an authored block had, or
    `{problems: [...]}` naming what stops the derivation.

    - `command_template`: the distinct templates of the rc=0 steps, in step order. A
      template repeated with other bindings is ONE how-to command with several trials.
      A step whose outputs a later step binds as a slot value, and which binds no
      per-sample slot, is a PREPARATION step (an index build, an annotation slice): it is
      provenance of that value and not a how-to command.
    - `inputs`: the input slots, with their declared or inferred format.
    - `outputs[*].files`: every step's outputs by their path under its work_dir, with the
      sample's values put back as slots; a tree is named by its top directory.
    - `trials`: one per sample — the steps grouped by agreeing bindings, every fixed value
      (bound the same way by every step that binds it) filled in. A sample is COMPLETE
      when every how-to command has a step whose bindings agree with it; the record carries
      the complete samples as trials and names the incomplete ones under `partial`.
    - `usage_verification`: status `verified`, source `steps`, and per trial the literal
      commands the steps ran, the image each ran in, the files they produced and what each
      was validated as — the transcript, carried through rather than re-derived."""
    steps = sorted([s for s in (spec.get("pipeline_steps") or [])
                    if isinstance(s, dict) and s.get("returncode") == 0],
                   key=lambda s: int(s.get("step") or 0))
    if not steps:
        return {"problems": ["no recorded step exited 0; the how-to is derived from the steps that ran"]}
    untemplated = [s for s in steps if s.get("template") is None]
    if untemplated:
        names = ", ".join(f"step {s.get('step')} ({s.get('tool')})" for s in untemplated)
        return {"problems": [
            f"{names}: recorded without its template (run_in_env, or a record that predates "
            f"bindings) — re-run it with run_pipeline_step / run_step_in_container / "
            f"run_step_on_cluster (bindings={{SLOT: value}} for its variable parts) so the "
            f"how-to can be derived from it"]}

    per_sample = _per_sample_slots(steps, spec)
    # preparation steps: a later step binds what they produced, and they bind no sample
    prep: set[int] = set()
    for i, s in enumerate(steps):
        if _step_is_per_sample(s, per_sample):
            continue
        later_values = [v for t in steps[i + 1:] for v in _step_bindings(t).values()]
        if any(_bound_from_step(v, s) for v in later_values):
            prep.add(int(s.get("step") or 0))
    howto = [s for s in steps if int(s.get("step") or 0) not in prep]
    if not howto:
        return {"problems": ["every recorded step is a preparation step (a later step binds its "
                             "output); nothing is left to be the how-to"]}

    templates: list[str] = []
    for s in howto:
        if s["template"] not in templates:
            templates.append(str(s["template"]))
    by_template: dict[int, list[dict]] = {i: [] for i in range(len(templates))}
    seen: set[tuple] = set()
    for s in howto:
        key = (s["template"], tuple(sorted(_step_bindings(s).items())))
        if key in seen:
            continue                            # the same command run twice is one proof
        seen.add(key)
        by_template[templates.index(s["template"])].append(s)
    in_slots = [p for t in templates for p in _core_data.slots_in(t) if not is_output_slot(p)]
    in_slots = list(dict.fromkeys(in_slots))
    out_slots = list(dict.fromkeys(p for t in templates for p in _core_data.output_slots_of(t)))

    # fixed values: one value across every step that binds the slot
    values: dict[str, list[str]] = {}
    formats: dict[str, str] = {}
    for s in howto:
        for k, v in _step_bindings(s).items():
            if v not in values.setdefault(k, []):
                values[k].append(v)
        for k, v in (s.get("formats") or {}).items():
            formats[str(k)] = str(v)
    fixed = {k: vs[0] for k, vs in values.items() if len(vs) == 1}

    # trials: steps grouped by agreeing bindings, in step order
    trials: list[dict] = []
    for ti, group in by_template.items():
        for s in group:
            b = _step_bindings(s)
            home = next((t for t in trials if ti not in t["steps"]
                         and all(t["bindings"].get(k, v) == v for k, v in b.items())), None)
            if home is None:
                home = {"bindings": {}, "steps": {}}
                trials.append(home)
            home["bindings"].update(b)
            home["steps"][ti] = s
    trials.sort(key=lambda t: min(int(s.get("step") or 0) for s in t["steps"].values()))
    for t in trials:
        t["bindings"] = {**fixed, **t["bindings"]}
        for ti in range(len(templates)):
            if ti in t["steps"]:
                continue
            proof = next((s for s in by_template[ti]
                          if all(t["bindings"].get(k) == v for k, v in _step_bindings(s).items())), None)
            if proof is not None:
                t["steps"][ti] = proof          # a fixed command proves every sample

    def _name(t: dict, i: int) -> str:
        for slot in _core_data.SAMPLE_SLOT_NAMES:
            if t["bindings"].get(slot):
                return str(t["bindings"][slot])
        for k in in_slots:
            if k in per_sample and t["bindings"].get(k, "").startswith("/"):
                return Path(t["bindings"][k]).name.split(".")[0]
        return f"trial_{i + 1}"

    complete, partial = [], []
    for i, t in enumerate(trials):
        t["name"] = _name(t, i)
        missing = [ti for ti in range(len(templates)) if ti not in t["steps"]]
        (partial if missing else complete).append((t, missing))
    if not complete:
        shapes = "; ".join(f"{t['name']}: no step ran how-to command(s) "
                           f"{', '.join(str(ti + 1) for ti in m)}" for t, m in partial)
        return {"problems": [f"no sample ran every how-to command ({len(templates)} distinct commands "
                             f"over {len(trials)} sample(s)): {shapes} — run the missing command(s) for "
                             f"one sample with the same bindings, in its working directory"]}

    # outputs: every observed file, templatized; a tree by its top directory
    patterns: list[str] = []
    for t, _m in complete:
        for ti in range(len(templates)):
            s = t["steps"][ti]
            for rel in _relative_outputs(s):
                head, sep, _tail = rel.partition("/")
                pat = _templatize(head, t["bindings"], per_sample) + ("/*" if sep else "")
                if pat not in patterns:
                    patterns.append(pat)
    out_name = out_slots[0] if out_slots else "OUTPUT_DIR"
    usage = {
        "description": str(spec.get("description") or "the recorded run, as its steps ran it"),
        "command_template": templates,
        "inputs": [{"name": k, "format": _input_format(k, values.get(k, []), formats)}
                   for k in in_slots],
        "outputs": [{"name": out_name, "files": patterns}],
        "trials": [{"name": t["name"],
                    "substitutions": {k: t["bindings"][k] for k in in_slots if k in t["bindings"]},
                    "description": f"steps {', '.join(str(s.get('step')) for s in t['steps'].values())}"}
                   for t, _m in complete],
    }
    loci = {_step_locus(s) for s in howto}
    proven_trials = []
    for t, _m in complete:
        ordered = [t["steps"][ti] for ti in range(len(templates))]
        work_dirs = list(dict.fromkeys(str(s.get("work_dir") or "") for s in ordered))
        subs = {k: t["bindings"][k] for k in in_slots if k in t["bindings"]}
        subs.update({o: work_dirs[0] for o in out_slots})
        produced, validations = [], []
        for s in ordered:
            wd = str(s.get("work_dir") or "").rstrip("/")
            for rel in _relative_outputs(s):
                if rel not in produced:
                    produced.append(rel)
            for path, v in (s.get("validation") or {}).items():
                if not isinstance(v, dict):
                    continue
                rel = path[len(wd) + 1:] if wd and path.startswith(wd + "/") else Path(path).name
                validations.append({"pattern": _templatize(rel.partition("/")[0], t["bindings"], per_sample),
                                    "file": rel, "expected_type": str(v.get("expected_type") or ""),
                                    "passed": bool(v.get("passed")),
                                    "method": str(v.get("validation_method") or v.get("method") or "")})
        proven_trials.append({
            "name": t["name"], "ok": True,
            "steps": [int(s.get("step") or 0) for s in ordered],
            "commands_run": [str(s.get("command") or "") for s in ordered],
            "images_run": [str(s.get("container_image_digest") or s.get("container_image")
                               or f"host env {spec.get('conda_env') or ''}".strip()) for s in ordered],
            "substitutions": subs, "output_slots": out_slots,
            "produced_files": produced, "validation_results": validations,
        })
    step_numbers = sorted(int(s.get("step") or 0) for s in howto)
    verification = {
        "status": _core_data.USAGE_VERIFIED, "source": _core_data.USAGE_SOURCE_STEPS,
        "reason": (f"derived from recorded steps {', '.join(str(n) for n in step_numbers)}: every "
                   f"how-to command is the template a validated step ran, with the sample's values "
                   f"where its slots are, and the outputs are those steps' validated outputs"),
        "locus": next(iter(loci)) if len(loci) == 1 else "mixed",
        "trial_count": len(complete), "passed": len(complete),
        "trials": proven_trials,
    }
    if partial:
        verification["partial"] = [{"name": t["name"],
                                    "missing_commands": [ti + 1 for ti in m]} for t, m in partial]
    if prep:
        verification["preparation_steps"] = sorted(prep)
    return {"usage": usage, "usage_verification": verification}


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
        # validation record is keyed. Never match on basename: a record for a DIFFERENT
        # file that happens to share the name is not coverage of this one.
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

        # A validate_output record EXISTING is not the same as it PASSING. The
        # runtime records passed:False for a malformed BAM / empty VCF / bad
        # JSON; a spec must not claim "outputs checked" over a step whose
        # outputs failed their type-aware check.
        #
        # THIS CLAUSE IS NOT OVERRIDABLE by `mark_step_validated=passed` — that
        # would let the agent's assertion outrank the runtime's own measurement,
        # the exact thing CLAUDE.md's opening promise rules out ("nothing is
        # taken on faith from the agent"). The other I3 clauses are about ABSENT
        # evidence, where an agent saying "I checked it another way" adds
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

        # DELIBERATELY no refusal on expected_type="any". Existence + non-empty
        # is a legitimate PRIMARY validation — snapshot, exit code and non-empty
        # carry the run's evidence; the 18 type-aware checkers are a bonus where
        # the format is known, never a requirement. A refusal here would be worse
        # than no gate: the identical exists-nonzero check seals GREEN under any
        # unknown type string ("pod5", "h5ad", a typo), so refusing only the
        # honest "any" punishes stating the truth and rewards inventing a format
        # name. The record states what actually ran (`validation_method`: "tool"
        # vs "exists_nonzero") and the RUN dashboard renders it; a reader sees
        # the depth, nothing rounds it up.

    # ------------------------------------------------------------------
    # I6.absolute_paths is not a walk clause: step-path
    # absoluteness is enforced at construction by PipelineStep._paths_are_absolute
    # (scoped by core_data.is_path_like), raised at the write funnel
    # (typed_nouns.check_draft) and re-validated when seal builds the
    # WorkflowSpec. The placeholder-declaredness half of I6 below is a
    # cross-field check (template vs declared inputs) and stays a walk clause.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # I6: every {PLACEHOLDER} in usage.command_template must resolve to a
    # declared usage.inputs[*].name OR be the output directory the self-test
    # allocates (core_data.is_output_slot). A typo like {OUPUT_DIR} reads as a
    # placeholder everywhere paths are checked, and the dynamic self-test
    # only catches it when a trial actually runs — this static check
    # surfaces it sooner.
    # ------------------------------------------------------------------
    usage = spec.get("usage") if isinstance(spec.get("usage"), dict) else None
    if usage:
        # Scan EVERY command (str or list[str]) through the one reader — an undeclared
        # placeholder in command 3 of a multi-step how-to is exactly as broken as one in
        # command 1, and re-reading the raw field here would have seen only the first.
        template_placeholders = set(usage_slots(usage))
        if template_placeholders:
            declared_input_names = {
                (inp.get("name") or "").strip()
                for inp in (usage.get("inputs") or [])
                if isinstance(inp, dict) and inp.get("name")
            }
            legal = declared_input_names | {p for p in template_placeholders if is_output_slot(p)}
            undeclared = sorted(template_placeholders - legal)
            if undeclared:
                violations.append({
                    "invariant": "I6.template_placeholders_declared",
                    "message":   f"usage.command_template references placeholders "
                                 f"{undeclared} that are not declared in usage.inputs[*].name "
                                 f"and are not the output directory ({OUTPUT_SLOT_RULE}). Either "
                                 f"add them to usage.inputs or fix the typo. Declared inputs: "
                                 f"{sorted(declared_input_names) or '(none)'}.",
                    "where":     "usage.command_template",
                    "undeclared_placeholders": undeclared,
                })

    # ------------------------------------------------------------------
    # I4: a how-to the seal DERIVED from the steps says nothing they did not run. Every
    # command is the template of a step that exited 0, and every trial's values agree
    # with such a step for every command. A how-to AUTHORED before the derivation
    # (source `trials` / `inferred`, or unrecorded) is not asked this: it was proven by
    # the self-test of its day, and that record stands as it was written.
    # ------------------------------------------------------------------
    if usage and _core_data.usage_source(spec) == _core_data.USAGE_SOURCE_STEPS:
        ok_steps = [s for s in (spec.get("pipeline_steps") or [])
                    if isinstance(s, dict) and s.get("returncode") == 0]
        templates = usage_commands(usage)
        for i, t in enumerate(templates):
            if not any(s.get("template") == t for s in ok_steps):
                violations.append({
                    "invariant": "I4.howto_from_steps",
                    "message":   f"how-to command {i + 1} is not the template of any step that exited 0 "
                                 f"— the how-to is derived from the recorded steps and may not say "
                                 f"anything they did not run",
                    "where":     f"usage.command_template[{i}]",
                })
        for trial in (usage.get("trials") or []):
            if not isinstance(trial, dict):
                continue
            subs = {str(k): str(v) for k, v in (trial.get("substitutions") or {}).items()}
            for i, t in enumerate(templates):
                if not any(s.get("template") == t
                           and all(subs.get(k) == v for k, v in _step_bindings(s).items())
                           for s in ok_steps):
                    violations.append({
                        "invariant": "I4.trial_unproven",
                        "message":   f"trial {trial.get('name')!r} binds values no recorded step ran "
                                     f"how-to command {i + 1} with — a trial is a sample the steps ran, "
                                     f"never a guess",
                        "where":     f"usage.trials[{trial.get('name')}]",
                    })

    # ------------------------------------------------------------------
    # I7 — the VALUES half only. Presence (I7.resource_usage_recorded) is
    # enforced at construction: an rc=0 step without resource_usage
    # is unconstructible (PipelineStep._rc0_has_resource_usage), refused at the
    # write funnel and again when seal builds the WorkflowSpec. Whether the
    # recorded values are a REAL observation stays a walk check, because only
    # the producer context distinguishes fabrication from a sampling limit.
    # ------------------------------------------------------------------
    for s in spec.get("pipeline_steps", []) or []:
        if not isinstance(s, dict):
            continue
        step_n = s.get("step")
        if s.get("returncode") not in (None, 0):
            continue   # failed / not-run steps don't need resource data
        ru = s.get("resource_usage")
        if not isinstance(ru, dict):
            continue   # absence for rc=0 is unconstructible at the typed gate

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
            # shape. It is tempting to reject either-zero (the refusal message itself
            # says a real process has nonzero RSS *and* wall), but a value-level check
            # CANNOT tell fabrication from a sampling limit:
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
    # seal time. Layer 1 bakes an artifact into the image bytes, but Layer 2
    # references artifacts OUTSIDE the image on disk, so 'install==ship'
    # doesn't cover them. The
    # downstream consumer reads the file, not the spec, so the spec's claim
    # about the file (sha256) must keep matching disk up to the seal moment.
    # ------------------------------------------------------------------
    violations.extend(_check_authored_artifact_integrity(spec))

    # ------------------------------------------------------------------
    # I8 (test-data integrity): the same re-anchoring for the external source
    # the documented protocol actually tells the agent to produce. See
    # `verify_test_data`.
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
    # I10 (service health): every declared service_dependency must have proven
    # healthy. An env doesn't need the service to BUILD, and VALIDATED_IN_IMAGE
    # says nothing about whether a RUNTIME service a workflow depends on ever
    # came up, so the check lives at the layer where service dependencies do
    # (the Layer-2 draft).
    # ------------------------------------------------------------------
    violations.extend(_check_service_health(spec))

    # ------------------------------------------------------------------
    # I5 (reference-database availability): every declared
    # reference_database.local_path must exist on disk (and, when the record
    # carries integrity anchors, still match them). A reference DB is, by the
    # model's own contract, "mounted at runtime rather than baked into the Docker
    # image" (tens-to-hundreds of GB), so VALIDATED_IN_IMAGE says nothing about
    # it: a runtime-external artifact a workflow depends on. I8.composition_coherence
    # treats a ref-DB's local_path as a valid input source WITHOUT checking the
    # file is there, so without this clause a step could seal green consuming a DB
    # that is absent or truncated. Existence is checked and strengthened with the
    # sha256 anchor the model carries for downstream re-run pinning.
    # ------------------------------------------------------------------
    violations.extend(_check_reference_database_availability(spec))

    return violations


# Layer-2 (workflow) invariant subset. A WorkflowSpec consumes a frozen env by digest,
# so only the RUN-side invariants apply — the env-build ones are Layer 1's, verified IN
# the shipped image by env_honesty.check_build.
#
# DERIVED FROM THE REGISTRY, never typed out — a literal copy here is one more roster
# for the others to drift from. Reading it from `invariants` means a new clause is
# registered once and every consumer follows.
_WORKFLOW_INVARIANT_TIERS = frozenset(
    i.id for i in _invariants.active(_invariants.LAYER_WORKFLOW)
    if i.enforced_by.endswith("check_workflow_invariants"))


def check_workflow_invariants(spec: dict) -> list[dict]:
    """Run only the workflow-relevant invariants — see `agent/skills/invariants.py` for
    the roster and each one's statement (deliberately NOT restated here; a restated
    roster is one more copy to drift).

    Pass the FULL draft so I8 sees the complete universe of prior outputs + external
    sources, but only the run-side violations are returned."""
    return [v for v in check_invariants(spec)
            if v.get("invariant", "").split(".")[0] in _WORKFLOW_INVARIANT_TIERS]


def _check_service_health(spec: dict) -> list[dict]:
    """I10: every declared service_dependency must have at
    least one HEALTHY probe in its health_check_log — proof the service actually
    came up while the workflow ran.

    A Layer-2 clause: an env doesn't need the service running to BUILD, and
    without this check a WorkflowSpec could seal green while depending on a
    service that never became healthy (a status=failed / zero-healthy record),
    with the ServiceDependency docstring promising 'health_check_log: … I10
    requires ≥1 healthy'. A probe counts healthy
    when its `healthy` is truthy OR returncode == 0 — the predicate lives in
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
    """I5: every declared reference_database with a
    local_path must actually be present — and, when the record carries integrity
    anchors, still match them.

    A Layer-2 clause: the ReferenceDatabase model says these are 'mounted at
    runtime rather than baked into the Docker image' (they can be hundreds of GB),
    so the image validation cannot speak to them at all — a runtime-external
    dependency a workflow relies on, like authored artifacts (I8) and services
    (I10).

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
    # The SAME cap as every other anchor check, by import rather than by a
    # re-typed literal a test could only pin by grepping source text.
    from agent.models.core_data import ANCHOR_HASH_CAP_BYTES as HASH_CAP_BYTES
    violations: list[dict] = []
    for i, rdb in enumerate(spec.get("reference_databases", []) or []):
        if not isinstance(rdb, dict):
            continue
        lp = rdb.get("local_path")
        if not lp or not isinstance(lp, str):
            # ReferenceDatabase requires local_path and the write funnel enforces it, so
            # only a record written before enforcement reaches here; its next write
            # refuses it with the field named, and nothing here can check it.
            continue
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
                from agent.models.core_data import sha256_file
                disk_sha = sha256_file(p)
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

    In Layer 1 the artifact is BAKED INTO the image and the image bytes are
    the truth. Layer-2 authored artifacts live OUTSIDE the image on disk, so
    'install==ship' does NOT cover them — this check is the Layer-2
    counterpart. Invariant ID is I8.* because the concern is external-source
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
    from agent.models.core_data import sha256_file
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
                disk_sha = sha256_file(p)
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


def untraced_step_inputs(spec: dict, step_n) -> list[dict]:
    """The inputs of pipeline_step `step_n` that trace to nothing the draft knows — no
    prior step's output, no declared external source — each with the remedy the seal
    would give. I8 read at run time: the run tools disclose these on the step's result,
    so the fix (select the test data, register the reference, stage the script) costs
    one call rather than a seal round trip. The walk is the seal's own, so the two
    cannot disagree about what traces."""
    where = f"pipeline_steps[step={step_n}].inputs"
    return [{"path": v["orphan_path"], "remedy": v.get("remedy", "")}
            for v in _check_composition_coherence(spec)
            if v.get("invariant") == "I8.composition_coherence"
            and v.get("where") == where and v.get("orphan_path")]


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
    # Relative test_data paths anchor at the resources zone — the same anchor
    # `core_data.resolve_data_path` uses, because a second answer here is a
    # second answer to "does this input trace", which is what I8 decides.
    data_root = workspace.resources_root()

    def _add_external(s: str) -> None:
        if not isinstance(s, str) or not s:
            return
        external_paths.add(s)
        p = Path(s)
        if not p.is_absolute():
            joined = data_root / p
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

    # THE one reading of "which test_data values are paths" is
    # core_data.test_data_paths — never re-derive it at a call site.
    for _path in _core_data.test_data_paths(spec.get("test_data")).values():
        _add_external(_path)

    # A reference DATABASE that is a directory (an aligner index, a VEP cache, the
    # Exomiser data bundle) is consumed file by file: a step input beneath its
    # local_path traces to it. Scoped to declared reference directories — a directory
    # here, or at another locus, where nothing can be stat'd — never to prior outputs,
    # whose prefix hole _dir_contains_a_prior_output keeps closed.
    external_dirs: list[str] = []
    for rdb in spec.get("reference_databases", []) or []:
        if isinstance(rdb, dict):
            _add_external(rdb.get("local_path"))
            lp = rdb.get("local_path")
            if isinstance(lp, str) and lp and (rdb.get("locus") == "cluster" or Path(lp).is_dir()):
                external_dirs.append(lp)
                try:
                    external_dirs.append(str(Path(lp).resolve()))
                except Exception:
                    pass

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
        if _core_data.is_script_path(p):
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
            if _under_declared_dir(p, external_dirs):
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
            # An UNSCOPED basename match would do two wrong things: it cannot cover
            # the sidecar case (`sample.bam.bai` != `sample.bam` as basenames), and it
            # would ACCEPT any input anywhere on the local filesystem that merely shares
            # a name with some prior output — `/somewhere/else/entirely/sample.bam`
            # tracing cleanly to `/run1/sample.bam`, which is the entire class of orphan
            # I8 exists to catch. Scoping it to the cross-locus case keeps the real
            # capability and drops the hole.
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
                # THE GATE IS THE GUIDE, and this gate's remedy has TWO shapes. If the
                # input is a real file the spec never anchored, declare it by WHAT IT IS
                # and re-seal — the declaring primitive is chosen by kind, because the
                # render treats each kind differently (a reference database stays a
                # path input; an authored script is carried into bin/). But if the step
                # itself is debris — a diagnostic or a dead iteration attempt — no
                # declaration can make its input honest, and pipeline_steps are
                # runtime-recorded and unpatchable BY DESIGN: the only exit is
                # discard_pipeline_draft, then redrive the good steps into a fresh draft.
                "remedy":    "if this input is a legitimate external file, declare it by kind "
                             "and re-seal: data you downloaded or built here (an index, an "
                             "annotation, a reference subset; a directory is fine) → "
                             "download_reference_database(name, url) or "
                             "download_reference_database(name, url='', local_path=<path>); "
                             "test reads from the core manifests → select_test_data; a file "
                             "you WROTE (a script, a small fixture) → stage_authored_artifact. "
                             "If the STEP is iteration debris (a diagnostic, a superseded "
                             "attempt), pipeline_steps cannot be edited — discard_pipeline_draft "
                             "and redrive the proven steps into a fresh draft",
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
