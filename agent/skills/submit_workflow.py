"""
submit_workflow_job — render → upload → sbatch the workflow on a
compute env. The PRODUCTION submission primitive. Takes a single-tool
spec and returns a SLURM job_id + a local submission manifest the
user can consult later (the agent may be cut off before the job
finishes — long-running jobs are not polled by this primitive).

What this is (and isn't)
------------------------
This is NOT a composite primitive. The caller still:
  - freezes the tool's env separately (`freeze`)
  - stages the .sif separately (`stage_apptainer_image`)
  - polls the job separately, AT THEIR OWN PACE (`cluster_job_status`)
  - downloads the outputs separately (`transfer.download`)

submit_workflow_job is the *submission* step: the irreducible
sequence of (render the per-project Nextflow files, upload them into
the workspace, kick off sbatch, document what was submitted).
Splitting these out would force the caller into a brittle 3-call
dance for one logical action.

This primitive is for PRODUCTION RUNS — runs that land in user-
declared project workspace paths. The wall: `workflow_dir` MUST be
covered by a `directories[]` entry on the project with both `upload`
and `exec`. Validation/seal runs do NOT use this primitive — they go
through `run_step_on_cluster`, which uses the agent's scratch sandbox
with env-level auth.

Authorization
-------------
Project must have a `compute_env_access` entry for `compute_env_name`,
AND a `directories[]` entry under that access whose path contains
`workflow_dir` (longest-prefix match) with `permissions:` including
BOTH `upload` (to write the rendered files) AND `exec` (so the
running SLURM job may write its own outputs alongside them).

The workflow_dir is supplied as a LITERAL absolute path, not
auto-prefixed. The caller decides the per-run subdir (the no-overwrite
contract on `transfer.upload` means a second submit to the same
workflow_dir would fail — that's the desired behavior).

Submission manifest
-------------------
On success, writes a local manifest:
    job_submissions/<project_name>/<workflow_name>_<job_id>.submission.json

The manifest records: job_id, workflow_dir, compute_env, tool_name,
command, inputs/outputs maps, apptainer_sif (by path on cluster),
slurm config, files_uploaded[], submitted_at. The user (or a later
agent invocation) can grep job_submissions/ to find a job by name or
by id without remembering the terminal output.

sbatch parsing
--------------
We use `sbatch --parsable` which returns just the job_id (or
`<id>;<cluster>` on a federation). We split on `;`, validate the
first token as digits, return it. Anything else surfaces as
`{"error": "...", "sbatch_stdout": "..."}` with the raw output so
the caller can diagnose.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from agent.skills import compute_access, transfer, workflow_render
from agent.skills.outcomes import proven, refused, broke
from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint


# A SLURM job_id as parsed from `sbatch --parsable`: digits, length-capped.
_JOB_ID_RE = re.compile(r"^\d{1,12}$")


# Local manifest root. submit_workflow_job writes one
# job_submissions/<project>/<workflow_name>_<job_id>.submission.json per
# successful submission so the user can find the job later by name or id.
_MANIFEST_ROOT = "job_submissions"


# Where the rendered workflow files are staged locally before upload. MUST live
# under a Globus-accessible location: Globus Connect Personal only scans its
# Accessible Folders (default $HOME) and REFUSES a system temp dir like macOS's
# /var/folders (which tempfile.TemporaryDirectory() defaults to) — that surfaced
# as a live `submit.upload_failed` on the first production run. The repo sits
# under $HOME, so a repo-local staging dir works for BOTH transports (scp doesn't
# care where the source is). Mirrors run_cluster_step._RENDER_STAGE_DIR.
_RENDER_STAGE_DIR = Path(__file__).resolve().parents[2] / "data" / "submit_render_staging"


def _validate_workflow_dir(workflow_dir: str) -> str:
    """The workflow_dir is an absolute path. Same shape rules as
    `transfer.upload`'s remote_abs_path — no `..`, no shell
    metacharacters. Normalized via os.path.normpath."""
    if not isinstance(workflow_dir, str) or not workflow_dir:
        raise ValueError("workflow_dir must be a non-empty string")
    if not workflow_dir.startswith("/"):
        raise ValueError(
            f"workflow_dir must be absolute (start with '/'), got "
            f"{workflow_dir!r}")
    if any(part == ".." for part in workflow_dir.split("/")):
        raise ValueError(
            f"workflow_dir has a '..' traversal component: {workflow_dir!r}")
    # Forbid shell metacharacters in case the path ever gets
    # interpolated into a shell line (sbatch's `cd <dir>` etc.).
    for c in workflow_dir:
        if c.isspace() or c in {";", "&", "|", "$", "`", "<", ">",
                                "(", ")", "{", "}", "*", "?", "[", "]",
                                "!", "\\", "'", "\""}:
            raise ValueError(
                f"workflow_dir contains forbidden character {c!r}: "
                f"{workflow_dir!r}")
    return os.path.normpath(workflow_dir)


def _parse_sbatch_parsable(stdout: str) -> Optional[str]:
    """sbatch --parsable returns `<job_id>` or `<job_id>;<cluster_name>`
    on stdout, nothing else. Strip whitespace, take what's before `;`,
    validate as digits. Returns None if the shape doesn't match — the
    caller surfaces it as an error with the raw stdout for debugging.
    """
    if not isinstance(stdout, str):
        return None
    head = stdout.strip().splitlines()[0] if stdout.strip() else ""
    if not head:
        return None
    token = head.split(";", 1)[0].strip()
    return token if _JOB_ID_RE.match(token) else None


_RENDERED_FILES = ("main.nf", "nextflow.config", "launcher.sh")


def _resolve_slurm_and_email(per_job_slurm: Mapping, env: Mapping) -> tuple[dict, str]:
    """Merge the per-job resource request with the env's slurm POLICY into the
    final render spec, and pull the notification email from the env.

    The agent's per-job dict carries resource SIZING (time/mem/cpus/ntasks/gpus);
    the HPC's constants (account, default partition, GPU convention) live in the
    env `slurm:` block and are merged here so a header follows the cluster's rules:
      - GPU (gpus>0): partition + qos come from env.slurm.gpu — the HPC's GPU
        convention. Refuse if the env declares none (GPU not configured here).
      - CPU (gpus==0): default partition from env.slurm.partition if the job set
        none; qos dropped (it's GPU-only in our convention).
      - account: from env.slurm.account unless the job set one explicitly.
    Returns (merged_slurm, email)."""
    sl = compute_access.get_slurm_config(env) or {}
    merged = dict(per_job_slurm or {})
    try:
        gpus = int(merged.get("gpus", 0) or 0)
    except (TypeError, ValueError):
        gpus = 0
    if gpus > 0:
        gpu = sl.get("gpu") or {}
        if not (gpu.get("partition") and gpu.get("qos")):
            raise ValueError(
                f"job requests gpus={gpus} but compute env {env.get('name')!r} "
                f"declares no slurm.gpu convention (partition + qos) — GPU is not "
                f"configured on this env")
        merged["partition"] = gpu["partition"]
        merged["qos"] = gpu["qos"]
    else:
        merged.pop("qos", None)                      # qos is GPU-only
        if "partition" not in merged and sl.get("partition"):
            merged["partition"] = sl["partition"]
    if "account" not in merged and sl.get("account"):
        merged["account"] = sl["account"]
    return merged, (env.get("email") or "")


def render_workflow_files(*, tool_name: str, command: str,
                          inputs: Mapping[str, str],
                          outputs: Mapping[str, str],
                          apptainer_sif: str,
                          apptainer_module: str,
                          nextflow_module: str,
                          slurm: Mapping,
                          workflow_name: str,
                          env: Optional[Mapping] = None) -> dict:
    """Render the three workflow files, merging the env's slurm policy + email
    into the per-job `slurm` request first (see _resolve_slurm_and_email).

    Centralizes the render call so submit_workflow_job and run_step_on_cluster
    (each of which authorizes a DIFFERENT workflow_dir family — directories[] vs
    scratch) share the same render+merge shape without sharing the upload/auth
    logic. `env` is optional for back-compat: when None, the per-job slurm renders
    as-is with no email."""
    if env is not None:
        slurm, email = _resolve_slurm_and_email(slurm, env)
    else:
        email = ""
    return workflow_render.render_workflow(
        tool_name=tool_name,
        command=command,
        inputs=inputs,
        outputs=outputs,
        apptainer_sif=apptainer_sif,
        apptainer_module=apptainer_module,
        nextflow_module=nextflow_module,
        slurm=slurm,
        workflow_name=workflow_name,
        email=email,
    )


def sbatch_via_ssh(env: dict, workflow_dir: str, *,
                   timeout: int = 300) -> dict:
    """ssh into `env`, cd to `workflow_dir`, run `sbatch --parsable
    launcher.sh`, parse and return the SLURM job_id.

    Auth-agnostic: callers MUST have already authorized `workflow_dir`
    for the operation they're performing. submit_workflow_job authorizes
    via `directories[]`; run_step_on_cluster authorizes via the env's
    scratch target. This helper only does the ssh-sbatch step.

    Returns {"job_id": "<digits>", "launcher": "<abs path>"} on
    success; {"error": "...", ...} on failure (the launcher key is
    included so the caller can surface where the rendered files
    landed for forensics)."""
    launcher = f"{workflow_dir}/launcher.sh"
    sbatch_cmd = (
        f"bash -lc 'cd {shlex.quote(workflow_dir)} && "
        f"sbatch --parsable launcher.sh'")
    argv = _ssh_argv(env, sbatch_cmd)
    try:
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return broke("submit.sbatch_timeout",
                error=f"sbatch timed out after {e.timeout}s",
                launcher=launcher)

    if res.returncode != 0:
        hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
        out = {
            "error": (
                f"sbatch failed (rc={res.returncode}): "
                f"{(res.stderr or '').strip()[:500]}"),
            "launcher": launcher,
        }
        if hint:
            out["hint"] = hint
        return broke("submit.sbatch_failed", **out)

    job_id = _parse_sbatch_parsable(res.stdout)
    if job_id is None:
        return broke("submit.sbatch_unparseable",
            error=(
                "sbatch returned 0 but stdout was not a parseable "
                "job_id (--parsable expected <id> or <id>;<cluster>)"),
            sbatch_stdout=(res.stdout or "").strip()[:500],
            sbatch_stderr=(res.stderr or "").strip()[:500],
            launcher=launcher,
        )

    return {"job_id": job_id, "launcher": launcher,
            "sbatch_command": sbatch_cmd}


def _write_submission_manifest(*, project_name: str, workflow_name: str,
                               job_id: str, manifest: dict) -> str:
    """Write the submission manifest to
    job_submissions/<project_name>/<workflow_name>_<job_id>.submission.json.

    Path is relative to the agent's working directory (the repo root in
    normal use). Returns the manifest path as a string for the caller's
    return payload.

    The manifest is the production-side deliverable: the user (or a
    future agent invocation) can find a submitted job by name or id
    without remembering the terminal output."""
    out_dir = Path(_MANIFEST_ROOT) / project_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{workflow_name}_{job_id}.submission.json"
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return str(out_path)


def submit_workflow_job(project_name: str,
                        compute_env_name: str,
                        workflow_dir: str,
                        workflow_name: str,
                        tool_name: str,
                        command: str,
                        inputs: Mapping[str, str],
                        outputs: Mapping[str, str],
                        apptainer_sif: str,
                        apptainer_module: str,
                        nextflow_module: str,
                        slurm: Mapping,
                        *,
                        access_path: Optional[str] = None,
                        timeout: int = 300) -> dict:
    """Render the workflow, upload the files to `workflow_dir`, sbatch
    launcher.sh, return the SLURM job_id + a local submission manifest.

    THE PRODUCTION SUBMISSION PRIMITIVE. For runs landing in user-
    declared project workspace paths. Validation/seal flows use
    `run_step_on_cluster` instead.

    `workflow_dir` is REQUIRED — a literal absolute path covered by a
    `directories[]` entry on the project with both `upload` and `exec`.
    The caller decides the per-run subdir (the no-overwrite contract on
    `transfer.upload` means a second submit to the same workflow_dir
    would fail — that's the desired behavior).

    No polling. submit-and-document semantics: the agent may be cut off
    long before a real production job finishes, so this primitive
    returns immediately after sbatch and writes a local manifest that
    the user (or a later agent invocation) can use to find the job.

    Returns on success:
      {
        "success":         True,
        "compute_env":     <env_name>,
        "job_id":          "<digits>",
        "workflow_dir":    <abs path on env>,
        "files_uploaded":  [<remote_path>, ...],
        "submitted_at":    "<iso utc>",
        "manifest_path":   "job_submissions/<project>/<name>_<id>.submission.json",
      }

    Returns {"error": "...", ...} on any refusal/failure. The
    rendered-but-not-uploaded files do not stick around (tempdir is
    cleaned up). Uploaded files DO stick around — they're useful
    forensics if sbatch fails post-upload.
    """
    try:
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        env_type = env.get("type")
        if env_type != "ssh":
            return refused("submit.non_ssh_env",
                error=f"submit_workflow_job only supports ssh compute envs; "
                f"got type={env_type!r} on env {compute_env_name!r}")

        if not workflow_dir or not workflow_dir.strip():
            return refused("submit.workflow_dir_required",
                error=
                "workflow_dir is required for submit_workflow_job — "
                "this is the production primitive that lands in a "
                "user-declared `directories[]` path. For "
                "validation/seal runs in the agent's scratch sandbox, "
                "use run_step_on_cluster instead.")

        normed_dir = _validate_workflow_dir(workflow_dir)

        # The workflow_dir itself must be authorized with BOTH `upload`
        # (so we can put files there) AND `exec` (so the SLURM job may
        # write its own outputs). Phase-1 directories[] gate; scratch
        # paths are NOT authorized here — that's run_step_on_cluster's
        # job through the env-level scratch target.
        compute_access.check_permission(
            project, compute_env_name, normed_dir, "upload")
        compute_access.check_permission(
            project, compute_env_name, normed_dir,
            "submit_workflow_job")

        # ─── Render the workflow files (strict, raises ValueError) ─────
        rendered = render_workflow_files(
            tool_name=tool_name,
            command=command,
            inputs=inputs,
            outputs=outputs,
            apptainer_sif=apptainer_sif,
            apptainer_module=apptainer_module,
            nextflow_module=nextflow_module,
            slurm=slurm,
            workflow_name=workflow_name,
            env=env,
        )

        # ─── Materialize them into a local tempdir, then upload ────────
        files_uploaded: list[str] = []
        upload_started = datetime.now(timezone.utc).isoformat()
        _RENDER_STAGE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="bioinf_submit_",
                                         dir=str(_RENDER_STAGE_DIR)) as td:
            tdp = Path(td)
            for fname in _RENDERED_FILES:
                (tdp / fname).write_text(rendered[fname])

            for fname in _RENDERED_FILES:
                local = str(tdp / fname)
                remote = f"{normed_dir}/{fname}"
                up = transfer.upload(
                    project_name=project_name,
                    compute_env_name=compute_env_name,
                    local_path=local,
                    remote_abs_path=remote,
                    access_path=str(Path(access_path)) if access_path else None,
                    timeout=timeout)
                if "error" in up:
                    return broke("submit.upload_failed",
                        error=
                            f"upload of {fname} failed before sbatch: "
                            f"{up['error']}",
                        files_uploaded=files_uploaded,
                        rendered_locally=True,
                    )
                files_uploaded.append(up["remote_abs_path"])

        # ─── sbatch launcher.sh, parse job_id ──────────────────────────
        sb = sbatch_via_ssh(env, normed_dir, timeout=timeout)
        if "error" in sb:
            # sb is ALREADY a tagged terminal from sbatch_via_ssh (broke
            # with a submit.sbatch_* code); pass it through verbatim, only
            # merging the forensic files_uploaded list. Re-wrapping would
            # clobber the inner code + duplicate the `code`/`outcome` kwargs.
            return {**sb, "files_uploaded": files_uploaded}

        job_id = sb["job_id"]
        submitted_at = datetime.now(timezone.utc).isoformat()

        # ─── Write the local submission manifest ───────────────────────
        manifest = {
            "project_name":     project_name,
            "compute_env":      compute_env_name,
            "workflow_name":    workflow_name,
            "tool_name":        tool_name,
            "job_id":           job_id,
            "workflow_dir":     normed_dir,
            "command":          command,
            "inputs":           dict(inputs),
            "outputs":          dict(outputs),
            "apptainer_sif":    apptainer_sif,
            "apptainer_module": apptainer_module,
            "nextflow_module":  nextflow_module,
            "slurm":            dict(slurm),
            "files_uploaded":   files_uploaded,
            "sbatch_command":   sb.get("sbatch_command"),
            "submitted_at":     submitted_at,
            "upload_started":   upload_started,
            "host":             env.get("host"),
            "follow_up": {
                "poll":     ("call cluster_job_status(project, env, "
                             f"job_id={job_id!r})"),
                "fetch":    ("call download(project, env, remote_abs_path, "
                             "local_path) for each expected output under "
                             "workflow_dir"),
            },
        }
        manifest_path = _write_submission_manifest(
            project_name=project_name, workflow_name=workflow_name,
            job_id=job_id, manifest=manifest)

        return proven(
            "submit_workflow.submitted",
            success=True,
            compute_env=compute_env_name,
            job_id=job_id,
            workflow_dir=normed_dir,
            files_uploaded=files_uploaded,
            submitted_at=submitted_at,
            upload_started=upload_started,
            manifest_path=manifest_path,
        )

    except (ValueError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return broke("submit_workflow.failed", error=f"{type(e).__name__}: {e}")
    except subprocess.TimeoutExpired as e:
        return broke("submit_workflow.sbatch_timeout", error=f"sbatch timed out after {e.timeout}s")
