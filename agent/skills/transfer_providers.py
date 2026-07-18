"""
transfer_providers — the wire-protocol layer for ssh-mode compute envs.

The unified `transfer.upload` / `transfer.download` primitives go
through a TransferProvider for everything that touches the remote:
the no-overwrite pre-check, parent-dir preparation, AND the byte
movement + verification. The primitive owns the POLICY (validate
paths, refuse an overwrite, anchor the local sha256, journal the
result); the provider owns the MECHANISM for its transport.

This split is deliberate: an ssh-connected env can use Globus as its
data plane, and the no-overwrite check / parent-dir mkdir must NOT
assume ssh just because the control plane is ssh. Each provider
answers `remote_exists` and `ensure_parent_dir` in its own transport,
so a Globus transfer needs no ssh session at all.

Two providers live here today:

  ScpHeadNodeProvider — the legacy default. scp over ssh + sha256sum
    round-trip on the remote side; we compare hashes ourselves. Its
    no-overwrite check is `ssh test -e`; its parent-dir prep is
    `ssh mkdir -p`. Fine for small files; pisses off head-node policy
    when used for the GB-scale .sif images that stage_apptainer_image
    ships.

  GlobusProvider — `globus transfer` end-to-end, fully off the head
    node. Its no-overwrite check is `globus ls` on the parent dir (a
    read-only op that doesn't even need data_access consent); its
    parent-dir prep is a NO-OP because the Globus Transfer service
    auto-creates the destination directory tree. Net effect: a Globus
    transfer makes ZERO ssh calls — submit, poll, verify, all via the
    `globus` CLI.

LOCAL-mode envs do NOT go through a provider. They use direct
shutil.copy in the primitives; the provider abstraction is explicitly
for ssh-mode wire-protocol selection.

The interface is four methods:

  remote_exists(env, abs_remote_path, timeout)        → {exists} | {error}
  ensure_parent_dir(env, abs_remote_path, timeout)    → None | {error}
  upload_one(env, local_path, abs_remote_path, local_sha256, timeout)
  download_one(env, abs_remote_path, local_path, timeout)

The transfer methods return a dict-in-dict-out shape: success path has
{success: True, bytes, duration_s, transferred_at, provider,
 remote_sha256?, task_id?, verified_method}; failure surfaces as
{error: "...", hint?, ...}.

`verified_method` is the provider's testimony for how it knows the
bytes arrived intact:
  sha256_round_trip — provider computed both ends + compared (scp)
  globus_end_to_end — Globus's own end-to-end checksum + task SUCCEEDED
"""
from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path
from typing import Optional

from agent.skills import compute_access
from agent.skills.outcomes import proven, refused, broke, loop


def _display_cmd(argv: list) -> str:
    """Render an argv list as a single copy-pasteable shell command
    string (each token shell-quoted). Used to record the ACTUAL command
    a transfer ran in the manifest."""
    return " ".join(shlex.quote(str(a)) for a in argv)


class TransferError(Exception):
    """A provider's transfer raised an error (timeout, sha mismatch,
    auth, endpoint deactivated). Caller surfaces as `{error: ...}`."""


class TransferProvider:
    """Base class. Each concrete provider implements upload_one +
    download_one. Providers are stateless; the env dict is passed on
    every call (so a single instance can serve many envs)."""

    name: str = ""

    def remote_exists(self, *, env: dict, abs_remote_path: str,
                      timeout: int) -> dict:
        """No-overwrite pre-check: does `abs_remote_path` already exist
        on the remote? Transport-specific (ssh `test -e` vs `globus
        ls`). Returns {exists: bool} or {error: ...}. The PRIMITIVE owns
        the policy (refuse when exists); the PROVIDER owns the mechanism
        so the check doesn't assume ssh on a Globus data plane."""
        raise NotImplementedError

    def ensure_parent_dir(self, *, env: dict, abs_remote_path: str,
                          timeout: int) -> Optional[dict]:
        """Make sure the destination's parent directory exists before
        the transfer. Transport-specific: ssh `mkdir -p`, or a no-op for
        transports (Globus) that auto-create the destination tree.
        Returns None on success, {error: ...} on failure."""
        raise NotImplementedError

    def upload_one(self, *, env: dict, local_path: Path,
                   abs_remote_path: str, local_sha256: str,
                   timeout: int,
                   label: Optional[str] = None) -> dict:
        """Provider-side upload. Blocks until the bytes are moved AND
        verified; `verified_method` records how the provider knows."""
        raise NotImplementedError

    def download_one(self, *, env: dict, abs_remote_path: str,
                     local_path: Path, timeout: int,
                     label: Optional[str] = None) -> dict:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# ScpHeadNodeProvider
#
# Existing logic factored into a class. The wire is scp+ssh+sha256sum
# round-trip. Helpers (_scp_argv, _ssh_argv, _compute_local_sha256,
# _remote_sha256_cmd, _parse_sha256sum_output, _ssh_failure_hint) still
# live in scratch.py / snapshot.py and are imported here rather than copied.
# NB: the remote-target and remote-sha256 steps are written INLINE below
# instead — the extraction duplicated them and orphaned the originals, which
# were deleted in tier 7. Inline is the only copy; keep it that way.
# ---------------------------------------------------------------------------

class ScpHeadNodeProvider(TransferProvider):
    """Legacy default. scp + ssh sha256sum round-trip. Each method does
    the byte movement only; the caller has already handled path safety,
    no-overwrite check, and (for upload) parent-dir mkdir."""

    name = "scp_head_node"

    def remote_exists(self, *, env: dict, abs_remote_path: str,
                      timeout: int) -> dict:
        """ssh `test -e` no-overwrite check. Delegates to the shared
        helper in transfer.py (which carries the ControlMaster-aware
        failure hint). Needs a live ssh session."""
        from agent.skills.transfer import _remote_existence_check
        return _remote_existence_check(env, abs_remote_path, timeout)

    def ensure_parent_dir(self, *, env: dict, abs_remote_path: str,
                          timeout: int) -> Optional[dict]:
        """ssh `mkdir -p` on the parent dir. Delegates to the shared
        helper; returns None on success or {error: ...}."""
        from agent.skills.transfer import _remote_mkdir_parent_ssh
        return _remote_mkdir_parent_ssh(env, abs_remote_path, timeout)

    def upload_one(self, *, env: dict, local_path: Path,
                   abs_remote_path: str, local_sha256: str,
                   timeout: int,
                   label: Optional[str] = None) -> dict:
        # scp is synchronous by nature; label is ignored.
        # We accept them so callers don't have to special-case the
        # provider when threading the flag through.
        from agent.skills.transfer import (
            _scp_argv, _remote_sha256_cmd, _parse_sha256sum_output,
            TransferError as ScratchPathError)
        from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint

        # Build the scp dst (user@host:abs_remote_path), with shell-safety
        # already enforced upstream.
        host = env["host"]
        user = env.get("user")
        target_host = f"{user}@{host}" if user else host
        scp_target = f"{target_host}:{abs_remote_path}"

        started = time.perf_counter()

        # 1) scp the file.
        argv = _scp_argv(env, str(local_path), scp_target)
        try:
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout)
        except subprocess.TimeoutExpired as e:
            return broke("transfer.scp_upload_timeout",
                    error=f"scp timed out after {e.timeout}s",
                    provider=self.name)
        if res.returncode != 0:
            hint = _ssh_failure_hint(res.stderr or "", host)
            out = {
                "error": (
                    f"scp failed (rc={res.returncode}): "
                    f"{(res.stderr or '').strip()[:500]}"),
                "provider": self.name,
            }
            if hint:
                out["hint"] = hint
            return broke("transfer.scp_upload_failed", **out)

        # 2) Verify via remote sha256sum.
        sha_cmd = _remote_sha256_cmd(abs_remote_path)
        sh_argv = _ssh_argv(env, sha_cmd)
        try:
            sh_res = subprocess.run(sh_argv, capture_output=True, text=True,
                                    timeout=timeout)
        except subprocess.TimeoutExpired as e:
            return broke("transfer.scp_upload_verify_timeout", error=
                f"remote sha256sum timed out after {e.timeout}s",
                provider=self.name)
        if sh_res.returncode != 0:
            return broke("transfer.scp_upload_verify_failed", error=
                f"remote sha256sum failed (rc={sh_res.returncode}): "
                f"{(sh_res.stderr or '').strip()[:500]}",
                provider=self.name)

        remote_sha = _parse_sha256sum_output(sh_res.stdout or "")
        if not remote_sha:
            return broke("transfer.scp_upload_verify_unparseable", error=
                f"remote sha256sum stdout unparseable: "
                f"{(sh_res.stdout or '').strip()[:500]!r}",
                provider=self.name)
        if remote_sha != local_sha256:
            return broke("transfer.scp_upload_sha_mismatch", error=
                f"sha256 mismatch on upload: local={local_sha256} "
                f"remote={remote_sha}",
                provider=self.name)

        duration = time.perf_counter() - started
        return proven("transfer.scp_uploaded",
            success=          True,
            provider=         self.name,
            command=          _display_cmd(argv),
            bytes=            local_path.stat().st_size,
            duration_s=       round(duration, 3),
            transferred_at=   _now_iso(),
            remote_sha256=    remote_sha,
            verified_method=  "sha256_round_trip",
        )

    def download_one(self, *, env: dict, abs_remote_path: str,
                     local_path: Path, timeout: int,
                     label: Optional[str] = None) -> dict:
        # label ignored — scp is sync-only.
        from agent.skills.transfer import (
            _scp_argv, _remote_sha256_cmd, _parse_sha256sum_output,
            _compute_local_sha256)
        from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint

        host = env["host"]
        user = env.get("user")
        target_host = f"{user}@{host}" if user else host
        scp_src = f"{target_host}:{abs_remote_path}"

        started = time.perf_counter()

        # 1) Pre-anchor: compute the remote sha BEFORE transfer so we have
        # the expected hash to compare against once the file lands locally.
        sha_cmd = _remote_sha256_cmd(abs_remote_path)
        sh_argv = _ssh_argv(env, sha_cmd)
        try:
            sh_res = subprocess.run(sh_argv, capture_output=True, text=True,
                                    timeout=timeout)
        except subprocess.TimeoutExpired as e:
            return broke("transfer.scp_download_verify_timeout", error=
                f"remote sha256sum timed out after {e.timeout}s",
                provider=self.name)
        if sh_res.returncode != 0:
            hint = _ssh_failure_hint(sh_res.stderr or "", host)
            out = {
                "error":
                    f"remote sha256sum failed (rc={sh_res.returncode}): "
                    f"{(sh_res.stderr or '').strip()[:500]}",
                "provider": self.name,
            }
            if hint:
                out["hint"] = hint
            return broke("transfer.scp_download_verify_failed", **out)
        remote_sha = _parse_sha256sum_output(sh_res.stdout or "")
        if not remote_sha:
            return broke("transfer.scp_download_verify_unparseable", error=
                f"remote sha256sum stdout unparseable: "
                f"{(sh_res.stdout or '').strip()[:500]!r}",
                provider=self.name)

        # 2) scp the file back.
        argv = _scp_argv(env, scp_src, str(local_path))
        try:
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout)
        except subprocess.TimeoutExpired as e:
            return broke("transfer.scp_download_timeout",
                    error=f"scp timed out after {e.timeout}s",
                    provider=self.name)
        if res.returncode != 0:
            hint = _ssh_failure_hint(res.stderr or "", host)
            out = {
                "error":
                    f"scp failed (rc={res.returncode}): "
                    f"{(res.stderr or '').strip()[:500]}",
                "provider": self.name,
            }
            if hint:
                out["hint"] = hint
            return broke("transfer.scp_download_failed", **out)

        # 3) Compute local sha + compare.
        local_sha = _compute_local_sha256(local_path)
        if local_sha != remote_sha:
            # The fetched bytes don't match what was on the remote. Remove
            # the corrupt local copy so a retry isn't refused as already-
            # existing.
            try:
                local_path.unlink()
            except OSError:
                pass
            return broke("transfer.scp_download_sha_mismatch", error=
                f"sha256 mismatch on download: remote={remote_sha} "
                f"local={local_sha}",
                provider=self.name)

        duration = time.perf_counter() - started
        return proven("transfer.scp_downloaded",
            success=          True,
            provider=         self.name,
            command=          _display_cmd(argv),
            bytes=            local_path.stat().st_size,
            duration_s=       round(duration, 3),
            transferred_at=   _now_iso(),
            remote_sha256=    remote_sha,
            local_sha256=     local_sha,
            verified_method=  "sha256_round_trip",
        )


# ---------------------------------------------------------------------------
# GlobusProvider
#
# Shells out to the `globus` CLI (user-installed via `pipx install
# globus-cli` + `globus login`). Tokens live in ~/.globus/ out-of-band
# from us, same posture as ssh-agent.
#
# Per-transfer shape (sync default):
#   1. Submit:  `globus transfer <src_ep>:<src> <dst_ep>:<dst>
#                   --label "..." --encrypt-data --format json`
#                Parse `task_id` from JSON stdout. Exit-code 0 = submitted.
#   2. Poll:    `globus task show <task_id> --format json`
#                Statuses: ACTIVE | INACTIVE | SUCCEEDED | FAILED.
#                INACTIVE = creds expired / endpoint needs activation —
#                we surface as a clear actionable error rather than
#                waiting indefinitely.
#                Loop with sleep until SUCCEEDED (success) or FAILED
#                (error with `fatal_error.code/description` exposed).
#
# The wait is capped (_SYNC_WAIT_S_DEFAULT). Past the cap we STOP WAITING but
# Globus does NOT stop transferring — the task runs on server-side, and
# globus_task_status(task_id) is how the caller finds out how it ended.
#
# PERMISSION_DENIED diagnosis — Globus surfaces this nice_status for
# at least three root causes that demand DIFFERENT fixes:
#
#   1) local_path_not_allowed — Globus Connect Personal on the laptop
#      refuses to scan a source path outside its Accessible Folders
#      (default $HOME only). The fix is a GUI config change in GCP
#      preferences. Common; especially trips up new users staging
#      from /tmp or system dirs.
#   2) remote_consent_missing — GCS v5's data_access scope hasn't
#      been granted for the destination collection's host GCS. The
#      fix is `globus login --gcs <UUID>`. The UUID is discovered via
#      `globus gcs collection show <remote_ep>` (globus-cli prints it
#      in a MissingLoginError when the scope is needed).
#   3) remote_filesystem — POSIX permissions / quota / ACL on the
#      cluster. The fix is on the cluster, not Globus.
#
# All three present identically as nice_status=PERMISSION_DENIED at the
# task level; the ONLY way to tell them apart is to fetch the task's
# event list and read which endpoint reported the error. The classifier
# (_classify_permission_denied → _permission_denied_error) does exactly
# that and routes the actionable hint by classification. Until we had
# this, our error message always pointed at #2 — which would mislead
# users hitting #1 or #3 into running a `--gcs` login that wouldn't
# fix anything.
# ---------------------------------------------------------------------------

_GLOBUS_TERMINAL_STATUSES = {"SUCCEEDED", "FAILED"}

# Globus surfaces this nice_status whenever ANY endpoint in the
# transfer (local or remote) refuses an operation: GCP path not in
# Accessible Folders, GCS data_access consent missing, remote POSIX
# permissions, etc. We detect it in the poll loop and call
# _classify_permission_denied to figure out WHICH endpoint reported
# the error before building the user-facing hint.
_PERMISSION_DENIED_TOKEN = "PERMISSION_DENIED"

# When we detect PERMISSION_DENIED in the poll loop we fetch the task's
# event list (one shellout) to surface the underlying error.endpoint +
# context.path. The classifier uses these patterns to decide which
# root cause to surface.
import re as _re
_UUID_RE = _re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    _re.IGNORECASE)
# GCP refuses to scan paths outside its Accessible Folders config with
# this exact FTP-style body. Distinctive enough to key off of.
_GCP_PATH_BLOCK_BODY_RE = _re.compile(r"path not allowed", _re.IGNORECASE)


class GlobusError(TransferError):
    """A Globus CLI call failed (cli missing, auth, endpoint state,
    task failed). Returned as `{error: ..., hint?: ...}` from the
    provider methods."""


class GlobusProvider(TransferProvider):
    name = "globus"

    # Submit happens via a short subprocess; the cluster doesn't see
    # this. The poll cadence is what matters for the watchdog.
    _SUBMIT_TIMEOUT_S = 60
    _POLL_INTERVAL_S = 10
    # Hard ceiling on the sync wait — bounded so the agent's stream-watchdog
    # (~600s silent kill) never takes the whole call down with no record. Past
    # it we return globus_sync_wait_exceeded WITH the task_id; the transfer is
    # still running server-side and globus_task_status resolves it.
    _SYNC_WAIT_S_DEFAULT = 540

    def __init__(self, globus_block: dict):
        # Validated at config-load time, so these four keys are guaranteed
        # present + well-shaped if we got here.
        self._local_ep = globus_block["local_endpoint_id"]
        self._local_name = globus_block["local_endpoint_name"]
        self._remote_ep = globus_block["remote_endpoint_id"]
        self._remote_name = globus_block["remote_endpoint_name"]

    # ─── public API ────────────────────────────────────────────────────

    def upload_one(self, *, env: dict, local_path: Path,
                   abs_remote_path: str, local_sha256: str,
                   timeout: int,
                   label: Optional[str] = None) -> dict:
        return self._run_transfer(
            src_ep=self._local_ep, src_path=str(local_path),
            dst_ep=self._remote_ep, dst_path=abs_remote_path,
            direction="upload",
            local_path=local_path, local_sha256=local_sha256,
            timeout=timeout, label=label)

    def download_one(self, *, env: dict, abs_remote_path: str,
                     local_path: Path, timeout: int,
                     label: Optional[str] = None) -> dict:
        return self._run_transfer(
            src_ep=self._remote_ep, src_path=abs_remote_path,
            dst_ep=self._local_ep, dst_path=str(local_path),
            direction="download",
            local_path=local_path, local_sha256="",
            timeout=timeout, label=label)

    def remote_exists(self, *, env: dict, abs_remote_path: str,
                      timeout: int) -> dict:
        """Globus-native no-overwrite check — NO ssh. Lists the parent
        directory via `globus ls` (read-only; doesn't even require
        data_access consent) and checks whether the basename is present.

        A missing parent dir means the target can't exist yet →
        {exists: False} (Globus auto-creates the tree on transfer). Any
        OTHER non-zero exit (auth, consent, perms) is surfaced as an
        error rather than guessed-as-absent — we never silently treat a
        real failure as "safe to write"."""
        import json as _json
        import posixpath
        parent = posixpath.dirname(abs_remote_path)
        base = posixpath.basename(abs_remote_path)
        # Trailing slash makes the directory-listing intent explicit.
        argv = ["globus", "ls", f"{self._remote_ep}:{parent}/",
                "--format", "json"]
        try:
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout)
        except FileNotFoundError:
            return broke("transfer.globus_ls_cli_missing", error=
                "globus CLI not on PATH for the no-overwrite check. "
                "Install it (`pipx install globus-cli`) and `globus "
                "login`, then retry.",
                provider=self.name)
        except subprocess.TimeoutExpired as e:
            return broke("transfer.globus_ls_timeout", error=
                f"`globus ls` (no-overwrite check) timed out after "
                f"{e.timeout}s",
                provider=self.name)
        if res.returncode != 0:
            body = ((res.stderr or "") + " " + (res.stdout or "")).lower()
            # Parent dir doesn't exist yet → target can't either → not an
            # overwrite. Globus will create the tree during the transfer.
            if any(tok in body for tok in (
                    "not found", "notfound", "no such",
                    "could not be found", "does not exist",
                    "dirlistingfailed")):
                return {"exists": False}
            # Real failure (consent/auth/perms) — surface it; do NOT
            # fall through to "safe to write".
            return broke("transfer.globus_ls_failed", error=
                f"`globus ls` no-overwrite check failed "
                f"(rc={res.returncode}): "
                f"{(res.stderr or '').strip()[:300]}",
                provider=self.name)
        try:
            payload = _json.loads(res.stdout or "{}")
        except _json.JSONDecodeError as e:
            return broke("transfer.globus_ls_bad_json", error=
                f"`globus ls` returned 0 but stdout was not valid JSON: "
                f"{e}. raw: {(res.stdout or '').strip()[:200]!r}",
                provider=self.name)
        names = {entry.get("name")
                 for entry in (payload.get("DATA") or [])}
        return {"exists": base in names}

    def ensure_parent_dir(self, *, env: dict, abs_remote_path: str,
                          timeout: int) -> Optional[dict]:
        """No-op for Globus. The Transfer service auto-creates the
        destination's parent directory tree during the transfer, so
        there's nothing to do here — and crucially, nothing that needs
        an ssh hop. Returns None (success)."""
        return None

    # ─── internals ─────────────────────────────────────────────────────

    def _run_transfer(self, *, src_ep: str, src_path: str,
                      dst_ep: str, dst_path: str, direction: str,
                      local_path: Path, local_sha256: str,
                      timeout: int,
                      label: Optional[str]) -> dict:
        started = time.perf_counter()

        # 1) Submit.
        sub = self._submit_transfer(
            src_ep=src_ep, src_path=src_path,
            dst_ep=dst_ep, dst_path=dst_path,
            label=label or f"bioinf-agent {direction}",
            timeout=self._SUBMIT_TIMEOUT_S)
        if "error" in sub:
            return sub
        task_id = sub["task_id"]
        command = sub.get("command")

        # 2) Poll to terminal.
        # Sync — block until terminal or watchdog ceiling.
        wait_cap = min(timeout, self._SYNC_WAIT_S_DEFAULT)
        wait_started = time.perf_counter()
        last_status = None
        while True:
            status = self._task_show(task_id, timeout=self._SUBMIT_TIMEOUT_S)
            if "error" in status:
                return {**status, "task_id": task_id}
            last_status = status
            # PERMISSION_DENIED while ACTIVE — task will hang indefinitely
            # (Globus keeps retrying the doomed operation). Classify the
            # event-list immediately and surface the right fix rather
            # than burning the sync-wait cap on a task that can't recover.
            if self._is_permission_denied(status):
                return self._permission_denied_error(task_id, status)
            if status.get("status") in _GLOBUS_TERMINAL_STATUSES:
                break
            if status.get("status") == "INACTIVE":
                # Don't spin on a task that's stuck waiting for activation —
                # surface with the nice_status (Creds Expired, etc.)
                return broke("transfer.globus_task_inactive",
                    error=
                        f"Globus task {task_id} is INACTIVE: "
                        f"{status.get('nice_status') or 'unknown reason'}. "
                        f"This usually means a credential or activation "
                        f"expired. Run `globus session update` (or "
                        f"`globus endpoint activate <UUID>`) and retry.",
                    provider=self.name,
                    task_id= task_id,
                    hint=    "globus credential / activation expired",
                )
            if (time.perf_counter() - wait_started) > wait_cap:
                return broke("transfer.globus_sync_wait_exceeded",
                    error=
                        f"Globus task {task_id} did not reach a terminal "
                        f"state within the {wait_cap}s sync-wait cap. The "
                        f"task is still {status.get('status')!r} — WE stopped "
                        f"waiting; Globus did NOT stop transferring. It keeps "
                        f"running server-side, so the bytes may well land. "
                        f"Poll it with globus_task_status(task_id="
                        f"'{task_id}') until SUCCEEDED/FAILED (or "
                        f"`globus task wait {task_id}` by hand). Do NOT assume "
                        f"the transfer failed, and do NOT re-run it blind — a "
                        f"second transfer of the same bytes races the first.",
                    provider=self.name,
                    task_id= task_id,
                    hint=    f"still running server-side — poll "
                             f"globus_task_status(task_id='{task_id}')",
                )
            time.sleep(self._POLL_INTERVAL_S)

        # 3) Inspect the terminal status.
        if last_status.get("status") == "FAILED":
            fe = last_status.get("fatal_error") or {}
            # FAILED with PERMISSION_DENIED → run the same classifier
            # as the ACTIVE-detector to figure out which endpoint
            # refused (local GCP path / remote consent / remote fs).
            if self._is_permission_denied(last_status):
                return self._permission_denied_error(task_id, last_status)
            return broke("transfer.globus_task_failed",
                error=
                    f"Globus task {task_id} FAILED: "
                    f"{fe.get('code', '?')}: "
                    f"{fe.get('description', '(no description)')}",
                provider=self.name,
                task_id= task_id,
                status=  last_status,
            )

        # SUCCEEDED. The Globus task object reports bytes_transferred +
        # in-flight checksum verification — that's our trust anchor here.
        # For downloads, also record the local sha256 of what landed (the
        # agent's content anchor; Globus already verified bytes match end-
        # to-end).
        out_sha = ""
        if direction == "download":
            try:
                from agent.skills.transfer import _compute_local_sha256
                out_sha = _compute_local_sha256(local_path)
            except (OSError, FileNotFoundError) as e:
                return broke("transfer.globus_local_unreadable",
                    error=
                        f"Globus task {task_id} SUCCEEDED but local "
                        f"file is unreadable for sha256: {e}",
                    provider=self.name,
                    task_id= task_id,
                )

        duration = time.perf_counter() - started
        result: dict = {
            "success":          True,
            "provider":         self.name,
            "command":          command,
            "task_id":          task_id,
            "verified_method":  "globus_end_to_end",
            "direction":        direction,
            "duration_s":       round(duration, 3),
            "transferred_at":   _now_iso(),
            "bytes":            last_status.get("bytes_transferred", 0),
            "files_transferred": last_status.get("files_transferred", 0),
            "globus_status":    "SUCCEEDED",
        }
        if direction == "upload":
            result["local_sha256_pre_transfer"] = local_sha256
        else:
            result["local_sha256"] = out_sha
        return proven("transfer.globus_transferred", **result)

    def _submit_transfer(self, *, src_ep: str, src_path: str,
                         dst_ep: str, dst_path: str, label: str,
                         timeout: int) -> dict:
        """Run `globus transfer ... --format json`, parse task_id."""
        import json as _json
        argv = [
            "globus", "transfer",
            f"{src_ep}:{src_path}",
            f"{dst_ep}:{dst_path}",
            "--label", label,
            "--encrypt-data",
            "--format", "json",
        ]
        try:
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout)
        except FileNotFoundError:
            return broke("transfer.globus_submit_cli_missing",
                error=
                    "globus CLI not on PATH. Install it (`pipx install "
                    "globus-cli`) and authenticate (`globus login`), "
                    "then retry.",
                provider=self.name,
                hint=    "globus CLI not installed",
            )
        except subprocess.TimeoutExpired as e:
            return broke("transfer.globus_submit_timeout", error=
                f"`globus transfer` submission timed out after "
                f"{e.timeout}s",
                provider=self.name)

        if res.returncode != 0:
            return broke("transfer.globus_submit_failed",
                error=
                    f"`globus transfer` exited {res.returncode}: "
                    f"{(res.stderr or '').strip()[:500]}",
                provider=self.name,
                hint=    self._submit_hint(res),
            )

        try:
            payload = _json.loads(res.stdout or "{}")
        except _json.JSONDecodeError as e:
            return broke("transfer.globus_submit_bad_json",
                error=
                    f"`globus transfer` returned 0 but stdout was not "
                    f"valid JSON: {e}. raw: "
                    f"{(res.stdout or '').strip()[:200]!r}",
                provider=self.name,
            )
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return broke("transfer.globus_submit_no_task_id",
                error=
                    "`globus transfer` JSON had no task_id. raw: "
                    f"{(res.stdout or '').strip()[:200]!r}",
                provider=self.name,
            )
        return {"task_id": task_id, "submit_payload": payload,
                "command": _display_cmd(argv)}

    def _task_show(self, task_id: str, *, timeout: int) -> dict:
        """Run `globus task show <id> --format json`, return the
        decoded task object. {error: ...} on shell or parse failure."""
        import json as _json
        argv = ["globus", "task", "show", task_id, "--format", "json"]
        try:
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout)
        except FileNotFoundError:
            return broke("transfer.globus_show_cli_missing", error=
                "globus CLI not on PATH (mid-task — was it uninstalled?)",
                provider=self.name)
        except subprocess.TimeoutExpired as e:
            return broke("transfer.globus_show_timeout", error=
                f"`globus task show {task_id}` timed out after {e.timeout}s",
                provider=self.name)
        if res.returncode != 0:
            return broke("transfer.globus_show_failed",
                error=
                    f"`globus task show {task_id}` exited {res.returncode}: "
                    f"{(res.stderr or '').strip()[:500]}",
                provider=self.name,
            )
        try:
            return _json.loads(res.stdout or "{}")
        except _json.JSONDecodeError as e:
            return broke("transfer.globus_show_bad_json",
                error=
                    f"`globus task show {task_id}` returned 0 but stdout "
                    f"was not valid JSON: {e}. raw: "
                    f"{(res.stdout or '').strip()[:200]!r}",
                provider=self.name,
            )

    @staticmethod
    def _is_permission_denied(task: dict) -> bool:
        """Does this task object carry a PERMISSION_DENIED signal? Globus
        surfaces it in nice_status while the task is ACTIVE (the most
        common case — task hangs forever), and again in fatal_error.code
        if it later goes FAILED. Either is enough to short-circuit."""
        nice = (task.get("nice_status") or "").upper()
        if _PERMISSION_DENIED_TOKEN in nice:
            return True
        fe = task.get("fatal_error") or {}
        code = (fe.get("code") or "").upper()
        return _PERMISSION_DENIED_TOKEN in code

    def _fetch_task_events(self, task_id: str, *, timeout: int) -> dict:
        """`globus task event-list <id> --format json` → decoded payload.
        Returns {'events': [{...}, ...]} on success or {'error': '...'}
        on failure. Used by the PERMISSION_DENIED classifier to figure
        out WHICH endpoint reported the error."""
        import json as _json
        argv = ["globus", "task", "event-list", task_id, "--format", "json"]
        try:
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout)
        except FileNotFoundError:
            return broke("transfer.globus_events_cli_missing", error=
                "globus CLI not on PATH (mid-classify — was it uninstalled?)")
        except subprocess.TimeoutExpired as e:
            return broke("transfer.globus_events_timeout", error=
                f"`globus task event-list {task_id}` timed out after "
                f"{e.timeout}s")
        if res.returncode != 0:
            return broke("transfer.globus_events_failed", error=
                f"`globus task event-list {task_id}` exited "
                f"{res.returncode}: {(res.stderr or '').strip()[:300]}")
        try:
            payload = _json.loads(res.stdout or "{}")
        except _json.JSONDecodeError as e:
            return broke("transfer.globus_events_bad_json", error=
                f"event-list stdout not valid JSON: {e}. raw: "
                f"{(res.stdout or '').strip()[:200]!r}")
        return {"events": payload.get("DATA") or []}

    def _classify_permission_denied(self, task_id: str,
                                    *, timeout: int = 30) -> dict:
        """Pull the task's event list and classify the root cause of the
        PERMISSION_DENIED into one of four buckets. The event details
        carry an `error.endpoint` field (display name + UUID) and a
        `context[].path` — together they tell us WHICH endpoint refused
        WHICH path, which is what determines the actionable hint.

        Returns a dict with `classification`, `endpoint`, `endpoint_id`,
        `endpoint_is_local`, `path`, `body`, `operation`. If event-list
        is unavailable for any reason, classification is `'unknown'`
        and the caller surfaces a generic-but-honest hint."""
        import json as _json
        evs = self._fetch_task_events(task_id, timeout=timeout)
        if "error" in evs:
            return {"classification": "unknown",
                    "fetch_error": evs["error"]}
        events = evs["events"]
        # Walk the events newest-first looking for the first error event
        # we can decode. Globus may emit multiple identical events (one
        # per retry) — first decodable one is enough to classify.
        for ev in events:
            if not ev.get("is_error"):
                continue
            details_raw = ev.get("details") or ""
            if not details_raw:
                continue
            try:
                details = _json.loads(details_raw)
            except _json.JSONDecodeError:
                continue
            err = details.get("error") or {}
            ctx_list = details.get("context") or []
            endpoint_str = err.get("endpoint") or ""
            body = err.get("body") or ""
            path = ""
            operation = ""
            if ctx_list:
                first = ctx_list[0]
                path = first.get("path") or ""
                operation = first.get("operation") or ""
            # Pull the UUID out of the "Display Name (uuid)" wrapper.
            m = _UUID_RE.search(endpoint_str)
            endpoint_id = m.group(0).lower() if m else ""
            is_local = (endpoint_id == self._local_ep.lower())
            is_remote = (endpoint_id == self._remote_ep.lower())
            # Now classify:
            if is_local and _GCP_PATH_BLOCK_BODY_RE.search(body):
                classification = "local_path_not_allowed"
            elif is_local:
                classification = "local_other"
            elif is_remote and "data_access" in body.lower():
                classification = "remote_consent_missing"
            elif is_remote:
                # Generic remote PERMISSION_DENIED — POSIX perms, quota,
                # filesystem ACL. We can't distinguish without a probe;
                # surface the raw body so the user has the real reason.
                classification = "remote_filesystem"
            else:
                classification = "unknown_endpoint"
            return {
                "classification": classification,
                "endpoint":       endpoint_str,
                "endpoint_id":    endpoint_id,
                "endpoint_is_local":  is_local,
                "endpoint_is_remote": is_remote,
                "path":           path,
                "body":           body.strip(),
                "operation":      operation,
            }
        return {"classification": "unknown",
                "fetch_error": "no decodable error event in task history"}

    def _permission_denied_error(self, task_id: str, task: dict,
                                 *, timeout: int = 30) -> dict:
        """Build the structured error + actionable hint after detecting
        PERMISSION_DENIED on a task. Fetches event-list, classifies the
        root cause, and routes the hint accordingly. Three real fix-it
        paths surface different actions:

          local_path_not_allowed — GCP refuses to scan a local path
            outside its Accessible Folders. Fix is GUI-side (GCP
            Preferences → Access tab) or move the file under $HOME.
          remote_consent_missing — the destination GCS endpoint needs
            data_access consent. Fix is `globus login --gcs <UUID>`.
            We don't claim to know the GCS UUID — point at the
            discovery command.
          remote_filesystem — POSIX perms / quota / ACL on the cluster.
            Fix is cluster-side, not Globus-side.

        The fourth case (unknown / event-list unavailable) returns an
        honest 'we don't know' message that points at globus task
        event-list for manual inspection."""
        cls = self._classify_permission_denied(task_id, timeout=timeout)
        classification = cls.get("classification", "unknown")
        ep_display = cls.get("endpoint") or "(unknown endpoint)"
        path = cls.get("path") or "(unknown path)"
        body = cls.get("body") or ""
        # Shared structured fields.
        common: dict = {
            "provider":             self.name,
            "task_id":              task_id,
            "remote_endpoint":      self._remote_ep,
            "remote_endpoint_name": self._remote_name,
            "globus_status":        task.get("status"),
            "nice_status":          task.get("nice_status"),
            "classification":       classification,
            "denied_endpoint":      ep_display,
            "denied_path":          path,
            "raw_body":             body,
        }
        if classification == "local_path_not_allowed":
            return broke("transfer.globus_denied_local_path", **{
                **common,
                "error": (
                    f"Globus task {task_id} blocked: Globus Connect "
                    f"Personal on your laptop refuses to scan {path!r} "
                    f"({ep_display}) — that path is not in GCP's "
                    f"Accessible Folders list. Fix: open Globus Connect "
                    f"Personal → Preferences → Access tab, add the "
                    f"folder containing {path!r}, then retry. Or stage "
                    f"the file under $HOME (always accessible by "
                    f"default). This is a local GCP config issue, NOT "
                    f"a cluster permissions problem."),
                "hint": "GCP Accessible Folders restriction on local source",
            })
        if classification == "remote_consent_missing":
            return broke("transfer.globus_denied_remote_consent", **{
                **common,
                "error": (
                    f"Globus task {task_id} blocked on remote "
                    f"collection {self._remote_name!r} "
                    f"({self._remote_ep}): {body or 'PERMISSION_DENIED'}. "
                    f"This is GCS v5's data_access consent gate. Run "
                    f"`globus gcs collection show {self._remote_ep}` — "
                    f"globus-cli will print the exact `globus login "
                    f"--gcs <UUID>` command for the GCS that hosts "
                    f"this collection. Run that, then retry."),
                "hint": "missing data_access consent on remote GCS",
            })
        if classification == "remote_filesystem":
            return broke("transfer.globus_denied_remote_fs", **{
                **common,
                "error": (
                    f"Globus task {task_id} blocked by remote-side "
                    f"permissions on {ep_display}: path {path!r} is "
                    f"not writable (or readable, for downloads) by "
                    f"your user. Underlying error: {body!r}. This is a "
                    f"cluster-side permissions issue (POSIX / ACL / "
                    f"quota), not a Globus consent problem. Fix on "
                    f"the cluster, then retry."),
                "hint": "remote filesystem permission denied",
            })
        if classification == "local_other":
            return broke("transfer.globus_denied_local_other", **{
                **common,
                "error": (
                    f"Globus task {task_id} blocked on local endpoint "
                    f"{ep_display}: {body!r}. Path: {path!r}. Inspect "
                    f"GCP's state (running? Accessible Folders set?) "
                    f"and check `globus task event-list {task_id}` for "
                    f"more detail."),
                "hint": "local Globus Connect Personal blocked the operation",
            })
        # 'unknown' or 'unknown_endpoint' — be honest about not knowing
        fetch_err = cls.get("fetch_error", "")
        suffix = (f" (event-list fetch failed: {fetch_err})"
                  if fetch_err else "")
        return broke("transfer.globus_denied_unclassified", **{
            **common,
            "error": (
                f"Globus task {task_id} reports PERMISSION_DENIED but "
                f"the cause could not be auto-classified{suffix}. "
                f"Inspect manually: `globus task event-list {task_id} "
                f"--format json` will show which endpoint refused the "
                f"operation and why."),
            "hint": "PERMISSION_DENIED (unclassified — see event-list)",
        })

    @staticmethod
    def _submit_hint(res: subprocess.CompletedProcess) -> Optional[str]:
        """Translate common globus-cli error patterns to actionable hints."""
        msg = (res.stderr or "") + " " + (res.stdout or "")
        msg_l = msg.lower()
        if "no globus login" in msg_l or "missing tokens" in msg_l or \
           "no valid auth" in msg_l:
            return "run `globus login` to authenticate first"
        if "endpoint not activated" in msg_l or \
           "needs activation" in msg_l:
            return ("an endpoint needs activation — run "
                    "`globus session update` or "
                    "`globus endpoint activate <UUID>`")
        if "consent required" in msg_l:
            return ("missing OAuth consent — run "
                    "`globus session consent <scope>`")
        if "no such file or directory" in msg_l or "not found" in msg_l:
            return ("source path or destination is unreachable — check "
                    "the path is correct + (for local endpoints) inside "
                    "the folder Globus Connect Personal serves")
        return None


# Public — the async-task polling primitive uses this from outside.
def globus_task_status(env: dict, task_id: str, *, timeout: int = 30) -> dict:
    """Look up the current state of a Globus transfer task by ID.

    WHERE THE task_id COMES FROM: the SYNC transfer path. Every successful
    globus upload/download journals its `globus_task_id` into the transfer
    manifest (transfer.py's _journal), so any transfer record is a valid input
    here. This is NOT an async-only tool — async submit was removed in tier 7
    (it never once ran across 92 real transfers); 80 of those 92 records carry
    a live task_id, all minted synchronously.

    THE FAILURE IT ANSWERS: a sync wait is capped at _SYNC_WAIT_S_DEFAULT and
    `timeout` is not on the MCP surface, so a transfer past that cap returns
    `globus_sync_wait_exceeded` while the task KEEPS RUNNING server-side. This
    is the only way to ask "did it finish after all?" — and that cap has bound
    in real usage.

    Returns the decoded `globus task show --format json` fields: `status`,
    `nice_status`, `bytes_transferred`, `files_transferred`, `fatal_error`.

    The outcome tag reflects the TASK's state, not merely the query's:
      SUCCEEDED        → proven  (success=True)
      FAILED           → broke   (success=False) — a task that failed must never
                         read as green; the caller branches on this to decide
                         whether the bytes are actually there
      ACTIVE/INACTIVE  → loop    (success=False) — not done yet, poll again"""
    if env.get("type") != "ssh":
        return refused("transfer.task_status_not_ssh", error=
            "globus_task_status is only for ssh-mode envs; got "
            f"type={env.get('type')!r}")
    g = compute_access.get_globus_endpoints(env)
    if g is None:
        return refused("transfer.task_status_not_globus", error=
            "this env does not declare data_transfer.type=globus; "
            "there's no Globus task on it to poll")
    # Validate the task_id shape BEFORE shelling out — Globus task IDs
    # are UUIDs. Defense against any caller smuggling a metacharacter
    # into the argv.
    import re as _re
    if not _re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            task_id):
        return refused("transfer.task_status_bad_uuid", error=
            f"task_id {task_id!r} is not a canonical UUID — refused "
            f"before any subprocess")
    provider = GlobusProvider(g)
    obj = provider._task_show(task_id, timeout=timeout)
    if "error" in obj:
        return obj
    status = obj.get("status")
    out = {
        "task_id":            task_id,
        "status":             status,
        "nice_status":        obj.get("nice_status"),
        "bytes_transferred":  obj.get("bytes_transferred", 0),
        "files_transferred":  obj.get("files_transferred", 0),
        "files_skipped":      obj.get("files_skipped", 0),
        "fatal_error":        obj.get("fatal_error"),
        "type":               obj.get("type"),
        "captured_at":        _now_iso(),
    }
    # The QUERY succeeding says nothing about the TASK succeeding. Reporting
    # success=True on a FAILED task is a false green in the one place a caller
    # asks "are my bytes there?" — so branch on the observed status.
    if status == "SUCCEEDED":
        return proven("transfer.task_status_ok", success=True, **out)
    if status == "FAILED":
        fe = obj.get("fatal_error") or {}
        return broke("transfer.task_failed", success=False,
                     error=(fe.get("description") or fe.get("code")
                            or "globus reports the task FAILED"), **out)
    # ACTIVE / INACTIVE — still running server-side; recoverable by polling again.
    return loop("transfer.task_still_running", success=False,
                hint=f"task {task_id} is {status}; poll again "
                     f"(or `globus task wait {task_id}` by hand)", **out)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "scp_head_node": ScpHeadNodeProvider,
    "globus":        GlobusProvider,
}


def get_transfer_provider(env: dict) -> Optional[TransferProvider]:
    """Return the configured TransferProvider for an ssh-mode env, or
    None for local-mode envs (those handle byte movement via shutil.copy
    in the primitives themselves, not through a wire-protocol provider).

    Reads `env.data_transfer.type`; defaults to scp_head_node when the
    block is absent (back-compat for envs that haven't been migrated to
    Globus yet). The schema validator has already enforced that
    type ∈ {scp_head_node, globus} and that the globus block is shaped
    correctly when type=globus."""
    if env.get("type") == "local":
        return None
    if env.get("type") != "ssh":
        raise compute_access.ConfigError(
            f"transfer providers are only for ssh / local envs; got "
            f"type={env.get('type')!r}")
    kind = compute_access.get_data_transfer_kind(env)
    cls = _PROVIDERS.get(kind)
    if cls is None:
        # Schema validator should have caught this; defense-in-depth.
        raise compute_access.ConfigError(
            f"unknown data_transfer.type {kind!r}; expected one of "
            f"{sorted(_PROVIDERS)!r}")
    if kind == "globus":
        return cls(compute_access.get_globus_endpoints(env))
    return cls()
