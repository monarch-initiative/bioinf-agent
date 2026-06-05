"""
Scratch sandbox primitives — upload_to_scratch + fetch_from_scratch.

The agent's writable sandbox on a compute env. Sibling to `snapshot.py`
(Phase 1's read-only primitive); same authorization shape, same
permission gate, same ControlMaster ssh pattern. New surface: bytes
move in both directions, sha256-anchored round-trip on every transfer.

Constrained operation contract
------------------------------
Two primitives — `upload_to_scratch` and `fetch_from_scratch` — each
emits ONE subprocess invocation per phase, with a pinned shape:

  local-mode:  shutil.copy + hashlib.sha256                (zero subprocess)
  ssh-mode:    `scp -o BatchMode=yes …` + `ssh … sha256sum`
               (BatchMode = fail fast, no password prompt; piggybacks on
               the user's open ssh ControlMaster the same way snapshot does)

The `remote_subpath` is the ONLY agent-supplied path component (the
scratch root comes from the env's validated `agent_scratch_target`),
and it goes through `_validate_remote_subpath` (pure-string, no I/O)
BEFORE any path resolution. Resolved paths are then re-checked to be
inside the scratch root (defense-in-depth against symlink trickery
that could redirect outside the sandbox).

Permission gate
---------------
Same as snapshot: every transfer goes through
`compute_access.check_permission(project, env, path, op)` BEFORE any
subprocess runs. Upload requires `upload`; fetch requires `fetch`.
A breach of one direction does not grant the other.

Trust contract
--------------
Every transfer records a sha256 of the local bytes AND (ssh-mode) a
sha256 computed remotely. Mismatch ⇒ refuse to declare success. This
is the laptop/cluster trust anchor — the cluster never produces bytes
that pass the round-trip without actually receiving/sending them.

Size cap
--------
`_MAX_TRANSFER_BYTES` (5 GB) is the practical limit for head-node
transfers. Larger payloads must go through `submit_cluster_job
(job_type=data_acquisition)` (Phase 2, Step 6) which curl-resumes
INSIDE a SLURM job, or eventually Globus (Phase 3). A 50 GB scp over
the head node is a poor citizen; refuse rather than enable.

Module surface
--------------
This module exposes two functions: `upload_to_scratch(...)` and
`fetch_from_scratch(...)`. The MCP wrappers live in
`agent/mcp_tools/bridge_tools.py`. Cheat-guards live under
`tests/integration/honesty/L14_compute_env_safety/`.
"""
from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.skills import compute_access
from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint  # reuse Phase 1 ssh shape


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Head-node transfer cap. Anything larger should go through a SLURM-job
# data_acquisition (Step 6) or Globus (Phase 3) — not over the agent's
# scp+head-node pipe. Refusing keeps us a good citizen on shared infra.
_MAX_TRANSFER_BYTES: int = 5 * 1024 * 1024 * 1024  # 5 GiB

# remote_subpath: the only agent-supplied path component. Constraints below
# are pure-string (no I/O). Total length cap defends against header-injection
# attempts where the path is later embedded in an audit-record string.
_MAX_REMOTE_SUBPATH_LEN: int = 255

# Characters never allowed in remote_subpath. The set is conservative — we
# refuse any byte that means something special to a shell, to find -printf,
# or to sbatch headers. Newline injection is the canonical sbatch attack;
# whitespace would corrupt the scp/ssh argv assembly downstream.
_FORBIDDEN_SUBPATH_CHARS: frozenset[str] = frozenset(
    "\x00\n\r\t ;|&$`<>(){}[]*?\"'\\")


# ---------------------------------------------------------------------------
# Pure-string validators (no I/O — these MUST be safe to call before any
# subprocess; the L14 cheat-guards pin them per-input)
# ---------------------------------------------------------------------------

class ScratchPathError(ValueError):
    """A remote_subpath / local_path failed validation. Raised BEFORE any
    subprocess runs. Surfaces as `{"error": "..."}` from the primitives."""


def _validate_remote_subpath(remote_subpath: str) -> str:
    """Return the normalized subpath if it passes every check, else raise
    ScratchPathError. The normalization is pure-string (no symlink resolve,
    no filesystem touch) — `os.path.normpath` collapses `a/./b` → `a/b`
    and rejects `..` segments at the security boundary by inspecting the
    output."""
    if not isinstance(remote_subpath, str):
        raise ScratchPathError(
            f"remote_subpath must be a string, got {type(remote_subpath).__name__}")
    if not remote_subpath:
        raise ScratchPathError("remote_subpath must be non-empty")
    if len(remote_subpath) > _MAX_REMOTE_SUBPATH_LEN:
        raise ScratchPathError(
            f"remote_subpath length {len(remote_subpath)} exceeds "
            f"cap {_MAX_REMOTE_SUBPATH_LEN}")
    if remote_subpath.startswith("/"):
        raise ScratchPathError(
            f"remote_subpath must be RELATIVE (no leading '/'), got "
            f"{remote_subpath!r} — the scratch root comes from the env's "
            f"agent_scratch_target.path")
    # Defense against shell-metachar smuggling. The scp/ssh argv pipeline
    # quotes via shlex, but a leaked metachar in the remote_subpath would
    # corrupt the audit-trail (logged verbatim) and may slip through if a
    # future caller forgets to quote.
    for c in remote_subpath:
        if c in _FORBIDDEN_SUBPATH_CHARS:
            raise ScratchPathError(
                f"remote_subpath contains forbidden character "
                f"{c!r} (codepoint {ord(c)}): {remote_subpath!r}")
    # Reject the literal token `..` as ANY path component. `normpath` will
    # collapse `a/b/../c` → `a/c`, which is fine, but `../escape` → `../escape`
    # which we detect by re-splitting.
    if any(part == ".." for part in remote_subpath.split("/")):
        raise ScratchPathError(
            f"remote_subpath has a '..' traversal component: {remote_subpath!r}")
    # Normalize the path. After this, the only legal output is a relative
    # path with no leading slash, no `..` components, no `.` components.
    norm = os.path.normpath(remote_subpath)
    if norm.startswith("/") or norm.startswith(".."):
        raise ScratchPathError(
            f"remote_subpath normalizes outside its own root: "
            f"{remote_subpath!r} → {norm!r}")
    if norm == "." or norm == "":
        raise ScratchPathError(
            f"remote_subpath normalizes to empty / current-dir: "
            f"{remote_subpath!r}")
    return norm


def _resolve_remote_path(scratch_root: str, remote_subpath_norm: str) -> str:
    """Join the scratch root + validated normalized subpath, then re-verify
    the result is inside the root. The pre-checks in
    `_validate_remote_subpath` already guarantee this, but we re-check at
    join time so a future refactor that bypasses validation surfaces here."""
    root = scratch_root.rstrip("/")
    joined = f"{root}/{remote_subpath_norm}"
    # Final sanity: even after join, normpath must produce a string with
    # the root as a strict prefix. This is the defense-in-depth layer.
    joined_norm = os.path.normpath(joined)
    if not (joined_norm == root or joined_norm.startswith(root + "/")):
        raise ScratchPathError(
            f"resolved remote path {joined_norm!r} escapes scratch root "
            f"{root!r} — refusing")
    return joined_norm


def _validate_local_path_for_upload(local_path: str) -> Path:
    """Check the local file we're uploading: must exist, be a REGULAR
    file (no symlinks — defense against a user's home symlink redirecting
    to /etc/shadow), and be under the size cap.

    Returns the resolved Path. Raises ScratchPathError on any failure.
    Does NOT touch network; pure local stat."""
    if not isinstance(local_path, str) or not local_path:
        raise ScratchPathError(f"local_path must be a non-empty string, got {local_path!r}")
    p = Path(local_path)
    # is_symlink check uses lstat — must happen BEFORE exists() (which follows).
    if p.is_symlink():
        raise ScratchPathError(
            f"local_path {local_path!r} is a symlink; upload refuses symlinks "
            f"to prevent redirect attacks. Pass the real path.")
    if not p.exists():
        raise ScratchPathError(f"local_path {local_path!r} does not exist")
    if not p.is_file():
        raise ScratchPathError(
            f"local_path {local_path!r} is not a regular file "
            f"(directories / devices are not supported by upload_to_scratch)")
    try:
        sz = p.stat().st_size
    except OSError as e:
        raise ScratchPathError(f"local_path {local_path!r} stat failed: {e}") from e
    if sz > _MAX_TRANSFER_BYTES:
        raise ScratchPathError(
            f"local_path {local_path!r} is {sz} bytes — exceeds head-node "
            f"transfer cap of {_MAX_TRANSFER_BYTES} bytes ({_MAX_TRANSFER_BYTES // (1024**3)} GiB). "
            f"Use submit_cluster_job(job_type='data_acquisition') for large "
            f"data or Globus for >50GB (Phase 3).")
    return p


def _validate_local_path_for_fetch(local_path: str) -> Path:
    """Check the local destination for a fetch: must be an absolute-or-
    relative path string whose parent directory exists and is writable.
    The path itself must NOT exist yet (no silent overwrites — same as
    upload's never-overwrite contract on the remote side)."""
    if not isinstance(local_path, str) or not local_path:
        raise ScratchPathError(f"local_path must be a non-empty string, got {local_path!r}")
    p = Path(local_path)
    if p.exists():
        raise ScratchPathError(
            f"local_path {local_path!r} already exists; fetch refuses to "
            f"overwrite. Remove the existing file (or pass a fresh path) "
            f"and retry.")
    parent = p.parent
    if not parent.exists():
        raise ScratchPathError(
            f"local_path's parent directory {str(parent)!r} does not exist; "
            f"create it before fetching")
    if not os.access(parent, os.W_OK):
        raise ScratchPathError(
            f"local_path's parent directory {str(parent)!r} is not writable")
    return p


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

_HASH_CHUNK: int = 1024 * 1024   # 1 MiB; balances syscalls vs RSS


def _compute_local_sha256(path: Path) -> str:
    """sha256 of a local file. Chunked read so a multi-GB file doesn't
    spike RSS. Caller is responsible for not handing in a path that
    failed `_validate_local_path_for_upload` (exists, regular, sized)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _remote_sha256_cmd(remote_path: str) -> str:
    """The remote shell string for `sha256sum <path>`. Pinned by a test.
    Path is the ONLY interpolated piece and is shlex.quote'd. Returns the
    LITERAL string; the caller wraps it in ssh argv via `_ssh_argv`."""
    return f"sha256sum {shlex.quote(remote_path)}"


def _parse_sha256sum_output(stdout: str) -> Optional[str]:
    """Parse the `sha256sum` output: '<hex>  <path>\\n'. Returns the hex
    or None on any malformation. sha256sum's output is stable across
    coreutils versions but we tolerate trailing whitespace + multiple
    lines (some sites' login shells emit MOTD banners)."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if parts and len(parts[0]) == 64 and all(
                c in "0123456789abcdef" for c in parts[0]):
            return parts[0]
    return None


# ---------------------------------------------------------------------------
# Subprocess shapes (pinned by tests)
# ---------------------------------------------------------------------------

def _scp_argv(env: dict, src: str, dst: str) -> list[str]:
    """The scp argv for an ssh-mode env. BatchMode=yes (fail fast, no
    password prompt) and -p (preserve mtime, useful for debugging).
    Piggybacks on the user's ControlMaster the same way ssh does — no
    explicit -S socket arg needed; ssh_config / the open ControlMaster
    take over.

    `src` and `dst` are passed as separate argv elements; no shell
    interpretation. One of them is of the form 'user@host:/abs/path' and
    that ARGV element is the only thing that ever names the remote side.
    """
    host = env["host"]
    user = env.get("user")
    target_host = f"{user}@{host}" if user else host
    # We don't string-format `target_host` into the src/dst here — the
    # caller does, because direction (upload vs fetch) decides which side
    # carries the prefix. We just emit the canonical scp argv.
    return ["scp", "-o", "BatchMode=yes", "-p", src, dst]


def _build_remote_target(env: dict, abs_remote_path: str) -> str:
    """Construct the `user@host:/abs/path` string for scp. The path is
    NOT shell-quoted — scp argv treats this as one argv element and the
    remote-side path is passed to the remote scp daemon which does its
    own minimal interpretation. We've already rejected shell metacharacters
    in remote_subpath, so this is safe by construction.

    However, paths containing whitespace would be a problem if we ever
    allowed them — our forbidden-char set includes space-equivalents,
    but to be defensive we ASSERT no space in the resolved path."""
    if " " in abs_remote_path or "\t" in abs_remote_path:
        # Should never happen — _validate_remote_subpath forbids these.
        # Assertion-style raise: this is a bug, not a user error.
        raise ScratchPathError(
            f"resolved remote path contains whitespace: {abs_remote_path!r}")
    host = env["host"]
    user = env.get("user")
    target_host = f"{user}@{host}" if user else host
    return f"{target_host}:{abs_remote_path}"


# ---------------------------------------------------------------------------
# Local-mode helpers (zero subprocess)
# ---------------------------------------------------------------------------

def _local_mkdir_parent(remote_abs_path: Path) -> None:
    """Create parent dirs for a local-mode "remote" path. Idempotent.
    We use 0o755 to keep things readable; on a real cluster the scp +
    sftp daemon will likewise mkdir as needed via the user's umask."""
    remote_abs_path.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# upload_to_scratch — the primitive
# ---------------------------------------------------------------------------

def upload_to_scratch(project_name: str,
                     compute_env_name: str,
                     local_path: str,
                     remote_subpath: str,
                     *,
                     access_path: Optional[str] = None,
                     timeout: int = 600) -> dict:
    """Push a local file into the project's authorized scratch sandbox on
    `compute_env_name`.

    Authorization chain (every link must hold):
      1. Project exists in projects_access.yaml
      2. compute_env_name is one of the project's compute_env_access entries
      3. The env declares an `agent_scratch_target` (path + permissions)
      4. The project's compute_env_access[].directories[] grants `upload`
         on a path that contains the env's scratch root (check_permission
         finds a matching entry via longest-prefix; here the resolved
         absolute path inside scratch is checked)

    Path safety chain:
      a. `local_path`: exists, is a REGULAR file (no symlinks), under the
         5 GiB head-node cap
      b. `remote_subpath`: non-empty, ≤255 chars, no leading '/',
         no '..', no shell metacharacters, normalizes inside scratch root
      c. The resolved absolute remote path is re-checked to be inside
         the scratch root (defense-in-depth)

    Trust chain:
      i.  Compute local sha256 BEFORE transfer
      ii. Transfer via shutil.copy (local) OR `scp -o BatchMode=yes`
          (ssh) — pinned shape, no shell
      iii. Compute remote sha256 via `sha256sum` (ssh-mode) or local read
           (local-mode)
      iv. Mismatch ⇒ raise; success only on byte-perfect round-trip

    Returns:
      {
        "success":     True,
        "compute_env": "<env_name>",
        "remote_path": "/abs/scratch/.../<remote_subpath>",
        "sha256":      "<hex>",
        "bytes":       <int>,
        "duration_s":  <float>,
        "transferred_at": "<iso utc>",
      }

    Returns {"error": "..."} on any validation/transfer/round-trip failure.
    NEVER raises to the caller — the MCP surface is dict-in-dict-out.
    """
    started = time.perf_counter()
    try:
        # 1. Load + look up project + env.
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        env_type = env.get("type")
        if env_type not in ("ssh", "local"):
            return {"error": f"unsupported compute_env type {env_type!r}"}

        # 2. The env must declare a scratch target.
        scratch = compute_access.get_agent_scratch_target(env)
        if scratch is None:
            return {"error":
                f"compute_env {compute_env_name!r} has no agent_scratch_target "
                f"declared in projects_access.yaml — add the block under that "
                f"compute_envs[] entry to enable upload_to_scratch."}
        scratch_root = scratch.get("path", "").rstrip("/")

        # 3. Path safety (pure-string; no I/O).
        norm_sub = _validate_remote_subpath(remote_subpath)
        abs_remote = _resolve_remote_path(scratch_root, norm_sub)

        # 4. Local-file safety + size cap.
        lp = _validate_local_path_for_upload(local_path)

        # 5. Permission gate — the resolved abs path is project-authorized
        # for the `upload_to_scratch` operation. Identical shape to Phase 1.
        compute_access.check_permission(
            project, compute_env_name, abs_remote, "upload_to_scratch")

        # 6. Compute local sha256 (anchor before transfer).
        local_sha = _compute_local_sha256(lp)
        size_bytes = lp.stat().st_size

        # 7. Transfer.
        if env_type == "local":
            dest = Path(abs_remote)
            _local_mkdir_parent(dest)
            # shutil.copy preserves content; we don't need permissions (the
            # daemon-equivalent for cluster mode wouldn't either).
            shutil.copy(str(lp), str(dest))
        else:  # ssh
            target = _build_remote_target(env, abs_remote)
            # Best-effort remote-mkdir of the subpath's parent dir; some
            # scp servers won't auto-create intermediate dirs. We do this
            # via a small ssh call BEFORE the scp.
            sub_parent_norm = os.path.dirname(norm_sub)
            if sub_parent_norm:
                parent_abs = _resolve_remote_path(scratch_root, sub_parent_norm)
                mkdir_cmd = f"mkdir -p {shlex.quote(parent_abs)}"
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

        # 8. Verify round-trip sha256.
        if env_type == "local":
            remote_sha = _compute_local_sha256(Path(abs_remote))
        else:
            sha_cmd = _remote_sha256_cmd(abs_remote)
            sha_argv = _ssh_argv(env, sha_cmd)
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
                f"remote={remote_sha!r}. File at {abs_remote!r} may be "
                f"corrupt; investigate before relying on it."}

        return {
            "success":        True,
            "compute_env":    compute_env_name,
            "remote_path":    abs_remote,
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
# fetch_from_scratch — the primitive
# ---------------------------------------------------------------------------

def fetch_from_scratch(project_name: str,
                      compute_env_name: str,
                      remote_subpath: str,
                      local_path: str,
                      *,
                      access_path: Optional[str] = None,
                      timeout: int = 600) -> dict:
    """Pull a file from the project's authorized scratch sandbox back to
    a local path.

    Symmetric to `upload_to_scratch` — same authorization chain (with
    `fetch` instead of `upload` as the required permission), same
    path safety chain, same round-trip sha256 verification.

    Local-path safety:
      - local_path must NOT exist yet (no silent overwrites)
      - its parent directory must exist and be writable

    Returns:
      {
        "success":     True,
        "compute_env": "<env_name>",
        "remote_path": "/abs/scratch/.../<remote_subpath>",
        "local_path":  "/abs/local/path",
        "sha256":      "<hex>",
        "bytes":       <int>,
        "duration_s":  <float>,
        "fetched_at":  "<iso utc>",
      }
    """
    started = time.perf_counter()
    try:
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        env_type = env.get("type")
        if env_type not in ("ssh", "local"):
            return {"error": f"unsupported compute_env type {env_type!r}"}

        scratch = compute_access.get_agent_scratch_target(env)
        if scratch is None:
            return {"error":
                f"compute_env {compute_env_name!r} has no agent_scratch_target "
                f"declared — fetch_from_scratch requires it."}
        scratch_root = scratch.get("path", "").rstrip("/")

        norm_sub = _validate_remote_subpath(remote_subpath)
        abs_remote = _resolve_remote_path(scratch_root, norm_sub)

        # Permission gate (FETCH this time).
        compute_access.check_permission(
            project, compute_env_name, abs_remote, "fetch_from_scratch")

        # Local destination: must not exist; parent must exist + be writable.
        lp = _validate_local_path_for_fetch(local_path)

        # Compute remote sha256 BEFORE transfer — that's the anchor.
        if env_type == "local":
            remote_p = Path(abs_remote)
            if not remote_p.exists():
                return {"error":
                    f"remote file does not exist: {abs_remote!r}"}
            if remote_p.is_symlink():
                return {"error":
                    f"refusing to fetch a symlink at {abs_remote!r} — "
                    f"defense against the env redirecting fetch outside scratch"}
            if not remote_p.is_file():
                return {"error":
                    f"remote path is not a regular file: {abs_remote!r}"}
            size_bytes = remote_p.stat().st_size
            if size_bytes > _MAX_TRANSFER_BYTES:
                return {"error":
                    f"remote file {abs_remote!r} is {size_bytes} bytes, "
                    f"exceeds head-node fetch cap {_MAX_TRANSFER_BYTES}; "
                    f"use a SLURM data_acquisition job to stage instead."}
            remote_sha = _compute_local_sha256(remote_p)
        else:
            # ssh-mode: stat (size + symlink check) then sha256.
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
                    f"exceeds head-node fetch cap {_MAX_TRANSFER_BYTES}; "
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

        # Transfer.
        if env_type == "local":
            shutil.copy(str(Path(abs_remote)), str(lp))
        else:
            src_target = _build_remote_target(env, abs_remote)
            argv = _scp_argv(env, src_target, str(lp))
            sc = subprocess.run(argv, capture_output=True, text=True,
                                timeout=timeout)
            if sc.returncode != 0:
                hint = _ssh_failure_hint(sc.stderr, env.get("host", "?"))
                return {"error":
                    f"scp failed (rc={sc.returncode}): {sc.stderr.strip()}",
                    **({"hint": hint} if hint else {})}

        # Verify round-trip sha256.
        local_sha = _compute_local_sha256(lp)
        if local_sha != remote_sha:
            # Clean up partial file on mismatch — caller doesn't get a
            # silently-corrupt artifact lingering on disk.
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
            "remote_path": abs_remote,
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
