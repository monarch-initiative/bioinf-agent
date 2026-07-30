"""
run_production — the locus-agnostic PRODUCTION run (the "missing verb").

ONE verb, `run_production_pipeline(project, env, ...)`, that runs a frozen env's
workflow against the USER'S OWN project data (a `directories[]` path), documented
like a production run — submit-and-document, no seal. It dispatches on the
compute env's `type`:

  • type == "local"  → render a re-runnable `run.sh` that `docker run`s the
    frozen env image against `workflow_dir` (same-path bind mount, so the
    absolute paths in `command` work verbatim), launch it in the BACKGROUND via
    the JobManager, and write a submission manifest. No scheduler — the laptop
    IS the compute. This is the "simple shell script" local path.

  • type == "ssh"    → delegate to `submit_workflow.submit_workflow_job` — the
    proven nextflow + slurm + apptainer path — sourcing the cluster specifics
    (apptainer/nextflow module names + slurm policy) FROM THE ENV CONFIG, and
    deriving the staged `.sif` path from `freeze_request_key`. Non-composite: it
    REFUSES if the `.sif` isn't already staged (call `stage_apptainer_image`
    first), exactly like `submit_workflow_job` doesn't freeze or stage for you.

Plug-and-play: the SAME call runs on either locus — you swap `compute_env_name`
(and the project must grant that env via `compute_envs`). What legitimately
differs between loci — the scheduler, the container runtime, the module names —
is an ENV PROPERTY read from projects_access.yaml, never a per-call argument.
That is the whole point: a compute env is a swappable RESOURCE, not a different
mode of the tool. `resources` (mem/cpus/time) is the one uniform per-run knob;
each locus honors it its own way (docker --memory/--cpus locally; #SBATCH on the
cluster).

This is the PRODUCTION sibling of the two validation verbs, completing the grid:

              LOCAL                      CLUSTER
  validate    run_step_in_container      run_step_on_cluster
  production  run_production_pipeline (local)  run_production_pipeline (ssh)

Layer boundary: a production run executes the user's real data and DOCUMENTS the
run (a manifest, findable later); it does NOT record a validated `pipeline_step`
or seal a `WorkflowSpec` — that is the validation verbs' job. It writes the same
submit-and-document manifest `submit_workflow_job` does.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from agent.skills import compute_access, stage_apptainer, submit_workflow
from agent.skills.outcomes import proven, refused, broke


# A {PLACEHOLDER} slot in the command template — same shape the Nextflow
# renderer recognizes, so a command written for the cluster runs locally too.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ---------------------------------------------------------------------------
# Local rendering — the "simple shell script"
# ---------------------------------------------------------------------------

def _render_local_command(command: str,
                          inputs: Mapping[str, str],
                          outputs: Mapping[str, str]) -> str:
    """Substitute every {PLACEHOLDER} in `command` with the concrete path from
    inputs∪outputs, shell-quoted. Enforces the same discipline I6 enforces on
    the cluster: every placeholder must be declared, and every declared path
    must be absolute. Raises ValueError on a violation — the local path is held
    to the same contract as the cluster path, not a looser one."""
    subs = {**dict(inputs or {}), **dict(outputs or {})}

    placeholders = set(_PLACEHOLDER_RE.findall(command or ""))
    undeclared = sorted(p for p in placeholders if p not in subs)
    if undeclared:
        raise ValueError(
            f"command has {{PLACEHOLDER}}(s) not declared in inputs/outputs: "
            f"{undeclared} — declare each in inputs or outputs (same rule I6 "
            f"holds on the cluster)")

    for key, val in subs.items():
        if not isinstance(val, str) or not val.startswith("/"):
            raise ValueError(
                f"inputs/outputs[{key!r}] must be an absolute path (got "
                f"{val!r}) — a production run binds a real project path")

    def _sub(m: re.Match) -> str:
        return shlex.quote(subs[m.group(1)])

    return _PLACEHOLDER_RE.sub(_sub, command)


def _docker_resource_flags(resources: Mapping) -> list[str]:
    """Map the uniform `resources` knob to docker run limits. Local honors what
    a laptop can enforce — memory + cpus; a scheduler-only field like `time`
    has no docker analog and is silently not applied (the run isn't killed on a
    wall clock locally)."""
    flags: list[str] = []
    mem_gb = resources.get("mem_gb")
    if mem_gb:
        flags += ["--memory", f"{mem_gb}g"]
    cpus = resources.get("cpus")
    if cpus:
        flags += ["--cpus", str(cpus)]
    return flags


def _render_run_script(*, image: str, workdir: str, platform: str,
                       concrete_command: str, resources: Mapping) -> str:
    """The re-runnable local production script. A human reads it, sees exactly
    which image runs which command against which dir, and can re-run it with
    `bash run.sh` — the local analog of the cluster launcher.sh."""
    res_flags = _docker_resource_flags(resources or {})
    res_line = ("  " + " ".join(shlex.quote(f) for f in res_flags) + " \\\n"
                if res_flags else "")
    return (
        "#!/usr/bin/env bash\n"
        "# Generated by bioinf_agent :: run_production_pipeline (local locus).\n"
        "# Runs the frozen env image against this project directory.\n"
        "# Re-runnable:  bash run.sh\n"
        "# The image is the exact artifact `freeze` produced — the same bytes\n"
        "# you would ship to a cluster (validated == shipped).\n"
        "set -euo pipefail\n"
        "\n"
        f"IMAGE={shlex.quote(image)}\n"
        f"WORKDIR={shlex.quote(workdir)}\n"
        f"PLATFORM={shlex.quote(platform)}\n"
        "\n"
        "# Pull the image if it isn't present locally (an adopted biocontainer\n"
        "# is referenced by digest and may not be on disk yet).\n"
        'docker image inspect "$IMAGE" >/dev/null 2>&1 || '
        'docker pull --platform "$PLATFORM" "$IMAGE"\n'
        "\n"
        'docker run --rm --platform "$PLATFORM" \\\n'
        f"{res_line}"
        '  -v "$WORKDIR":"$WORKDIR" \\\n'
        '  -w "$WORKDIR" \\\n'
        '  "$IMAGE" \\\n'
        f"  bash -c {shlex.quote(concrete_command)}\n"
    )


# ---------------------------------------------------------------------------
# Local locus — render run.sh, launch in the background, document
# ---------------------------------------------------------------------------

def _run_local(*, project: dict, project_name: str, compute_env_name: str,
               env: dict, record: dict, freeze_request_key: str,
               workflow_name: str, tool_name: str, command: str,
               inputs: Mapping[str, str], outputs: Mapping[str, str],
               workflow_dir: str, resources: Mapping, platform: str,
               access_path: Optional[str],
               _job_manager, _docker_available, _daemon_is_remote) -> dict:
    image = record.get("image")
    if not image:
        return refused("run_production.no_image_handle",
            error=f"freeze record for {freeze_request_key!r} has no image handle")

    # Docker preflight — the background run.sh does `docker run`; a dead or
    # remote daemon must fail LOUD here, not opaquely inside the job.
    docker_refusal = _docker_available()
    if docker_refusal:
        return docker_refusal
    if _daemon_is_remote():
        return refused("run_production.remote_daemon",
            error="run_production_pipeline (local) bind-mounts the local project "
            "dir, but the active Docker daemon is REMOTE (DOCKER_HOST) and can't "
            "see local paths. Use a local daemon, or run this on the cluster env.")

    # workflow_dir must exist (no auto-mkdir in the user's territory) and be
    # authorized with BOTH upload (to write run.sh) AND exec (the container
    # writes its outputs there) — the same production wall as the cluster.
    normed_dir = submit_workflow._validate_workflow_dir(workflow_dir)
    if not Path(normed_dir).is_dir():
        return refused("run_production.workflow_dir_missing",
            error=f"workflow_dir {normed_dir!r} does not exist. Create the project "
            f"directory first — the agent does not mkdir in your territory.")
    compute_access.check_permission(project, compute_env_name, normed_dir, "upload")
    compute_access.check_permission(project, compute_env_name, normed_dir,
                                    "run_production_pipeline")

    # Render the concrete command + the re-runnable run.sh, write it into the
    # project dir (auth checked above).
    concrete_command = _render_local_command(command, inputs, outputs)
    script = _render_run_script(image=image, workdir=normed_dir, platform=platform,
                                concrete_command=concrete_command, resources=resources)
    run_sh = Path(normed_dir) / f"{workflow_name}.run.sh"
    run_sh.write_text(script)
    run_sh.chmod(0o755)

    # Launch in the background — a real production run outlives the agent's
    # stream watchdog, so we submit-and-document exactly like the cluster path.
    started = datetime.now(timezone.utc).isoformat()
    job_id = f"prod_{workflow_name}_{_short_stamp(started)}"
    launch = _job_manager.start(
        command=f"bash {shlex.quote(str(run_sh))}",
        job_id=job_id, working_dir=normed_dir)
    if "error" in launch:
        return {**launch, "run_script": str(run_sh)}
    job_id = launch.get("job_id", job_id)

    manifest = {
        "locus":            "local",
        "project_name":     project_name,
        "compute_env":      compute_env_name,
        "workflow_name":    workflow_name,
        "tool_name":        tool_name,
        "job_id":           job_id,
        "workflow_dir":     normed_dir,
        "command":          command,
        "concrete_command": concrete_command,
        "inputs":           dict(inputs),
        "outputs":          dict(outputs),
        "image":            image,
        "image_digest":     record.get("image_digest"),
        "freeze_request_key": freeze_request_key,
        "resources":        dict(resources or {}),
        "run_script":       str(run_sh),
        "submitted_at":     started,
        "follow_up": {
            "poll":    f"call check_job(job_id={job_id!r})",
            "outputs": "the declared outputs land under workflow_dir",
            "rerun":   f"bash {run_sh}",
        },
    }
    manifest_path = submit_workflow._write_submission_manifest(
        project_name=project_name, workflow_name=workflow_name,
        job_id=job_id, manifest=manifest)

    return proven("run_production.local_launched", success=True,
        locus="local", compute_env=compute_env_name, job_id=job_id,
        workflow_dir=normed_dir, run_script=str(run_sh),
        image=image, submitted_at=started, manifest_path=manifest_path,
        follow_up=manifest["follow_up"])


def _short_stamp(iso: str) -> str:
    """A filesystem-safe run stamp from an ISO timestamp (no Date.now() here —
    the caller supplies the timestamp)."""
    return re.sub(r"[^0-9]", "", iso)[:14] or "run"


# ---------------------------------------------------------------------------
# Cluster locus — source specifics from the env, delegate to the proven path
# ---------------------------------------------------------------------------

def _run_cluster(*, project_name: str, compute_env_name: str, env: dict,
                 record: dict, freeze_request_key: str, workflow_name: str,
                 tool_name: str, command: str, inputs: Mapping[str, str],
                 outputs: Mapping[str, str], workflow_dir: str,
                 resources: Mapping, platform: str,
                 access_path: Optional[str], timeout: int) -> dict:
    # Cluster specifics come from the ENV config, never the call — that's what
    # makes the env swappable. Refuse clearly (don't invent) if absent.
    modules = compute_access.get_container_modules(env)
    missing_mods = [m for m in ("apptainer_module", "nextflow_module")
                    if not modules.get(m)]
    if missing_mods:
        return refused("run_production.env_missing_modules",
            error=f"compute env {compute_env_name!r} declares no {missing_mods} — "
            f"a cluster production run loads apptainer + nextflow as Lmod modules. "
            f"Add `apptainer_module:` and `nextflow_module:` to this env in "
            f"projects_access.yaml (e.g. apptainer/1.5.0, nextflow/25.04.7).")

    # A scheduler needs a resource request; a laptop doesn't. This is a real
    # per-locus requirement, not a hidden mode — name it plainly.
    slurm = _resources_to_slurm(resources or {})
    if not (slurm.get("mem") and slurm.get("time")):
        return refused("run_production.resources_required",
            error="a cluster production run needs `resources` with at least "
            "mem_gb and time (e.g. {'mem_gb': 8, 'time': '02:00:00', 'cpus': 4}) — "
            "the SLURM job must declare what it asks for.")

    # Derive the staged .sif path from the freeze record (the SAME path
    # stage_apptainer_image writes to). Non-composite: we do NOT stage here —
    # we require it's already staged, and say so if not.
    ct = env.get("container_upload_target") or {}
    ct_path = (ct.get("path") or "").rstrip("/")
    if not ct_path:
        return refused("run_production.no_container_target",
            error=f"env {compute_env_name!r} has no container_upload_target — "
            f"cannot locate a staged .sif. Declare one and stage_apptainer_image first.")
    env_name_for_sif = record.get("name") or freeze_request_key.split("|", 1)[0]
    content_digest = record.get("content_digest") or record.get("image_digest", "")
    sif_name = f"{env_name_for_sif}_{stage_apptainer._short_digest(content_digest)}.sif"
    sif_remote_abs = f"{ct_path}/{sif_name}"
    if not stage_apptainer._remote_sif_exists(env, sif_remote_abs, timeout=min(timeout, 120)):
        return refused("run_production.sif_not_staged",
            error=f"the frozen env's .sif is not staged on {compute_env_name!r} at "
            f"{sif_remote_abs!r}. Call stage_apptainer_image(project, env, "
            f"freeze_request_key={freeze_request_key!r}) first, then re-run.",
            expected_sif=sif_remote_abs)

    # Delegate to the proven production path. It renders nextflow+slurm+apptainer,
    # uploads, sbatches, writes the manifest, returns the job_id.
    return submit_workflow.submit_workflow_job(
        project_name=project_name,
        compute_env_name=compute_env_name,
        workflow_dir=workflow_dir,
        workflow_name=workflow_name,
        tool_name=tool_name,
        command=command,
        inputs=inputs,
        outputs=outputs,
        apptainer_sif=sif_remote_abs,
        apptainer_module=modules["apptainer_module"],
        nextflow_module=modules["nextflow_module"],
        slurm=slurm,
        access_path=access_path,
        timeout=timeout,
    )


def _resources_to_slurm(resources: Mapping) -> dict:
    """Map the neutral `resources` knob to the slurm request vocabulary
    submit_workflow expects. mem_gb→'Ng', time passthrough, cpus/gpus/ntasks
    passthrough. Env POLICY (account/partition) is merged separately by
    submit_workflow._resolve_slurm_and_email — this only carries the per-job
    SIZING."""
    out: dict = {}
    mem_gb = resources.get("mem_gb")
    if mem_gb:
        out["mem"] = f"{mem_gb}g"
    for k in ("time",):
        if resources.get(k):
            out[k] = resources[k]
    for k in ("cpus", "gpus", "ntasks"):
        if resources.get(k) is not None:
            out[k] = resources[k]
    return out


# ---------------------------------------------------------------------------
# The verb
# ---------------------------------------------------------------------------

def run_production_pipeline(project_name: str,
                            compute_env_name: str,
                            workflow_name: str,
                            tool_name: str,
                            command: str,
                            inputs: Mapping[str, str],
                            outputs: Mapping[str, str],
                            freeze_request_key: str,
                            workflow_dir: str,
                            *,
                            resources: Optional[Mapping] = None,
                            platform: str = "linux/amd64",
                            access_path: Optional[str] = None,
                            timeout: int = 300,
                            _env_cache=None,
                            _job_manager=None) -> dict:
    """Run a frozen env's workflow in PRODUCTION against the user's project data,
    on WHICHEVER compute env is named — local laptop or ssh cluster. Same call,
    swap the env. See the module docstring for the full contract.

    workflow_dir: a LITERAL absolute path covered by a project `directories[]`
      entry with both `upload` and `exec`. On local it's a laptop path; on the
      cluster it's a path under the project's cluster workspace.
    resources: {mem_gb, cpus, time, gpus?} — the uniform per-run sizing knob.
      Optional locally (a laptop runs unconstrained unless you cap it); REQUIRED
      on the cluster (a SLURM job must declare mem + time).
    """
    try:
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        # Both loci consume the frozen env BY its Layer-1 contract — the serving
        # question, not "is there a record?". A production run of an env whose
        # contract no longer holds would ship an unverified artifact to the user's
        # real data.
        if _env_cache is None:
            from agent import mcp_server as _ms
            _env_cache = _ms._env_cache
        record, violations = _env_cache.lookup_verified(freeze_request_key)
        if violations:
            return refused("run_production.env_contract_violated",
                error=f"the frozen env {freeze_request_key!r} no longer satisfies the "
                f"Layer-1 honesty contract — re-run freeze() to re-earn it",
                honesty_violations=violations, violation_count=len(violations))
        if not record:
            return refused("run_production.no_frozen_env",
                error=f"no frozen env for {freeze_request_key!r} — run freeze() first")

        env_type = env.get("type")
        if env_type == "local":
            if _job_manager is None:
                from agent import mcp_server as _ms
                _job_manager = _ms._job_manager
            from agent import mcp_server as _ms
            return _run_local(
                project=project, project_name=project_name,
                compute_env_name=compute_env_name, env=env, record=record,
                freeze_request_key=freeze_request_key, workflow_name=workflow_name,
                tool_name=tool_name, command=command, inputs=inputs, outputs=outputs,
                workflow_dir=workflow_dir, resources=resources or {},
                platform=platform, access_path=access_path,
                _job_manager=_job_manager,
                _docker_available=_ms._check_docker_available,
                _daemon_is_remote=_ms._locus.daemon_is_remote)
        if env_type == "ssh":
            return _run_cluster(
                project_name=project_name, compute_env_name=compute_env_name,
                env=env, record=record, freeze_request_key=freeze_request_key,
                workflow_name=workflow_name, tool_name=tool_name, command=command,
                inputs=inputs, outputs=outputs, workflow_dir=workflow_dir,
                resources=resources or {}, platform=platform,
                access_path=access_path, timeout=timeout)
        return refused("run_production.unknown_env_type",
            error=f"compute env {compute_env_name!r} has type={env_type!r}; "
            f"run_production_pipeline supports 'local' and 'ssh'")

    except (ValueError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return broke("run_production.failed", error=f"{type(e).__name__}: {e}")
    except subprocess.TimeoutExpired as e:
        return broke("run_production.timeout",
                     error=f"a remote probe timed out after {e.timeout}s")
