"""
run_production — the locus-agnostic PRODUCTION run.

ONE verb, `run_production_pipeline(project, env, ...)`, runs a frozen env's
workflow against the user's project data and DOCUMENTS the run (a manifest) —
it does not seal a WorkflowSpec (that's the validation verbs' job). It dispatches
on the compute env's `type`:

  local → render a re-runnable `run.sh` (`docker run` the frozen image against
          `workflow_dir`, same-path bind mounts), launch it in the background via
          the JobManager, write a manifest. No scheduler — the laptop is the compute.
  ssh   → delegate to `submit_workflow_job` (nextflow + slurm + apptainer),
          sourcing the module names + slurm policy from the ENV config and
          deriving the staged `.sif` path from `freeze_request_key`. Refuses if
          the `.sif` isn't staged (non-composite — stage_apptainer_image first).

Plug-and-play: the SAME call runs on either locus — swap `compute_env_name`. What
differs (scheduler, container runtime, module names) is an env property, never a
call arg; `resources` (mem/cpus/time) is the one uniform per-run knob, honored
per-locus (docker --memory/--cpus locally; #SBATCH on the cluster). Completes the
run grid: validate = run_step_in_container / run_step_on_cluster; production =
run_production_pipeline (local / ssh).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from agent.skills import compute_access, stage_apptainer, submit_workflow
from agent.skills.outcomes import proven, refused, broke


# ${name} — the SAME placeholder syntax workflow_render uses, so one command
# string is valid on both loci (the local path can't invent a `{name}` dialect).
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ---------------------------------------------------------------------------
# Local rendering — the "simple shell script"
# ---------------------------------------------------------------------------

def _render_local_command(command: str,
                          inputs: Mapping[str, str],
                          outputs: Mapping[str, str]) -> str:
    """Substitute every ${PLACEHOLDER} with its shell-quoted value, held to the
    exact contract the cluster path enforces: every placeholder declared (I6),
    inputs ABSOLUTE, outputs BARE filenames written into workflow_dir."""
    subs = {**dict(inputs or {}), **dict(outputs or {})}

    undeclared = sorted(p for p in set(_PLACEHOLDER_RE.findall(command or ""))
                        if p not in subs)
    if undeclared:
        raise ValueError(
            f"command has ${{PLACEHOLDER}}(s) not declared in inputs/outputs: "
            f"{undeclared}")
    for key, val in (inputs or {}).items():
        if not isinstance(val, str) or not val.startswith("/"):
            raise ValueError(
                f"inputs[{key!r}] must be an absolute path (got {val!r})")
    for key, val in (outputs or {}).items():
        if (not isinstance(val, str) or not val or "/" in val
                or ".." in val.split("/")):
            raise ValueError(
                f"outputs[{key!r}] must be a bare filename written into "
                f"workflow_dir (got {val!r})")

    return _PLACEHOLDER_RE.sub(lambda m: shlex.quote(subs[m.group(1)]), command)


def _docker_resource_flags(resources: Mapping) -> list[str]:
    """The docker-run limits a laptop can enforce from `resources`. `time` has
    no docker analog and is skipped (a local run isn't wall-clock killed)."""
    flags: list[str] = []
    if resources.get("mem_gb"):
        flags += ["--memory", f"{resources['mem_gb']}g"]
    if resources.get("cpus"):
        flags += ["--cpus", str(resources["cpus"])]
    return flags


def _render_run_script(*, image: str, workdir: str, platform: str,
                       concrete_command: str, resources: Mapping,
                       mounts: list) -> str:
    """The re-runnable local production script (the local analog of launcher.sh).
    Each dir in `mounts` is bind-mounted same-path so absolute paths resolve
    verbatim: workflow_dir, plus the parent of any input outside it."""
    res_flags = _docker_resource_flags(resources or {})
    res_line = ("  " + " ".join(shlex.quote(f) for f in res_flags) + " \\\n"
                if res_flags else "")
    mount_lines = "".join(
        f"  -v {shlex.quote(m)}:{shlex.quote(m)} \\\n" for m in mounts)
    return (
        "#!/usr/bin/env bash\n"
        "# run_production_pipeline (local) — runs the frozen env image here.\n"
        "# Re-run:  bash <this script>\n"
        "set -euo pipefail\n"
        "\n"
        f"IMAGE={shlex.quote(image)}\n"
        f"WORKDIR={shlex.quote(workdir)}\n"
        f"PLATFORM={shlex.quote(platform)}\n"
        "\n"
        "# Pull the image if absent (an adopted biocontainer is by-digest).\n"
        'docker image inspect "$IMAGE" >/dev/null 2>&1 || '
        'docker pull --platform "$PLATFORM" "$IMAGE"\n'
        "\n"
        'docker run --rm --platform "$PLATFORM" \\\n'
        f"{res_line}"
        f"{mount_lines}"
        '  -w "$WORKDIR" \\\n'
        '  "$IMAGE" \\\n'
        f"  bash -c {shlex.quote(concrete_command)}\n"
    )


def _short_stamp(iso: str) -> str:
    """Filesystem-safe run stamp from an ISO timestamp (supplied by the caller)."""
    return re.sub(r"[^0-9]", "", iso)[:14] or "run"


# ---------------------------------------------------------------------------
# Local locus — render run.sh, launch in the background, document
# ---------------------------------------------------------------------------

def _run_local(*, project: dict, project_name: str, compute_env_name: str,
               record: dict, freeze_request_key: str, workflow_name: str,
               tool_name: str, command: str, inputs: Mapping[str, str],
               outputs: Mapping[str, str], workflow_dir: str,
               resources: Mapping, platform: str,
               _job_manager, _docker_available, _daemon_is_remote) -> dict:
    image = record.get("image")
    if not image:
        return refused("run_production.no_image_handle",
            error=f"freeze record for {freeze_request_key!r} has no image handle")

    # Docker preflight — the background run.sh does `docker run`; fail loud here.
    docker_refusal = _docker_available()
    if docker_refusal:
        return docker_refusal
    if _daemon_is_remote():
        return refused("run_production.remote_daemon",
            error="run_production_pipeline (local) bind-mounts local paths, but the "
            "active Docker daemon is REMOTE (DOCKER_HOST) and can't see them.")

    # workflow_dir must exist (no auto-mkdir) and be authorized upload+exec —
    # the same production wall as the cluster.
    normed_dir = submit_workflow._validate_workflow_dir(workflow_dir)
    if not Path(normed_dir).is_dir():
        return refused("run_production.workflow_dir_missing",
            error=f"workflow_dir {normed_dir!r} does not exist — create it first "
            f"(the agent does not mkdir in your territory).")
    compute_access.check_permission(project, compute_env_name, normed_dir, "upload")
    compute_access.check_permission(project, compute_env_name, normed_dir,
                                    "run_production_pipeline")

    concrete_command = _render_local_command(command, inputs, outputs)
    mounts = [normed_dir]
    for val in (inputs or {}).values():
        parent = os.path.normpath(str(Path(str(val)).parent))
        if (parent != normed_dir and not parent.startswith(normed_dir + os.sep)
                and parent not in mounts):
            mounts.append(parent)
    script = _render_run_script(image=image, workdir=normed_dir, platform=platform,
                                concrete_command=concrete_command,
                                resources=resources, mounts=mounts)
    run_sh = Path(normed_dir) / f"{workflow_name}.run.sh"
    run_sh.write_text(script)
    run_sh.chmod(0o755)

    # Background launch — a real production run outlives the stream watchdog, so
    # we submit-and-document like the cluster path.
    started = datetime.now(timezone.utc).isoformat()
    job_id = f"prod_{workflow_name}_{_short_stamp(started)}"
    launch = _job_manager.start(command=f"bash {shlex.quote(str(run_sh))}",
                                job_id=job_id, working_dir=normed_dir)
    if "error" in launch:
        return {**launch, "run_script": str(run_sh)}
    job_id = launch.get("job_id", job_id)

    manifest = {
        "locus": "local", "project_name": project_name,
        "compute_env": compute_env_name, "workflow_name": workflow_name,
        "tool_name": tool_name, "job_id": job_id, "workflow_dir": normed_dir,
        "command": command, "concrete_command": concrete_command,
        "inputs": dict(inputs), "outputs": dict(outputs), "image": image,
        "image_digest": record.get("image_digest"),
        "freeze_request_key": freeze_request_key, "resources": dict(resources or {}),
        "run_script": str(run_sh), "submitted_at": started,
        "follow_up": {
            "poll": f"call check_job(job_id={job_id!r})",
            "outputs": "the declared outputs land under workflow_dir",
            "rerun": f"bash {run_sh}",
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


# ---------------------------------------------------------------------------
# Cluster locus — source specifics from the env, delegate to the proven path
# ---------------------------------------------------------------------------

def _run_cluster(*, project_name: str, compute_env_name: str, env: dict,
                 record: dict, freeze_request_key: str, workflow_name: str,
                 tool_name: str, command: str, inputs: Mapping[str, str],
                 outputs: Mapping[str, str], workflow_dir: str,
                 resources: Mapping, access_path: Optional[str],
                 timeout: int) -> dict:
    # Cluster specifics come from the ENV config, never the call — that's what
    # makes the env swappable. Refuse (don't invent) if absent.
    modules = compute_access.get_container_modules(env)
    missing_mods = [m for m in ("apptainer_module", "nextflow_module")
                    if not modules.get(m)]
    if missing_mods:
        return refused("run_production.env_missing_modules",
            error=f"compute env {compute_env_name!r} declares no {missing_mods} — "
            f"add them to this env in projects_access.yaml "
            f"(e.g. apptainer/1.5.0, nextflow/25.04.7).")

    # A scheduler needs a resource request; a laptop doesn't.
    slurm = _resources_to_slurm(resources or {})
    if not (slurm.get("mem") and slurm.get("time")):
        return refused("run_production.resources_required",
            error="a cluster production run needs `resources` with at least "
            "mem_gb and time (e.g. {'mem_gb': 8, 'time': '02:00:00', 'cpus': 4}).")

    # Derive the staged .sif path (the SAME path stage_apptainer_image writes).
    # Non-composite: require it's already staged, don't stage here.
    ct_path = ((env.get("container_upload_target") or {}).get("path") or "").rstrip("/")
    if not ct_path:
        return refused("run_production.no_container_target",
            error=f"env {compute_env_name!r} has no container_upload_target — "
            f"declare one and stage_apptainer_image first.")
    env_name_for_sif = record.get("name") or freeze_request_key.split("|", 1)[0]
    content_digest = record.get("content_digest") or record.get("image_digest", "")
    sif_remote_abs = (f"{ct_path}/{env_name_for_sif}_"
                      f"{stage_apptainer._short_digest(content_digest)}.sif")
    if not stage_apptainer._remote_sif_exists(env, sif_remote_abs, timeout=min(timeout, 120)):
        return refused("run_production.sif_not_staged",
            error=f"the frozen env's .sif is not staged on {compute_env_name!r} at "
            f"{sif_remote_abs!r}. Call stage_apptainer_image(project, env, "
            f"freeze_request_key={freeze_request_key!r}) first, then re-run.",
            expected_sif=sif_remote_abs)

    return submit_workflow.submit_workflow_job(
        project_name=project_name, compute_env_name=compute_env_name,
        workflow_dir=workflow_dir, workflow_name=workflow_name,
        tool_name=tool_name, command=command, inputs=inputs, outputs=outputs,
        apptainer_sif=sif_remote_abs,
        apptainer_module=modules["apptainer_module"],
        nextflow_module=modules["nextflow_module"],
        slurm=slurm, access_path=access_path, timeout=timeout)


def _resources_to_slurm(resources: Mapping) -> dict:
    """Map the neutral `resources` knob to the slurm SIZING vocabulary
    submit_workflow expects (env policy is merged separately)."""
    out: dict = {}
    if resources.get("mem_gb"):
        out["mem"] = f"{resources['mem_gb']}g"
    if resources.get("time"):
        out["time"] = resources["time"]
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
    """Run a frozen env's workflow in PRODUCTION on whichever env is named —
    local laptop or ssh cluster. Same call, swap the env.

    command: ${PLACEHOLDER} slots. inputs: {NAME: absolute_path}. outputs:
      {NAME: bare_filename} (lands in workflow_dir). Same contract both loci.
    workflow_dir: a directories[] path with both `upload` and `exec`.
    resources: {mem_gb, cpus, time, gpus?} — optional locally, REQUIRED on the
      cluster (a SLURM job must declare mem + time).
    """
    try:
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        # Consume the frozen env BY its Layer-1 contract — the serving question,
        # so a production run can't ship an env whose contract no longer holds.
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
            from agent import mcp_server as _ms
            return _run_local(
                project=project, project_name=project_name,
                compute_env_name=compute_env_name, record=record,
                freeze_request_key=freeze_request_key, workflow_name=workflow_name,
                tool_name=tool_name, command=command, inputs=inputs, outputs=outputs,
                workflow_dir=workflow_dir, resources=resources or {}, platform=platform,
                _job_manager=_job_manager or _ms._job_manager,
                _docker_available=_ms._check_docker_available,
                _daemon_is_remote=_ms._locus.daemon_is_remote)
        if env_type == "ssh":
            return _run_cluster(
                project_name=project_name, compute_env_name=compute_env_name,
                env=env, record=record, freeze_request_key=freeze_request_key,
                workflow_name=workflow_name, tool_name=tool_name, command=command,
                inputs=inputs, outputs=outputs, workflow_dir=workflow_dir,
                resources=resources or {}, access_path=access_path, timeout=timeout)
        return refused("run_production.unknown_env_type",
            error=f"compute env {compute_env_name!r} has type={env_type!r}; "
            f"run_production_pipeline supports 'local' and 'ssh'")

    except (ValueError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return broke("run_production.failed", error=f"{type(e).__name__}: {e}")
    except subprocess.TimeoutExpired as e:
        return broke("run_production.timeout",
                     error=f"a remote probe timed out after {e.timeout}s")
