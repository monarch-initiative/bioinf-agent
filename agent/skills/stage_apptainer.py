"""
stage_apptainer_image — get an apptainer .sif onto a compute env.

What this is
------------
A small, MODE-AWARE primitive that takes a frozen env (by
request_key into the EnvCache) and puts the runnable .sif somewhere
on a compute env so a SLURM job can `apptainer exec` it.

What it ISN'T
-------------
Not a composite. The caller still calls `freeze()` to populate the
EnvCache and `submit_workflow_job` to run jobs. This primitive is
the irreducible "get the bytes there" step.

How it picks the right method
-----------------------------
The EnvCache record's `mode` decides:

  ADOPT
    Public BioContainer. ONE ssh hop:
      apptainer pull <sif_path> docker://<image_by_digest>
    No bytes move through us — apptainer fetches direct from quay.io
    (or wherever). Idempotent: skip the pull if the .sif already
    exists at sif_path.

  BUILD (registry-free)
    Local docker-save tarball. TWO steps:
      1. transfer the tar to the cluster via the env's configured
         `bulk_transfer.type` (today: scp head node via
         upload_to_common_data; future: datamover/globus — see
         comment below).
      2. ssh apptainer build <sif_path> docker-archive://<tar_path>
    Idempotent on the .sif side; the .tar lands at its own
    deterministic path.

  BUILD (with push_target)
    Pushed to a registry. ONE ssh hop:
      apptainer pull <sif_path> docker://<push_target>
    Same shape as adopt.

Where the .sif lands
--------------------
Default: `<env.agent_common_data_target>/apptainer/<env_name>_<digest>.sif`
- env_name is the freeze record's env name (so multiple frozen envs
  coexist)
- digest is the short content_digest so a re-freeze with a different
  result doesn't clobber the old .sif
- under `apptainer/` so the dir doesn't compete with reference data
  for namespace

Caller may override the subpath via `sif_subpath`; we still resolve
against `agent_common_data_target.path` (so the file stays in the
project-shared zone, not in scratch).

Authorization
-------------
Same env-implicit grant as `upload_to_common_data`: project on env +
env.agent_common_data_target.permissions includes `upload`. For
ADOPT mode we ALSO need apptainer + a network egress on the head
node — that's a cluster property, not something this primitive can
guarantee; failures surface in `apptainer_stderr`.

Bulk-transfer extension point
-----------------------------
BUILD mode's step 1 currently always goes through scp on the head
node (via upload_to_common_data). HPC's DataMover (and Globus) move
the same bytes off the head node via a separate transfer node — the
primitive's signature doesn't change to support them; only ONE
internal branch:

    if env.get("bulk_transfer", {}).get("type") == "datamover":
        # route through datamover adapter (future)
    else:
        # scp head node via upload_to_common_data (today)

ADOPT mode never goes through us, so it's already off the head-node
critical path.
"""
from __future__ import annotations

import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.skills import common_data, compute_access
from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint


def _short_digest(content_digest: str) -> str:
    """Take the first 12 hex chars of a sha256:abcd... digest so the
    filename stays readable while keeping enough entropy to dedupe."""
    if not isinstance(content_digest, str) or ":" not in content_digest:
        return "unknown"
    hexdigest = content_digest.split(":", 1)[1]
    return hexdigest[:12] if hexdigest else "unknown"


def _default_sif_subpath(env_name: str, content_digest: str) -> str:
    """Where the .sif lives under the env's agent_common_data_target.
    Subdir 'apptainer/' keeps .sif files separate from reference data."""
    return f"apptainer/{env_name}_{_short_digest(content_digest)}.sif"


def _build_apptainer_pull_cmd(sif_remote_abs: str,
                              docker_uri: str) -> str:
    """The remote shell command that fetches an image via apptainer.
    Login shell so `module load apptainer` works on systems where
    apptainer lives behind Lmod. Skips the pull if the .sif already
    exists (idempotent re-stage)."""
    # `module load apptainer` is intentionally outside the test for
    # the file existing — even when we skip the pull, we want failures
    # to be diagnosed via `apptainer --version` rather than a missing
    # `apptainer` command.
    return (
        f"bash -lc 'module load apptainer >/dev/null 2>&1 || true; "
        f"if [ -f {shlex.quote(sif_remote_abs)} ]; then "
        f"  echo SKIP_ALREADY_STAGED; "
        f"else "
        f"  mkdir -p {shlex.quote(str(Path(sif_remote_abs).parent))} && "
        f"  apptainer pull {shlex.quote(sif_remote_abs)} "
        f"{shlex.quote(docker_uri)}; "
        f"fi'"
    )


def stage_apptainer_image(
        project_name: str,
        compute_env_name: str,
        freeze_request_key: str,
        *,
        sif_subpath: str = "",
        env_cache=None,
        access_path: Optional[str] = None,
        timeout: int = 1800) -> dict:
    """Get the apptainer .sif for `freeze_request_key` onto
    `compute_env_name`. Mode-aware: ADOPT → pull on cluster; BUILD →
    upload .tar + build on cluster.

    Returns on success:
      {success: True, compute_env, mode, sif_path, image_digest,
       request_key, skipped: bool, staged_at}
    The `skipped: True` flag means the .sif already existed at
    sif_path — a re-stage is a no-op, not an error.

    Returns {"error": ..., ...} on any refusal/failure. Useful
    diagnostic fields when present:
      - `apptainer_stderr` (ADOPT path)
      - `tar_upload_error` / `apptainer_build_error` (BUILD path)
      - `hint` (e.g. "open ssh hpc-agent in a side terminal")
    """
    try:
        # EnvCache lookup — find the freeze record by request_key.
        # EnvCache.lookup(key) reads from disk on every call, so a freeze
        # in a sibling process is visible immediately.
        if env_cache is None:
            from agent import mcp_server as _ms  # late import; singleton
            env_cache = _ms._env_cache
        record = env_cache.lookup(freeze_request_key)
        if not record:
            return {"error":
                f"freeze_request_key {freeze_request_key!r} not in "
                f"EnvCache. Call freeze() first."}

        # Resolve env + project + auth (env-implicit common_data perm)
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        env_type = env.get("type")
        if env_type != "ssh":
            return {"error":
                f"stage_apptainer_image only supports ssh envs; "
                f"got type={env_type!r} on env {compute_env_name!r}"}

        # Project must have access to env (same shape as the other
        # env-implicit primitives — no per-directory perm needed for
        # ADOPT; for BUILD upload, we re-check via upload_to_common_data).
        has_access = any(
            isinstance(b, dict) and b.get("compute_env") == compute_env_name
            for b in (project.get("compute_env_access") or []))
        if not has_access:
            return {"error":
                f"PermissionDenied: project {project_name!r} has no "
                f"compute_env_access entry for compute_env "
                f"{compute_env_name!r}"}

        # The .sif will land under agent_common_data_target — verify
        # the env declares one with `upload` perm.
        cd = env.get("agent_common_data_target") or {}
        cd_path = cd.get("path", "").rstrip("/")
        if not cd_path:
            return {"error":
                f"env {compute_env_name!r} has no agent_common_data_target; "
                f"add one with permissions including `upload` to use "
                f"stage_apptainer_image."}
        if "upload" not in (cd.get("permissions") or []):
            return {"error":
                f"env {compute_env_name!r}'s agent_common_data_target "
                f"does not include `upload` in permissions."}

        # Resolve sif path (under agent_common_data_target). Caller's
        # sif_subpath overrides the default, but still resolves under
        # cd_path — no escaping into project_path or scratch.
        env_name_for_sif = (record.get("name")
                            or freeze_request_key.split("|", 1)[0])
        content_digest = record.get("content_digest") or record.get(
            "image_digest", "")
        subpath = sif_subpath or _default_sif_subpath(
            env_name_for_sif, content_digest)
        # Refuse traversal in sif_subpath
        if ".." in subpath.split("/") or subpath.startswith("/"):
            return {"error":
                f"sif_subpath {subpath!r} must be relative + no `..`"}
        sif_remote_abs = f"{cd_path}/{subpath}"

        mode = record.get("mode")
        image_digest = record.get("image_digest") or ""

        # ─── ADOPT path ──────────────────────────────────────────────
        if mode == "adopt":
            image = record.get("image")
            if not image:
                return {"error":
                    f"ADOPT record missing `image`; can't pull "
                    f"(request_key={freeze_request_key!r})"}
            docker_uri = f"docker://{image}"
            remote_cmd = _build_apptainer_pull_cmd(sif_remote_abs, docker_uri)
            argv = _ssh_argv(env, remote_cmd)
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout)
            if res.returncode != 0:
                hint = _ssh_failure_hint(res.stderr or "",
                                          env.get("host", "?"))
                return {
                    "error":
                        f"apptainer pull failed (rc={res.returncode}): "
                        f"{(res.stderr or '').strip()[:500]}",
                    "apptainer_stderr": (res.stderr or "").strip()[:1000],
                    "remote_cmd":       remote_cmd[:200],
                    **({"hint": hint} if hint else {}),
                }
            skipped = "SKIP_ALREADY_STAGED" in (res.stdout or "")
            return {
                "success":       True,
                "compute_env":   compute_env_name,
                "mode":          "adopt",
                "sif_path":      sif_remote_abs,
                "image_digest":  image_digest,
                "request_key":   freeze_request_key,
                "skipped":       skipped,
                "staged_at":     datetime.now(timezone.utc).isoformat(),
            }

        # ─── BUILD-with-push_target path ─────────────────────────────
        push_target = record.get("push_target")
        if push_target and not record.get("gated"):
            docker_uri = f"docker://{push_target}"
            remote_cmd = _build_apptainer_pull_cmd(sif_remote_abs, docker_uri)
            argv = _ssh_argv(env, remote_cmd)
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout)
            if res.returncode != 0:
                return {
                    "error":
                        f"apptainer pull (push_target) failed (rc="
                        f"{res.returncode}): {(res.stderr or '').strip()[:500]}",
                    "apptainer_stderr": (res.stderr or "").strip()[:1000],
                }
            return {
                "success":       True,
                "compute_env":   compute_env_name,
                "mode":          "build_push",
                "sif_path":      sif_remote_abs,
                "image_digest":  image_digest,
                "request_key":   freeze_request_key,
                "skipped":       "SKIP_ALREADY_STAGED" in (res.stdout or ""),
                "staged_at":     datetime.now(timezone.utc).isoformat(),
            }

        # ─── BUILD-registry-free path (.tar transfer + build) ────────
        # CONFIG-LEVEL CHECK FIRST: bulk_transfer.type is a config
        # decision the user makes in projects_access.yaml — surface
        # config errors BEFORE deployment-state errors (e.g. missing
        # tarball). Extension hook: future datamover/globus implementer
        # adds ONE branch HERE.
        bulk = env.get("bulk_transfer") or {}
        transfer_type = bulk.get("type", "scp_head_node")
        if transfer_type != "scp_head_node":
            return {"error":
                f"env {compute_env_name!r} declares "
                f"bulk_transfer.type={transfer_type!r}, but only "
                f"`scp_head_node` is implemented today. To wire "
                f"`datamover` or `globus`, add a branch in "
                f"stage_apptainer_image."}

        tarball = record.get("tarball")
        if not tarball:
            return {"error":
                f"freeze record has mode={mode!r} but no `tarball` or "
                f"`push_target` — can't stage. (request_key="
                f"{freeze_request_key!r})"}
        tarball_path = Path(tarball)
        if not tarball_path.exists():
            return {"error":
                f"freeze record tarball missing on disk: {tarball!r}. "
                f"Re-run freeze() to regenerate."}

        tar_subpath = f"apptainer_sources/{tarball_path.name}"
        up = common_data.upload_to_common_data(
            project_name=project_name,
            compute_env_name=compute_env_name,
            local_path=str(tarball_path),
            remote_subpath=tar_subpath,
            access_path=str(Path(access_path)) if access_path else None,
            timeout=timeout,
        )
        if "error" in up:
            # Tolerate the upload-already-exists case as a no-op.
            if "already exists" not in (up.get("error") or ""):
                return {
                    "error":
                        f"tar upload to common_data failed: {up['error']}",
                    "tar_upload_error": up.get("error"),
                }
        tar_remote_abs = f"{cd_path}/{tar_subpath}"

        # Step 2: apptainer build .sif docker-archive://<tar>
        build_cmd = (
            f"bash -lc 'module load apptainer >/dev/null 2>&1 || true; "
            f"if [ -f {shlex.quote(sif_remote_abs)} ]; then "
            f"  echo SKIP_ALREADY_STAGED; "
            f"else "
            f"  mkdir -p {shlex.quote(str(Path(sif_remote_abs).parent))} && "
            f"  apptainer build {shlex.quote(sif_remote_abs)} "
            f"docker-archive://{shlex.quote(tar_remote_abs)}; "
            f"fi'"
        )
        argv = _ssh_argv(env, build_cmd)
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
        if res.returncode != 0:
            return {
                "error":
                    f"apptainer build failed (rc={res.returncode}): "
                    f"{(res.stderr or '').strip()[:500]}",
                "apptainer_build_error": (res.stderr or "").strip()[:1000],
                "tar_remote_path":       tar_remote_abs,
            }
        return {
            "success":       True,
            "compute_env":   compute_env_name,
            "mode":          "build_archive",
            "sif_path":      sif_remote_abs,
            "tar_remote_path": tar_remote_abs,
            "image_digest":  image_digest,
            "request_key":   freeze_request_key,
            "skipped":       "SKIP_ALREADY_STAGED" in (res.stdout or ""),
            "staged_at":     datetime.now(timezone.utc).isoformat(),
        }

    except (ValueError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    except subprocess.TimeoutExpired as e:
        return {"error": f"stage_apptainer_image timed out after {e.timeout}s"}
