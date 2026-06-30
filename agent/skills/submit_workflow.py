"""
submit_workflow_job — render → upload → sbatch the workflow on a
compute env. The end-to-end primitive that takes a single-tool spec
and returns a SLURM job_id the agent can poll with
cluster_job_status.

What this is (and isn't)
------------------------
This is NOT a composite primitive. The caller still:
  - freezes the tool's env separately (`freeze`)
  - uploads the .sif separately (`upload_to_common_data`)
  - polls the job separately (`cluster_job_status`)
  - downloads the outputs separately (`download_from_project_path`)

submit_workflow_job is the *submission* step: the irreducible
sequence of (render the per-project Nextflow files, upload them into
the workspace, kick off sbatch). Splitting these out would force the
caller into a brittle 3-call dance for one logical action.

Authorization
-------------
Project must have a `compute_env_access` entry for `compute_env_name`,
AND a `directories[]` entry under that access whose path contains
`workflow_dir` (longest-prefix match) with `permissions:` including
BOTH `upload` (to write the rendered files) AND `exec` (so the
running SLURM job may write its own outputs alongside them).

The workflow_dir is supplied as a LITERAL absolute path, not
auto-prefixed. The caller decides the per-run subdir (the no-overwrite
contract on upload_to_project_path means a second submit to the same
workflow_dir would fail — that's the desired behavior).

sbatch parsing
--------------
We use `sbatch --parsable` which returns just the job_id (or
`<id>;<cluster>` on a federation). We split on `;`, validate the
first token as digits, return it. Anything else surfaces as
`{"error": "...", "sbatch_stdout": "..."}` with the raw output so
the caller can diagnose.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from agent.skills import compute_access, project_path, workflow_render
from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint


# A SLURM job_id as parsed from `sbatch --parsable`: digits, length-capped.
_JOB_ID_RE = re.compile(r"^\d{1,12}$")


def _validate_workflow_dir(workflow_dir: str) -> str:
    """The workflow_dir is an absolute path. Same shape rules as
    upload_to_project_path's abs_path — no `..`, no shell
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
    launcher.sh, return the SLURM job_id.

    `workflow_dir` semantics:
      - If empty / not provided: AUTO-DERIVED as
        `<env.agent_scratch_target.path>/<project_name>/<workflow_name>`.
        This is the canonical "per-run staging in scratch" path — uses
        the env-implicit scratch grant (no per-project YAML declaration
        needed); requires the scratch target to have `upload` + `exec`.
      - If provided: literal absolute path. Must be authorized by
        EITHER the env-implicit scratch grant for this project OR an
        explicit `project.directories[]` entry with `upload` + `exec`.
        Use this to land workflow files in a long-lived project_path
        location instead of ephemeral scratch.

    Returns on success:
      {
        "success":         True,
        "compute_env":     <env_name>,
        "job_id":          "<digits>",
        "workflow_dir":    <abs path on env>,
        "files_uploaded":  [<remote_path>, ...],   # one per rendered file
        "submitted_at":    "<iso utc>",
      }

    Returns {"error": "...", ...} on any refusal/failure. The
    rendered-but-not-uploaded files do not stick around (tempdir is
    cleaned up). Uploaded files DO stick around — they're useful
    forensics if sbatch fails post-upload.
    """
    try:
        # ─── Resolve access first; we may need env to auto-derive workflow_dir
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        env_type = env.get("type")
        if env_type != "ssh":
            return {"error":
                f"submit_workflow_job only supports ssh compute envs; "
                f"got type={env_type!r} on env {compute_env_name!r}"}

        # ─── Auto-derive workflow_dir from scratch when not supplied ───
        if not workflow_dir or not workflow_dir.strip():
            scratch = env.get("agent_scratch_target") or {}
            scratch_path = (scratch.get("path") or "").rstrip("/")
            if not scratch_path:
                return {"error":
                    f"no workflow_dir supplied and env "
                    f"{compute_env_name!r} has no agent_scratch_target "
                    f"to auto-derive from; either declare a scratch "
                    f"target on the env or pass workflow_dir explicitly."}
            workflow_dir = f"{scratch_path}/{project_name}/{workflow_name}"

        normed_dir = _validate_workflow_dir(workflow_dir)

        # The workflow_dir itself must be authorized with BOTH `upload`
        # (so we can put files there) AND `exec` (so the SLURM job may
        # write its own outputs). Two separate check_permission calls
        # to get distinct error messages. `env` passed so paths under
        # <env.agent_scratch_target>/<project>/ pick up the env-implicit
        # scratch grant — same auth posture as upload_to_scratch.
        compute_access.check_permission(
            project, compute_env_name, normed_dir,
            "upload_to_project_path", env=env)
        compute_access.check_permission(
            project, compute_env_name, normed_dir,
            "submit_workflow_job", env=env)

        # ─── Render the workflow files (strict, raises ValueError) ─────
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
        )

        # ─── Materialize them into a local tempdir, then upload ────────
        files_uploaded: list[str] = []
        upload_started = datetime.now(timezone.utc).isoformat()
        with tempfile.TemporaryDirectory(prefix="bioinf_submit_") as td:
            tdp = Path(td)
            for fname in _RENDERED_FILES:
                (tdp / fname).write_text(rendered[fname])

            for fname in _RENDERED_FILES:
                local = str(tdp / fname)
                remote = f"{normed_dir}/{fname}"
                up = project_path.upload_to_project_path(
                    project_name=project_name,
                    compute_env_name=compute_env_name,
                    abs_path=remote,
                    local_path=local,
                    access_path=str(Path(access_path)) if access_path else None,
                    timeout=timeout)
                if "error" in up:
                    return {
                        "error":
                            f"upload of {fname} failed before sbatch: "
                            f"{up['error']}",
                        "files_uploaded": files_uploaded,
                        "rendered_locally": True,
                    }
                files_uploaded.append(up["remote_path"])

        # ─── sbatch launcher.sh, parse job_id ──────────────────────────
        launcher = f"{normed_dir}/launcher.sh"
        # Use `--parsable` so stdout is just the job_id (or
        # `id;cluster`). cd into the dir first so the SBATCH output
        # log paths (relative in the launcher) land alongside the
        # rendered files.
        sbatch_cmd = (
            f"bash -lc 'cd {shlex.quote(normed_dir)} && "
            f"sbatch --parsable launcher.sh'")
        argv = _ssh_argv(env, sbatch_cmd)
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
        if res.returncode != 0:
            hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
            return {
                "error":
                    f"sbatch failed (rc={res.returncode}): "
                    f"{(res.stderr or '').strip()[:500]}",
                "files_uploaded": files_uploaded,
                "launcher": launcher,
                **({"hint": hint} if hint else {}),
            }

        job_id = _parse_sbatch_parsable(res.stdout)
        if job_id is None:
            return {
                "error":
                    "sbatch returned 0 but stdout was not a parseable "
                    "job_id (--parsable expected <id> or <id>;<cluster>)",
                "sbatch_stdout":  (res.stdout or "").strip()[:500],
                "sbatch_stderr":  (res.stderr or "").strip()[:500],
                "files_uploaded": files_uploaded,
                "launcher":       launcher,
            }

        return {
            "success":        True,
            "compute_env":    compute_env_name,
            "job_id":         job_id,
            "workflow_dir":   normed_dir,
            "files_uploaded": files_uploaded,
            "submitted_at":   datetime.now(timezone.utc).isoformat(),
            "upload_started": upload_started,
        }

    except (ValueError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    except subprocess.TimeoutExpired as e:
        return {"error": f"sbatch timed out after {e.timeout}s"}
