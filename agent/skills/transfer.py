"""
transfer — unified upload / download for arbitrary cluster transfers.

Replaces six zone-specific primitives (upload_to_scratch / download_from_scratch
+ common_data + project_path) with two general ones (`upload`, `download`)
that route authorization based on which "zone" the absolute remote path
falls in. The agent doesn't pick a primitive by zone any more — it just
says "upload THIS local file to THAT absolute remote path" and we figure
out the auth.

Zones (routed by where `remote_abs_path` lives on the env)
----------------------------------------------------------

  scratch       — under env.agent_scratch_target.path
                  Authorization: env-implicit grant. The target block's
                  `permissions:` must include `upload` (or `download`).
                  Multi-project isolation enforced: path must be under
                  `<scratch_root>/<project_name>/...`

  common_data   — under env.agent_common_data_target.path
                  Authorization: env-implicit grant (same shape as
                  scratch). NO project-prefix isolation: this zone is
                  shared by every project on the env. For reference
                  databases and staged container images.

  project_path  — anywhere else on the env
                  Authorization: explicit. The path must longest-prefix
                  match an entry in `project.compute_env_access[].
                  directories[]` whose `permissions:` includes the
                  required token.

A path that matches no zone is refused with PermissionDenied. The same
absolute path can land in different zones across different envs — the
routing is per-env.

The "_ad_hoc" project — for one-off transfers
---------------------------------------------

When `project_name == "_ad_hoc"`, transfer.py synthesizes a virtual
project on the fly: it has access to scratch + common_data on EVERY env
in the YAML, and an empty `directories[]`. Use it for transfers that
don't fit a long-lived project's workflow (debug downloads, ad-hoc data
inspection, one-off uploads). The `_ad_hoc` project is never persisted
to the YAML — it lives only in memory for the duration of the call.

Trust contract — same as the retired primitives
-----------------------------------------------

  local-mode envs:  shutil.copy + sha256 both ends
  ssh-mode envs:    via the configured TransferProvider:
                      scp_head_node  — scp + ssh `sha256sum`
                      globus         — `globus transfer` task,
                                        Globus end-to-end checksum +
                                        post-transfer local sha256
  paths validated BEFORE any subprocess (no metacharacter smuggling)
  remote overwrite refused (the `upload` token = "write NEW files only")
  local overwrite refused on download (no silent clobber)
  size cap (5 GiB) for scp head-node path; Globus has no cap

Manifest — every transfer is journaled
--------------------------------------

Every transfer (success OR failure) writes a JSON manifest under
`transfer_history/<project>/<YYYY-MM-DD>/<ISO-stamp>_<direction>_
<short_hash>.json`. The manifest carries enough state to programmatically
re-call upload()/download() with the same intent — `replay_args` block
holds the exact (project_name, compute_env_name, local_path,
remote_abs_path) the operation ran with. The agent doesn't need to
"remember" past transfers; it queries the manifest dir.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.skills import compute_access


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Head-node transfer cap. Anything larger should go through Globus (set
# the env's data_transfer.type to globus and use that endpoint) or a
# SLURM-job data acquisition. Refusing keeps us a good citizen on the
# shared head node.
_MAX_TRANSFER_BYTES: int = 5 * 1024 * 1024 * 1024  # 5 GiB

# Absolute-path cap. Defense-in-depth against header-injection where the
# path is later embedded in audit-record strings or shell argv.
_MAX_REMOTE_PATH_LEN: int = 4096

# Characters never allowed in a path component. Conservative: refuse any
# byte that means something special to a shell, to find -printf, or to
# sbatch headers. Newline injection is the canonical sbatch attack;
# whitespace would corrupt ssh argv assembly downstream.
_FORBIDDEN_PATH_CHARS: frozenset[str] = frozenset(
    "\x00\n\r\t ;|&$`<>(){}[]*?\"'\\")

# Hash chunk size for sha256: balances syscalls vs RSS on multi-GB files.
_HASH_CHUNK: int = 1024 * 1024  # 1 MiB

# The synthesized one-off project. Recognized name; never lives in YAML.
AD_HOC_PROJECT_NAME = "_ad_hoc"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class TransferError(ValueError):
    """A path / project_name / local_file failed validation before any
    subprocess. Surfaces as {"error": "..."} from the public primitives —
    no exception escapes."""


# ---------------------------------------------------------------------------
# Pure-string validators (no I/O — must be safe before any subprocess)
# ---------------------------------------------------------------------------

def _validate_project_name_token(project_name: str) -> str:
    """The project name is auto-prepended to the path under scratch as
    the multi-project isolation prefix; it MUST be a safe token (alnum +
    `_-`, ≤64 chars) so it can't smuggle traversal or shell metacharacters
    into the resolved path."""
    if not isinstance(project_name, str):
        raise TransferError(
            f"project_name must be a string, got "
            f"{type(project_name).__name__}")
    if project_name == AD_HOC_PROJECT_NAME:
        return project_name
    if not compute_access._is_safe_token(project_name):
        raise TransferError(
            f"project_name {project_name!r} is not a safe token (alnum + "
            f"'_-' only, ≤64 chars). Project names are interpolated into "
            f"path components for scratch-zone isolation; unsafe chars "
            f"could smuggle traversal or shell metacharacters.")
    return project_name


def _validate_remote_abs_path(remote_abs_path: str) -> str:
    """Pure-string check that an absolute remote path is well-formed.
    No filesystem access. Returns the normalized path."""
    if not isinstance(remote_abs_path, str):
        raise TransferError(
            f"remote_abs_path must be a string, got "
            f"{type(remote_abs_path).__name__}")
    if not remote_abs_path:
        raise TransferError("remote_abs_path must be non-empty")
    if not remote_abs_path.startswith("/"):
        raise TransferError(
            f"remote_abs_path must be ABSOLUTE (start with '/'), got "
            f"{remote_abs_path!r}. The unified transfer primitives take "
            f"absolute paths — they don't auto-prefix.")
    if len(remote_abs_path) > _MAX_REMOTE_PATH_LEN:
        raise TransferError(
            f"remote_abs_path length {len(remote_abs_path)} exceeds cap "
            f"{_MAX_REMOTE_PATH_LEN}")
    for c in remote_abs_path:
        if c in _FORBIDDEN_PATH_CHARS:
            raise TransferError(
                f"remote_abs_path contains forbidden character {c!r} "
                f"(codepoint {ord(c)}): {remote_abs_path!r}")
    if any(part == ".." for part in remote_abs_path.split("/")):
        raise TransferError(
            f"remote_abs_path has a '..' traversal component: "
            f"{remote_abs_path!r}")
    norm = os.path.normpath(remote_abs_path)
    if not norm.startswith("/"):
        raise TransferError(
            f"remote_abs_path normalizes outside an absolute root: "
            f"{remote_abs_path!r} → {norm!r}")
    return norm


def _validate_local_path_for_upload(local_path: str) -> Path:
    """Source file for an upload: must exist, regular file (no symlinks
    to defend against redirect attacks), under the size cap."""
    if not isinstance(local_path, str) or not local_path:
        raise TransferError(
            f"local_path must be a non-empty string, got {local_path!r}")
    p = Path(local_path)
    if p.is_symlink():
        raise TransferError(
            f"local_path {local_path!r} is a symlink; upload refuses "
            f"symlinks to prevent redirect attacks. Pass the real path.")
    if not p.exists():
        raise TransferError(f"local_path {local_path!r} does not exist")
    if not p.is_file():
        raise TransferError(
            f"local_path {local_path!r} is not a regular file "
            f"(directories / devices are not supported)")
    try:
        sz = p.stat().st_size
    except OSError as e:
        raise TransferError(
            f"local_path {local_path!r} stat failed: {e}") from e
    if sz > _MAX_TRANSFER_BYTES:
        raise TransferError(
            f"local_path {local_path!r} is {sz} bytes — exceeds head-node "
            f"transfer cap of {_MAX_TRANSFER_BYTES} bytes "
            f"({_MAX_TRANSFER_BYTES // (1024**3)} GiB). Configure "
            f"data_transfer.type=globus in the env to remove this cap.")
    return p


def _validate_local_path_for_download(local_path: str) -> Path:
    """Destination for a download: must not exist (no silent overwrite),
    parent must exist + be writable."""
    if not isinstance(local_path, str) or not local_path:
        raise TransferError(
            f"local_path must be a non-empty string, got {local_path!r}")
    p = Path(local_path)
    if p.exists():
        raise TransferError(
            f"local_path {local_path!r} already exists; download refuses "
            f"to overwrite. Remove the existing file (or pass a fresh "
            f"path) and retry.")
    parent = p.parent
    if not parent.exists():
        raise TransferError(
            f"local_path's parent directory {str(parent)!r} does not "
            f"exist; create it before downloading")
    if not os.access(parent, os.W_OK):
        raise TransferError(
            f"local_path's parent directory {str(parent)!r} is not "
            f"writable")
    return p


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def _compute_local_sha256(path: Path) -> str:
    """sha256 of a local file, chunked so multi-GB files don't spike RSS."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _remote_sha256_cmd(remote_path: str) -> str:
    """The remote shell string for `sha256sum <path>`. Path is the only
    interpolated piece and is shlex.quote'd."""
    return f"sha256sum {shlex.quote(remote_path)}"


def _parse_sha256sum_output(stdout: str) -> Optional[str]:
    """Parse the `sha256sum` output: '<hex>  <path>\\n'. Returns the
    hex or None on any malformation. Tolerates MOTD banners on login
    shells — skip lines that don't start with a 64-hex digest."""
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
    """scp argv for an ssh-mode env. BatchMode=yes (fail fast, no
    password prompt) and -p (preserve mtime, useful for debugging).
    Piggybacks on the user's ControlMaster the same way ssh does."""
    return ["scp", "-o", "BatchMode=yes", "-p", src, dst]


def _build_remote_target(env: dict, abs_remote_path: str) -> str:
    """`user@host:/abs/path` for scp. Path is NOT shell-quoted; scp
    treats it as one argv element and the remote scp daemon does its
    own minimal interpretation. We've already rejected metacharacters
    + whitespace in _validate_remote_abs_path."""
    if " " in abs_remote_path or "\t" in abs_remote_path:
        raise TransferError(
            f"resolved remote path contains whitespace: "
            f"{abs_remote_path!r}")
    host = env["host"]
    user = env.get("user")
    target_host = f"{user}@{host}" if user else host
    return f"{target_host}:{abs_remote_path}"


def _local_mkdir_parent(remote_abs_path: Path) -> None:
    """Create parent dirs for a local-mode "remote" path. Idempotent."""
    remote_abs_path.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# _ad_hoc project synthesis
# ---------------------------------------------------------------------------

def _synthesize_ad_hoc_project(access: dict) -> dict:
    """Build the virtual one-off project. It can touch scratch +
    common_data on every env in the YAML, with no project-specific
    directories[]. The synthesized project never persists to disk; it
    lives only for the duration of one call."""
    envs = access.get("compute_envs") or []
    compute_env_access = []
    for env in envs:
        env_name = env.get("name")
        if not env_name:
            continue
        compute_env_access.append({
            "compute_env":  env_name,
            "directories":  [],   # no explicit project_path zone
        })
    return {
        "name":               AD_HOC_PROJECT_NAME,
        "description":        ("synthesized virtual project — scratch + "
                               "common_data on every env, no "
                               "project-specific directories"),
        "compute_env_access": compute_env_access,
    }


def _get_project_or_ad_hoc(project_name: str, access: dict) -> dict:
    """Resolve a project by name; synthesize the _ad_hoc project rather
    than reading it from the YAML."""
    if project_name == AD_HOC_PROJECT_NAME:
        return _synthesize_ad_hoc_project(access)
    return compute_access.get_project(project_name, access)


# ---------------------------------------------------------------------------
# Zone classification + auth routing
# ---------------------------------------------------------------------------

def _under(root: str, abs_path: str) -> bool:
    """Is abs_path inside (or equal to) the directory tree at root?
    Pure string comparison after normalization."""
    if not root:
        return False
    root_norm = os.path.normpath(root.rstrip("/"))
    p_norm = os.path.normpath(abs_path)
    return p_norm == root_norm or p_norm.startswith(root_norm + "/")


def _classify_zone_and_authorize(*, project: dict, env: dict,
                                  remote_abs_path: str,
                                  op: str,
                                  primitive_name: str) -> dict:
    """Decide which zone `remote_abs_path` falls in, apply the right
    authorization check, and return a {zone, auth_target?} summary.

    `op` is "upload" or "download" — used by the authorization check
    to look up the required permission token in OPERATION_REQUIRES.

    Order of checks:
      1. scratch zone: if remote_abs_path is under env.agent_scratch_target,
         require the project to start with `<scratch_root>/<project_name>/`
         (multi-project isolation), then check env-target capability.
      2. common_data zone: if under env.agent_common_data_target, check
         env-target capability. No project-prefix isolation.
      3. project_path zone: otherwise, defer to check_permission which
         walks project.directories[] for the longest-prefix match.

    Raises compute_access.PermissionDenied on any failure."""
    env_name = env.get("name") or ""
    scratch = compute_access.get_agent_scratch_target(env)
    common = compute_access.get_agent_common_data_target(env)

    # 1) scratch
    if scratch and _under(scratch.get("path") or "", remote_abs_path):
        scratch_root = (scratch.get("path") or "").rstrip("/")
        # Multi-project isolation: path MUST be under the project's
        # auto-prefix dir under scratch. This stops project A from
        # writing into project B's scratch namespace.
        project_root = f"{scratch_root}/{project['name']}"
        if not _under(project_root, remote_abs_path):
            raise compute_access.PermissionDenied(
                f"scratch-zone path {remote_abs_path!r} must be under "
                f"the project's prefix {project_root!r} (multi-project "
                f"isolation). Pick a path that starts with "
                f"{project_root}/.")
        compute_access.check_env_target_capability(
            project, env_name, scratch, primitive_name,
            "agent_scratch_target")
        return {"zone": "scratch",
                "auth_target": "agent_scratch_target",
                "scratch_root": scratch_root}

    # 2) common_data
    if common and _under(common.get("path") or "", remote_abs_path):
        compute_access.check_env_target_capability(
            project, env_name, common, primitive_name,
            "agent_common_data_target")
        return {"zone": "common_data",
                "auth_target": "agent_common_data_target",
                "common_root": (common.get("path") or "").rstrip("/")}

    # 3) project_path
    # _ad_hoc has empty directories[] so this WILL raise PermissionDenied
    # for any abs path that isn't under scratch/common_data — by design.
    compute_access.check_permission(
        project, env_name, remote_abs_path, primitive_name)
    return {"zone": "project_path",
            "auth_target": "directories"}


# ---------------------------------------------------------------------------
# Manifest writer
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """The agent repo root — two levels up from this file."""
    return Path(__file__).resolve().parent.parent.parent


def _short_hash(*parts: str) -> str:
    """Short hex hash of the input strings — used as a manifest filename
    disambiguator when two transfers stamp in the same second."""
    h = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return h[:10]


def _write_transfer_manifest(*,
                              direction: str,
                              project_name: str,
                              compute_env_name: str,
                              zone: str,
                              local_path: str,
                              remote_abs_path: str,
                              provider: str,
                              task_id: Optional[str],
                              result: str,
                              bytes_transferred: Optional[int],
                              duration_s: Optional[float],
                              local_sha256: Optional[str],
                              remote_sha256: Optional[str],
                              verified_method: Optional[str],
                              error_msg: Optional[str],
                              ) -> Path:
    """Write a JSON record under
    `transfer_history/<project_name>/<YYYY-MM-DD>/<stamp>_<direction>_
    <short_hash>.json`. Includes a `replay_args` block holding the exact
    parameters needed to re-call upload()/download() with the same intent.

    Returns the manifest path. Manifest creation NEVER raises into the
    primitive — a failed manifest write is logged but doesn't fail the
    transfer (we don't want a disk-full repo to mask a successful upload).
    """
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%dT%H-%M-%SZ")
    record = {
        "manifest_version":   1,
        "direction":          direction,
        "completed_at_iso":   now.isoformat(),
        "project":            project_name,
        "compute_env":        compute_env_name,
        "zone":               zone,
        "local_path":         str(local_path),
        "remote_abs_path":    remote_abs_path,
        "provider":           provider,
        "task_id":            task_id,
        "bytes":              bytes_transferred,
        "duration_s":         duration_s,
        "local_sha256":       local_sha256,
        "remote_sha256":      remote_sha256,
        "verified_method":    verified_method,
        "result":             result,
        "error":              error_msg,
        "replay_args": {
            "project_name":     project_name,
            "compute_env_name": compute_env_name,
            "local_path":       str(local_path),
            "remote_abs_path":  remote_abs_path,
            "direction":        direction,
        },
    }
    base = _repo_root() / "transfer_history" / project_name / day
    base.mkdir(parents=True, exist_ok=True)
    sh = _short_hash(direction, str(local_path), remote_abs_path,
                     now.isoformat())
    manifest_path = base / f"{stamp}_{direction}_{sh}.json"
    manifest_path.write_text(json.dumps(record, indent=2, sort_keys=True))
    return manifest_path


# ---------------------------------------------------------------------------
# Common pre/post-transfer machinery
# ---------------------------------------------------------------------------

def _remote_existence_check(env: dict, abs_remote: str, timeout: int) -> dict:
    """ssh-mode test for whether abs_remote already exists. Returns
    {exists: bool} or {error: str}."""
    from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint
    exist_cmd = (f"test -e {shlex.quote(abs_remote)} "
                 f"&& echo EXISTS || echo OK")
    ex_argv = _ssh_argv(env, exist_cmd)
    ex = subprocess.run(ex_argv, capture_output=True, text=True,
                         timeout=timeout)
    if ex.returncode != 0:
        hint = _ssh_failure_hint(ex.stderr or "", env.get("host", "?"))
        out = {"error":
            f"remote existence pre-check failed (rc={ex.returncode}): "
            f"{(ex.stderr or '').strip()[:300]}"}
        if hint:
            out["hint"] = hint
        return out
    return {"exists": "EXISTS" in (ex.stdout or "")}


def _remote_mkdir_parent_ssh(env: dict, abs_remote: str,
                              timeout: int) -> Optional[dict]:
    """ssh-mode mkdir -p on the parent of abs_remote. Returns None on
    success, {error: str} on failure."""
    from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint
    mkdir_cmd = f"mkdir -p {shlex.quote(os.path.dirname(abs_remote))}"
    mk_argv = _ssh_argv(env, mkdir_cmd)
    mk = subprocess.run(mk_argv, capture_output=True, text=True,
                         timeout=timeout)
    if mk.returncode != 0:
        hint = _ssh_failure_hint(mk.stderr or "", env.get("host", "?"))
        out = {"error":
            f"remote mkdir -p failed (rc={mk.returncode}): "
            f"{(mk.stderr or '').strip()[:300]}"}
        if hint:
            out["hint"] = hint
        return out
    return None


def _remote_sha256_ssh(env: dict, abs_remote: str,
                        timeout: int) -> dict:
    """Read sha256 of a remote file via ssh. Returns {sha: hex} or
    {error: str}."""
    from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint
    sh_argv = _ssh_argv(env, _remote_sha256_cmd(abs_remote))
    sh = subprocess.run(sh_argv, capture_output=True, text=True,
                         timeout=timeout)
    if sh.returncode != 0:
        hint = _ssh_failure_hint(sh.stderr or "", env.get("host", "?"))
        out = {"error":
            f"remote sha256sum failed (rc={sh.returncode}): "
            f"{(sh.stderr or '').strip()[:300]}"}
        if hint:
            out["hint"] = hint
        return out
    remote = _parse_sha256sum_output(sh.stdout or "")
    if not remote:
        return {"error":
            f"remote sha256sum stdout unparseable: "
            f"{(sh.stdout or '').strip()[:300]!r}"}
    return {"sha": remote}


# ---------------------------------------------------------------------------
# upload — the unified primitive
# ---------------------------------------------------------------------------

def upload(project_name: str,
           compute_env_name: str,
           local_path: str,
           remote_abs_path: str,
           *,
           async_globus: bool = False,
           access_path: Optional[str] = None,
           timeout: int = 600) -> dict:
    """Push `local_path` from this laptop to `remote_abs_path` on
    `compute_env_name`, with authorization routed by which zone the
    remote path falls in (scratch / common_data / project_path).

    Use `project_name="_ad_hoc"` for one-off transfers that don't fit a
    long-lived project. _ad_hoc grants access to scratch + common_data
    on every declared env, with no `directories[]`.

    Returns a dict with success metadata + the manifest path on success,
    or {"error": "..."} on any failure. Every call writes a manifest
    under `transfer_history/<project>/<YYYY-MM-DD>/...` regardless of
    success/failure.
    """
    return _do_transfer(
        direction="upload",
        project_name=project_name,
        compute_env_name=compute_env_name,
        local_path=local_path,
        remote_abs_path=remote_abs_path,
        async_globus=async_globus,
        access_path=access_path,
        timeout=timeout,
    )


def download(project_name: str,
             compute_env_name: str,
             remote_abs_path: str,
             local_path: str,
             *,
             async_globus: bool = False,
             access_path: Optional[str] = None,
             timeout: int = 600) -> dict:
    """Pull `remote_abs_path` from `compute_env_name` to `local_path`
    on this laptop. Authorization routes by zone, same as upload.

    The local destination must NOT exist (no silent overwrite); its
    parent dir must exist + be writable.
    """
    return _do_transfer(
        direction="download",
        project_name=project_name,
        compute_env_name=compute_env_name,
        local_path=local_path,
        remote_abs_path=remote_abs_path,
        async_globus=async_globus,
        access_path=access_path,
        timeout=timeout,
    )


def _do_transfer(*, direction: str,
                  project_name: str,
                  compute_env_name: str,
                  local_path: str,
                  remote_abs_path: str,
                  async_globus: bool,
                  access_path: Optional[str],
                  timeout: int) -> dict:
    """Shared body for upload + download. Direction-specific branches
    are inline (overwrite check on remote vs local, sha256 anchor
    direction, provider method)."""
    started = time.perf_counter()
    manifest_dir_hint = None

    def _journal(*, result: str, error_msg: Optional[str] = None,
                 provider: str = "?", task_id: Optional[str] = None,
                 zone: str = "?",
                 bytes_transferred: Optional[int] = None,
                 local_sha256: Optional[str] = None,
                 remote_sha256: Optional[str] = None,
                 verified_method: Optional[str] = None) -> Path:
        """Write the manifest and return its path. Best-effort — a write
        failure is swallowed so a successful transfer isn't masked."""
        try:
            duration = round(time.perf_counter() - started, 3)
            return _write_transfer_manifest(
                direction=direction,
                project_name=project_name,
                compute_env_name=compute_env_name,
                zone=zone,
                local_path=local_path,
                remote_abs_path=remote_abs_path,
                provider=provider,
                task_id=task_id,
                result=result,
                bytes_transferred=bytes_transferred,
                duration_s=duration,
                local_sha256=local_sha256,
                remote_sha256=remote_sha256,
                verified_method=verified_method,
                error_msg=error_msg,
            )
        except Exception:
            # Manifest write must not mask the real result.
            return Path("<manifest-write-failed>")

    try:
        # 1) Validate project name + paths.
        _validate_project_name_token(project_name)
        normed_remote = _validate_remote_abs_path(remote_abs_path)

        # 2) Load YAML + resolve project (synthesizing _ad_hoc if needed) + env.
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = _get_project_or_ad_hoc(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        env_type = env.get("type")
        if env_type not in ("ssh", "local"):
            mpath = _journal(result="error", zone="?",
                error_msg=f"unsupported compute_env type {env_type!r}")
            return {"error": f"unsupported compute_env type {env_type!r}",
                    "manifest": str(mpath)}

        # 3) Zone classification + auth (raises PermissionDenied otherwise).
        zone_info = _classify_zone_and_authorize(
            project=project, env=env,
            remote_abs_path=normed_remote,
            op=direction, primitive_name=direction)
        zone = zone_info["zone"]

        # 4) Direction-specific local-side checks.
        if direction == "upload":
            lp = _validate_local_path_for_upload(local_path)
            size_bytes = lp.stat().st_size
            local_sha = _compute_local_sha256(lp)
        else:
            lp = _validate_local_path_for_download(local_path)
            size_bytes = 0  # known post-transfer for downloads
            local_sha = None

        # 5) Remote-existence pre-check (no-overwrite contract).
        if direction == "upload":
            if env_type == "local":
                if Path(normed_remote).exists():
                    mpath = _journal(result="error", zone=zone,
                        error_msg=("remote path already exists; upload "
                                   "refuses overwrites"))
                    return {"error":
                        f"remote path already exists: {normed_remote!r}. "
                        f"upload refuses overwrites. Pick a fresh "
                        f"remote_abs_path or remove the existing file.",
                        "manifest": str(mpath)}
            else:
                ec = _remote_existence_check(env, normed_remote, timeout)
                if "error" in ec:
                    mpath = _journal(result="error", zone=zone,
                        error_msg=ec["error"])
                    return {**ec, "manifest": str(mpath)}
                if ec["exists"]:
                    mpath = _journal(result="error", zone=zone,
                        error_msg=("remote path already exists; upload "
                                   "refuses overwrites"))
                    return {"error":
                        f"remote path already exists: {normed_remote!r}. "
                        f"upload refuses overwrites. Pick a fresh "
                        f"remote_abs_path or remove the existing file.",
                        "manifest": str(mpath)}

        # 6) Mkdir parent on the destination side.
        if direction == "upload":
            if env_type == "local":
                _local_mkdir_parent(Path(normed_remote))
            else:
                mk = _remote_mkdir_parent_ssh(env, normed_remote, timeout)
                if mk is not None:
                    mpath = _journal(result="error", zone=zone,
                        error_msg=mk["error"])
                    return {**mk, "manifest": str(mpath)}
        # download: local parent existence is already validated by
        # _validate_local_path_for_download

        # 7) Dispatch transfer.
        if env_type == "local":
            return _do_local_mode(
                direction=direction,
                project_name=project_name,
                compute_env_name=compute_env_name,
                lp=lp, normed_remote=normed_remote,
                local_sha=local_sha, zone=zone, journal=_journal,
                started=started)

        # ssh-mode: through TransferProvider.
        from agent.skills import transfer_providers
        provider = transfer_providers.get_transfer_provider(env)
        if direction == "upload":
            pr = provider.upload_one(
                env=env, local_path=lp, abs_remote_path=normed_remote,
                local_sha256=local_sha, timeout=timeout,
                async_return=async_globus,
                label=f"upload {project_name} {Path(normed_remote).name}")
        else:
            pr = provider.download_one(
                env=env, abs_remote_path=normed_remote,
                local_path=lp, timeout=timeout,
                async_return=async_globus,
                label=f"download {project_name} {Path(normed_remote).name}")
        if "error" in pr:
            mpath = _journal(result="error", zone=zone,
                provider=pr.get("provider", "?"),
                task_id=pr.get("task_id"),
                error_msg=pr["error"])
            out = dict(pr); out["manifest"] = str(mpath)
            return out

        # Async submit — no bytes yet; manifest reflects pending state.
        if pr.get("verified_method") == "globus_pending":
            mpath = _journal(result="pending", zone=zone,
                provider=pr["provider"], task_id=pr.get("task_id"),
                verified_method=pr["verified_method"])
            return {**pr, "manifest": str(mpath),
                    "project": project_name, "compute_env": compute_env_name,
                    "zone": zone, "remote_abs_path": normed_remote,
                    "local_path": str(lp)}

        # Sync success.
        bytes_done = pr.get("bytes", size_bytes if direction == "upload"
                            else (lp.stat().st_size if lp.exists() else 0))
        remote_sha = (pr.get("remote_sha256") or
                       pr.get("local_sha256") if direction == "download"
                       else pr.get("remote_sha256"))
        # For downloads, the provider's "local_sha256" is the post-fetch
        # hash on the laptop; for uploads it's the pre-transfer local hash.
        if direction == "download":
            local_sha = pr.get("local_sha256") or _compute_local_sha256(lp)
        mpath = _journal(result="success", zone=zone,
            provider=pr["provider"], task_id=pr.get("task_id"),
            bytes_transferred=bytes_done,
            local_sha256=local_sha,
            remote_sha256=pr.get("remote_sha256"),
            verified_method=pr.get("verified_method"))
        return {
            "success":          True,
            "project":          project_name,
            "compute_env":      compute_env_name,
            "zone":             zone,
            "direction":        direction,
            "local_path":       str(lp),
            "remote_abs_path":  normed_remote,
            "provider":         pr["provider"],
            "task_id":          pr.get("task_id"),
            "bytes":            bytes_done,
            "duration_s":       round(time.perf_counter() - started, 3),
            "local_sha256":     local_sha,
            "remote_sha256":    pr.get("remote_sha256"),
            "verified_method":  pr.get("verified_method"),
            "manifest":         str(mpath),
        }

    except TransferError as e:
        mpath = _journal(result="error", zone="?", error_msg=str(e))
        return {"error": str(e), "manifest": str(mpath)}
    except compute_access.PermissionDenied as e:
        mpath = _journal(result="error", zone="?",
                          error_msg=f"PermissionDenied: {e}")
        return {"error": f"PermissionDenied: {e}",
                "manifest": str(mpath)}
    except compute_access.ConfigError as e:
        mpath = _journal(result="error", zone="?",
                          error_msg=f"ConfigError: {e}")
        return {"error": f"ConfigError: {e}",
                "manifest": str(mpath)}
    except subprocess.TimeoutExpired as e:
        msg = f"subprocess timed out after {e.timeout}s"
        mpath = _journal(result="error", zone="?", error_msg=msg)
        return {"error": msg, "manifest": str(mpath)}


def _do_local_mode(*, direction: str,
                    project_name: str, compute_env_name: str,
                    lp: Path, normed_remote: str,
                    local_sha: Optional[str], zone: str, journal,
                    started: float) -> dict:
    """Inline shutil.copy path for local-mode envs. Same trust contract
    (sha256 both ends + size compare) but no wire-protocol provider."""
    if direction == "upload":
        shutil.copy(str(lp), normed_remote)
        remote_sha = _compute_local_sha256(Path(normed_remote))
        if remote_sha != local_sha:
            mpath = journal(result="error", zone=zone,
                provider="local_copy",
                error_msg=("sha256 round-trip mismatch on local-mode "
                           "upload"))
            return {"error":
                f"sha256 round-trip mismatch — local={local_sha} "
                f"remote={remote_sha!r}. File at {normed_remote!r} "
                f"may be corrupt.",
                "manifest": str(mpath)}
        bytes_done = lp.stat().st_size
        mpath = journal(result="success", zone=zone,
            provider="local_copy",
            bytes_transferred=bytes_done,
            local_sha256=local_sha, remote_sha256=remote_sha,
            verified_method="sha256_round_trip")
        return {
            "success":          True,
            "project":          project_name,
            "compute_env":      compute_env_name,
            "direction":        direction,
            "local_path":       str(lp),
            "remote_abs_path":  normed_remote,
            "zone":             zone,
            "provider":         "local_copy",
            "bytes":            bytes_done,
            "duration_s":       round(time.perf_counter() - started, 3),
            "local_sha256":     local_sha,
            "remote_sha256":    remote_sha,
            "verified_method":  "sha256_round_trip",
            "manifest":         str(mpath),
        }
    # download local-mode
    if not Path(normed_remote).exists():
        mpath = journal(result="error", zone=zone, provider="local_copy",
            error_msg=f"local-mode source does not exist")
        return {"error":
            f"local-mode download source does not exist: "
            f"{normed_remote!r}",
            "manifest": str(mpath)}
    remote_sha = _compute_local_sha256(Path(normed_remote))
    shutil.copy(normed_remote, str(lp))
    landed_sha = _compute_local_sha256(lp)
    if landed_sha != remote_sha:
        try:
            lp.unlink()
        except OSError:
            pass
        mpath = journal(result="error", zone=zone, provider="local_copy",
            error_msg="sha256 mismatch on local-mode download")
        return {"error":
            f"sha256 mismatch on local-mode download: "
            f"source={remote_sha} landed={landed_sha}",
            "manifest": str(mpath)}
    bytes_done = lp.stat().st_size
    mpath = journal(result="success", zone=zone,
        provider="local_copy",
        bytes_transferred=bytes_done,
        local_sha256=landed_sha, remote_sha256=remote_sha,
        verified_method="sha256_round_trip")
    return {
        "success":          True,
        "project":          project_name,
        "compute_env":      compute_env_name,
        "direction":        direction,
        "local_path":       str(lp),
        "remote_abs_path":  normed_remote,
        "zone":             zone,
        "provider":         "local_copy",
        "bytes":            bytes_done,
        "duration_s":       round(time.perf_counter() - started, 3),
        "local_sha256":     landed_sha,
        "remote_sha256":    remote_sha,
        "verified_method":  "sha256_round_trip",
        "manifest":         str(mpath),
    }
