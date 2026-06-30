"""
Common-data primitives — upload_to_common_data + download_from_common_data.

Sibling to `scratch.py`. Same env-implicit authorization, same sha256-
anchored round-trip, same ControlMaster ssh pattern. The key difference:

  * scratch  is PER-PROJECT (auto-prefixed by project name; project A
             can't see project B's working dir)
  * common_data is SHARED (no project prefix; pipelines can mix-and-match
             reference data across projects, matching how reference
             databases / genomes / public assets actually get used)

The shared-namespace design is intentional: a pipeline that builds an
annotation set for one project should be readable by another pipeline
that consumes it. The single-tenant security posture for Phase 2 (one
user, all their projects) makes the convenience worth the looser
isolation. Multi-tenant evolution (Phase 3+) can layer project-prefix
optionality on top.

Both this module and scratch.py refuse to overwrite existing remote
files — the `upload` permission contract is "write NEW files; never
overwrites" (documented in projects_access.yaml.example). Agents
version their subpaths (`exomiser/v3.2.0/data.zip`, etc.); deletes
are a user-side action.

The actual code paths reuse scratch.py's helpers: path validators,
sha256, scp argv, sha256sum cmd, ssh failure hints. The only divergence
is _resolve_common_data_path which DOESN'T auto-prefix by project.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.skills import compute_access
from agent.skills.scratch import (
    ScratchPathError,
    _build_remote_target,
    _compute_local_sha256,
    _local_mkdir_parent,
    _parse_sha256sum_output,
    _remote_sha256_cmd,
    _scp_argv,
    _validate_local_path_for_download,
    _validate_local_path_for_upload,
    _validate_project_name_token,
    _validate_remote_subpath,
    _MAX_TRANSFER_BYTES,
)
from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint


# ---------------------------------------------------------------------------
# Path resolution — DIFFERS from scratch.py (no project auto-prefix)
# ---------------------------------------------------------------------------

def _resolve_common_data_path(common_data_root: str,
                              remote_subpath_norm: str) -> str:
    """Join `<common_data_root>/<remote_subpath_norm>` and re-verify the
    result is inside the root. No project-name prefix is applied —
    common_data is intentionally a SHARED namespace so pipelines from
    different projects can mix and match reference data."""
    root = common_data_root.rstrip("/")
    joined = f"{root}/{remote_subpath_norm}"
    joined_norm = os.path.normpath(joined)
    if not (joined_norm == root or joined_norm.startswith(root + "/")):
        raise ScratchPathError(
            f"resolved remote path {joined_norm!r} escapes common_data "
            f"root {root!r} — refusing")
    return joined_norm


# ---------------------------------------------------------------------------
# upload_to_common_data
# ---------------------------------------------------------------------------

def upload_to_common_data(project_name: str,
                         compute_env_name: str,
                         local_path: str,
                         remote_subpath: str,
                         *,
                         async_globus: bool = False,
                         access_path: Optional[str] = None,
                         timeout: int = 600) -> dict:
    """Push a local file into the env's SHARED common-data zone.

    Authorization (env-implicit grant):
      1. Project exists; has compute_env_access for compute_env_name
      2. The env declares an `agent_common_data_target` block whose
         `permissions:` includes `upload`

    Path safety:
      a. `local_path`: exists, regular file (no symlinks), ≤5 GiB
      b. `remote_subpath`: non-empty, ≤255 chars, no leading '/',
         no '..', no shell metacharacters; normalizes inside the
         common_data root
      c. `project_name`: safe-token validated (used as the AUTH key
         even though it's NOT in the path)

    Overwrite refusal: if the resolved remote path already exists, the
    primitive refuses to upload. Reference data is meant to be versioned,
    not silently replaced. Agents pick fresh subpaths (e.g. semantic
    version directories: `exomiser/v3.2.0/data.zip`).

    Returns {success, compute_env, remote_path, sha256, bytes,
    duration_s, transferred_at} on success; {"error": "..."} on any
    failure (no exception escapes)."""
    started = time.perf_counter()
    try:
        _validate_project_name_token(project_name)
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        env_type = env.get("type")
        if env_type not in ("ssh", "local"):
            return {"error": f"unsupported compute_env type {env_type!r}"}

        common = compute_access.get_agent_common_data_target(env)
        if common is None:
            return {"error":
                f"compute_env {compute_env_name!r} has no "
                f"agent_common_data_target declared in projects_access.yaml — "
                f"add the block under that compute_envs[] entry to enable "
                f"upload_to_common_data."}
        common_root = common.get("path", "").rstrip("/")

        compute_access.check_env_target_capability(
            project, compute_env_name, common, "upload_to_common_data",
            "agent_common_data_target")

        norm_sub = _validate_remote_subpath(remote_subpath)
        abs_remote = _resolve_common_data_path(common_root, norm_sub)

        lp = _validate_local_path_for_upload(local_path)
        local_sha = _compute_local_sha256(lp)
        size_bytes = lp.stat().st_size

        # Overwrite refusal — same shape as scratch.upload_to_scratch.
        if env_type == "local":
            if Path(abs_remote).exists():
                return {"error":
                    f"remote path already exists: {abs_remote!r}. The "
                    f"upload contract refuses overwrites (reference data "
                    f"is versioned, not replaced). Pick a fresh "
                    f"remote_subpath (e.g. `exomiser/v3.2.0/...`) or "
                    f"delete the existing file first."}
        else:
            exist_cmd = (
                f"test -e {shlex.quote(abs_remote)} && echo EXISTS || echo OK")
            ex_argv = _ssh_argv(env, exist_cmd)
            ex = subprocess.run(ex_argv, capture_output=True, text=True,
                                timeout=timeout)
            if ex.returncode != 0:
                hint = _ssh_failure_hint(ex.stderr, env.get("host", "?"))
                return {"error":
                    f"remote existence pre-check failed (rc={ex.returncode}): "
                    f"{ex.stderr.strip()}",
                    **({"hint": hint} if hint else {})}
            if "EXISTS" in ex.stdout:
                return {"error":
                    f"remote path already exists: {abs_remote!r}. The "
                    f"upload contract refuses overwrites. Pick a fresh "
                    f"remote_subpath or delete the existing file first."}

        # Transfer.
        if env_type == "local":
            dest = Path(abs_remote)
            _local_mkdir_parent(dest)
            shutil.copy(str(lp), str(dest))
            remote_sha = _compute_local_sha256(Path(abs_remote))
            if remote_sha != local_sha:
                return {"error":
                    f"sha256 round-trip mismatch — local={local_sha} "
                    f"remote={remote_sha!r}. File at {abs_remote!r} may be "
                    f"corrupt; investigate before relying on it."}
            provider_info = {"provider": "local_copy",
                             "verified_method": "sha256_round_trip"}
        else:
            # mkdir parent — uniform across providers (ssh hop regardless
            # of wire-protocol).
            mkdir_cmd = f"mkdir -p {shlex.quote(os.path.dirname(abs_remote))}"
            mk_argv = _ssh_argv(env, mkdir_cmd)
            mk = subprocess.run(mk_argv, capture_output=True,
                                text=True, timeout=timeout)
            if mk.returncode != 0:
                hint = _ssh_failure_hint(mk.stderr, env.get("host", "?"))
                return {"error":
                    f"remote mkdir -p failed (rc={mk.returncode}): "
                    f"{mk.stderr.strip()}",
                    **({"hint": hint} if hint else {})}
            from agent.skills import transfer_providers
            provider = transfer_providers.get_transfer_provider(env)
            pr = provider.upload_one(
                env=env, local_path=lp, abs_remote_path=abs_remote,
                local_sha256=local_sha, timeout=timeout,
                async_return=async_globus,
                label=f"upload_to_common_data {remote_subpath}")
            if "error" in pr:
                return pr
            provider_info = {
                "provider":        pr.get("provider"),
                "verified_method": pr.get("verified_method"),
            }
            if pr.get("task_id"):
                provider_info["task_id"] = pr["task_id"]

        return {
            "success":        True,
            "compute_env":    compute_env_name,
            "remote_path":    abs_remote,
            "sha256":         local_sha,
            "bytes":          size_bytes,
            "duration_s":     round(time.perf_counter() - started, 3),
            "transferred_at": datetime.now(timezone.utc).isoformat(),
            **provider_info,
        }

    except (ScratchPathError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    except subprocess.TimeoutExpired as e:
        return {"error": f"transfer timed out after {e.timeout}s"}


# ---------------------------------------------------------------------------
# download_from_common_data
# ---------------------------------------------------------------------------

def download_from_common_data(project_name: str,
                             compute_env_name: str,
                             remote_subpath: str,
                             local_path: str,
                             *,
                             async_globus: bool = False,
                             access_path: Optional[str] = None,
                             timeout: int = 600) -> dict:
    """Pull a file from the env's SHARED common-data zone back to local.

    Symmetric to `upload_to_common_data` — same env-implicit
    authorization with `download` (instead of `upload`) as the required
    capability. Same SHARED namespace (no project prefix); any project
    with env access can read any file in common_data.

    Local-path safety: must NOT exist (no overwrite); parent must be
    writable.

    Returns {success, compute_env, remote_path, local_path, sha256,
    bytes, duration_s, fetched_at} on success; {"error": "..."} on
    refusal/failure."""
    started = time.perf_counter()
    try:
        _validate_project_name_token(project_name)
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        env_type = env.get("type")
        if env_type not in ("ssh", "local"):
            return {"error": f"unsupported compute_env type {env_type!r}"}

        common = compute_access.get_agent_common_data_target(env)
        if common is None:
            return {"error":
                f"compute_env {compute_env_name!r} has no "
                f"agent_common_data_target declared — "
                f"download_from_common_data requires it."}
        common_root = common.get("path", "").rstrip("/")

        compute_access.check_env_target_capability(
            project, compute_env_name, common, "download_from_common_data",
            "agent_common_data_target")

        norm_sub = _validate_remote_subpath(remote_subpath)
        abs_remote = _resolve_common_data_path(common_root, norm_sub)
        lp = _validate_local_path_for_download(local_path)

        # Remote sha256 BEFORE transfer (the anchor).
        if env_type == "local":
            remote_p = Path(abs_remote)
            if not remote_p.exists():
                return {"error":
                    f"remote file does not exist: {abs_remote!r}"}
            if remote_p.is_symlink():
                return {"error":
                    f"refusing to download a symlink at {abs_remote!r} — "
                    f"defense against the env redirecting download outside "
                    f"common_data root"}
            if not remote_p.is_file():
                return {"error":
                    f"remote path is not a regular file: {abs_remote!r}"}
            size_bytes = remote_p.stat().st_size
            if size_bytes > _MAX_TRANSFER_BYTES:
                return {"error":
                    f"remote file {abs_remote!r} is {size_bytes} bytes, "
                    f"exceeds head-node download cap {_MAX_TRANSFER_BYTES}; "
                    f"use a SLURM data_acquisition job to stage instead."}
            remote_sha = _compute_local_sha256(remote_p)
        else:
            stat_cmd = (
                f"stat -L -c '%F %s' {shlex.quote(abs_remote)} && "
                f"test ! -L {shlex.quote(abs_remote)}")
            stat_argv = _ssh_argv(env, stat_cmd)
            st = subprocess.run(stat_argv, capture_output=True, text=True,
                                timeout=timeout)
            if st.returncode != 0:
                hint = _ssh_failure_hint(st.stderr, env.get("host", "?"))
                return {"error":
                    f"remote stat failed (rc={st.returncode}): "
                    f"{st.stderr.strip()}",
                    **({"hint": hint} if hint else {})}
            parts = st.stdout.strip().split()
            if len(parts) < 2 or "regular" not in st.stdout:
                return {"error":
                    f"remote path is not a regular file: {abs_remote!r} "
                    f"(stat: {st.stdout!r})"}
            try:
                size_bytes = int(parts[-1])
            except ValueError:
                return {"error":
                    f"could not parse remote size from {st.stdout!r}"}
            if size_bytes > _MAX_TRANSFER_BYTES:
                return {"error":
                    f"remote file {abs_remote!r} is {size_bytes} bytes, "
                    f"exceeds head-node download cap {_MAX_TRANSFER_BYTES}; "
                    f"use a SLURM data_acquisition job to stage instead."}
            sha_argv = _ssh_argv(env, _remote_sha256_cmd(abs_remote))
            sr = subprocess.run(sha_argv, capture_output=True, text=True,
                                timeout=timeout)
            if sr.returncode != 0:
                return {"error":
                    f"remote sha256sum failed (rc={sr.returncode}): "
                    f"{sr.stderr.strip()}"}
            remote_sha = _parse_sha256sum_output(sr.stdout) or ""
            if not remote_sha:
                return {"error":
                    f"could not parse sha256 from sha256sum output: "
                    f"{sr.stdout!r}"}

        # Transfer + verify (dispatched by data_transfer.type).
        if env_type == "local":
            shutil.copy(str(Path(abs_remote)), str(lp))
            local_sha = _compute_local_sha256(lp)
            if local_sha != remote_sha:
                try:
                    lp.unlink()
                except OSError:
                    pass
                return {"error":
                    f"sha256 round-trip mismatch — remote={remote_sha} "
                    f"local={local_sha}. Local file removed."}
            provider_info = {"provider": "local_copy",
                             "verified_method": "sha256_round_trip"}
        else:
            from agent.skills import transfer_providers
            provider = transfer_providers.get_transfer_provider(env)
            pr = provider.download_one(
                env=env, abs_remote_path=abs_remote, local_path=lp,
                timeout=timeout, async_return=async_globus,
                label=f"download_from_common_data {remote_subpath}")
            if "error" in pr:
                return pr
            local_sha = pr.get("local_sha256") or _compute_local_sha256(lp)
            provider_info = {
                "provider":        pr.get("provider"),
                "verified_method": pr.get("verified_method"),
            }
            if pr.get("task_id"):
                provider_info["task_id"] = pr["task_id"]

        return {
            "success":     True,
            "compute_env": compute_env_name,
            "remote_path": abs_remote,
            "local_path":  str(lp),
            "sha256":      local_sha,
            "bytes":       size_bytes,
            "duration_s":  round(time.perf_counter() - started, 3),
            "fetched_at":  datetime.now(timezone.utc).isoformat(),
            **provider_info,
        }

    except (ScratchPathError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    except subprocess.TimeoutExpired as e:
        return {"error": f"transfer timed out after {e.timeout}s"}
