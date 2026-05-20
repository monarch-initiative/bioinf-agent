"""
spec_writer — persist pipeline spec + provenance YAML artifacts.

Two pure functions exposed to the MCP server:

    save_pipeline_spec(spec, config) -> {saved_yaml, saved_html}
    write_provenance(inputs, config) -> {written, sample_key}

save_pipeline_spec derives validation_status per step from each step's
validation dict, then derives the pipeline-level status from those — so
"fully_validated" can only land if every step's outputs actually passed
validate_output, not just exited zero.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

from agent.models.core_data import (
    AssemblyInput, BamInput, GenomeRef, GenotypeArrayInput, OutputFile,
    PedigreeInput, PhenotypeInput, PipelineSpec, Provenance, QuantitativeTraitInput,
    ReadInput, VcfInput,
)
from agent.skills.report_builder import generate as generate_report


# ---------------------------------------------------------------------------
# Pipeline spec persistence
# ---------------------------------------------------------------------------

def save_pipeline_spec(spec: dict, config: dict, env_manager: Optional[Any] = None) -> dict:
    """Validate and write a PipelineSpec dict as YAML + HTML report.

    Derives each pipeline_step's validation_status from its validation dict,
    then derives env_status (from install_steps + package verifications) and
    pipeline_status (from pipeline_steps) separately. Anything the caller
    passed for these is overwritten by the derived values.

    If env_manager is provided, runs a single reconciliation pass against the
    live conda env: re-queries actual installed versions, fills missing
    install_method types, and derives homepages from (channel, name, source).
    This is the source-of-truth pass — eliminates drift between what was
    requested at search-time and what the solver actually installed.

    Filename stem is `{pipeline_name}_{version}` where version is the
    resolved_version of the package whose name best matches pipeline_name
    (exact > substring), searching both spec.packages and
    install_steps[].installed_packages. Falls back to the first non-conda-pack
    package's version, then to "latest".
    """
    project_root = Path(__file__).parent.parent.parent.resolve()
    pipelines_dir = project_root / config["paths"]["pipelines_dir"]
    pipelines_dir.mkdir(parents=True, exist_ok=True)

    if env_manager is not None and spec.get("conda_env"):
        derive_packages_from_env(spec, env_manager, spec["conda_env"])
        reconcile_packages_with_env(spec, env_manager, spec["conda_env"])

    derive_step_dag(spec)
    _derive_reference_database_availability(spec)
    _derive_step_validation_status(spec)
    spec["env_status"]      = _derive_env_status(spec)
    spec["pipeline_status"] = _derive_pipeline_status(spec)
    spec["docker_status"]   = _derive_docker_status(spec)

    # Machine-verify usage.command_template by executing it on real test data.
    # Only meaningful when env_manager is available + we have a usage block.
    # Sets usage_verified=True if the command runs and outputs match the
    # patterns the agent declared. Result also recorded in usage._self_test
    # for transparency in the report (visible audit trail).
    if env_manager is not None and spec.get("usage"):
        st = self_test_usage(spec, env_manager)
        spec["usage_verified"] = bool(st.get("ok"))
        spec["usage"]["_self_test"] = st

    # Invariant gate. Every spec must satisfy these honesty rules or finalize
    # refuses to write — keeps the report-can't-be-faked guarantee.
    violations = check_invariants(spec)
    if violations:
        return {
            "error":      "invariant violations — finalize refused to write",
            "violations": violations,
            "violation_count": len(violations),
        }

    try:
        pspec = PipelineSpec.model_validate(spec)
        write_spec = pspec.model_dump(exclude_none=True)
    except Exception as e:
        print(f"[spec_writer] WARN: PipelineSpec validation failed: {e}", file=sys.stderr)
        write_spec = spec

    name    = write_spec.get("pipeline_name", "pipeline")
    version = _pick_version_for_filename(write_spec, name)
    stem    = f"{name}_{version}" if version else name

    yaml_path = pipelines_dir / f"{stem}.yaml"
    with open(yaml_path, "w") as f:
        yaml.dump(write_spec, f, default_flow_style=False, sort_keys=False)

    html_path = pipelines_dir / f"{stem}.html"
    html_path.write_text(generate_report(write_spec))

    # Reproducibility artifacts: environment.yml is the portable conda recipe
    # (anyone can `conda env create -f` it on any platform); .lock is the
    # URL-pinned explicit list for bit-exact recreation on the build platform.
    # We also record the sha256 of the .lock file in the spec so a future user
    # can verify a recreated env matches bit-for-bit.
    env_yml_path: Optional[Path] = None
    lock_path:    Optional[Path] = None
    if env_manager is not None and write_spec.get("conda_env"):
        env_name = write_spec["conda_env"]
        env_yml = env_manager.export_environment_yml(env_name, from_history=True)
        if env_yml:
            env_yml_path = pipelines_dir / f"{stem}.environment.yml"
            env_yml_path.write_text(env_yml)
        lock = env_manager.export_explicit_lock(env_name)
        if lock:
            lock_path = pipelines_dir / f"{stem}.lock"
            lock_path.write_text(lock)
            import hashlib
            lock_sha = hashlib.sha256(lock.encode()).hexdigest()
            write_spec["lock_sha256"] = lock_sha
            # Rewrite the YAML with the lock_sha256 included.
            with open(yaml_path, "w") as f:
                yaml.dump(write_spec, f, default_flow_style=False, sort_keys=False)
            # Regenerate HTML so the sha shows in the report header.
            html_path.write_text(generate_report(write_spec))

    return {
        "saved_yaml":      str(yaml_path),
        "saved_html":      str(html_path),
        "saved_env_yml":   str(env_yml_path) if env_yml_path else None,
        "saved_lock":      str(lock_path)    if lock_path    else None,
        "env_status":      write_spec.get("env_status"),
        "pipeline_status": write_spec.get("pipeline_status"),
    }


def _normalize_pkg_name(name: str) -> str:
    """Canonical key for comparing package / pipeline names across naming variants.

    Lowercase + drop hyphens / underscores / dots so:
      open-cravat == opencravat == open_cravat == OpenCravat
      bioconductor-deseq2 == bioconductordeseq2 == DESeq2 (substring after prefix strip)
    Software-driven; replaces eyeballed substring matching with a deterministic rule.
    """
    return (name or "").lower().replace("-", "").replace("_", "").replace(".", "")


def _pick_version_for_filename(spec: dict, pipeline_name: str) -> str:
    """Find the version that should appear in the filename stem.

    Search order (using normalized names so open-cravat ↔ opencravat ↔ open_cravat match):
      1. spec.packages where normalized(name) == normalized(pipeline_name)
      2. spec.packages where pipeline_name substring-matches the package name
         (after stripping common conda recipe prefixes like bioconductor-)
      3. install_steps[].installed_packages — same exact then substring search;
         this is where R packages installed via install_github / BiocManager
         and JAR-installed tools have their version recorded
      4. First non-conda-pack, non-infrastructure package's resolved_version
    """
    name_n = _normalize_pkg_name(pipeline_name)
    packages = spec.get("packages", [])

    def _candidates(name: str) -> set:
        n = _normalize_pkg_name(name)
        cs = {n}
        # Strip common conda recipe prefixes so bioconductor-deseq2 matches deseq2
        for prefix in ("bioconductor", "r", "python"):
            if n.startswith(prefix) and len(n) > len(prefix):
                cs.add(n[len(prefix):])
        return cs

    # Pass 1: exact match in spec.packages
    for p in packages:
        if name_n in _candidates(p.get("name", "")):
            v = p.get("resolved_version") or p.get("version")
            if v:
                return v

    # Pass 2: substring match in spec.packages
    for p in packages:
        pn = p.get("name", "")
        if pn == "conda-pack" or not pn:
            continue
        pn_n = _normalize_pkg_name(pn)
        cands = _candidates(pn)
        if any(name_n in c or c in name_n for c in cands):
            v = p.get("resolved_version") or p.get("version")
            if v:
                return v

    # Pass 3: search install_steps' installed_packages (exact then substring)
    install_pkgs = [
        ip for s in spec.get("install_steps", [])
        for ip in s.get("installed_packages", []) if isinstance(ip, dict)
    ]
    for ip in install_pkgs:
        if name_n in _candidates(ip.get("name", "")) and ip.get("version"):
            return ip["version"]
    for ip in install_pkgs:
        pn = ip.get("name", "")
        if not pn:
            continue
        cands = _candidates(pn)
        if any(name_n in c or c in name_n for c in cands) and ip.get("version"):
            return ip["version"]

    # Pass 4: first non-conda-pack package version
    for p in packages:
        if p.get("name") == "conda-pack":
            continue
        v = p.get("resolved_version") or p.get("version")
        if v:
            return v

    return ""


# ---------------------------------------------------------------------------
# Invariants — the honesty rules that any spec must satisfy at finalize.
#
# Every invariant cross-references an agent-claimed field against an
# authoritative source (filesystem, conda list, validate_output result).
# An honest spec passes all checks; a spec missing any invariant cannot
# achieve env_status / pipeline_status of "fully_validated".
#
# The rules are simple but cascading: from them, "trustworthy report"
# falls out automatically.
# ---------------------------------------------------------------------------


def self_test_usage(spec: dict, env_manager: Any) -> dict:
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
        _run_one_trial(plan, template, placeholders, outputs_spec, env_manager, env_name, spec)
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
        slot_lower = slot.lower()
        if "output" in slot_lower or "out_dir" in slot_lower:
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
    env_manager: Any, env_name: str, spec: dict,
) -> dict:
    """Execute one trial: substitute, run in fresh scratch, verify outputs."""
    import fnmatch, shutil, tempfile

    trial_name = plan["name"]
    subs       = dict(plan.get("substitutions") or {})
    scratch    = Path(tempfile.mkdtemp(prefix=f"selftest_{spec.get('pipeline_name','x')}_{trial_name}_"))

    # Always override output slots with this trial's scratch dir.
    for slot in placeholders:
        if "output" in slot.lower() or "out_dir" in slot.lower():
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

    missing_outputs = []
    for o in outputs_spec:
        patterns = o.get("files") or []
        if not patterns:
            continue
        for pat in patterns:
            if not any(fnmatch.fnmatch(f, pat) or fnmatch.fnmatch(Path(f).name, pat) for f in produced):
                missing_outputs.append({"slot": o.get("name"), "pattern": pat})

    if missing_outputs:
        return {
            "name": trial_name, "ok": False,
            "reason": "command ran but expected outputs missing",
            "missing_outputs": missing_outputs,
            "produced_files": produced[:20],
            "command_run": command, "scratch_dir": str(scratch),
            "substitutions": subs,
        }

    return {
        "name": trial_name, "ok": True,
        "command_run": command, "substitutions": subs,
        "produced_files": produced[:20], "scratch_dir": str(scratch),
        "description": plan.get("description"),
    }


def check_invariants(spec: dict) -> list[dict]:
    """Return a list of invariant violations. Empty list = the spec is
    machine-verifiable as honest — every claim has a corresponding artifact
    on disk or a recorded execution.

    Each violation: {invariant: <id>, message: <str>, where: <optional path>}.

    This runs at finalize. A spec with any violation cannot be written to
    {name}_{version}.yaml; the draft is preserved and the violations are
    returned so the agent can fix the gap.
    """
    violations: list[dict] = []

    # ------------------------------------------------------------------
    # I1: every install_step's command actually ran and returned a code.
    # The runtime sets returncode from the subprocess; an agent cannot fake
    # this without bypassing the install_step machinery entirely.
    # ------------------------------------------------------------------
    for s in spec.get("install_steps", []) or []:
        if not isinstance(s, dict):
            continue
        if s.get("returncode") is None:
            violations.append({
                "invariant": "I1.install_step_executed",
                "message":  f"install_step {s.get('step')} has no returncode — "
                            f"the command was never actually run",
            })
        elif s.get("returncode") != 0 and s.get("status") != "failed":
            violations.append({
                "invariant": "I1.install_step_failure_acknowledged",
                "message":  f"install_step {s.get('step')} returncode "
                            f"{s.get('returncode')} but status not marked failed",
            })

    # ------------------------------------------------------------------
    # I2: every non-infrastructure package has a verify_output proving it
    # was actually checked. The verify_output is captured stdout/stderr
    # from the check_command running in the env — without this, the
    # PackageRecord is an unverified claim.
    # ------------------------------------------------------------------
    for p in spec.get("packages", []) or []:
        if not isinstance(p, dict):
            continue
        name = p.get("name", "")
        if not name or name in _INFRASTRUCTURE_PACKAGES:
            continue
        if not (p.get("verify_output") or "").strip():
            violations.append({
                "invariant": "I2.package_verified",
                "message":  f"package '{name}' has no verify_output — "
                            f"call verify_installation(env, '{name}', '<check_command>') "
                            f"so the env is provably real.",
                "where":    f"packages[name={name}]",
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

    # ------------------------------------------------------------------
    # I5: every declared local_path on disk actually exists. Reference DBs
    # without their data don't run. The schema's `available` field already
    # encodes this; here we promote it to an invariant violation when False.
    # ------------------------------------------------------------------
    for rdb in spec.get("reference_databases", []) or []:
        if not isinstance(rdb, dict):
            continue
        lp = rdb.get("local_path")
        if lp and not Path(lp).exists():
            violations.append({
                "invariant": "I5.reference_database_available",
                "message":  f"reference_database '{rdb.get('name')}' has "
                            f"local_path that doesn't exist on disk: {lp}",
                "where":    f"reference_databases[name={rdb.get('name')}]",
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

    # ------------------------------------------------------------------
    # I8: composition coherence. Every step's inputs must trace to either
    # an external source (test_data, reference_databases, runtime_configs,
    # config_files on this or a prior step) or a prior step's outputs.
    # An orphan input — a path no upstream source produces — means the
    # pipeline doesn't actually compose: step N consumed something step N-1
    # didn't make.
    # ------------------------------------------------------------------
    violations.extend(_check_composition_coherence(spec))

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
    external_paths: set[str] = set()

    td = spec.get("test_data") or {}
    if isinstance(td, dict):
        for k in ("r1", "r2", "vcf", "tbi", "bam", "bai", "ped",
                  "reference_fasta", "core_data_dir", "file"):
            v = td.get(k)
            if isinstance(v, str) and v:
                external_paths.add(v)

    for rdb in spec.get("reference_databases", []) or []:
        if isinstance(rdb, dict):
            lp = rdb.get("local_path")
            if isinstance(lp, str) and lp:
                external_paths.add(lp)

    for rc in spec.get("runtime_configs", []) or []:
        if isinstance(rc, dict):
            p = rc.get("path")
            if isinstance(p, str) and p:
                external_paths.add(p)

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
        # config_files declared on this step are written before the step runs.
        for cf in s.get("config_files", []) or []:
            if isinstance(cf, dict):
                p = cf.get("path")
                if isinstance(p, str) and p:
                    universe.add(p)

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


def _derive_docker_status(spec: dict) -> str:
    """A separate status for the Docker artifact, distinct from env / pipeline.

    Returns:
      not_attempted  — no docker block present, or build_attempted=False
      built          — build_attempted=True AND build_success=True
      failed         — build_attempted=True AND build_success=False
                       (the spec is otherwise fine but the image won't deploy)

    Kept orthogonal to env_status / pipeline_status so a Docker-daemon-down
    machine doesn't false-fail an otherwise valid install — but the failure is
    still visible at the top of the report rather than buried in the docker block.
    """
    docker = spec.get("docker")
    if not docker or not docker.get("build_attempted"):
        return "not_attempted"
    return "built" if docker.get("build_success") else "failed"


def _derive_reference_database_availability(spec: dict) -> None:
    """Set reference_databases[*].available based on whether local_path exists.

    The schema default is `available: False` which is misleading once data has
    actually been downloaded — Exomiser's 36 GB bundle was on disk and the spec
    still said available=false. Filesystem state at finalize-time is the truth.
    """
    for rdb in spec.get("reference_databases", []) or []:
        if not isinstance(rdb, dict):
            continue
        lp = rdb.get("local_path")
        if lp:
            rdb["available"] = Path(lp).exists()


def _derive_step_validation_status(spec: dict) -> None:
    """Fill in validation_status on each step from its validation dict.

    Rules:
      - any entry with passed=False    → "failed"
      - all entries with passed=True   → "passed"
      - empty / missing validation     → leave as None
    Respects an explicit validation_status the caller already set.
    """
    for step in spec.get("pipeline_steps", []):
        if step.get("validation_status"):
            continue
        validation = step.get("validation") or {}
        if not isinstance(validation, dict) or not validation:
            continue
        entries = [v for v in validation.values() if isinstance(v, dict)]
        if not entries:
            continue
        if any(v.get("passed") is False for v in entries):
            step["validation_status"] = "failed"
        elif all(v.get("passed") is True for v in entries):
            step["validation_status"] = "passed"


def _derive_env_status(spec: dict) -> str:
    """Compute env_status from install_steps + per-package verifications.

      failed             — any install_step.returncode != 0
      fully_validated    — every install_step clean AND every non-infrastructure
                           package has a verify_output (the env is provably real)
      partially_validated — installs clean but some packages lack verify_output
      complete           — installs clean but no packages derived (unusual)
    """
    install_steps = spec.get("install_steps", [])
    if any(s.get("returncode") not in (None, 0) for s in install_steps):
        return "failed"
    packages = [p for p in spec.get("packages", []) if p.get("name") not in _INFRASTRUCTURE_PACKAGES]
    if not packages:
        return "complete"
    unverified = [p for p in packages if not (p.get("verify_output") or "").strip()]
    if unverified:
        return "partially_validated"
    return "fully_validated"


def _derive_pipeline_status(spec: dict) -> str:
    """Compute pipeline_status from pipeline_steps' execution + validation."""
    steps = spec.get("pipeline_steps", [])
    if not steps:
        # No algorithm runs means there's nothing to validate — but save_pipeline_spec
        # was called, so we're past in_progress. "complete" is correct for a spec
        # with only env setup (e.g. bioinf_core_tools).
        return "complete"

    if any(s.get("returncode") not in (None, 0) for s in steps):
        return "failed"

    val_states = [s.get("validation_status") for s in steps]
    if any(v == "failed" for v in val_states):
        return "failed"

    passed_count = sum(1 for v in val_states if v == "passed")
    if passed_count == len(steps):
        return "fully_validated"
    if passed_count > 0:
        return "partially_validated"
    return "complete"


# ---------------------------------------------------------------------------
# Environment reconciliation
#
# The draft accumulates what we *requested* during install — search_package
# returns the channel's "latest", but the solver may pin a different version
# based on co-installed packages' constraints (a downgrade for r-base 4.4,
# a newer build pulled in transitively, etc). At finalize-time we probe the
# live env and patch each PackageRecord so the saved spec reflects truth.
#
# Single probe per env (one conda list + one Rscript per r_install package).
# Pure templating for homepage and install_method defaults.
# ---------------------------------------------------------------------------

_R_INSTALL_GITHUB_REGEX = re.compile(r"""['"]([^'"\s]+/[^'"\s]+)['"]""")

# Packages that are infrastructure (not user-facing tools) and shouldn't be
# surfaced as primary spec entries. conda-pack is added to every env automatically.
_INFRASTRUCTURE_PACKAGES = frozenset({"conda-pack", "pip", "python"})


def derive_packages_from_env(spec: dict, env_manager: Any, env_name: str) -> dict:
    """Rebuild spec['packages'] from sources of truth: `conda list` + every
    install_step's `installed_packages` array.

    This replaces the accumulator-based packages list (which drifted from the
    actual install) with one derived directly from what conda + run_install_command
    actually did. Annotations (description, homepage, channel) recorded earlier
    via search_package or run_install_command are preserved by name when possible.

    Returns a summary dict for diagnostics.
    """
    cache         = spec.get("search_cache",  {}) or {}
    verifications = spec.get("verifications", {}) or {}
    # Preserve annotations from any prior `packages` list (description, channel,
    # homepage, verify_*, install_method.source, etc.) keyed by name.
    prior_by_name: dict[str, dict] = {
        p.get("name"): p for p in spec.get("packages", []) or []
        if isinstance(p, dict) and p.get("name")
    }

    # Pull the full conda record (version + channel + build_string) so the
    # PackageRecord.channel can be set from conda's authoritative answer,
    # not from whatever the agent passed to install_packages (which is the
    # hint, not the resolved channel).
    conda_records = env_manager.list_conda_package_records(env_name)
    conda_versions = {n: rec["version"] for n, rec in conda_records.items()}
    # Only user-requested conda packages belong in spec.packages — not the full
    # transitive closure (samtools brings in 100 dynamic-link deps you didn't ask
    # for). The lock file captures the closure for reproducibility; this list
    # is the user-facing tool roster.
    explicit = env_manager.list_explicit_conda_packages(env_name)
    if explicit:
        conda_versions = {n: v for n, v in conda_versions.items() if n in explicit}
        conda_records  = {n: r for n, r in conda_records.items() if n in explicit}

    # Collect non-conda packages from install_steps. install_steps records each
    # run_install_command's installed_packages, which captures JARs (Exomiser),
    # R packages from CRAN/Bioc/GitHub, pip wheels, anything outside conda.
    install_step_packages: dict[str, dict] = {}
    for step in spec.get("install_steps", []) or []:
        if not isinstance(step, dict):
            continue
        # Only trust successful install steps as a source of truth.
        if step.get("returncode") not in (None, 0):
            continue
        for ip in step.get("installed_packages", []) or []:
            if not isinstance(ip, dict):
                continue
            name = ip.get("name")
            if not name or name in _INFRASTRUCTURE_PACKAGES:
                continue
            # If the same name appears in conda list, conda owns it; skip.
            # Otherwise this is a non-conda install (R / pip / JAR / etc).
            if name in conda_versions:
                continue
            install_step_packages[name] = ip

    rebuilt: list[dict] = []

    # 1) Every conda-tracked package
    for name in sorted(conda_versions):
        if name in _INFRASTRUCTURE_PACKAGES:
            continue
        prior = prior_by_name.get(name, {})
        v = verifications.get(name) or {}
        # Channel from conda list --json wins — it's the resolved channel. The
        # prior.channel was the agent's hint (e.g. "bioconda") which conda may
        # have honored or not; here we want the truth.
        conda_rec = conda_records.get(name, {})
        rec = {
            "name":              name,
            "requested_version": prior.get("requested_version") or "latest",
            "resolved_version":  conda_versions[name],
            "channel":           conda_rec.get("channel") or prior.get("channel"),
            "install_method":    prior.get("install_method") or {"type": "conda"},
            "description":       prior.get("description") or (cache.get(name) or {}).get("description"),
            "homepage":          prior.get("homepage") or (cache.get(name) or {}).get("homepage"),
            "verify_command":    v.get("verify_command") or prior.get("verify_command"),
            "verify_output":     v.get("verify_output")  or prior.get("verify_output"),
        }
        rebuilt.append({k: v for k, v in rec.items() if v is not None})

    # 2) Every non-conda install_step package
    # Late import to avoid touching env_manager at module load.
    from agent.skills.env_manager import parse_version_from_url
    pip_versions = {}
    try:
        pip_versions = env_manager.list_pip_packages(env_name) or {}
    except Exception:
        pass
    for name, ip in install_step_packages.items():
        prior = prior_by_name.get(name, {})
        v = verifications.get(name) or {}
        channel = ip.get("channel") or prior.get("channel", "")
        # Version fallback ladder (last-resort only — caller's ip.version wins):
        #   a) pip list (when channel=pip and pip's catalog has this name)
        #   b) parse from install_method.source URL (GitHub release URLs, etc.)
        if not ip.get("version"):
            if (channel or "").lower() in ("pip", "pypi") and pip_versions.get(name):
                ip["version"] = pip_versions[name]
            else:
                source_url = (ip.get("install_method") or {}).get("source") or ip.get("source") or ""
                if source_url:
                    parsed = parse_version_from_url(source_url)
                    if parsed:
                        ip["version"] = parsed
        # Trust the install_step's install_method first — install_jar_tool sets
        # type=jar, run_install_command-emitted JAR steps do too. The
        # channel-based heuristic is only a last-resort fallback when no caller
        # has stated a concrete type. Same precedence for `source`.
        ip_im     = ip.get("install_method") or {}
        prior_im  = prior.get("install_method") or {}
        im_type = (
            ip_im.get("type")
            or prior_im.get("type")
            or _CHANNEL_TO_INSTALL_METHOD.get((channel or "").lower(), "source")
        )
        install_method = {"type": im_type}
        # Carry through every supplementary install_method field the caller set
        # (jar_path, wrapper_script, java_flags, source, ...). ip wins over prior.
        for k_ in set(ip_im) | set(prior_im):
            if k_ == "type":
                continue
            val = ip_im.get(k_) if k_ in ip_im else prior_im.get(k_)
            if val is not None:
                install_method[k_] = val
        if ip.get("source") and "source" not in install_method:
            install_method["source"] = ip["source"]
        rec = {
            "name":              name,
            "requested_version": prior.get("requested_version") or ip.get("requested_version") or ip.get("version") or "latest",
            "resolved_version":  ip.get("version") or prior.get("resolved_version"),
            "channel":           channel or None,
            "install_method":    install_method,
            "description":       prior.get("description") or (cache.get(name) or {}).get("description"),
            "homepage":          prior.get("homepage") or (cache.get(name) or {}).get("homepage"),
            "verify_command":    v.get("verify_command") or prior.get("verify_command"),
            "verify_output":     v.get("verify_output")  or prior.get("verify_output"),
        }
        rebuilt.append({k: v for k, v in rec.items() if v is not None})

    spec["packages"] = rebuilt
    return {
        "conda_count":         len(conda_versions),
        "install_step_count":  len(install_step_packages),
        "rebuilt_count":       len(rebuilt),
    }


# Mirror of _DERIVE_INSTALL_METHOD_TYPE in mcp_server; duplicated here so
# spec_writer can rebuild packages without importing from mcp_server (which
# would create a cycle via FastMCP).
_CHANNEL_TO_INSTALL_METHOD: dict[str, str] = {
    "github":        "r_install",
    "cran":          "r_install",
    "bioconductor":  "r_install",
    "pip":           "pip",
    "pypi":          "pip",
    "conda-forge":   "conda",
    "bioconda":      "conda",
    "noarch":        "conda",
    "docker":        "docker_pull",
}


def derive_step_dag(spec: dict) -> None:
    """For each pipeline_step missing `depends_on`, derive it from the
    input/output overlap with earlier steps.

    Specifically: if step N's inputs contain a path that appears in step M's
    outputs (M < N), then N depends_on M. The result is a partial order that
    a downstream tool (Nextflow generator, parallel scheduler) can consume.
    """
    steps = spec.get("pipeline_steps", []) or []
    if not steps:
        return

    # Build a map: output_path -> step_number that produced it.
    produced_by: dict[str, int] = {}
    for s in steps:
        if not isinstance(s, dict):
            continue
        step_num = s.get("step")
        if step_num is None:
            continue
        for out in s.get("outputs", []) or []:
            produced_by[out] = step_num

    for s in steps:
        if not isinstance(s, dict):
            continue
        if s.get("depends_on"):
            continue   # caller-provided; trust it
        step_num = s.get("step")
        if step_num is None:
            continue
        deps: set[int] = set()
        for entry in s.get("inputs", []) or []:
            # Inputs are StepInput dicts after model coercion; the YAML may
            # still see strings if hand-edited. Handle both.
            paths: list[str] = []
            if isinstance(entry, dict):
                if entry.get("path"):
                    paths.append(entry["path"])
                paths.extend(entry.get("references", []) or [])
            elif isinstance(entry, str):
                paths.append(entry)
            for p in paths:
                producer = produced_by.get(p)
                if producer is not None and producer < step_num:
                    deps.add(producer)
        if deps:
            s["depends_on"] = sorted(deps)


def reconcile_packages_with_env(spec: dict, env_manager: Any, env_name: str) -> dict:
    """Patch spec.packages in place against the live conda env.

    Returns a summary dict for diagnostics:
        {patched_versions:    [{name, from, to}, ...],
         filled_homepages:    [name, ...],
         filled_install_methods: N}
    """
    packages = spec.get("packages", []) or []
    if not packages or not env_name:
        return {"patched_versions": [], "filled_homepages": [], "filled_install_methods": 0}

    conda_versions = env_manager.list_conda_packages(env_name)
    patched_versions: list[dict] = []
    filled_homepages: list[str] = []
    filled_install_methods = 0

    for p in packages:
        name = p.get("name", "")
        if not name:
            continue

        # 1. Default install_method to conda when unset (most common case)
        im = p.get("install_method")
        if not isinstance(im, dict) or not im.get("type"):
            p["install_method"] = {"type": "conda"}
            im = p["install_method"]
            filled_install_methods += 1

        im_type = im.get("type", "")

        # 2. Reconcile resolved_version against the source of truth for this
        #    install path. conda list for conda packages; packageVersion() for
        #    r_install packages — that's the number users see at library() time.
        before = p.get("resolved_version")
        actual = ""
        if im_type == "conda":
            actual = conda_versions.get(name, "")
        elif im_type == "r_install":
            actual = env_manager.r_package_version(env_name, name)

        if actual and actual != before:
            p["resolved_version"] = actual
            patched_versions.append({"name": name, "from": before, "to": actual})

        # 2b. For R packages installed as conda (r-*, bioconductor-*), the
        #     recipe version and packageVersion() differ. Capture both:
        #     resolved_version stays as the conda truth (for reproducibility),
        #     runtime_version exposes the R-side number users see at library() time.
        is_r_conda = im_type == "conda" and (
            name.startswith("r-") or name.startswith("bioconductor-")
        )
        if is_r_conda and not p.get("runtime_version"):
            r_pkg = name.split("-", 1)[1] if "-" in name else name
            rt = env_manager.r_package_version(env_name, r_pkg)
            if rt and rt != p.get("resolved_version"):
                p["runtime_version"] = rt

        # 3. Fill missing homepage from (channel, name, source) — deterministic
        if not p.get("homepage"):
            hp = _derive_homepage(name, p.get("channel", ""), im)
            if hp:
                p["homepage"] = hp
                filled_homepages.append(name)

    return {
        "patched_versions":       patched_versions,
        "filled_homepages":       filled_homepages,
        "filled_install_methods": filled_install_methods,
    }


_GITHUB_OWNER_REPO_RE = re.compile(
    r"(?:https?://(?:www\.)?github\.com/|git@github\.com:|['\"])"
    r"(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?(?:/|['\"]|$)"
)


def _derive_homepage(name: str, channel: str, install_method: dict) -> str:
    """Return a canonical upstream URL given (name, channel, install_method).

    Fully software-driven — no hardcoded per-tool URL table. Each branch is a
    derivation rule keyed off (channel, install_method.source). Returns "" only
    when there's genuinely no signal to derive from.

    Order of preference:
      1. install_method.source contains a github.com URL (any install type) →
         strip to owner/repo → https://github.com/{owner}/{repo}
      2. Channel-direct mappings (cran / bioconductor / pypi / bioconda / conda-forge / defaults)
      3. Name-prefix conventions for conda packages (r-* → CRAN, bioconductor-* → Bioconductor)
      4. external/local channels with a usable source URL → the URL's origin
    """
    ch = (channel or "").lower()
    source  = (install_method or {}).get("source", "") or ""

    # 1. GitHub URL in source — works for r_install, jar downloads, source builds, etc.
    if "github.com" in source:
        m = _GITHUB_OWNER_REPO_RE.search(source)
        if m:
            return f"https://github.com/{m.group('owner')}/{m.group('repo')}"

    # 2. Channel-direct mappings.
    if ch == "cran":
        return f"https://CRAN.R-project.org/package={name}"
    if ch == "bioconductor":
        return f"https://bioconductor.org/packages/{name}/"
    if ch in ("pypi", "pip"):
        return f"https://pypi.org/project/{name}/"
    if ch == "bioconda":
        return f"https://bioconda.github.io/recipes/{name}/README.html"
    if ch in ("conda-forge",):
        return f"https://anaconda.org/conda-forge/{name}"
    if ch in ("defaults", "anaconda", "main"):
        return f"https://anaconda.org/anaconda/{name}"

    # 3. Name-prefix conventions (conda-installed CRAN/Bioc packages whose channel
    # got recorded as just `bioconda` or `conda-forge` — still resolves to the
    # upstream CRAN/Bioc landing page, which is what users actually want).
    if name.startswith("r-"):
        return f"https://CRAN.R-project.org/package={name[2:]}"
    if name.startswith("bioconductor-"):
        return f"https://bioconductor.org/packages/{name[len('bioconductor-'):]}/"

    # 4. github channel without an extractable URL: best-effort guess from name.
    # NOTE: this is a fallback, not a hardcoded mapping.
    if ch == "github" and "/" in name:
        return f"https://github.com/{name}"

    # 5. External/local channels with a non-github source URL — fall back to the
    # origin so the agent at least surfaces *some* link rather than nothing.
    if ch in ("external", "local") and source.startswith(("http://", "https://")):
        try:
            from urllib.parse import urlparse
            parsed = urlparse(source)
            return f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass

    return ""


# ---------------------------------------------------------------------------
# Provenance persistence
# ---------------------------------------------------------------------------

_DB_ALIASES = {"SRA": "EBI_SRA", "NCBI": "NCBI_SRA", "EBI": "EBI_SRA"}


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
