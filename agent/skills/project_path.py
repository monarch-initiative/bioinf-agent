"""
Project-workspace primitives — upload_to_project_path + download_from_project_path.

The THIRD transfer family. Sibling to scratch.py (per-project sandbox)
and common_data.py (shared zone), but with a different auth model:

  scratch       — env-implicit + auto-prefix by project (sandbox)
  common_data   — env-implicit + shared namespace (reference data)
  project_path  — PROJECT-EXPLICIT + literal abs_path (per-project workspace)

Project workspaces are the "this is project A's data" zone. They live
under paths the user has explicitly declared in their project's
`compute_env_access[].directories[]` block, with the `upload` / `download`
permission tokens. The user controls exactly what's writable.

Why not just re-use the scratch pattern? Two reasons:

  1. Workspace paths are SPECIFIC to a project's domain. They're not
     auto-allocated — they're declared by the user up front
     (`/work/.../PLANT_PROJECT`). Auto-prefixing would corrupt their
     existing directory layout.

  2. Workspaces hold the user's REAL DATA. The Phase-1 directories[]
     model lets the user be precise about which subdirs are writable
     (typically: outputs/, intermediate/ — NOT raw_inputs/).

Auth (Phase-1 model):
  - The project's `compute_env_access[].directories[]` MUST include an
    entry whose path matches (longest-prefix) the requested abs_path
  - That entry's `permissions:` MUST include `upload` (for writes) or
    `download` (for reads)
  - Both fire via the existing `compute_access.check_permission`

Same path-safety as scratch/common_data (regular file, no symlinks,
≤5 GiB cap, sha256 round-trip). Same overwrite-refusal policy.

Reuses scratch.py's helpers — only the resolver differs (project_path
takes a literal abs_path; no scratch_root + remote_subpath join).
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
    _FORBIDDEN_SUBPATH_CHARS,
    _MAX_TRANSFER_BYTES,
)
from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint


# ---------------------------------------------------------------------------
# Validators — the abs_path interface differs from remote_subpath
# ---------------------------------------------------------------------------

def _validate_abs_remote_path(abs_path: str) -> str:
    """Verify the agent-supplied absolute remote path is well-formed
    BEFORE any I/O. The check_permission gate downstream will then
    verify it's authorized by the project's directories[] list."""
    if not isinstance(abs_path, str):
        raise ScratchPathError(
            f"abs_path must be a string, got {type(abs_path).__name__}")
    if not abs_path:
        raise ScratchPathError("abs_path must be non-empty")
    if not abs_path.startswith("/"):
        raise ScratchPathError(
            f"abs_path must be absolute (start with '/'), got {abs_path!r}")
    if len(abs_path) > 4096:
        # OS-level PATH_MAX is typically 4096 on linux; sane upper bound.
        raise ScratchPathError(
            f"abs_path length {len(abs_path)} exceeds 4096")
    # Defense against shell-metachar smuggling — same set as remote_subpath
    # but we still allow '/' (paths) and don't reject the leading slash.
    forbidden = _FORBIDDEN_SUBPATH_CHARS - {"/"}
    for c in abs_path:
        if c in forbidden:
            raise ScratchPathError(
                f"abs_path contains forbidden character {c!r} "
                f"(codepoint {ord(c)}): {abs_path!r}")
    # Reject `..` as ANY path component. normpath would collapse `a/b/..`
    # into `a` which is benign for a literal abs_path; but explicit `../`
    # smells like agent confusion. Refuse it so authorized paths cannot be
    # smuggled out of via traversal at the agent layer.
    if any(part == ".." for part in abs_path.split("/")):
        raise ScratchPathError(
            f"abs_path has a '..' traversal component: {abs_path!r}")
    normed = os.path.normpath(abs_path)
    if not normed.startswith("/"):
        raise ScratchPathError(
            f"abs_path normalizes to a non-absolute string: "
            f"{abs_path!r} → {normed!r}")
    return normed


# ---------------------------------------------------------------------------
# upload_to_project_path
# ---------------------------------------------------------------------------

def upload_to_project_path(project_name: str,
                          compute_env_name: str,
                          abs_path: str,
                          local_path: str,
                          *,
                          access_path: Optional[str] = None,
                          timeout: int = 600) -> dict:
    """Push a local file to an authorized project-workspace path on the
    given compute env.

    Authorization (Phase-1 explicit, NOT env-implicit):
      - The project must declare a `directories[]` entry on this env
        whose path contains `abs_path` (longest-prefix match)
      - That entry's `permissions:` must include `upload`

    Same path-safety + sha256-round-trip + overwrite-refusal rules as
    upload_to_scratch / upload_to_common_data. The DIFFERENCE: the path
    is supplied as a literal absolute path (the user's project workspace
    has a real directory layout the agent must respect), not as a
    relative remote_subpath auto-prefixed by something.

    Returns {success, compute_env, remote_path, sha256, bytes,
    duration_s, transferred_at} on success; {"error": "..."} on
    refusal/failure."""
    started = time.perf_counter()
    try:
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        env_type = env.get("type")
        if env_type not in ("ssh", "local"):
            return {"error": f"unsupported compute_env type {env_type!r}"}

        normed = _validate_abs_remote_path(abs_path)

        # Auth gate — Phase-1 directories[] only. Scratch + common_data
        # have their own dedicated primitives (upload_to_scratch /
        # upload_to_common_data) that use env-level auth; this primitive
        # is exclusively for user-declared project workspace paths.
        compute_access.check_permission(
            project, compute_env_name, normed,
            "upload_to_project_path")

        lp = _validate_local_path_for_upload(local_path)
        local_sha = _compute_local_sha256(lp)
        size_bytes = lp.stat().st_size

        # Overwrite refusal — uniform policy across all upload primitives.
        if env_type == "local":
            if Path(normed).exists():
                return {"error":
                    f"remote path already exists: {normed!r}. The upload "
                    f"contract refuses overwrites. Pick a fresh abs_path "
                    f"(e.g. timestamp-stamped) or delete the existing file "
                    f"first."}
        else:
            exist_cmd = (
                f"test -e {shlex.quote(normed)} && echo EXISTS || echo OK")
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
                    f"remote path already exists: {normed!r}. The upload "
                    f"contract refuses overwrites. Pick a fresh abs_path "
                    f"or delete the existing file first."}

        # Transfer.
        if env_type == "local":
            dest = Path(normed)
            _local_mkdir_parent(dest)
            shutil.copy(str(lp), str(dest))
        else:
            target = _build_remote_target(env, normed)
            mkdir_cmd = f"mkdir -p {shlex.quote(os.path.dirname(normed))}"
            mk_argv = _ssh_argv(env, mkdir_cmd)
            mk = subprocess.run(mk_argv, capture_output=True,
                                text=True, timeout=timeout)
            if mk.returncode != 0:
                hint = _ssh_failure_hint(mk.stderr, env.get("host", "?"))
                return {"error":
                    f"remote mkdir -p failed (rc={mk.returncode}): "
                    f"{mk.stderr.strip()}",
                    **({"hint": hint} if hint else {})}
            argv = _scp_argv(env, str(lp), target)
            sc = subprocess.run(argv, capture_output=True, text=True,
                                timeout=timeout)
            if sc.returncode != 0:
                hint = _ssh_failure_hint(sc.stderr, env.get("host", "?"))
                return {"error":
                    f"scp failed (rc={sc.returncode}): {sc.stderr.strip()}",
                    **({"hint": hint} if hint else {})}

        # Round-trip sha256.
        if env_type == "local":
            remote_sha = _compute_local_sha256(Path(normed))
        else:
            sha_argv = _ssh_argv(env, _remote_sha256_cmd(normed))
            sr = subprocess.run(sha_argv, capture_output=True, text=True,
                                timeout=timeout)
            if sr.returncode != 0:
                return {"error":
                    f"remote sha256sum failed (rc={sr.returncode}): "
                    f"{sr.stderr.strip()}"}
            remote_sha = _parse_sha256sum_output(sr.stdout) or ""
        if remote_sha != local_sha:
            return {"error":
                f"sha256 round-trip mismatch — local={local_sha} "
                f"remote={remote_sha!r}. File at {normed!r} may be "
                f"corrupt; investigate before relying on it."}

        return {
            "success":        True,
            "compute_env":    compute_env_name,
            "remote_path":    normed,
            "sha256":         local_sha,
            "bytes":          size_bytes,
            "duration_s":     round(time.perf_counter() - started, 3),
            "transferred_at": datetime.now(timezone.utc).isoformat(),
        }

    except (ScratchPathError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    except subprocess.TimeoutExpired as e:
        return {"error": f"transfer timed out after {e.timeout}s"}


# ---------------------------------------------------------------------------
# download_from_project_path
# ---------------------------------------------------------------------------

def download_from_project_path(project_name: str,
                              compute_env_name: str,
                              abs_path: str,
                              local_path: str,
                              *,
                              access_path: Optional[str] = None,
                              timeout: int = 600) -> dict:
    """Pull a file from an authorized project-workspace path back to local.

    Symmetric to upload_to_project_path; required permission is `download`
    on the matching directories[] entry. Discrete capabilities — `upload`
    alone does NOT satisfy `download`.

    Local-path safety: must NOT exist; parent must be writable.

    Returns {success, compute_env, remote_path, local_path, sha256,
    bytes, duration_s, fetched_at} on success; {"error": "..."} on
    refusal/failure."""
    started = time.perf_counter()
    try:
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        env_type = env.get("type")
        if env_type not in ("ssh", "local"):
            return {"error": f"unsupported compute_env type {env_type!r}"}

        normed = _validate_abs_remote_path(abs_path)
        # Phase-1 directories[] only — scratch/common_data have dedicated
        # download primitives.
        compute_access.check_permission(
            project, compute_env_name, normed,
            "download_from_project_path")

        lp = _validate_local_path_for_download(local_path)

        if env_type == "local":
            remote_p = Path(normed)
            if not remote_p.exists():
                return {"error": f"remote file does not exist: {normed!r}"}
            if remote_p.is_symlink():
                return {"error":
                    f"refusing to download a symlink at {normed!r} — "
                    f"defense against the env redirecting download outside "
                    f"the authorized path"}
            if not remote_p.is_file():
                return {"error":
                    f"remote path is not a regular file: {normed!r}"}
            size_bytes = remote_p.stat().st_size
            if size_bytes > _MAX_TRANSFER_BYTES:
                return {"error":
                    f"remote file {normed!r} is {size_bytes} bytes, "
                    f"exceeds head-node download cap {_MAX_TRANSFER_BYTES}; "
                    f"use a SLURM data_acquisition job to stage instead."}
            remote_sha = _compute_local_sha256(remote_p)
        else:
            stat_cmd = (
                f"stat -L -c '%F %s' {shlex.quote(normed)} && "
                f"test ! -L {shlex.quote(normed)}")
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
                    f"remote path is not a regular file: {normed!r} "
                    f"(stat: {st.stdout!r})"}
            try:
                size_bytes = int(parts[-1])
            except ValueError:
                return {"error":
                    f"could not parse remote size from {st.stdout!r}"}
            if size_bytes > _MAX_TRANSFER_BYTES:
                return {"error":
                    f"remote file {normed!r} is {size_bytes} bytes, "
                    f"exceeds head-node download cap {_MAX_TRANSFER_BYTES}; "
                    f"use a SLURM data_acquisition job to stage instead."}
            sha_argv = _ssh_argv(env, _remote_sha256_cmd(normed))
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

        # Transfer.
        if env_type == "local":
            shutil.copy(str(Path(normed)), str(lp))
        else:
            src_target = _build_remote_target(env, normed)
            argv = _scp_argv(env, src_target, str(lp))
            sc = subprocess.run(argv, capture_output=True, text=True,
                                timeout=timeout)
            if sc.returncode != 0:
                hint = _ssh_failure_hint(sc.stderr, env.get("host", "?"))
                return {"error":
                    f"scp failed (rc={sc.returncode}): {sc.stderr.strip()}",
                    **({"hint": hint} if hint else {})}

        local_sha = _compute_local_sha256(lp)
        if local_sha != remote_sha:
            try:
                lp.unlink()
            except OSError:
                pass
            return {"error":
                f"sha256 round-trip mismatch — remote={remote_sha} "
                f"local={local_sha}. Local file removed; the bytes that "
                f"arrived didn't match the source."}

        return {
            "success":     True,
            "compute_env": compute_env_name,
            "remote_path": normed,
            "local_path":  str(lp),
            "sha256":      remote_sha,
            "bytes":       size_bytes,
            "duration_s":  round(time.perf_counter() - started, 3),
            "fetched_at":  datetime.now(timezone.utc).isoformat(),
        }

    except (ScratchPathError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    except subprocess.TimeoutExpired as e:
        return {"error": f"transfer timed out after {e.timeout}s"}
