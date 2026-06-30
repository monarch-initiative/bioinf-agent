"""
Scratch sandbox primitives — upload_to_scratch + download_from_scratch.

The agent's writable sandbox on a compute env. Sibling to `snapshot.py`
(Phase 1's read-only primitive); same ControlMaster ssh pattern, same
sha256-anchored round-trip. New surface: bytes move in both directions
under env-implicit authorization.

Authorization model (env-implicit)
----------------------------------
The env-level `agent_scratch_target` block declares:
  - the path that exists on the env
  - the capabilities supported (`upload`, `download`, `exec` —
    plus optionally `file_name_only` for snapshot visibility)

Any project listed under this env via `compute_env_access` inherits
the target en bloc. The project's `compute_env_access[].directories[]`
list is reserved for PROJECT-SPECIFIC paths (the project's own data,
typically protected). There is NO per-project re-declaration of env-
level target paths.

The security posture this enforces: env-level target paths are PUBLIC-
DATA ZONES (no PHI, no protected data). Anything sensitive goes in a
project's `directories[]` entry, where `file_name_only` is opt-in and
the agent can never list a dir it wasn't explicitly authorized for.

Multi-project isolation: every transfer is auto-prefixed with the
project name (`<scratch>/<project>/<remote_subpath>`) so two projects
on the same env cannot accidentally collide on the same scratch path.

Constrained operation contract
------------------------------
Two primitives emit ONE subprocess invocation per phase, with a pinned
shape:

  local-mode:  shutil.copy + hashlib.sha256                (zero subprocess)
  ssh-mode:    `scp -o BatchMode=yes …` + `ssh … sha256sum`
               (BatchMode = fail fast, no password prompt; piggybacks on
               the user's open ssh ControlMaster the same way snapshot does)

`remote_subpath` is the only agent-supplied path component (the scratch
root + project prefix come from the validated config), and it goes
through `_validate_remote_subpath` (pure-string, no I/O) BEFORE any
path resolution. Resolved paths are re-checked to be inside the
project's scratch root.

Trust contract
--------------
Every transfer records a sha256 of the local bytes AND (ssh-mode) a
sha256 computed remotely. Mismatch ⇒ refuse to declare success. The
laptop is the verifier; the cluster is the executor.

Size cap
--------
`_MAX_TRANSFER_BYTES` (5 GiB) is the practical limit for head-node
transfers. Larger payloads must go through `submit_cluster_job
(job_type=data_acquisition)` (Phase 2, Step 6) which curl-resumes
INSIDE a SLURM job, or eventually Globus (Phase 3).
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
    """A remote_subpath / local_path / project_name failed validation.
    Raised BEFORE any subprocess runs. Surfaces as {"error": "..."} from
    the primitives."""


def _validate_remote_subpath(remote_subpath: str) -> str:
    """Return the normalized subpath if it passes every check, else raise
    ScratchPathError. The normalization is pure-string (no symlink resolve,
    no filesystem touch) — `os.path.normpath` collapses `a/./b` → `a/b`
    and we then re-check for `..` segments at the security boundary."""
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
            f"agent_scratch_target.path and the project-name prefix is auto-applied")
    # Defense against shell-metachar smuggling.
    for c in remote_subpath:
        if c in _FORBIDDEN_SUBPATH_CHARS:
            raise ScratchPathError(
                f"remote_subpath contains forbidden character "
                f"{c!r} (codepoint {ord(c)}): {remote_subpath!r}")
    # Reject the literal token `..` as ANY path component.
    if any(part == ".." for part in remote_subpath.split("/")):
        raise ScratchPathError(
            f"remote_subpath has a '..' traversal component: {remote_subpath!r}")
    # Normalize the path.
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


def _validate_project_name_token(project_name: str) -> str:
    """The project name is auto-prepended to remote_subpath as the multi-
    project isolation prefix; it MUST be a safe token (alnum + `_-`,
    ≤64 chars) so it can't smuggle traversal or shell metacharacters into
    the resolved path. Returns the unchanged name if it passes; raises
    ScratchPathError otherwise."""
    if not isinstance(project_name, str):
        raise ScratchPathError(
            f"project_name must be a string, got {type(project_name).__name__}")
    if not compute_access._is_safe_token(project_name):
        raise ScratchPathError(
            f"project_name {project_name!r} is not a safe token (alnum + "
            f"'_-' only, ≤64 chars). It's used as a path prefix; allowing "
            f"unsafe chars would let a project name smuggle traversal or "
            f"shell metacharacters into the resolved path.")
    return project_name


def _resolve_remote_path(scratch_root: str, project_name: str,
                        remote_subpath_norm: str) -> str:
    """Join `<scratch_root>/<project_name>/<remote_subpath_norm>` and
    re-verify the result is inside the project's scratch namespace. The
    project_name auto-prefix is the multi-project isolation knob: two
    projects on the same env cannot collide on the same scratch path
    because their resolved paths start with different project_name
    components."""
    root = scratch_root.rstrip("/")
    proj_root = f"{root}/{project_name}"
    joined = f"{proj_root}/{remote_subpath_norm}"
    joined_norm = os.path.normpath(joined)
    if not (joined_norm == proj_root or joined_norm.startswith(proj_root + "/")):
        raise ScratchPathError(
            f"resolved remote path {joined_norm!r} escapes project's scratch "
            f"namespace {proj_root!r} — refusing")
    return joined_norm


def _validate_local_path_for_upload(local_path: str) -> Path:
    """Check the local file we're uploading: must exist, be a REGULAR
    file (no symlinks — defense against a user's home symlink redirecting
    to /etc/shadow), and be under the size cap."""
    if not isinstance(local_path, str) or not local_path:
        raise ScratchPathError(f"local_path must be a non-empty string, got {local_path!r}")
    p = Path(local_path)
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


def _validate_local_path_for_download(local_path: str) -> Path:
    """Check the local destination for a download: must be a path string
    whose parent directory exists and is writable; the path itself must
    NOT exist (no silent overwrites — same as upload's never-overwrite
    contract on the remote side)."""
    if not isinstance(local_path, str) or not local_path:
        raise ScratchPathError(f"local_path must be a non-empty string, got {local_path!r}")
    p = Path(local_path)
    if p.exists():
        raise ScratchPathError(
            f"local_path {local_path!r} already exists; download refuses to "
            f"overwrite. Remove the existing file (or pass a fresh path) "
            f"and retry.")
    parent = p.parent
    if not parent.exists():
        raise ScratchPathError(
            f"local_path's parent directory {str(parent)!r} does not exist; "
            f"create it before downloading")
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
    spike RSS."""
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
    Path is the ONLY interpolated piece and is shlex.quote'd."""
    return f"sha256sum {shlex.quote(remote_path)}"


def _parse_sha256sum_output(stdout: str) -> Optional[str]:
    """Parse the `sha256sum` output: '<hex>  <path>\\n'. Returns the hex
    or None on any malformation. Tolerates MOTD banners on login shells —
    skip any line that doesn't start with a 64-hex digest."""
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
    Piggybacks on the user's ControlMaster the same way ssh does."""
    return ["scp", "-o", "BatchMode=yes", "-p", src, dst]


def _build_remote_target(env: dict, abs_remote_path: str) -> str:
    """Construct the `user@host:/abs/path` string for scp. The path is
    NOT shell-quoted — scp argv treats this as one argv element and the
    remote-side path is passed to the remote scp daemon which does its
    own minimal interpretation. We've already rejected shell metacharacters
    + whitespace in remote_subpath."""
    if " " in abs_remote_path or "\t" in abs_remote_path:
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
    """Create parent dirs for a local-mode "remote" path. Idempotent."""
    remote_abs_path.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# upload_to_scratch — the primitive
# ---------------------------------------------------------------------------

def upload_to_scratch(project_name: str,
                     compute_env_name: str,
                     local_path: str,
                     remote_subpath: str,
                     *,
                     async_globus: bool = False,
                     access_path: Optional[str] = None,
                     timeout: int = 600) -> dict:
    """Push a local file into the agent's scratch sandbox on `compute_env_name`,
    under this project's auto-prefixed namespace.

    Authorization (env-implicit grant):
      1. Project exists in projects_access.yaml
      2. Project has a `compute_env_access` entry naming `compute_env_name`
      3. The env declares an `agent_scratch_target` block whose
         `permissions:` includes `upload`

    Multi-project isolation: the resolved path is auto-prefixed with
    project_name. `upload_to_scratch('proj_a', ..., remote_subpath='x.txt')`
    lands at `<scratch.path>/proj_a/x.txt` — proj_b cannot collide.

    Path safety:
      a. `local_path`: exists, is a REGULAR file (no symlinks), under
         the 5 GiB head-node cap
      b. `remote_subpath`: non-empty, ≤255 chars, no leading '/', no '..',
         no shell metacharacters, normalizes inside the project's
         scratch namespace
      c. `project_name`: safe token (alnum + '_-', ≤64 chars) — used as
         the path-prefix component

    Trust:
      i.  Compute local sha256 BEFORE transfer
      ii. Transfer via shutil.copy (local) OR `scp -o BatchMode=yes`
          (ssh) — pinned shape, no shell
      iii. Compute remote sha256 via `sha256sum` (ssh) or local read
           (local-mode)
      iv. Mismatch ⇒ raise; success only on byte-perfect round-trip

    Returns {success, compute_env, remote_path, sha256, bytes, duration_s,
    transferred_at} on success; {"error": "..."} on any failure (no
    exception escapes — MCP surface is dict-in-dict-out).
    """
    started = time.perf_counter()
    try:
        # 1. Validate the project_name token BEFORE any I/O. It becomes part
        # of every path we touch on the env, so it must be safe.
        _validate_project_name_token(project_name)

        # 2. Load + look up project + env.
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        env_type = env.get("type")
        if env_type not in ("ssh", "local"):
            return {"error": f"unsupported compute_env type {env_type!r}"}

        # 3. The env must declare a scratch target.
        scratch = compute_access.get_agent_scratch_target(env)
        if scratch is None:
            return {"error":
                f"compute_env {compute_env_name!r} has no agent_scratch_target "
                f"declared in projects_access.yaml — add the block under that "
                f"compute_envs[] entry to enable upload_to_scratch."}
        scratch_root = scratch.get("path", "").rstrip("/")

        # 4. Env-implicit grant check (project on env + env-target advertises
        # the required capability). NO project-level directories[] walk.
        compute_access.check_env_target_capability(
            project, compute_env_name, scratch, "upload_to_scratch",
            "agent_scratch_target")

        # 5. Path safety (pure-string; no I/O).
        norm_sub = _validate_remote_subpath(remote_subpath)
        abs_remote = _resolve_remote_path(scratch_root, project_name, norm_sub)

        # 6. Local-file safety + size cap.
        lp = _validate_local_path_for_upload(local_path)

        # 7. Compute local sha256 (anchor before transfer).
        local_sha = _compute_local_sha256(lp)
        size_bytes = lp.stat().st_size

        # 8. Refuse to overwrite a pre-existing remote file. The `upload`
        # permission contract is "write NEW files; never overwrites" —
        # documented in projects_access.yaml.example. Pre-check the
        # destination; surface a clear error if it exists so the agent
        # knows to pick a fresh subpath rather than silently clobbering.
        if env_type == "local":
            if Path(abs_remote).exists():
                return {"error":
                    f"remote path already exists: {abs_remote!r}. The "
                    f"upload contract refuses overwrites. Pick a fresh "
                    f"remote_subpath (e.g. timestamp-stamped) or delete "
                    f"the existing file first."}
        else:  # ssh
            exist_cmd = f"test -e {shlex.quote(abs_remote)} && echo EXISTS || echo OK"
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

        # 9. Mkdir parent — uniform across providers (uses ssh for ssh-mode
        # envs regardless of data_transfer.type; for local-mode it's
        # Path.mkdir). The wire-protocol provider only owns byte movement.
        if env_type == "local":
            _local_mkdir_parent(Path(abs_remote))
        else:
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

        # 10. Transfer + verify (dispatched by data_transfer.type).
        if env_type == "local":
            # Inline local-copy + recompute sha both ends. No wire protocol.
            shutil.copy(str(lp), abs_remote)
            remote_sha = _compute_local_sha256(Path(abs_remote))
            if remote_sha != local_sha:
                return {"error":
                    f"sha256 round-trip mismatch — local={local_sha} "
                    f"remote={remote_sha!r}. File at {abs_remote!r} may be "
                    f"corrupt; investigate before relying on it."}
            provider_info = {"provider": "local_copy",
                             "verified_method": "sha256_round_trip"}
        else:
            from agent.skills import transfer_providers
            provider = transfer_providers.get_transfer_provider(env)
            pr = provider.upload_one(
                env=env, local_path=lp, abs_remote_path=abs_remote,
                local_sha256=local_sha, timeout=timeout,
                async_return=async_globus,
                label=f"upload_to_scratch {project_name}/{remote_subpath}")
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
# download_from_scratch — the primitive
# ---------------------------------------------------------------------------

def download_from_scratch(project_name: str,
                         compute_env_name: str,
                         remote_subpath: str,
                         local_path: str,
                         *,
                         async_globus: bool = False,
                         access_path: Optional[str] = None,
                         timeout: int = 600) -> dict:
    """Pull a file from this project's scratch namespace back to a local
    path.

    Symmetric to `upload_to_scratch` — same env-implicit authorization,
    same auto-prefix-by-project, same path-safety chain. The required
    capability is `download` (discrete from `upload`; not a lattice).

    Local-path safety:
      - local_path must NOT exist yet (no silent overwrites)
      - its parent directory must exist and be writable

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

        scratch = compute_access.get_agent_scratch_target(env)
        if scratch is None:
            return {"error":
                f"compute_env {compute_env_name!r} has no agent_scratch_target "
                f"declared — download_from_scratch requires it."}
        scratch_root = scratch.get("path", "").rstrip("/")

        # Env-implicit grant: project on env + target advertises `download`.
        compute_access.check_env_target_capability(
            project, compute_env_name, scratch, "download_from_scratch",
            "agent_scratch_target")

        norm_sub = _validate_remote_subpath(remote_subpath)
        abs_remote = _resolve_remote_path(scratch_root, project_name, norm_sub)

        lp = _validate_local_path_for_download(local_path)

        # Compute remote sha256 BEFORE transfer — that's the anchor.
        if env_type == "local":
            remote_p = Path(abs_remote)
            if not remote_p.exists():
                return {"error":
                    f"remote file does not exist: {abs_remote!r}"}
            if remote_p.is_symlink():
                return {"error":
                    f"refusing to download a symlink at {abs_remote!r} — "
                    f"defense against the env redirecting download outside scratch"}
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
                label=f"download_from_scratch {project_name}/{remote_subpath}")
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
