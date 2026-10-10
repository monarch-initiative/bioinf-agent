"""
The sbatch leg every cluster run shares: validate the remote run directory, merge the
env's SLURM policy into a job's header, `sbatch --parsable` it over ssh and parse the
job_id, and write the local submission manifest a user (or a later agent invocation)
finds the job by.

Used by `run_production_pipeline` (a rendered pipeline's launcher.sh), `run_step_on_cluster`
(the validation/seal run in the agent's scratch sandbox) and the reference-data download
job (`acquire_data`). None of them polls: a cluster job may outlive the agent, so
`cluster_job_status` reads the state later and `transfer.download` fetches the outputs.

Submission manifest
-------------------
One local file per submission:
    <scratch>/job_submissions/<project_name>/<name>_<job_id>.submission.json
with job_id, the run directory, the env, what was uploaded and submitted, and when.

sbatch parsing
--------------
`sbatch --parsable` prints the job_id (or `<id>;<cluster>` on a federation). The first
`;`-separated token must be digits; anything else surfaces with the raw stdout so the
caller can diagnose.
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
from agent.skills import workspace


# A SLURM job_id as parsed from `sbatch --parsable`: digits, length-capped.
_JOB_ID_RE = re.compile(r"^\d{1,12}$")


# Local manifest root: one job_submissions/<project>/<name>_<job_id>.submission.json
# per successful submission, so the user can find the job later by name or id.
#
# Anchored via transfer._record_root (the same anchor the transfer manifests use),
# never the process CWD. A CWD-relative root lands the manifest — the production-side
# deliverable whose whole job is to be findable later — wherever the agent happened to
# be invoked from, and lets an un-chdir'd test write a fake-host submission into the
# user's live audit trail.
# One anchor for both manifest kinds also means test isolation patches one function.
_MANIFEST_ROOT = "job_submissions"


def _manifest_root() -> Path:
    return transfer._record_root() / _MANIFEST_ROOT


# Where the rendered workflow files are staged locally before upload. MUST live
# under a Globus-accessible location: Globus Connect Personal only scans its
# Accessible Folders (default $HOME) and REFUSES a system temp dir like macOS's
# /var/folders (which tempfile.TemporaryDirectory() defaults to) — that surfaced
# as a live `submit.upload_failed` on the first production run. The workspace is
# required to sit under $HOME, so the scratch zone works for BOTH transports (scp
# doesn't care where the source is). Mirrors run_cluster_step._render_stage_dir.
# A FUNCTION, not a module constant. The location depends on the resolved
# workspace, and a constant computed at import freezes whatever the environment
# said at import time — which for a test process is "before the fixture
# redirected it", so every staged file would land in the developer's real
# workspace.
def _render_stage_dir():
    return workspace.scratch_dir("submit_render_staging")


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


def resolve_gpu_placement(per_job_slurm: Mapping, env: Mapping) -> dict:
    """Decide a GPU job's partition/qos from the two places either can come from,
    and STATE which happened. Returns

        {"state": <one of workflow_render._GPU_PLACEMENT_STATES>,
         "gpus": int,
         "partition": str|None, "partition_source": "job"|"env_convention"|None,
         "qos":       str|None, "qos_source":       "job"|"env_convention"|None}

    Two sources, filled PER SLOT with the job winning:

      job             the caller named it in this job's `slurm` request. It wins
                      because it is the more specific statement AND the better
                      informed one: a caller that just ran `cluster_partitions`
                      read the placement off the live cluster, while the env
                      block is whatever was typed into the config once.
      env_convention  env.slurm.gpu.{partition,qos} — this HPC's standing rule,
                      filling any slot the job left open.

    Neither is required. Refusing a GPU request when the env declares no
    `slurm.gpu` block would make the env key a de-facto requirement for GPU
    work, and letting the env override the job's own values would leave a
    caller who discovered a real partition with no way to use it. Every slot
    is fillable from either side or from neither, and what actually resolved
    is reported rather than demanded.
    """
    sl = compute_access.get_slurm_config(env) or {}
    gpu = sl.get("gpu") or {}
    job = per_job_slurm or {}
    try:
        gpus = int(job.get("gpus", 0) or 0)
    except (TypeError, ValueError):
        gpus = 0
    out: dict = {"gpus": gpus}
    for slot in ("partition", "qos"):
        if job.get(slot):
            out[slot], out[f"{slot}_source"] = job[slot], "job"
        elif gpus > 0 and gpu.get(slot):
            out[slot], out[f"{slot}_source"] = gpu[slot], "env_convention"
        else:
            out[slot], out[f"{slot}_source"] = None, None
    out["state"] = workflow_render.gpu_placement_state(
        gpus, out["partition"], out["qos"])
    return out


def _resolve_slurm_and_email(per_job_slurm: Mapping,
                             env: Mapping) -> tuple[dict, str, dict]:
    """Merge the per-job resource request with the env's slurm POLICY into the
    final render spec, and pull the notification email from the env.

    The agent's per-job dict carries resource SIZING (time/mem/cpus/ntasks/gpus);
    the HPC's constants (account, default partition, GPU convention) live in the
    env `slurm:` block and are merged here so a header follows the cluster's rules:
      - GPU (gpus>0): partition + qos per `resolve_gpu_placement` — job first,
        then the env convention, then neither. Never a refusal.
      - CPU (gpus==0): default partition from env.slurm.partition if the job set
        none; a job-supplied qos is HONOURED — "qos is GPU-only" is this
        codebase's convention, not SLURM's; plenty of sites attach a qos to CPU
        work, and silently discarding one the caller typed is the
        accept-a-knob-and-ignore-it shape we delete elsewhere.
      - account: from env.slurm.account unless the job set one explicitly.
    Returns (merged_slurm, email, gpu_placement)."""
    sl = compute_access.get_slurm_config(env) or {}
    merged = dict(per_job_slurm or {})
    placement = resolve_gpu_placement(merged, env)
    for slot in ("partition", "qos"):
        if placement[slot]:
            merged[slot] = placement[slot]
        else:
            merged.pop(slot, None)
    # env.slurm.partition is the CPU DEFAULT, so it fills only a CPU job's empty
    # slot. Letting it fill a GPU job's would take the one outcome the old refusal
    # existed to prevent — a GPU request landing on a CPU partition — and make it
    # the silent default. `undeclared` has to mean no --partition line at all.
    if placement["state"] == "not_applicable" and not merged.get("partition") \
            and sl.get("partition"):
        merged["partition"] = sl["partition"]
    if "account" not in merged and sl.get("account"):
        merged["account"] = sl["account"]
    return merged, (env.get("email") or ""), placement


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

    Centralizes the render call so run_step_on_cluster and the download job
    (each of which authorizes a DIFFERENT workflow_dir family — directories[] vs
    scratch) share the same render+merge shape without sharing the upload/auth
    logic. `env` is optional for back-compat: when None, the per-job slurm renders
    as-is with no email.

    The returned dict carries `gpu_placement` alongside the file strings — the
    stated record of which GPU partition/qos resolved and from where, so the
    caller can put it in its own record. A relaxed gate that reported nothing
    would just be a deleted gate."""
    if env is not None:
        slurm, email, placement = _resolve_slurm_and_email(slurm, env)
    else:
        email = ""
        placement = resolve_gpu_placement(slurm, {})
    rendered = workflow_render.render_workflow(
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
    rendered["gpu_placement"] = placement
    return rendered


def sbatch_via_ssh(env: dict, workflow_dir: str, *,
                   timeout: int = 300,
                   sbatch_args: tuple[str, ...] | list[str] = (),
                   script_args: tuple[str, ...] | list[str] = ()) -> dict:
    """ssh into `env`, cd to `workflow_dir`, run `sbatch --parsable [sbatch_args]
    launcher.sh [script_args]`, parse and return the SLURM job_id. `sbatch_args` are
    options for sbatch itself (`--time=…` overrides the launcher's header);
    `script_args` follow the script and reach it as `"$@"` — a rendered launcher
    forwards them to Nextflow. Both are pre-validated by the caller; every token
    is shell-quoted here regardless.

    Auth-agnostic: callers MUST have already authorized `workflow_dir`
    for the operation they're performing. run_production_pipeline authorizes
    via `directories[]`; run_step_on_cluster authorizes via the env's
    scratch target. This helper only does the ssh-sbatch step.

    Returns {"job_id": "<digits>", "launcher": "<abs path>"} on
    success; {"error": "...", ...} on failure (the launcher key is
    included so the caller can surface where the rendered files
    landed for forensics)."""
    launcher = f"{workflow_dir}/launcher.sh"
    parts = ["sbatch", "--parsable", *[str(a) for a in sbatch_args], "launcher.sh",
             *[str(a) for a in script_args]]
    sbatch_cmd = (
        f"bash -lc 'cd {shlex.quote(workflow_dir)} && "
        f"{' '.join(shlex.quote(x) for x in parts)}'")
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

    Anchored to the repo root (see _manifest_root), NOT to the process CWD.
    Returns the manifest path as a string for the caller's return payload.

    The manifest is the production-side deliverable: the user (or a
    future agent invocation) can find a submitted job by name or id
    without remembering the terminal output."""
    out_dir = _manifest_root() / project_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{workflow_name}_{job_id}.submission.json"
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return str(out_path)
