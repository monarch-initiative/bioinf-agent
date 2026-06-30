"""
transfer_providers — the wire-protocol layer for ssh-mode compute envs.

Every upload_to_X / download_from_X primitive (scratch, common_data,
project_path) goes through a TransferProvider for the actual byte
movement. The primitive owns: path validation, no-overwrite check on
the remote, mkdir of parent dirs, local sha256 anchoring. The provider
owns: the transfer itself + verification that the bytes arrived
unchanged.

Two providers live here today:

  ScpHeadNodeProvider — the legacy default. scp over ssh + sha256sum
    round-trip on the remote side; we compare hashes ourselves. Fine
    for small files; pisses off head-node policy when used for the
    GB-scale .sif images that stage_apptainer_image ships.

  GlobusProvider — STUB. Phase 1 only registers it so the factory
    can dispatch to it; Phase 2 builds out the actual `globus
    transfer` shell-out, the activation precheck, the task poll, and
    the async-task primitive. Calls into the stub raise
    NotImplementedError with a clear "Phase 2 not implemented yet"
    message so misconfigured envs surface immediately rather than
    silently falling back.

LOCAL-mode envs do NOT go through a provider. They use direct
shutil.copy in the primitives; the provider abstraction is explicitly
for ssh-mode wire-protocol selection.

The interface is two methods:

  upload_one(env, local_path, abs_remote_path, local_sha256, timeout)
  download_one(env, abs_remote_path, local_path, timeout)

Both return a dict-in-dict-out shape: success path has
{success: True, bytes, duration_s, transferred_at, provider,
 remote_sha256?, task_id?, verified_method}; failure surfaces as
{error: "...", hint?, ...}.

`verified_method` is the provider's testimony for how it knows the
bytes arrived intact:
  sha256_round_trip — provider computed both ends + compared (scp)
  globus_end_to_end — Globus's own end-to-end checksum + task SUCCEEDED
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional

from agent.skills import compute_access


class TransferError(Exception):
    """A provider's transfer raised an error (timeout, sha mismatch,
    auth, endpoint deactivated). Caller surfaces as `{error: ...}`."""


class TransferProvider:
    """Base class. Each concrete provider implements upload_one +
    download_one. Providers are stateless; the env dict is passed on
    every call (so a single instance can serve many envs)."""

    name: str = ""

    def upload_one(self, *, env: dict, local_path: Path,
                   abs_remote_path: str, local_sha256: str,
                   timeout: int,
                   async_return: bool = False,
                   label: Optional[str] = None) -> dict:
        """Provider-side upload. `async_return` is meaningful only for
        async-capable providers (Globus): when True, submit-and-return
        a task_id immediately instead of waiting for SUCCEEDED. Non-
        async providers ignore the flag."""
        raise NotImplementedError

    def download_one(self, *, env: dict, abs_remote_path: str,
                     local_path: Path, timeout: int,
                     async_return: bool = False,
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
# live in scratch.py / snapshot.py — we import them here so this is
# strictly a packaging change, not a duplication.
# ---------------------------------------------------------------------------

class ScpHeadNodeProvider(TransferProvider):
    """Legacy default. scp + ssh sha256sum round-trip. Each method does
    the byte movement only; the caller has already handled path safety,
    no-overwrite check, and (for upload) parent-dir mkdir."""

    name = "scp_head_node"

    def upload_one(self, *, env: dict, local_path: Path,
                   abs_remote_path: str, local_sha256: str,
                   timeout: int,
                   async_return: bool = False,
                   label: Optional[str] = None) -> dict:
        # scp is synchronous by nature; async_return + label are ignored.
        # We accept them so callers don't have to special-case the
        # provider when threading the flag through.
        from agent.skills.scratch import (
            _scp_argv, _remote_sha256_cmd, _parse_sha256sum_output,
            ScratchPathError)
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
            return {"error": f"scp timed out after {e.timeout}s",
                    "provider": self.name}
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
            return out

        # 2) Verify via remote sha256sum.
        sha_cmd = _remote_sha256_cmd(abs_remote_path)
        sh_argv = _ssh_argv(env, sha_cmd)
        try:
            sh_res = subprocess.run(sh_argv, capture_output=True, text=True,
                                    timeout=timeout)
        except subprocess.TimeoutExpired as e:
            return {"error":
                f"remote sha256sum timed out after {e.timeout}s",
                "provider": self.name}
        if sh_res.returncode != 0:
            return {"error":
                f"remote sha256sum failed (rc={sh_res.returncode}): "
                f"{(sh_res.stderr or '').strip()[:500]}",
                "provider": self.name}

        remote_sha = _parse_sha256sum_output(sh_res.stdout or "")
        if not remote_sha:
            return {"error":
                f"remote sha256sum stdout unparseable: "
                f"{(sh_res.stdout or '').strip()[:500]!r}",
                "provider": self.name}
        if remote_sha != local_sha256:
            return {"error":
                f"sha256 mismatch on upload: local={local_sha256} "
                f"remote={remote_sha}",
                "provider": self.name}

        duration = time.perf_counter() - started
        return {
            "success":          True,
            "provider":         self.name,
            "bytes":            local_path.stat().st_size,
            "duration_s":       round(duration, 3),
            "transferred_at":   _now_iso(),
            "remote_sha256":    remote_sha,
            "verified_method":  "sha256_round_trip",
        }

    def download_one(self, *, env: dict, abs_remote_path: str,
                     local_path: Path, timeout: int,
                     async_return: bool = False,
                     label: Optional[str] = None) -> dict:
        # async_return + label ignored — scp is sync-only.
        from agent.skills.scratch import (
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
            return {"error":
                f"remote sha256sum timed out after {e.timeout}s",
                "provider": self.name}
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
            return out
        remote_sha = _parse_sha256sum_output(sh_res.stdout or "")
        if not remote_sha:
            return {"error":
                f"remote sha256sum stdout unparseable: "
                f"{(sh_res.stdout or '').strip()[:500]!r}",
                "provider": self.name}

        # 2) scp the file back.
        argv = _scp_argv(env, scp_src, str(local_path))
        try:
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout)
        except subprocess.TimeoutExpired as e:
            return {"error": f"scp timed out after {e.timeout}s",
                    "provider": self.name}
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
            return out

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
            return {"error":
                f"sha256 mismatch on download: remote={remote_sha} "
                f"local={local_sha}",
                "provider": self.name}

        duration = time.perf_counter() - started
        return {
            "success":          True,
            "provider":         self.name,
            "bytes":            local_path.stat().st_size,
            "duration_s":       round(duration, 3),
            "transferred_at":   _now_iso(),
            "remote_sha256":    remote_sha,
            "local_sha256":     local_sha,
            "verified_method":  "sha256_round_trip",
        }


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
# With async_return=True we return immediately after submit with the
# task_id; the caller polls later via the globus_task_status MCP tool.
# This is the escape hatch for huge transfers that exceed the agent's
# stream-watchdog (~10 min silent kill).
# ---------------------------------------------------------------------------

_GLOBUS_TERMINAL_STATUSES = {"SUCCEEDED", "FAILED"}


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
    # Hard ceiling on the sync wait. Anything longer → caller should
    # set async_return=True and poll via globus_task_status themselves.
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
                   timeout: int, async_return: bool = False,
                   label: Optional[str] = None) -> dict:
        return self._run_transfer(
            src_ep=self._local_ep, src_path=str(local_path),
            dst_ep=self._remote_ep, dst_path=abs_remote_path,
            direction="upload",
            local_path=local_path, local_sha256=local_sha256,
            timeout=timeout, async_return=async_return, label=label)

    def download_one(self, *, env: dict, abs_remote_path: str,
                     local_path: Path, timeout: int,
                     async_return: bool = False,
                     label: Optional[str] = None) -> dict:
        return self._run_transfer(
            src_ep=self._remote_ep, src_path=abs_remote_path,
            dst_ep=self._local_ep, dst_path=str(local_path),
            direction="download",
            local_path=local_path, local_sha256="",
            timeout=timeout, async_return=async_return, label=label)

    # ─── internals ─────────────────────────────────────────────────────

    def _run_transfer(self, *, src_ep: str, src_path: str,
                      dst_ep: str, dst_path: str, direction: str,
                      local_path: Path, local_sha256: str,
                      timeout: int, async_return: bool,
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

        # 2) Either return immediately (async) or poll to terminal (sync).
        if async_return:
            return {
                "success":          True,
                "provider":         self.name,
                "task_id":          task_id,
                "verified_method":  "globus_pending",
                "direction":        direction,
                "src":              f"{src_ep}:{src_path}",
                "dst":              f"{dst_ep}:{dst_path}",
                "submitted_at":     _now_iso(),
                "note": (
                    "Async submission — poll completion via "
                    f"globus_task_status(task_id='{task_id}') and "
                    "verify SUCCEEDED before relying on the destination."),
            }

        # Sync — block until terminal or watchdog ceiling.
        wait_cap = min(timeout, self._SYNC_WAIT_S_DEFAULT)
        wait_started = time.perf_counter()
        last_status = None
        while True:
            status = self._task_show(task_id, timeout=self._SUBMIT_TIMEOUT_S)
            if "error" in status:
                return {**status, "task_id": task_id}
            last_status = status
            if status.get("status") in _GLOBUS_TERMINAL_STATUSES:
                break
            if status.get("status") == "INACTIVE":
                # Don't spin on a task that's stuck waiting for activation —
                # surface with the nice_status (Creds Expired, etc.)
                return {
                    "error":
                        f"Globus task {task_id} is INACTIVE: "
                        f"{status.get('nice_status') or 'unknown reason'}. "
                        f"This usually means a credential or activation "
                        f"expired. Run `globus session update` (or "
                        f"`globus endpoint activate <UUID>`) and retry.",
                    "provider": self.name,
                    "task_id":  task_id,
                    "hint":     "globus credential / activation expired",
                }
            if (time.perf_counter() - wait_started) > wait_cap:
                return {
                    "error":
                        f"Globus task {task_id} did not reach a terminal "
                        f"state within {wait_cap}s sync-wait cap. The "
                        f"task is still {status.get('status')!r}; pass "
                        f"async_return=True to submit-and-walk-away on "
                        f"transfers of this size, then poll via "
                        f"globus_task_status.",
                    "provider": self.name,
                    "task_id":  task_id,
                    "hint":     "use async_return=True for big transfers",
                }
            time.sleep(self._POLL_INTERVAL_S)

        # 3) Inspect the terminal status.
        if last_status.get("status") == "FAILED":
            fe = last_status.get("fatal_error") or {}
            return {
                "error":
                    f"Globus task {task_id} FAILED: "
                    f"{fe.get('code', '?')}: "
                    f"{fe.get('description', '(no description)')}",
                "provider": self.name,
                "task_id":  task_id,
                "status":   last_status,
            }

        # SUCCEEDED. The Globus task object reports bytes_transferred +
        # in-flight checksum verification — that's our trust anchor here.
        # For downloads, also record the local sha256 of what landed (the
        # agent's content anchor; Globus already verified bytes match end-
        # to-end).
        out_sha = ""
        if direction == "download":
            try:
                from agent.skills.scratch import _compute_local_sha256
                out_sha = _compute_local_sha256(local_path)
            except (OSError, FileNotFoundError) as e:
                return {
                    "error":
                        f"Globus task {task_id} SUCCEEDED but local "
                        f"file is unreadable for sha256: {e}",
                    "provider": self.name,
                    "task_id":  task_id,
                }

        duration = time.perf_counter() - started
        result: dict = {
            "success":          True,
            "provider":         self.name,
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
        return result

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
            return {
                "error":
                    "globus CLI not on PATH. Install it (`pipx install "
                    "globus-cli`) and authenticate (`globus login`), "
                    "then retry.",
                "provider": self.name,
                "hint":     "globus CLI not installed",
            }
        except subprocess.TimeoutExpired as e:
            return {"error":
                f"`globus transfer` submission timed out after "
                f"{e.timeout}s",
                "provider": self.name}

        if res.returncode != 0:
            return {
                "error":
                    f"`globus transfer` exited {res.returncode}: "
                    f"{(res.stderr or '').strip()[:500]}",
                "provider": self.name,
                "hint":     self._submit_hint(res),
            }

        try:
            payload = _json.loads(res.stdout or "{}")
        except _json.JSONDecodeError as e:
            return {
                "error":
                    f"`globus transfer` returned 0 but stdout was not "
                    f"valid JSON: {e}. raw: "
                    f"{(res.stdout or '').strip()[:200]!r}",
                "provider": self.name,
            }
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return {
                "error":
                    "`globus transfer` JSON had no task_id. raw: "
                    f"{(res.stdout or '').strip()[:200]!r}",
                "provider": self.name,
            }
        return {"task_id": task_id, "submit_payload": payload}

    def _task_show(self, task_id: str, *, timeout: int) -> dict:
        """Run `globus task show <id> --format json`, return the
        decoded task object. {error: ...} on shell or parse failure."""
        import json as _json
        argv = ["globus", "task", "show", task_id, "--format", "json"]
        try:
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout)
        except FileNotFoundError:
            return {"error":
                "globus CLI not on PATH (mid-task — was it uninstalled?)",
                "provider": self.name}
        except subprocess.TimeoutExpired as e:
            return {"error":
                f"`globus task show {task_id}` timed out after {e.timeout}s",
                "provider": self.name}
        if res.returncode != 0:
            return {
                "error":
                    f"`globus task show {task_id}` exited {res.returncode}: "
                    f"{(res.stderr or '').strip()[:500]}",
                "provider": self.name,
            }
        try:
            return _json.loads(res.stdout or "{}")
        except _json.JSONDecodeError as e:
            return {
                "error":
                    f"`globus task show {task_id}` returned 0 but stdout "
                    f"was not valid JSON: {e}. raw: "
                    f"{(res.stdout or '').strip()[:200]!r}",
                "provider": self.name,
            }

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

    Pairs with the async_return=True flag on upload_to_X /
    download_from_X. The caller submits (returns immediately with a
    task_id) and polls THIS function until status in {SUCCEEDED,
    FAILED}. The poll cadence is the caller's choice; this function
    is a single CLI query, not a loop.

    Returns the decoded `globus task show --format json` object —
    contains `status`, `nice_status`, `bytes_transferred`,
    `files_transferred`, `fatal_error`, etc. {"error": "..."} on
    failure."""
    if env.get("type") != "ssh":
        return {"error":
            "globus_task_status is only for ssh-mode envs; got "
            f"type={env.get('type')!r}"}
    g = compute_access.get_globus_endpoints(env)
    if g is None:
        return {"error":
            "this env does not declare data_transfer.type=globus; "
            "there's no Globus task on it to poll"}
    # Validate the task_id shape BEFORE shelling out — Globus task IDs
    # are UUIDs. Defense against any caller smuggling a metacharacter
    # into the argv.
    import re as _re
    if not _re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            task_id):
        return {"error":
            f"task_id {task_id!r} is not a canonical UUID — refused "
            f"before any subprocess"}
    provider = GlobusProvider(g)
    obj = provider._task_show(task_id, timeout=timeout)
    if "error" in obj:
        return obj
    return {
        "success":            True,
        "task_id":            task_id,
        "status":             obj.get("status"),
        "nice_status":        obj.get("nice_status"),
        "bytes_transferred":  obj.get("bytes_transferred", 0),
        "files_transferred":  obj.get("files_transferred", 0),
        "files_skipped":      obj.get("files_skipped", 0),
        "fatal_error":        obj.get("fatal_error"),
        "type":               obj.get("type"),
        "captured_at":        _now_iso(),
    }


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
