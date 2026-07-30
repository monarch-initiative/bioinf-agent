"""
L14 cheat-guards — cluster_job_status's refuse-to-emit + parsing surface.

cluster_job_status is pure-read: ONE ssh invocation that runs
`bash -lc 'sacct -j <id> -P --noheader -X -o <fields>'`, parses the
pipe-delimited output, returns row-dicts. These tests pin:

  - job_id validation: refuses anything that isn't `\\d{1,12}(_\\d{1,12})?$`
    BEFORE any ssh (so a smuggled `12345; rm -rf /` never reaches the
    cluster)
  - remote-cmd shape: the literal `bash -lc 'sacct -j <id> -P --noheader
    -X -o JobID,State,...'` shape (pinned so a refactor doesn't drift
    the field list out of sync with the parser)
  - output parsing: pipe-delimited splitter; defensive drop of malformed
    rows; empty output → empty jobs list
  - authorization gate: project must have compute_env_access for the
    env (no per-directory permission needed — we don't touch the fs)
  - the primitive only works on ssh envs (refuses local)
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agent.skills import cluster_jobs, compute_access


def _write_access(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


@pytest.fixture
def _ssh_env_with_project_grant(tmp_path):
    """An ssh env with a project that grants `compute_env_access`. No
    real ssh round-trip; tests monkeypatch subprocess.run."""
    access = {
        "compute_envs": [{
            "name": "fakehpc",
            "type": "ssh",
            "host": "fake.example.edu",
            "user": "u",
            "container_upload_target": None,
        }],
        "projects": [{
            "name": "myproj",
            "compute_envs": ["fakehpc"],
            "directories": [],
        }],
    }
    return _write_access(tmp_path, access)


# ===========================================================================
# 1. Job-id validation (pure-string, BEFORE any ssh)
# ===========================================================================

class TestJobIdValidator:
    @pytest.mark.integration
    @pytest.mark.parametrize("good", [
        "1", "12345", "999999999999", "12345_0", "12345_7", "1_1",
    ])
    def test_accepts_good_job_ids(self, good):
        assert cluster_jobs._validate_job_id(good) == good

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "12345; rm -rf /",
        "12345 && cat /etc/shadow",
        "$(id)",
        "12345\nwhoami",
        "12345 67890",            # space (would let `-j 12345 67890` split)
        "12345|sacct",
        "12345`backtick`",
        "abc",                    # non-numeric
        "12345_abc",
        "12345_",                 # trailing underscore (no task)
        "_12345",                 # leading underscore
        "12345_12345_12345",      # extra underscore
        "1" * 13,                 # length cap
    ])
    def test_refuses_metachars_and_malformed(self, bad):
        with pytest.raises(ValueError):
            cluster_jobs._validate_job_id(bad)

    @pytest.mark.integration
    def test_refuses_empty(self):
        with pytest.raises(ValueError) as exc:
            cluster_jobs._validate_job_id("")
        assert "required" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_non_string(self):
        with pytest.raises(ValueError):
            cluster_jobs._validate_job_id(12345)  # type: ignore[arg-type]


# ===========================================================================
# 2. Remote command shape pinning
# ===========================================================================

class TestRemoteCmdShape:
    @pytest.mark.integration
    def test_canonical_shape(self):
        # Pinned: bash -lc 'sacct -j 12345 -P --noheader -X -o <fields>'.
        # `-l` forces a login shell so sacct is on PATH on systems where
        # it's added via /etc/profile.d/slurm.sh.
        # `-P` => pipe-delimited (the parser depends on this).
        # `--noheader` => one row per line, no column-header preamble.
        # `-X` => one row per job/array-task, not per batch/extern step.
        cmd = cluster_jobs._build_sacct_cmd("12345")
        assert cmd == (
            "bash -lc 'sacct -j 12345 -P --noheader -X -o "
            "JobID,State,Elapsed,ExitCode,NodeList,Reason,Start,End'"
        )

    @pytest.mark.integration
    def test_array_task_shape(self):
        cmd = cluster_jobs._build_sacct_cmd("12345_3")
        assert "sacct -j 12345_3 -P --noheader -X -o" in cmd

    @pytest.mark.integration
    def test_field_list_pinned_to_parser(self):
        # The parser depends on the exact column ordering. If anyone
        # reorders `_SACCT_FIELDS`, the test forces them to update both.
        assert cluster_jobs._SACCT_FIELDS == (
            "JobID", "State", "Elapsed", "ExitCode",
            "NodeList", "Reason", "Start", "End")


# ===========================================================================
# 3. Output parsing
# ===========================================================================

class TestOutputParser:
    @pytest.mark.integration
    def test_parses_single_completed_job(self):
        # Realistic sacct -P --noheader -X output: pipe-delimited, no
        # trailing pipe, no header row.
        text = ("12345|COMPLETED|00:05:23|0:0|c-129-3|None|"
                "2026-06-05T20:00:00|2026-06-05T20:05:23\n")
        rows = cluster_jobs._parse_sacct_output(text)
        assert len(rows) == 1
        assert rows[0] == {
            "job_id":    "12345",
            "state":     "COMPLETED",
            "elapsed":   "00:05:23",
            "exit_code": "0:0",
            "nodelist":  "c-129-3",
            "reason":    "None",
            "start":     "2026-06-05T20:00:00",
            "end":       "2026-06-05T20:05:23",
        }

    @pytest.mark.integration
    def test_parses_pending_job_with_unknown_times(self):
        # Pending jobs have Unknown timestamps and (None) NodeList.
        text = ("99999|PENDING|00:00:00|0:0|(None)|Priority|"
                "Unknown|Unknown\n")
        rows = cluster_jobs._parse_sacct_output(text)
        assert len(rows) == 1
        assert rows[0]["state"] == "PENDING"
        assert rows[0]["reason"] == "Priority"
        assert rows[0]["start"] == "Unknown"

    @pytest.mark.integration
    def test_parses_array_tasks(self):
        # -X emits one row per array task.
        text = (
            "12345_0|COMPLETED|00:01:00|0:0|n1|None|t|u\n"
            "12345_1|FAILED|00:00:45|1:0|n2|NonZeroExitCode|t|u\n"
        )
        rows = cluster_jobs._parse_sacct_output(text)
        assert len(rows) == 2
        assert rows[0]["job_id"] == "12345_0"
        assert rows[1]["state"] == "FAILED"
        assert rows[1]["exit_code"] == "1:0"

    @pytest.mark.integration
    def test_empty_output_returns_empty_list(self):
        assert cluster_jobs._parse_sacct_output("") == []
        assert cluster_jobs._parse_sacct_output("   \n\n") == []

    @pytest.mark.integration
    def test_skips_malformed_rows(self):
        # A row with the wrong column count is dropped defensively
        # rather than crashing the whole call.
        text = (
            "12345|COMPLETED|00:00:01|0:0|n1|None|t|u\n"
            "garbage|too|few|columns\n"
            "12346|RUNNING|00:00:30|0:0|n2|None|t|u\n"
        )
        rows = cluster_jobs._parse_sacct_output(text)
        assert len(rows) == 2
        assert [r["job_id"] for r in rows] == ["12345", "12346"]


# ===========================================================================
# 4. Authorization gate — project must have compute_env_access
# ===========================================================================

class TestAuthGate:
    @pytest.mark.integration
    def test_project_without_access_refused_no_ssh(
            self, _ssh_env_with_project_grant, monkeypatch):
        access_path = _ssh_env_with_project_grant
        data = yaml.safe_load(access_path.read_text())
        data["projects"].append({
            "name": "outsider",
            "compute_envs": [],
        })
        access_path.write_text(yaml.safe_dump(data))

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = cluster_jobs.cluster_job_status(
            project_name="outsider",
            compute_env_name="fakehpc",
            job_id="12345",
            access_path=str(access_path))
        assert "error" in result
        assert "PermissionDenied" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_unknown_project_refused_no_ssh(
            self, _ssh_env_with_project_grant, monkeypatch):
        access_path = _ssh_env_with_project_grant
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = cluster_jobs.cluster_job_status(
            project_name="not_a_project",
            compute_env_name="fakehpc",
            job_id="12345",
            access_path=str(access_path))
        assert "error" in result and "KeyError" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_local_env_refused_no_ssh(self, tmp_path, monkeypatch):
        access = {
            "compute_envs": [{"name": "laptop", "type": "local",
                              "container_upload_target": None}],
            "projects": [{
                "name": "myproj",
                "compute_envs": ["laptop"],
                "directories": [],
            }],
        }
        access_path = _write_access(tmp_path, access)
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = cluster_jobs.cluster_job_status(
            project_name="myproj", compute_env_name="laptop",
            job_id="12345", access_path=str(access_path))
        assert "error" in result
        assert "only supports ssh" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_bad_job_id_refused_no_ssh(
            self, _ssh_env_with_project_grant, monkeypatch):
        access_path = _ssh_env_with_project_grant
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = cluster_jobs.cluster_job_status(
            project_name="myproj", compute_env_name="fakehpc",
            job_id="12345; rm -rf /", access_path=str(access_path))
        assert "error" in result and "ValueError" in result["error"]
        assert called == []


# ===========================================================================
# 5. Smoke test against a mocked ssh response (happy path)
# ===========================================================================

class TestSshHappyPathMock:
    @pytest.mark.integration
    def test_returns_parsed_rows_on_successful_ssh(
            self, _ssh_env_with_project_grant, monkeypatch):
        access_path = _ssh_env_with_project_grant
        fake_output = (
            "12345|COMPLETED|00:05:23|0:0|c-129-3|None|"
            "2026-06-05T20:00:00|2026-06-05T20:05:23\n")

        def fake_run(*args, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = fake_output
            mock.stderr = ""
            return mock

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = cluster_jobs.cluster_job_status(
            project_name="myproj", compute_env_name="fakehpc",
            job_id="12345", access_path=str(access_path))
        assert "error" not in result, result
        assert result["compute_env"] == "fakehpc"
        assert result["job_id"] == "12345"
        assert len(result["jobs"]) == 1
        assert result["jobs"][0]["state"] == "COMPLETED"
        assert result["jobs"][0]["exit_code"] == "0:0"

    @pytest.mark.integration
    def test_ssh_failure_returns_error_with_hint(
            self, _ssh_env_with_project_grant, monkeypatch):
        access_path = _ssh_env_with_project_grant

        def fake_run(*args, **kwargs):
            mock = MagicMock()
            mock.returncode = 255
            mock.stdout = ""
            mock.stderr = ("Permission denied (publickey,password).\n"
                           "ssh_exchange_identification: Connection closed")
            return mock

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = cluster_jobs.cluster_job_status(
            project_name="myproj", compute_env_name="fakehpc",
            job_id="12345", access_path=str(access_path))
        assert "error" in result
        assert "rc=255" in result["error"]

    @pytest.mark.integration
    def test_empty_output_returns_empty_jobs_list(
            self, _ssh_env_with_project_grant, monkeypatch):
        # sacct returning nothing — job not yet in slurmdbd, or never
        # existed. The primitive returns jobs=[] (not an error) so the
        # caller can retry.
        access_path = _ssh_env_with_project_grant

        def fake_run(*args, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            return mock

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = cluster_jobs.cluster_job_status(
            project_name="myproj", compute_env_name="fakehpc",
            job_id="99999", access_path=str(access_path))
        assert "error" not in result
        assert result["jobs"] == []
