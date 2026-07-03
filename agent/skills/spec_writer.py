"""
spec_writer — Layer-2 (workflow) spec + provenance persistence.

The pre-respine combined env+workflow writer (save_pipeline_spec) and its
env-build invariants (I1/I2/I5/I9/I10/I11/I12/I13/I14) have been retired: an
env is now solved once by freeze() and verified IN the shipped image by
env_honesty.check_build (install==ship). What survives here is the Layer-2
surface that consumes a frozen env:

    write_workflow_spec(workflow, config)   -> {workflow_spec_path}
    check_workflow_invariants(spec)         -> run-side violations (I0/I3/I6/I7/I8)
    self_test_usage(spec, env_manager, ...) -> executes usage.command_template (I4)
    write_provenance(inputs, config)        -> {written, sample_key}

check_invariants is now the run-side checker (I0/I3/I6/I7/I8 only); the
env-build tiers moved to agent/skills/env_honesty.py.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

from agent.models.core_data import (
    AssemblyInput, BamInput, GenomeRef, GenotypeArrayInput, OutputFile,
    PedigreeInput, PhenotypeInput, Provenance, QuantitativeTraitInput,
    ReadInput, VcfInput,
)
from agent.skills.outcomes import refused


# ---------------------------------------------------------------------------
# Pipeline spec persistence
# ---------------------------------------------------------------------------

def write_workflow_spec(workflow: dict, config: dict) -> dict:
    """Validate + write a Layer-2 WorkflowSpec as YAML.

    The human-facing how-to is rendered as HTML into {name}.RUN.html by the seal
    (run_dashboard_html), NOT a markdown guide — the markdown GUIDE.md is retired.
    Any legacy `user_guide` markdown on the workflow dict is dropped here (never
    inlined in the YAML, never written to disk)."""
    from agent.models.core_data import WorkflowSpec

    project_root = Path(__file__).parent.parent.parent.resolve()
    out_dir = project_root / config["paths"]["pipelines_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        wf = WorkflowSpec.model_validate(workflow)
    except Exception as e:
        return refused("spec_writer.validation_failed", error=f"WorkflowSpec validation failed: {e}")

    name = wf.workflow_name
    yaml_path = out_dir / f"{name}.workflow.yaml"
    data = wf.model_dump(exclude_none=True)
    data.pop("user_guide", None)        # retired markdown guide — never persist it
    data.pop("user_guide_path", None)
    yaml_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return {"workflow_spec_path": str(yaml_path)}


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
    usage = spec.get("usage")
    if not usage or not isinstance(usage, dict):
        return {"ok": False, "reason": "no usage block to self-test", "trials": []}
    template = (usage.get("command_template") or "").strip()
    if not template:
        return {"ok": False, "reason": "usage.command_template is empty", "trials": []}
    env_name = spec.get("conda_env")
    if not env_name:
        return {"ok": False, "reason": "no conda_env on spec — cannot run self-test", "trials": []}

    placeholders = set(re.findall(r"\{([A-Z][A-Z0-9_]*)\}", template))
    if not placeholders:
        return {"ok": False, "reason": "command_template has no {PLACEHOLDER} slots", "trials": []}

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
        trial_plans = [{
            "name":          "auto",
            "substitutions": inferred,
            "description":   "inferred from pipeline_steps[*].inputs (no usage.trials declared)",
        }]

    trial_results = [
        _run_one_trial(plan, template, placeholders, outputs_spec, env_manager, env_name, spec, validator)
        for plan in trial_plans
    ]

    overall_ok = bool(trial_results) and all(t.get("ok") for t in trial_results)
    return {
        "ok":     overall_ok,
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
    plan: dict, template: str, placeholders: set, outputs_spec: list,
    env_manager: Any, env_name: str, spec: dict, validator: Optional[Any],
) -> dict:
    """Execute one trial: substitute, run in fresh scratch, verify outputs.

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
    for slot in placeholders:
        if _is_output_slot(slot):
            subs[slot] = str(scratch)

    missing = [s for s in placeholders if s not in subs or not subs[s]]
    if missing:
        shutil.rmtree(scratch, ignore_errors=True)
        return {
            "name": trial_name, "ok": False,
            "reason": f"could not resolve placeholders: {missing}",
            "substitutions": subs,
        }

    command = template
    for slot, val in subs.items():
        command = command.replace("{" + slot + "}", str(val))

    result = env_manager.run_in_env(env_name, command, timeout=600, watch_dir=str(scratch))
    rc = result.get("returncode")
    if rc != 0:
        return {
            "name": trial_name, "ok": False,
            "reason": f"command_template execution failed (rc={rc})",
            "command_run": command, "scratch_dir": str(scratch),
            "stderr_tail": (result.get("stderr") or "")[-500:],
            "substitutions": subs,
        }

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
            "command_run": command, "scratch_dir": str(scratch),
            "substitutions": subs,
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
                "command_run": command, "scratch_dir": str(scratch),
                "substitutions": subs,
            }

    return {
        "name": trial_name, "ok": True,
        "command_run": command, "substitutions": subs,
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
    env-build invariants (I1/I2/I5/I9/I10/I11/I12/I13/I14) are NOT here — an env
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
        validated_basenames = set(validation.keys())
        unvalidated = [
            o for o in outs
            if Path(o).name not in validated_basenames
        ]
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
        # failed their type-aware check. An explicit mark_step_validated=passed
        # is the only sanctioned override (it re-anchors by other means and
        # itself refuses to pass an outputs-empty step).
        failed_validations = [
            fn for fn, v in validation.items()
            if isinstance(v, dict) and v.get("passed") is False
        ]
        if failed_validations and s.get("validation_status") != "passed":
            violations.append({
                "invariant": "I3.validation_passed",
                "message":   f"pipeline_step {step_n} has {len(failed_validations)} output(s) "
                             f"whose validate_output result is passed=False — the run recorded "
                             f"that these outputs FAILED type-aware validation. A spec cannot seal "
                             f"over a failed output check (use mark_step_validated only when the "
                             f"output was genuinely verified by other means).",
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
        template = (usage.get("command_template") or "").strip()
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
    # I8 (lineage integrity): file-type-agnostic universal lineage. When a
    # step's input path matches a prior step's output, the on-disk bytes must
    # match the producer's recorded sha256 from run time. Catches an agent
    # silently mutating a file between steps; the path-only I8 walk would not.
    # ------------------------------------------------------------------
    violations.extend(_check_lineage_integrity(spec))

    return violations


# Layer-2 (workflow) invariant subset. A WorkflowSpec consumes a frozen env by
# digest, so only the RUN-side invariants apply — the env-build invariants
# (I1/I2/I5/I9/I10/I11/I12/I13/I14) are Layer 1's, verified IN the shipped image
# by env_honesty.check_build. check_invariants is now itself run-side-only, so
# this filter is belt-and-suspenders (it stays the named Layer-2 entry point and
# guards against a future non-run-side clause leaking into check_invariants).
_WORKFLOW_INVARIANT_TIERS = {"I0", "I3", "I6", "I7", "I8"}


def check_workflow_invariants(spec: dict) -> list[dict]:
    """Run only the workflow-relevant invariants (I0 shape · I3 validated
    outputs · I6 paths/placeholders · I7 resource_usage · I8 input provenance).
    Pass the FULL draft so I8 sees the complete universe of prior outputs +
    external sources, but only the run-side violations are returned."""
    return [v for v in check_invariants(spec)
            if v.get("invariant", "").split(".")[0] in _WORKFLOW_INVARIANT_TIERS]


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

    td = spec.get("test_data") or {}
    if isinstance(td, dict):
        for k in ("r1", "r2", "vcf", "tbi", "bam", "bai", "ped",
                  "reference_fasta", "core_data_dir", "file"):
            _add_external(td.get(k))

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
            # Allow a basename-match fallback: agents sometimes record an
            # output path with an extra suffix (e.g. {p}.gz, {p}.bai) where
            # the prior step produced the unsuffixed parent.
            base = Path(p).name
            if any(Path(u).name == base for u in universe):
                continue
            violations.append({
                "invariant": "I8.composition_coherence",
                "message":   f"pipeline_step {step_n} input '{p}' has no producing source — "
                             f"not in test_data, reference_databases, runtime_configs, "
                             f"or any prior step's outputs",
                "where":     f"pipeline_steps[step={step_n}].inputs",
                "orphan_path": p,
            })

        for o in s.get("outputs", []) or []:
            if isinstance(o, str) and o:
                universe.add(o)
        for o in s.get("detected_outputs", []) or []:
            if isinstance(o, str) and o:
                universe.add(o)

    return violations


def write_provenance(inputs: dict, config: dict) -> dict:
    """Build and write a validated Provenance YAML for one pipeline run.

    Tool versions are read from the spec YAML's packages list rather than
    probing binaries — the spec is the authoritative source for what was
    actually installed (PackageSearch resolved versions at install time)."""
    output_dir = Path(inputs["output_dir"])
    sample_key = inputs["sample_key"]
    prov_path = output_dir / f"{sample_key}_provenance.yaml"

    def _rel(abs_path: str) -> str:
        return os.path.relpath(Path(abs_path).resolve(), output_dir.resolve())

    genome = None
    if inputs.get("reference_path"):
        genome = GenomeRef(
            genome_build=inputs.get("genome_build", ""),
            chromosome_subset=inputs.get("chromosome", ""),
            reference=_rel(inputs["reference_path"]),
            reference_fai=_rel(inputs["reference_path"] + ".fai"),
        )

    reads = None
    if inputs.get("reads"):
        r = inputs["reads"]
        raw_db = r.get("database", "EBI_SRA")
        db = _DB_ALIASES.get(raw_db, raw_db)
        reads = [ReadInput(
            read_type=r.get("read_type", "short_read"),
            end_type=r.get("end_type", "paired_end"),
            assay_type=r.get("assay_type", "exome"),
            subset=r.get("subset", ""),
            num_reads=int(r.get("num_reads", 0)),
            r1=_rel(r["r1"]),
            r2=_rel(r["r2"]) if r.get("r2") else None,
            sample=r.get("sample", ""),
            accession=r.get("accession", ""),
            database=db,
        )]

    bam_input = None
    if inputs.get("bam_input"):
        b = inputs["bam_input"]
        bam_input = BamInput(bam=_rel(b["bam"]), bai=_rel(b["bai"]))

    vcf_input = None
    if inputs.get("vcf_input"):
        v = inputs["vcf_input"]
        vcf_input = VcfInput(
            vcf=_rel(v["vcf"]),
            tbi=_rel(v["tbi"]) if v.get("tbi") else None,
            genome_build=v.get("genome_build", inputs.get("genome_build", "")),
            upstream_pipeline=v.get("upstream_pipeline"),
            sample_ids=v.get("sample_ids", []),
        )

    assembly_input = None
    if inputs.get("assembly_input"):
        a = inputs["assembly_input"]
        assembly_input = AssemblyInput(
            assembly=_rel(a["assembly"]),
            upstream_pipeline=a.get("upstream_pipeline"),
        )

    phenotype = None
    if inputs.get("phenotype"):
        p = inputs["phenotype"]
        phenotype = PhenotypeInput(
            ontology=p.get("ontology", "HPO"),
            terms=p["terms"],
            source=p.get("source"),
        )

    pedigree = None
    if inputs.get("pedigree"):
        g = inputs["pedigree"]
        pedigree = PedigreeInput(
            ped=_rel(g["ped"]),
            proband=g.get("proband"),
        )

    genotype_array = None
    if inputs.get("genotype_array"):
        ga = inputs["genotype_array"]
        genotype_array = GenotypeArrayInput(
            file=_rel(ga["file"]),
            format=ga["format"],
            bim=_rel(ga["bim"]) if ga.get("bim") else None,
            fam=_rel(ga["fam"]) if ga.get("fam") else None,
            n_samples=ga.get("n_samples"),
            n_snps=ga.get("n_snps"),
            genome_build=ga.get("genome_build"),
            upstream_pipeline=ga.get("upstream_pipeline"),
        )

    quantitative_traits = None
    if inputs.get("quantitative_traits"):
        qt = inputs["quantitative_traits"]
        quantitative_traits = QuantitativeTraitInput(
            traits=qt["traits"],
            file=_rel(qt["file"]),
            n_samples=qt.get("n_samples"),
            measurement_type=qt.get("measurement_type", "continuous"),
        )

    pipeline_spec_path = Path(inputs["pipeline_spec_path"]).resolve()
    try:
        spec_rel = str(pipeline_spec_path.relative_to(output_dir.resolve()))
    except ValueError:
        spec_rel = str(pipeline_spec_path)

    outputs = [
        OutputFile(file=f["file"], type=f["type"], indexed=f.get("indexed", False))
        for f in inputs.get("output_files", [])
    ]

    tool_versions = _tool_versions_from_spec(pipeline_spec_path)

    prov = Provenance(
        pipeline=inputs["pipeline"],
        pipeline_spec=spec_rel,
        conda_env=Path(inputs["conda_env_path"]).name,
        created_at=str(date.today()),
        tool_versions=tool_versions,
        genome=genome,
        reads=reads,
        bam_input=bam_input,
        vcf_input=vcf_input,
        assembly_input=assembly_input,
        phenotype=phenotype,
        pedigree=pedigree,
        genotype_array=genotype_array,
        quantitative_traits=quantitative_traits,
        upstream_pipelines=inputs.get("upstream_pipelines", []),
        parameters=inputs.get("parameters") or None,
        outputs=outputs,
    )

    written = prov.write(prov_path)
    return {"written": str(written), "sample_key": sample_key}


def _tool_versions_from_spec(spec_path: Path) -> dict[str, str]:
    """Read tool versions from a pipeline spec YAML's packages list.

    The spec is the authoritative source — PackageSearch already resolved
    each package's exact version at install time and stored it there.
    Falls back to {} if the spec file is missing or unreadable."""
    if not spec_path.exists():
        return {}
    try:
        with open(spec_path) as f:
            spec = yaml.safe_load(f) or {}
    except Exception:
        return {}
    versions: dict[str, str] = {}
    for pkg in spec.get("packages", []):
        name = pkg.get("name")
        if not name or name == "conda-pack":
            continue
        ver = pkg.get("resolved_version") or pkg.get("version")
        if ver:
            versions[name] = ver
    return versions
