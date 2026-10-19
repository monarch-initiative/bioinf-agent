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

  scratch          — under env.agent_scratch_target.path
                     Authorization: env-implicit grant. The target
                     block's `permissions:` must include `upload` (or
                     `download`). Multi-project isolation enforced:
                     path must be under
                     `<scratch_root>/<project_name>/...`

  common_data      — under env.agent_common_data_target.path
                     Authorization: env-implicit grant (same shape as
                     scratch). NO project-prefix isolation: this zone
                     is shared by every project on the env. For
                     reference databases.

  container_upload — under env.container_upload_target.path
                     Authorization: env-implicit grant. NO project
                     prefix (a .sif filename is content-addressed by
                     image digest; collisions are impossible). Distinct
                     from common_data per the routing memo — .sif files
                     and recipe artifacts go here, reference DBs go in
                     common_data.

  pipelines        — under env.agent_pipelines_target.path
                     Authorization: env-implicit grant. NO project
                     prefix (a pipeline directory is named for the
                     pipeline, and one artifact store serves every
                     session). Where a rendered pipeline is copied to run
                     on the env and where its run records land; the
                     target validator requires `upload`, `download` and
                     `exec`.

  project_path     — anywhere else on the env
                     Authorization: explicit. The path must longest-
                     prefix match an entry in `project.directories[]`
                     (tagged with this env) whose `permissions:`
                     includes the required token.

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

Trust contract
--------------

  local-mode envs:  shutil.copy (copytree for a directory) + sha256 both ends
  ssh-mode envs:    via the configured TransferProvider, which owns the
                    WHOLE remote interaction for its transport — the
                    no-overwrite check, parent-dir prep, and the byte
                    movement:
                      scp_head_node  — ssh `seen` (no-overwrite) +
                                        ssh `mkdir -p` (parent) + scp to
                                        the partial name + ssh `sha256sum`
                                        (verify) + ssh `mv` into place
                      globus         — `globus stat` (no-overwrite) +
                                        auto-mkdir (no-op) + `globus
                                        transfer` to the partial name
                                        (Globus end-to-end checksum) +
                                        `globus rename`. ZERO ssh calls —
                                        the whole point of the Globus path
                                        being off the head node.
  a directory moves as one transfer: every file is checked (sha256 both
    ends, or Globus's own per-file checksum plus a count of what moved), its
    checksums land beside the receipt, and symlinks are never followed
  either direction lands under the destination's hidden partial name and is
    renamed into place once verified, so a final name is always a whole copy
  paths validated BEFORE any subprocess (no metacharacter smuggling)
  remote overwrite refused (the `upload` token = "write NEW files only")
  local overwrite refused on download (no silent clobber)
  size cap (5 GiB) on every upload, whatever the wire

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
from agent.skills import workspace
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
from agent.skills.backgroundable import detached
from agent.skills.outcomes import proven, refused, broke, degraded, loop


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Upload cap, applied on every wire. Anything larger is pulled by a compute
# node (download_reference_database with compute_env, acquire_reference_via_recipe)
# rather than pushed from here; refusing keeps us a good citizen on the shared
# head node.
_MAX_TRANSFER_BYTES: int = 5 * 1024 * 1024 * 1024  # 5 GiB

# A detached transfer (`background=True`) has no stream watchdog over it, so it
# waits as long as a Globus task lives by default (its 24 h deadline).
_DETACHED_TIMEOUT_S: int = 24 * 3600

# Absolute-path cap. Defense-in-depth against header-injection where the
# path is later embedded in audit-record strings or shell argv.
_MAX_REMOTE_PATH_LEN: int = 4096

# Characters never allowed in a path component. Conservative: refuse any
# byte that means something special to a shell, to find -printf, or to
# sbatch headers. Newline injection is the canonical sbatch attack;
# whitespace would corrupt ssh argv assembly downstream.
_FORBIDDEN_PATH_CHARS: frozenset[str] = frozenset(
    "\x00\n\r\t ;|&$`<>(){}[]*?\"'\\")

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
    """Source for an upload: must exist, be a regular file or a directory of
    them (no symlinks anywhere, to defend against redirect attacks), and be
    under the size cap."""
    if not isinstance(local_path, str) or not local_path:
        raise TransferError(
            f"local_path must be a non-empty string, got {local_path!r}")
    # Absolutize (without resolving symlinks — the symlink guard below still
    # fires): a RELATIVE local_path diverges between the CWD it's hashed against
    # and the Globus local-endpoint root it's transferred against. abspath keeps
    # both sides on the same absolute path.
    p = Path(os.path.abspath(os.path.expanduser(local_path)))
    if p.is_symlink():
        raise TransferError(
            f"local_path {local_path!r} is a symlink; upload refuses "
            f"symlinks to prevent redirect attacks. Pass the real path.")
    if not p.exists():
        raise TransferError(f"local_path {local_path!r} does not exist")
    if not (p.is_file() or p.is_dir()):
        raise TransferError(
            f"local_path {local_path!r} is neither a regular file nor a directory")
    try:
        sz = _local_bytes(p)
    except OSError as e:
        raise TransferError(
            f"local_path {local_path!r} stat failed: {e}") from e
    if sz > _MAX_TRANSFER_BYTES:
        raise TransferError(
            f"local_path {local_path!r} is {sz} bytes — exceeds the upload "
            f"cap of {_MAX_TRANSFER_BYTES} bytes "
            f"({_MAX_TRANSFER_BYTES // (1024**3)} GiB). Have a compute node pull it "
            f"instead: download_reference_database(compute_env=…) or "
            f"acquire_reference_via_recipe.")
    return p


def _validate_local_path_for_download(local_path: str) -> Path:
    """Destination for a download: must not exist (no silent overwrite),
    parent must exist + be writable."""
    if not isinstance(local_path, str) or not local_path:
        raise TransferError(
            f"local_path must be a non-empty string, got {local_path!r}")
    # Absolutize: a RELATIVE download dest lands under the Globus local-endpoint
    # root but is hashed against the process CWD — they diverge, so Globus reports
    # SUCCEEDED yet the post-fetch sha256 open()s a path that isn't there. abspath
    # keeps the transfer target and the verification on the same absolute path.
    p = Path(os.path.abspath(os.path.expanduser(local_path)))
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
    """sha256 of a local file, chunked so multi-GB files don't spike RSS.
    Delegates to the one implementation (core_data.sha256_file); raises on
    unreadable, as every transfer round-trip caller relies on."""
    from agent.models.core_data import sha256_file
    return sha256_file(path)


def _unreadable(e: OSError):
    raise TransferError(f"{e.filename} can't be read: {e.strerror}")


def _tree_files(root: Path) -> list[Path]:
    """Every file under the directory `root`. A directory transfers as its
    regular files: a symlink anywhere in it is refused, because following one
    could reach outside the zone the transfer was authorized for, and so is
    any other special file or a directory that can't be read."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=_unreadable):
        for name in dirnames + filenames:
            p = Path(dirpath, name)
            if p.is_symlink() or not (p.is_dir() or p.is_file()):
                raise TransferError(
                    f"{p} is a symlink or a special file; a directory transfers "
                    f"as regular files and directories only")
            if p.is_file():
                files.append(p)
    return sorted(files)


def _local_bytes(p: Path) -> int:
    """The size of a file, or the total size of a directory's files."""
    if p.is_dir():
        return sum(f.stat().st_size for f in _tree_files(p))
    return p.stat().st_size


def _scan_tree(root: Path) -> tuple[dict[str, str], int]:
    """A directory's {path relative to `root`: sha256} and its files' total
    size, from one walk."""
    sums, total = {}, 0
    for f in _tree_files(root):
        try:
            sums[f.relative_to(root).as_posix()] = _compute_local_sha256(f)
            total += f.stat().st_size
        except OSError as e:
            raise TransferError(f"{f} can't be read: {e.strerror or e}") from e
    return sums, total


def _tree_mismatch(source: dict[str, str], copy: dict[str, str]) -> Optional[str]:
    """How a directory's copy differs from its source listing, in one line;
    None when every file arrived with the source's sha256."""
    if source == copy:
        return None
    gaps = (("missing", sorted(set(source) - set(copy))),
            ("not in the source", sorted(set(copy) - set(source))),
            ("with a different sha256",
             sorted(k for k in set(source) & set(copy) if source[k] != copy[k])))
    return "; ".join(f"{len(names)} file(s) {what} (first: {names[0]})"
                     for what, names in gaps if names)


def _remote_tree_cmd(remote_path: str) -> str:
    """The remote shell string that lists a directory as `_scan_tree`
    does: `<sha256>  ./<relative path>` per file, plus `NOT_A_FILE ./<path>`
    for an entry that is neither a file nor a directory (a symlink)."""
    return (f"cd {shlex.quote(remote_path)} && "
            f"find . ! -type d ! -type f -exec printf 'NOT_A_FILE %s\\n' {{}} + && "
            f"find . -type f -print0 | xargs -0 -r sha256sum")


_TREE_LINE_RE = re.compile(r"(?:([0-9a-f]{64})  |NOT_A_FILE )\./(.+)")


def _parse_tree_listing(stdout: str) -> tuple[dict[str, str], list[str]]:
    """`_remote_tree_cmd`'s output as ({relative path: sha256}, the entries
    that are not files). Lines of any other shape (a login banner) are skipped."""
    sums: dict[str, str] = {}
    not_files: list[str] = []
    for m in filter(None, map(_TREE_LINE_RE.fullmatch, stdout.splitlines())):
        if m[1]:
            sums[m[2]] = m[1]
        else:
            not_files.append(m[2])
    return sums, not_files


def partial_path(abs_path: str) -> str:
    """Where a transfer lands until its bytes are verified: a hidden sibling of
    the destination. Every transport renames it into place only after the
    check passes, so a file under its final name is always a complete one and
    an existence check can be trusted."""
    parent, base = os.path.split(str(abs_path).rstrip("/"))
    return f"{parent}/.{base}.partial"


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

def _scp_argv(env: dict, src: str, dst: str, *, tree: bool = False) -> list[str]:
    """scp argv for an ssh-mode env. BatchMode=yes (fail fast, no
    password prompt), -p (preserve mtime, useful for debugging) and, for a
    directory, -r. Piggybacks on the user's ControlMaster the same way ssh does."""
    return ["scp", "-o", "BatchMode=yes", "-p", *(["-r"] if tree else []), src, dst]


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
                                  primitive_name: str) -> dict:
    """Decide which zone `remote_abs_path` falls in, apply the right
    authorization check, and return a {zone, auth_target?} summary.

    `primitive_name` names the operation whose permission token
    (OPERATION_REQUIRES) the path must carry: upload, download, or a
    primitive such as run_production_pipeline (exec).

    Order of checks:
      1. scratch zone: if remote_abs_path is under env.agent_scratch_target,
         require the project to start with `<scratch_root>/<project_name>/`
         (multi-project isolation), then check env-target capability.
      2. common_data zone: if under env.agent_common_data_target, check
         env-target capability. No project-prefix isolation.
      3. container_upload zone: if under env.container_upload_target, check
         env-target capability. No project prefix (staged images are shared).
      4. pipelines zone: if under env.agent_pipelines_target, check env-target
         capability. No project prefix (see below).
      5. project_path zone: otherwise, defer to check_permission which
         walks project.directories[] for the longest-prefix match.

    Raises compute_access.PermissionDenied on any failure."""
    env_name = env.get("name") or ""
    scratch = compute_access.get_agent_scratch_target(env)
    common = compute_access.get_agent_common_data_target(env)
    container = compute_access.get_container_upload_target(env)
    pipelines = compute_access.get_agent_pipelines_target(env)

    # 1) scratch
    if scratch and _under(scratch.get("path") or "", remote_abs_path):
        scratch_root = (scratch.get("path") or "").rstrip("/")
        # Multi-project isolation: path MUST be under the project's
        # auto-prefix dir under scratch. This stops project A from
        # writing into project B's scratch namespace.
        project_prefix = f"{scratch_root}/{project['name']}"
        if not _under(project_prefix, remote_abs_path):
            raise compute_access.PermissionDenied(
                f"scratch-zone path {remote_abs_path!r} must be under "
                f"the project's prefix {project_prefix!r} (multi-project "
                f"isolation). Pick a path that starts with "
                f"{project_prefix}/.")
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

    # 3) container_upload_target — env-implicit grant, same shape as
    # scratch/common_data; no project-prefix isolation (the .sif filename
    # is content-addressed by image digest, collisions are impossible).
    # See [[project-container-artifacts-routing]] for why this is its own
    # zone and not collapsed into common_data.
    if container and _under(container.get("path") or "", remote_abs_path):
        compute_access.check_env_target_capability(
            project, env_name, container, primitive_name,
            "container_upload_target")
        return {"zone": "container_upload",
                "auth_target": "container_upload_target",
                "container_root": (container.get("path") or "").rstrip("/")}

    # 4) agent_pipelines_target — env-implicit grant, same shape as the three
    # above: where a rendered pipeline directory is copied to run on the env and
    # where its run records are read back from.
    #
    # No project-prefix isolation, for the same reason the container zone has
    # none: one workspace is one artifact store shared by many sessions, and a
    # pipeline directory is named for the pipeline, not for the project that
    # happened to render it.
    if pipelines and _under(pipelines.get("path") or "", remote_abs_path):
        compute_access.check_env_target_capability(
            project, env_name, pipelines, primitive_name,
            "agent_pipelines_target")
        return {"zone": "pipelines",
                "auth_target": "agent_pipelines_target",
                "pipelines_root": (pipelines.get("path") or "").rstrip("/")}

    # 5) project_path
    # _ad_hoc has empty directories[] so this WILL raise PermissionDenied
    # for any abs path that isn't under scratch / common_data /
    # container_upload_target / agent_pipelines_target — by design.
    compute_access.check_permission(
        project, env_name, remote_abs_path, primitive_name)
    return {"zone": "project_path",
            "auth_target": "directories"}


# ---------------------------------------------------------------------------
# Manifest writer
# ---------------------------------------------------------------------------

def staging_dir(name: str) -> Path:
    """Where a file or directory bound for an upload is put together: a folder
    of the workspace's scratch zone. Globus Connect Personal reads only its
    Accessible Folders (default $HOME) and refuses a system temp dir such as
    /var/folders or /tmp; the workspace is required to sit under $HOME, so a
    source here works on every wire. A function, not a constant: the workspace
    is resolved when it is called, so a test that redirects it is never written
    past."""
    return workspace.scratch_dir(name)


def _checksums_path(manifest_path: Path) -> Path:
    """Where a directory transfer's checksums go: beside its receipt."""
    return manifest_path.with_suffix(".sha256")


def _record_root() -> Path:
    """Where transfer receipts and submission manifests are written: the scratch
    zone (``transfer_history/`` and ``job_submissions/`` under it). Every receipt
    is also returned to the caller when the transfer or submission happens."""
    return workspace.scratch_dir()


def _short_hash(*parts: str) -> str:
    """Short hex hash of the input strings — used as a manifest filename
    disambiguator when two transfers stamp in the same second."""
    h = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return h[:10]


# --- human-readable formatting helpers for the manifest ------------------

def _human_size(n: Optional[int]) -> Optional[str]:
    """Bytes → a compact human string (e.g. 68.9 MB). None passes through."""
    if n is None:
        return None
    step = 1024.0
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < step or unit == "TB":
            # No decimals for plain bytes; one decimal above that.
            return f"{int(val)} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= step
    return f"{n} B"


def _human_duration(seconds: Optional[float]) -> Optional[str]:
    """Seconds → a compact human string (e.g. 3.8s, 1m04s)."""
    if seconds is None:
        return None
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}m{s:02d}s"


def _clean_error(msg: Optional[str]) -> Optional[str]:
    """Strip login-banner / MOTD noise (the box-drawing lines many
    clusters print on every ssh) out of a captured error so the manifest
    carries the actual cause, not a wall of decoration. Generic — keys
    off the banner SHAPE (box-drawing / pipe-prefixed lines), not any
    particular site's wording."""
    if not msg:
        return msg
    keep: list[str] = []
    for ln in msg.splitlines():
        s = ln.strip()
        if not s:
            continue
        # Box-drawing banner lines: only pipes / dashes / spaces, or a
        # line that opens the banner box with a leading pipe.
        if set(s) <= set("-|+ ") or s.startswith("|"):
            continue
        keep.append(s)
    cleaned = " ".join(keep).strip().rstrip(":").strip()
    # If an ssh connection failed (rc=255) the meaningful remainder is
    # often empty after stripping the banner — say something useful.
    if "rc=255" in (cleaned or "") or not cleaned:
        if "rc=255" in (msg or "") or not cleaned:
            return ("could not reach the compute env over ssh "
                    "(is `ssh hpc-agent` open in a terminal?)")
    return cleaned


# Maps the internal status + verified_method to human-facing strings.
_OUTCOME_VERB = {
    ("success",  "upload"):   "uploaded",
    ("success",  "download"): "downloaded",
    ("refused",  "upload"):   "refused",
    ("refused",  "download"): "refused",
    ("error",    "upload"):   "failed",
    ("error",    "download"): "failed",
    ("waiting",  "upload"):   "still running",
    ("waiting",  "download"): "still running",
}

_VERIFIED_PHRASE = {
    "sha256_round_trip": "sha256 matched on both ends",
    "globus_end_to_end": "Globus end-to-end checksum (task SUCCEEDED)",
}


def _write_transfer_manifest(*,
                              direction: str,
                              project_name: str,
                              compute_env_name: str,
                              zone: str,
                              transport: Optional[str],
                              local_path: str,
                              remote_abs_path: str,
                              command: Optional[str],
                              result: str,
                              bytes_transferred: Optional[int],
                              duration_s: Optional[float],
                              sha256: Optional[str],
                              task_id: Optional[str],
                              verified_method: Optional[str],
                              error_msg: Optional[str],
                              checksums: Optional[dict] = None,
                              pending_rename: Optional[dict] = None,
                              path: Optional[Path] = None,
                              ) -> Path:
    """Write a human-readable JSON record of one transfer under
    `transfer_history/<project>/<YYYY-MM-DD>/<stamp>_<direction>_<hash>.json`,
    or over `path`, the record this transfer already wrote.

    The record leads with a plain-English `outcome` + `summary`, carries
    the ACTUAL command that ran (`command`), and OMITS any field that
    doesn't apply to this transfer (no wall of nulls). A directory's
    checksums go beside it as `<same name>.sha256`, the list `sha256sum -c`
    reads from inside the directory. Returns the path.
    Manifest creation never raises into the primitive — a write failure
    is swallowed so it can't mask a real transfer result.
    """
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%dT%H-%M-%SZ")

    outcome = _OUTCOME_VERB.get((result, direction), result)
    fname = os.path.basename(remote_abs_path) or os.path.basename(local_path)
    size_h = _human_size(bytes_transferred)
    # `to`/`from` carry the env name on the remote side so a reader knows
    # WHICH cluster without cross-referencing.
    if direction == "upload":
        src_disp = str(local_path)
        dst_disp = f"{compute_env_name}:{remote_abs_path}"
    else:
        src_disp = f"{compute_env_name}:{remote_abs_path}"
        dst_disp = str(local_path)

    clean_err = _clean_error(error_msg)
    summary = _summarize(
        outcome=outcome, direction=direction, transport=transport,
        fname=fname, size_h=size_h, compute_env=compute_env_name,
        remote_abs_path=remote_abs_path, task_id=task_id, reason=clean_err,
        files=None if checksums is None else len(checksums))

    # Build the record in a logical reading order, omitting empty fields.
    record: dict = {
        "outcome":      outcome,
        "summary":      summary,
        "when":         now.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    if duration_s is not None:
        record["took"] = _human_duration(duration_s)
    record["direction"] = direction
    if transport:
        record["transport"] = transport
    record["file"] = fname
    if checksums is not None:
        record["files"] = len(checksums)
    if size_h:
        record["size"] = size_h
    record["from"] = src_disp
    record["to"] = dst_disp
    if command:
        record["command"] = command
    if task_id:
        record["globus_task_id"] = task_id
    verified = _VERIFIED_PHRASE.get(verified_method or "")
    if verified:
        record["verified"] = verified
    if sha256:
        record["sha256"] = sha256
    if clean_err and outcome in ("failed", "refused"):
        record["reason"] = clean_err
    if pending_rename:
        # a transfer still in flight lands under its partial name; whoever sees the
        # task succeed renames it into place (resolve_globus_task)
        record["pending_rename"] = pending_rename
    record["zone"] = zone
    record["project"] = project_name
    record["manifest_version"] = 2

    if path is None:
        base = _record_root() / "transfer_history" / project_name / day
        base.mkdir(parents=True, exist_ok=True)
        sh = _short_hash(direction, str(local_path), remote_abs_path,
                         now.isoformat())
        path = base / f"{stamp}_{direction}_{sh}.json"
    if checksums is not None:
        record["checksums"] = str(_write_checksums(path, checksums))
    path.write_text(json.dumps(record, indent=2))
    return path


def _write_checksums(manifest_path: Path, checksums: dict) -> Path:
    """A directory's checksums beside its receipt, the list `sha256sum -c` reads."""
    sums_path = _checksums_path(manifest_path)
    sums_path.write_text("".join(f"{sha}  {rel}\n" for rel, sha in sorted(checksums.items())))
    return sums_path


def _summarize(*, outcome: str, direction: str, transport: Optional[str],
               fname: str, size_h: Optional[str], compute_env: str,
               remote_abs_path: str, task_id: Optional[str],
               reason: Optional[str], files: Optional[int] = None) -> str:
    """One plain-English sentence describing what happened."""
    size_part = ", ".join(([] if files is None else [f"{files} files"])
                          + ([size_h] if size_h else []))
    size_part = f" ({size_part})" if size_part else ""
    via = {"globus": "via Globus",
           "scp_head_node": "via scp",
           "local_copy": "via local copy"}.get(transport or "", "")
    via_part = f" {via}" if via else ""
    if outcome == "uploaded":
        return (f"Uploaded {fname}{size_part} to {compute_env}"
                f"{via_part}.")
    if outcome == "downloaded":
        return (f"Downloaded {fname}{size_part} from {compute_env}"
                f"{via_part}.")
    if outcome == "refused":
        return (f"Refused: {reason or 'precondition not met'}. "
                f"({direction} of {fname} {'to' if direction == 'upload' else 'from'} {compute_env})")
    if outcome == "still running":
        return (f"Still moving {fname}{size_part} {'to' if direction == 'upload' else 'from'} "
                f"{compute_env}{via_part}: Globus task {task_id}, whose end has not "
                f"been seen.")
    # failed
    return (f"Failed to {direction} {fname} {'to' if direction == 'upload' else 'from'} "
            f"{compute_env}: {reason or 'see logs'}.")


# ---------------------------------------------------------------------------
# Common pre/post-transfer machinery
# ---------------------------------------------------------------------------

def _remote_existence_check(env: dict, abs_remote: str, timeout: int) -> dict:
    """ssh-mode test for whether abs_remote already exists. Returns
    {exists: bool} or {error: str}. Through `seen`: a stale "not there" on
    the login node would let an upload overwrite a file another node wrote."""
    from agent.skills.snapshot import _SEEN_FN, _ssh_argv, _ssh_failure_hint
    body = _SEEN_FN + f"seen {shlex.quote(abs_remote)} && echo EXISTS || echo OK"
    ex_argv = _ssh_argv(env, f"bash -c {shlex.quote(body)}")
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
    return {"exists": "EXISTS" in (ex.stdout or "").splitlines()}


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


# ---------------------------------------------------------------------------
# upload — the unified primitive
# ---------------------------------------------------------------------------

def upload(project_name: str,
           compute_env_name: str,
           local_path: str,
           remote_abs_path: str,
           *,
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
        access_path=access_path,
        timeout=timeout,
    )


def download(project_name: str,
             compute_env_name: str,
             remote_abs_path: str,
             local_path: str,
             *,
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
        access_path=access_path,
        timeout=timeout,
    )


def _do_transfer(*, direction: str,
                  project_name: str,
                  compute_env_name: str,
                  local_path: str,
                  remote_abs_path: str,
                  access_path: Optional[str],
                  timeout: int) -> dict:
    """Shared body for upload + download. Direction-specific branches
    are inline (overwrite check on remote vs local, sha256 anchor
    direction, provider method)."""
    started = time.perf_counter()
    if detached():
        timeout = max(timeout, _DETACHED_TIMEOUT_S)
    receipt: dict = {}    # this transfer's record, once written: every later write replaces it

    def _journal(*, result: str, error_msg: Optional[str] = None,
                 transport: Optional[str] = None,
                 command: Optional[str] = None,
                 task_id: Optional[str] = None,
                 zone: str = "?",
                 bytes_transferred: Optional[int] = None,
                 sha256: Optional[str] = None,
                 verified_method: Optional[str] = None,
                 checksums: Optional[dict] = None,
                 pending_rename: Optional[dict] = None) -> Path:
        """Write the manifest and return its path. Best-effort — a write
        failure is swallowed so a successful transfer isn't masked."""
        try:
            duration = round(time.perf_counter() - started, 3)
            return _write_transfer_manifest(
                direction=direction,
                project_name=project_name,
                compute_env_name=compute_env_name,
                zone=zone,
                transport=transport,
                local_path=local_path,
                remote_abs_path=remote_abs_path,
                command=command,
                result=result,
                bytes_transferred=bytes_transferred,
                duration_s=duration,
                sha256=sha256,
                task_id=task_id,
                verified_method=verified_method,
                error_msg=error_msg,
                checksums=checksums,
                pending_rename=pending_rename,
                path=receipt.get("path"),
            )
        except Exception:
            # Manifest write must not mask the real result.
            return Path("<manifest-write-failed>")

    try:
        # 1) Validate project name + paths.
        _validate_project_name_token(project_name)
        normed_remote = _validate_remote_abs_path(remote_abs_path)

        # 2) Load YAML + resolve project (synthesizing _ad_hoc if needed) + env.
        try:
            access = compute_access.load_access(
                Path(access_path) if access_path else None)
        except FileNotFoundError as e:
            # an absent manifest is an interpretable refusal: nothing to authorize against
            mpath = _journal(result="error", zone="?",
                              error_msg=f"access manifest not found: {e}")
            return refused("transfer.access_config_missing",
                    error=str(e), manifest=str(mpath))
        project = _get_project_or_ad_hoc(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        env_type = env.get("type")
        if env_type not in ("ssh", "local"):
            mpath = _journal(result="error", zone="?",
                error_msg=f"unsupported compute_env type {env_type!r}")
            return refused("transfer.unsupported_env_type",
                    error=f"unsupported compute_env type {env_type!r}",
                    manifest=str(mpath))

        # 3) Zone classification + auth (raises PermissionDenied otherwise).
        zone_info = _classify_zone_and_authorize(
            project=project, env=env,
            remote_abs_path=normed_remote,
            primitive_name=direction)
        zone = zone_info["zone"]

        # 4) Direction-specific local-side checks.
        tree = None    # a directory upload's {relative path: sha256}
        if direction == "upload":
            lp = _validate_local_path_for_upload(local_path)
            if lp.is_dir():
                tree, size_bytes = _scan_tree(lp)
                local_sha = None
            else:
                size_bytes = lp.stat().st_size
                local_sha = _compute_local_sha256(lp)
        else:
            lp = _validate_local_path_for_download(local_path)
            size_bytes = 0  # known post-transfer for downloads
            local_sha = None

        # Provider for ssh-mode envs. Created ONCE here and reused for the
        # transport-specific no-overwrite check, parent-dir prep, AND the
        # transfer itself — so the pre-checks don't assume ssh when the
        # wire protocol is Globus (a Globus transfer makes zero ssh calls).
        # local-mode envs handle byte movement inline (no provider).
        provider = None
        if env_type != "local":
            from agent.skills import transfer_providers
            provider = transfer_providers.get_transfer_provider(env)
        transport_name = "local_copy" if env_type == "local" else provider.name

        # 5) An earlier transfer to the same place whose task has not been seen to
        # end is still landing there under its partial name: a second copy would race it.
        dest = normed_remote if direction == "upload" else os.path.realpath(lp)
        inflight = (pending_upload(env, dest) if direction == "upload"
                    else pending_download(dest, access))
        if inflight is not None:
            path, rec = inflight
            tid = rec.get("globus_task_id")
            call = status_call(str(rec.get("project", "?")), str(rec["pending_rename"].get("env")), tid)
            mpath = _journal(result="refused", zone=zone, transport=transport_name,
                             error_msg=f"an earlier {direction} to it, Globus task {tid}, has not been seen to end")
            return loop("transfer.in_flight",
                error=(f"an earlier {direction} to {dest!r} is Globus task {tid}, whose end "
                       f"has not been seen; a second copy would race it, so nothing was sent"),
                task_id=tid, receipt=str(path), manifest=str(mpath), status_call=call,
                remedy=(f"{call}: it puts the bytes in place once the task has succeeded, "
                        f"and clears the way if it ended otherwise"))

        # Remote-existence pre-check (no-overwrite contract). Routed
        # through the provider so it's the login-node `seen` for scp but
        # `globus stat` for Globus — no ssh on the Globus path. A hit is a
        # REFUSAL (intended safety behavior), not a failure.
        if direction == "upload":
            if env_type == "local":
                if Path(normed_remote).exists():
                    mpath = _journal(result="refused", zone=zone,
                        transport=transport_name,
                        error_msg="a file already exists at the destination")
                    return refused("transfer.remote_exists_local",
                        error=(
                        f"remote path already exists: {normed_remote!r}. "
                        f"upload refuses overwrites. Pick a fresh "
                        f"remote_abs_path or remove the existing file."),
                        manifest=str(mpath))
            else:
                ec = provider.remote_exists(
                    env=env, abs_remote_path=normed_remote, timeout=timeout)
                if "error" in ec:
                    mpath = _journal(result="error", zone=zone,
                        transport=transport_name, error_msg=ec["error"])
                    return broke("transfer.remote_exists_check_failed",
                        **ec, manifest=str(mpath))
                if ec["exists"]:
                    mpath = _journal(result="refused", zone=zone,
                        transport=transport_name,
                        error_msg="a file already exists at the destination")
                    return refused("transfer.remote_exists",
                        error=(
                        f"remote path already exists: {normed_remote!r}. "
                        f"upload refuses overwrites. Pick a fresh "
                        f"remote_abs_path or remove the existing file."),
                        manifest=str(mpath))

        # 6) Ensure the destination parent dir exists. Provider-routed:
        # ssh `mkdir -p` for scp, a no-op for Globus (the Transfer
        # service auto-creates the destination tree).
        if direction == "upload":
            if env_type == "local":
                _local_mkdir_parent(Path(normed_remote))
            else:
                mk = provider.ensure_parent_dir(
                    env=env, abs_remote_path=normed_remote, timeout=timeout)
                if mk is not None:
                    mpath = _journal(result="error", zone=zone,
                        transport=transport_name, error_msg=mk["error"])
                    return broke("transfer.mkdir_failed",
                        **mk, manifest=str(mpath))
        # download: local parent existence is already validated by
        # _validate_local_path_for_download

        # 7) Dispatch transfer: a local-mode env copies here (a directory as a
        # tree); an ssh-mode env goes through the already-created provider.
        if env_type == "local":
            src, dst = ((lp, Path(normed_remote)) if direction == "upload"
                        else (Path(normed_remote), lp))
            if not src.is_dir():
                return _do_local_mode(
                    direction=direction,
                    project_name=project_name,
                    compute_env_name=compute_env_name,
                    lp=lp, normed_remote=normed_remote,
                    local_sha=local_sha, zone=zone, journal=_journal,
                    started=started)
            pr = _copy_local_tree(src, dst, source=tree, appeared=(
                "transfer.remote_exists_local" if direction == "upload" else "transfer.local_exists"))
        else:
            def on_task(task_id: str) -> None:
                # Journaled the moment the task exists, so a call that dies while it
                # waits still leaves the hold and the rename for globus_task_status.
                pending = {"env": compute_env_name, "to": dest,
                           "from": partial_path(normed_remote if direction == "upload" else str(lp))}
                written = _journal(result="waiting", zone=zone, transport=provider.name,
                                   task_id=task_id, pending_rename=pending)
                if written.is_file():
                    receipt["path"] = written
                    receipt["hold"] = _hold_receipt(written)
            if direction == "upload":
                pr = provider.upload_one(
                    env=env, local_path=lp, abs_remote_path=normed_remote,
                    local_sha256=local_sha, timeout=timeout,
                    label=f"upload {project_name} {Path(normed_remote).name}",
                    tree=tree, on_task=on_task)
            else:
                pr = provider.download_one(
                    env=env, abs_remote_path=normed_remote,
                    local_path=lp, timeout=timeout,
                    label=f"download {project_name} {Path(normed_remote).name}",
                    on_task=on_task)
        if pr.get("outcome") == "loop":
            # Globus may still be running the task: we stopped waiting, or could not
            # see how it ended. Not a failure, and the receipt must not say one: it
            # carries the task, and the rename that completes it once the task succeeds.
            pending = ({"env": compute_env_name, "from": pr["partial_path"], "to": dest}
                       if pr.get("partial_path") else None)
            mpath = _journal(result="waiting", zone=zone,
                transport=pr.get("provider") or transport_name,
                command=pr.get("command"), task_id=pr.get("task_id"),
                pending_rename=pending)
            out = {k: v for k, v in pr.items() if k not in ("outcome", "code")}
            out["status_call"] = status_call(project_name, compute_env_name, pr.get("task_id"))
            out["remedy"] = f"{out['status_call']} says how it ended; do not re-run the transfer before it does"
            if pr.get("code") == "transfer.globus_sync_wait_exceeded":
                return loop("transfer.globus_sync_wait_exceeded", **out, manifest=str(mpath))
            return loop("transfer.globus_task_unobserved", **out, manifest=str(mpath))
        if "error" in pr:
            said_no = pr.get("outcome") == "refused"
            mpath = _journal(result="refused" if said_no else "error", zone=zone,
                transport=pr.get("provider") or transport_name,
                command=pr.get("command"),
                task_id=pr.get("task_id"),
                error_msg=pr["error"])
            out = dict(pr); out["manifest"] = str(mpath)
            if pr.get("code") in _APPEARED:                   # it appeared while this transfer ran
                return refused(pr["code"], **out)
            if said_no:
                return refused("transfer.provider_refused", **out)
            return broke("transfer.provider_failed", **out)

        # Sync success.
        checksums = pr.get("checksums")   # a directory's {relative path: sha256}
        bytes_done = pr.get("bytes", size_bytes if direction == "upload"
                            else (lp.stat().st_size if lp.exists() else 0))
        # For downloads, the provider's "local_sha256" is the post-fetch
        # hash on the laptop; for uploads it's the pre-transfer local hash.
        # A directory has its checksums instead.
        if direction == "download" and checksums is None:
            local_sha = pr.get("local_sha256") or _compute_local_sha256(lp)
        # The single sha we record: scp/local verify via round-trip (both
        # ends equal); Globus has none (it does its own end-to-end check).
        sha_record = (pr.get("remote_sha256") or local_sha
                      if pr.get("verified_method") == "sha256_round_trip"
                      else None)
        mpath = _journal(result="success", zone=zone,
            transport=pr["provider"], command=pr.get("command"),
            task_id=pr.get("task_id"),
            bytes_transferred=bytes_done,
            sha256=sha_record,
            verified_method=pr.get("verified_method"),
            checksums=checksums)
        sums_path = _checksums_path(mpath)
        tree_fields = ({} if checksums is None else
                       {"files": len(checksums),
                        **({"checksums": str(sums_path)} if sums_path.is_file() else {})})
        return proven("transfer.transferred",
            success=True,
            project=project_name,
            compute_env=compute_env_name,
            zone=zone,
            direction=direction,
            local_path=str(lp),
            remote_abs_path=normed_remote,
            provider=pr["provider"],
            task_id=pr.get("task_id"),
            bytes=bytes_done,
            duration_s=round(time.perf_counter() - started, 3),
            local_sha256=local_sha,
            remote_sha256=pr.get("remote_sha256"),
            verified_method=pr.get("verified_method"),
            manifest=str(mpath),
            **tree_fields,
        )

    except TransferError as e:
        mpath = _journal(result="error", zone="?", error_msg=str(e))
        return refused("transfer.validation_error",
                       error=str(e), manifest=str(mpath))
    except compute_access.PermissionDenied as e:
        mpath = _journal(result="error", zone="?",
                          error_msg=f"PermissionDenied: {e}")
        return refused("transfer.permission_denied",
                error=f"PermissionDenied: {e}",
                manifest=str(mpath))
    except compute_access.ConfigError as e:
        mpath = _journal(result="error", zone="?",
                          error_msg=f"ConfigError: {e}")
        return refused("transfer.config_error",
                error=f"ConfigError: {e}",
                manifest=str(mpath))
    except KeyError as e:
        # get_project / get_compute_env raise KeyError for an unknown name.
        # A hostile/typo'd project or env is a clean auth failure, not a crash
        # the agent can't interpret (C4).
        msg = str(e).strip("'\"")
        mpath = _journal(result="error", zone="?", error_msg=msg)
        return refused("transfer.project_or_env_not_found",
                error=msg, manifest=str(mpath))
    except subprocess.TimeoutExpired as e:
        msg = f"subprocess timed out after {e.timeout}s"
        mpath = _journal(result="error", zone="?", error_msg=msg)
        return broke("transfer.timeout", error=msg, manifest=str(mpath))
    except OSError as e:
        # this machine's side: the access file unreadable, a local copy that
        # could not be read or written
        msg = f"{type(e).__name__}: {e}"
        mpath = _journal(result="error", zone="?", error_msg=msg)
        return broke("transfer.local_io_failed", error=msg, manifest=str(mpath))
    finally:
        if receipt.get("hold") is not None:
            os.close(receipt["hold"])


def resolve_globus_task(env: dict, task_id: str) -> dict:
    """A Globus task's real end state, and the step a sync transfer left undone:
    a transfer whose task was not seen to end lands under its partial name, and
    its receipt names the rename that completes it. When the task has SUCCEEDED,
    that rename is made here (an upload's on the cluster, a download's on this
    machine once what landed has been read) and the receipt says the transfer
    finished; when it ended otherwise, the receipt says it failed and no longer
    holds the destination for it, and a download's partial is removed. A task is
    resolved only on the env it ran on."""
    from agent.skills import transfer_providers
    status = transfer_providers.globus_task_status(env, task_id)
    found = _pending_receipt(task_id=task_id)
    if found is None or status.get("code") not in ("transfer.task_status_ok", "transfer.task_failed",
                                                     "transfer.task_incomplete"):
        return status
    path = found[0]
    hold = _hold_receipt(path)
    if hold is None:
        return loop("transfer.task_call_waiting", success=False, task_id=task_id, manifest=str(path),
                    task_status=status.get("status"),
                    error=(f"the call that started Globus task {task_id} is still waiting on it and "
                           f"puts the bytes in place itself; nothing was changed here"),
                    remedy=("wait for that call to return (check_job, if it ran with background=True); "
                            "globus_task_status after it says how the transfer ended"))
    try:
        return _resolve_held(env, task_id, status, path)
    finally:
        os.close(hold)


def _resolve_held(env: dict, task_id: str, status: dict, path: Path) -> dict:
    """resolve_globus_task's work, under the hold on the receipt at `path`: read again
    under the hold, it may have been finished since it was found."""
    from agent.skills import transfer_providers
    try:
        rec = json.loads(path.read_text())
    except (OSError, ValueError):
        return status
    pending = rec.get("pending_rename") if isinstance(rec, dict) else None
    if not isinstance(pending, dict):
        return status
    direction = "download" if rec.get("direction") == "download" else "upload"
    if pending.get("env") != env.get("name"):
        call = status_call(str(rec.get("project", "?")), str(pending.get("env")), task_id)
        return refused("transfer.task_other_env", success=False, manifest=str(path), status_call=call,
                       error=(f"Globus task {task_id} is {'a download from' if direction == 'download' else 'an upload to'} "
                              f"env {pending.get('env')!r}; it is resolved there, so nothing was changed"),
                       remedy=call)
    rec.pop("pending_rename")
    if status["code"] != "transfer.task_status_ok":
        # the task ended without the transfer: nothing is put in place, the way is clear
        if direction == "download":
            transfer_providers._discard_local(Path(pending["from"]))
            return _ended_unplaced(env, task_id, path, rec, direction,
                                   f"{status['error']}; what had arrived was removed", status)
        return _ended_unplaced(env, task_id, path, rec, direction,
                               f"{status['error']}; anything that arrived is at {pending['from']}",
                               {**status, "partial_path": pending["from"]})
    fields = {k: v for k, v in status.items() if k not in ("outcome", "code", "success")}
    if direction == "download":
        return _download_into_place(env, task_id, path, rec, pending, status, fields)
    provider = transfer_providers.GlobusProvider(compute_access.get_globus_endpoints(env))
    moved = provider.rename(pending["from"], pending["to"])
    if "error" in moved:
        return _unrenamed(provider, env, task_id, path, rec, pending, moved["error"], fields)
    rec["outcome"] = "uploaded"
    rec["summary"] = _summarize(
        outcome="uploaded", direction="upload", transport="globus",
        fname=rec.get("file", ""), size_h=rec.get("size"),
        compute_env=env.get("name", "?"), remote_abs_path=pending["to"],
        task_id=task_id, reason=None, files=rec.get("files"))
    rec["verified"] = _VERIFIED_PHRASE["globus_end_to_end"]
    _rewrite_receipt(path, rec)
    return proven("transfer.task_status_ok", success=True, renamed_to=pending["to"],
                  manifest=str(path), **fields)


def _ended_unplaced(env: dict, task_id: str, path: Path, rec: dict, direction: str,
                    reason: str, out: dict) -> dict:
    """Rewrite a transfer's receipt to say it failed, for `reason`, and return `out`
    with the receipt named."""
    rec["outcome"] = "failed"
    rec["reason"] = reason
    rec["summary"] = _summarize(
        outcome="failed", direction=direction, transport="globus",
        fname=rec.get("file", ""), size_h=rec.get("size"),
        compute_env=env.get("name", "?"), remote_abs_path="",
        task_id=task_id, reason=reason, files=rec.get("files"))
    _rewrite_receipt(path, rec)
    return {**out, "manifest": str(path)}


def _download_into_place(env: dict, task_id: str, path: Path, rec: dict, pending: dict,
                         status: dict, fields: dict) -> dict:
    """A download whose task SUCCEEDED: what landed under the partial name is read
    as the sync path reads it (a directory must hold every file the task moved) and
    renamed into place, unless something arrived there meanwhile. What cannot be put
    in place is removed, as after a failed download. With nothing left under the
    partial name, what is at the destination decides, as for an upload's rename."""
    from agent.skills import transfer_providers
    part, dst = Path(pending["from"]), Path(pending["to"])
    there = dst.exists() or dst.is_symlink()
    if not (part.exists() or part.is_symlink()):
        why = f"nothing is at {part} any more"
        if there:
            rec["outcome"] = "unconfirmed"
            rec["reason"] = (f"the task succeeded and {dst} holds a file, but the rename that would "
                             f"have put this download there was not seen to complete")
            rec["summary"] = f"Download of {rec.get('file', '')} from {env.get('name', '?')}: {rec['reason']}."
            _rewrite_receipt(path, rec)
            return degraded("transfer.task_rename_unconfirmed", success=False, manifest=str(path),
                            **fields, error=f"{why}; {rec['reason']}",
                            remedy=f"download it again to another path and compare, if {dst} must be "
                                   f"this task's bytes")
        return _ended_unplaced(env, task_id, path, rec, "download", why, broke(
            "transfer.task_rename_failed", success=False, **fields, error=why,
            remedy=f"download it again: the way to {dst} is clear"))
    try:
        checksums, nbytes = _scan_tree(part) if part.is_dir() else (None, part.stat().st_size)
        sha = None if checksums is not None else _compute_local_sha256(part)
        short = None if checksums is None else transfer_providers._tree_shortfall(status, len(checksums))
        if short:
            raise TransferError(f"the task SUCCEEDED, but {short}")
    except (OSError, TransferError) as e:
        transfer_providers._discard_local(part)
        why = f"{e}; what had arrived was removed"
        return _ended_unplaced(env, task_id, path, rec, "download", why, broke(
            "transfer.task_rename_failed", success=False, **fields, error=why,
            remedy=f"download it again: the way to {dst} is clear"))
    try:
        _into_place(part, dst)
    except TransferError as e:
        why = str(e)
        return _ended_unplaced(env, task_id, path, rec, "download", why, broke(
            "transfer.task_rename_blocked", success=False, **fields, error=why,
            remedy=f"download to another path: {dst} holds a file this download did not put there"))
    rec["outcome"] = "downloaded"
    rec["size"] = _human_size(nbytes)
    rec["summary"] = _summarize(
        outcome="downloaded", direction="download", transport="globus",
        fname=rec.get("file", ""), size_h=rec["size"], compute_env=env.get("name", "?"),
        remote_abs_path="", task_id=task_id, reason=None,
        files=None if checksums is None else len(checksums))
    rec["verified"] = _VERIFIED_PHRASE["globus_end_to_end"]
    landed: dict = {"local_sha256": sha}
    if checksums is not None:
        sums = str(_write_checksums(path, checksums))
        rec.update(files=len(checksums), checksums=sums)
        landed = {"files": len(checksums), "checksums": sums}
    _rewrite_receipt(path, rec)
    return proven("transfer.task_status_ok", success=True, renamed_to=str(dst),
                  manifest=str(path), **landed, **fields)


def _unrenamed(provider, env: dict, task_id: str, path: Path, rec: dict, pending: dict,
               why: str, fields: dict) -> dict:
    """A succeeded task whose rename did not answer as made, by what is at each name.
    The receipt keeps holding the destination only while a later rename can still
    make it: the partial is there and the destination is not, or either could not
    be read."""
    def at(p: str):
        seen = provider.remote_exists(env=env, abs_remote_path=p, timeout=60)
        return seen.get("exists"), seen.get("error")
    (part, part_err), (dest, dest_err) = at(pending["from"]), at(pending["to"])
    if part is None or dest is None or (part and not dest):
        unread = "; ".join(e for e in (part_err, dest_err) if e)
        return broke("transfer.task_rename_failed", success=False, partial_path=pending["from"],
                     manifest=str(path), **fields, error=why + (f"; {unread}" if unread else ""),
                     remedy="globus_task_status again: it retries the rename")
    rec["reason"] = {
        (False, False): f"the task succeeded, but nothing is at {pending['from']} to rename",
        (False, True): (f"the task succeeded and {pending['to']} holds a file, but the rename "
                        f"that would have put this upload there was not seen to complete"),
        (True, True): (f"the task succeeded, but {pending['to']} was written while it ran; "
                       f"this upload's bytes stay at {pending['from']} and nothing was replaced"),
    }[(part, dest)]
    rec["outcome"] = "unconfirmed" if (part, dest) == (False, True) else "failed"
    rec["summary"] = f"Upload of {rec.get('file', '')} to {env.get('name', '?')}: {rec['reason']}."
    _rewrite_receipt(path, rec)
    if not part and dest:
        return degraded("transfer.task_rename_unconfirmed", success=False, manifest=str(path),
                        **fields, error=f"{why}; {rec['reason']}",
                        remedy=(f"download {pending['to']} and compare it with the file uploaded, "
                                f"if it must be this upload's bytes"))
    if part and dest:
        return broke("transfer.task_rename_blocked", success=False, partial_path=pending["from"],
                       manifest=str(path), **fields, error=f"{why}; {rec['reason']}",
                       remedy=(f"upload to another path; {pending['from']} holds this upload's "
                               f"verified bytes, left for you to keep or remove"))
    return broke("transfer.task_rename_failed", success=False, manifest=str(path), **fields,
                 error=f"{why}; nothing is at {pending['from']} any more",
                 remedy=f"upload it again: the way to {pending['to']} is clear")


def _hold_receipt(path: Path) -> Optional[int]:
    """An exclusive hold on a transfer's receipt, so one process at a time puts its bytes
    in place: the call that started the transfer while it lives, else globus_task_status.
    None when another process has it. The kernel lets go when the holder exits, however
    it exits."""
    import fcntl
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _pending_receipt(*, task_id: Optional[str] = None, env: Optional[str] = None,
                     direction: Optional[str] = None,
                     to: Optional[str] = None) -> Optional[tuple[Path, dict]]:
    """The receipt, under any project, of a transfer whose Globus task has not been
    seen to end, and so still waits to be renamed into place: by its task, or by its
    direction and the path it goes to (its pending rename's `to`), on `env` when one
    is named."""
    root = _record_root() / "transfer_history"
    for p in sorted(root.glob("*/*/*.json"), key=lambda p: p.name, reverse=True):
        try:
            text = p.read_text()
            rec = json.loads(text) if '"pending_rename"' in text else None
        except (OSError, ValueError):
            continue
        pending = rec.get("pending_rename") if isinstance(rec, dict) else None
        if not isinstance(pending, dict):
            continue
        if (rec.get("globus_task_id") == task_id if task_id
                else (rec.get("direction"), pending.get("to")) == (direction, to)
                and env in (None, pending.get("env"))):
            return p, rec
    return None


def _speaks_globus(env: dict) -> bool:
    """The one wire globus_task_status resolves a task on, so the only one a hold lifts on."""
    return env.get("type") == "ssh" and compute_access.get_globus_endpoints(env) is not None


def pending_upload(env: dict, remote_abs_path: str) -> Optional[tuple[Path, dict]]:
    """The receipt of an earlier upload to `remote_abs_path` on `env` whose Globus task
    has not been seen to end; only on an env that speaks Globus."""
    if not _speaks_globus(env):
        return None
    return _pending_receipt(env=env.get("name"), direction="upload",
                            to=_validate_remote_abs_path(remote_abs_path))


def pending_download(local_path: str, access: dict) -> Optional[tuple[Path, dict]]:
    """The receipt of an earlier download to `local_path` (resolved) on this machine,
    from any env in `access` on any wire, whose Globus task has not been seen to end;
    held while the env it came from still speaks Globus."""
    envs = {e.get("name"): e for e in access.get("compute_envs") or []}
    if not any(map(_speaks_globus, envs.values())):
        return None
    found = _pending_receipt(direction="download", to=local_path)
    if found is None:
        return None
    holder = envs.get(found[1]["pending_rename"].get("env"))
    return found if holder is not None and _speaks_globus(holder) else None


def status_call(project_name: str, compute_env_name: str, task_id: Optional[str]) -> str:
    """The globus_task_status call that resolves `task_id`, every argument named."""
    return (f"globus_task_status(project_name={project_name!r}, compute_env_name={compute_env_name!r}, "
            f"task_id={task_id!r})")


def _rewrite_receipt(path: Path, rec: dict) -> None:
    try:
        path.write_text(json.dumps(rec, indent=2))
    except OSError:
        pass


def _into_place(part: Path, dst: Path) -> None:
    """Rename a verified local copy to its final name, unless something arrived
    there while it was copied: then the copy is discarded and nothing is replaced."""
    if dst.exists() or dst.is_symlink():
        if part.is_dir():
            shutil.rmtree(part, ignore_errors=True)
        else:
            part.unlink(missing_ok=True)
        raise TransferError(f"{dst} appeared while the copy ran; transfers never overwrite, "
                            f"so the copy was discarded")
    os.rename(part, dst)


#: The refusals for a destination that appeared while the transfer ran: on the env (scp,
#: Globus), on a local env, on this machine.
_APPEARED = frozenset({"transfer.remote_exists", "transfer.remote_exists_local", "transfer.local_exists"})


def _copy_local_tree(src: Path, dst: Path, *, source: Optional[dict], appeared: str) -> dict:
    """A local-mode env's directory transfer, shaped like a provider's: copy
    the tree to its partial name, read both sides back and compare every
    file's sha256, then rename it into place."""
    source = source if source is not None else _scan_tree(src)[0]
    command = f"cp -R {shlex.quote(str(src))} {shlex.quote(str(dst))}"
    part = Path(partial_path(str(dst)))
    shutil.rmtree(part, ignore_errors=True)
    shutil.copytree(src, part)
    copy, nbytes = _scan_tree(part)
    mismatch = _tree_mismatch(source, copy)
    if mismatch:
        shutil.rmtree(part, ignore_errors=True)
        return broke("transfer.local_tree_mismatch",
                     error=f"the copy of {src} differs from it: {mismatch}",
                     provider="local_copy", command=command)
    from agent.skills.transfer_providers import _put_in_place
    return _put_in_place(part, dst, proven("transfer.local_tree_copied", success=True,
                                           provider="local_copy", command=command,
                                           bytes=nbytes, checksums=copy,
                                           verified_method="sha256_round_trip"), appeared=appeared)


def _do_local_mode(*, direction: str,
                    project_name: str, compute_env_name: str,
                    lp: Path, normed_remote: str,
                    local_sha: Optional[str], zone: str, journal,
                    started: float) -> dict:
    """Inline shutil.copy path for local-mode envs. Same trust contract
    (sha256 both ends + size compare) but no wire-protocol provider."""
    cp_cmd_up = f"cp {shlex.quote(str(lp))} {shlex.quote(normed_remote)}"
    if direction == "upload":
        part = Path(partial_path(normed_remote))
        shutil.copy(str(lp), part)
        remote_sha = _compute_local_sha256(part)
        if remote_sha != local_sha:
            part.unlink(missing_ok=True)
            mpath = journal(result="error", zone=zone,
                transport="local_copy", command=cp_cmd_up,
                error_msg=("sha256 round-trip mismatch on local-mode "
                           "upload"))
            return broke("transfer.sha_mismatch_upload",
                error=(
                f"sha256 round-trip mismatch — local={local_sha} "
                f"remote={remote_sha!r}; the copy was discarded."),
                manifest=str(mpath))
        try:
            _into_place(part, Path(normed_remote))
        except TransferError as e:
            mpath = journal(result="refused", zone=zone, transport="local_copy",
                            command=cp_cmd_up, error_msg=str(e))
            return refused("transfer.remote_exists_local", error=str(e), manifest=str(mpath))
        bytes_done = lp.stat().st_size
        mpath = journal(result="success", zone=zone,
            transport="local_copy", command=cp_cmd_up,
            bytes_transferred=bytes_done,
            sha256=local_sha,
            verified_method="sha256_round_trip")
        return proven("transfer.local_uploaded",
            success=True,
            project=project_name,
            compute_env=compute_env_name,
            direction=direction,
            local_path=str(lp),
            remote_abs_path=normed_remote,
            zone=zone,
            provider="local_copy",
            bytes=bytes_done,
            duration_s=round(time.perf_counter() - started, 3),
            local_sha256=local_sha,
            remote_sha256=remote_sha,
            verified_method="sha256_round_trip",
            manifest=str(mpath),
        )
    # download local-mode
    cp_cmd_dn = f"cp {shlex.quote(normed_remote)} {shlex.quote(str(lp))}"
    if not Path(normed_remote).exists():
        mpath = journal(result="error", zone=zone, transport="local_copy",
            error_msg=f"local-mode source does not exist")
        return broke("transfer.local_source_missing",
            error=(
            f"local-mode download source does not exist: "
            f"{normed_remote!r}"),
            manifest=str(mpath))
    remote_sha = _compute_local_sha256(Path(normed_remote))
    part = Path(partial_path(str(lp)))
    shutil.copy(normed_remote, part)
    landed_sha = _compute_local_sha256(part)
    if landed_sha != remote_sha:
        part.unlink(missing_ok=True)
        mpath = journal(result="error", zone=zone, transport="local_copy",
            command=cp_cmd_dn,
            error_msg="sha256 mismatch on local-mode download")
        return broke("transfer.sha_mismatch_download",
            error=(
            f"sha256 mismatch on local-mode download: "
            f"source={remote_sha} landed={landed_sha}"),
            manifest=str(mpath))
    try:
        _into_place(part, lp)
    except TransferError as e:
        mpath = journal(result="refused", zone=zone, transport="local_copy",
                        command=cp_cmd_dn, error_msg=str(e))
        return refused("transfer.local_exists", error=str(e), manifest=str(mpath))
    bytes_done = lp.stat().st_size
    mpath = journal(result="success", zone=zone,
        transport="local_copy", command=cp_cmd_dn,
        bytes_transferred=bytes_done,
        sha256=landed_sha,
        verified_method="sha256_round_trip")
    return proven("transfer.local_downloaded",
        success=True,
        project=project_name,
        compute_env=compute_env_name,
        direction=direction,
        local_path=str(lp),
        remote_abs_path=normed_remote,
        zone=zone,
        provider="local_copy",
        bytes=bytes_done,
        duration_s=round(time.perf_counter() - started, 3),
        local_sha256=landed_sha,
        remote_sha256=remote_sha,
        verified_method="sha256_round_trip",
        manifest=str(mpath),
    )
