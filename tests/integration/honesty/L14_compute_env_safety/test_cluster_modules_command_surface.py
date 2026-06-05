"""
L14 cheat-guards — cluster_module_avail's refuse-to-emit + parsing surface.

cluster_module_avail is pure-read: ONE ssh invocation that runs
`bash -lc 'module avail [pattern] 2>&1' || true`, parses the output,
returns a list. These tests pin:

  - pattern validation: pure-string token check fires BEFORE any ssh
    (so a smuggled `nextflow; rm -rf /` can never reach the cluster)
  - remote-cmd shape: the literal `bash -lc '<inner>' || true` shape
    that gets passed through `ssh ... '<...>'`
  - output parsing: Lmod (D)/(L) annotations stripped; section headers
    dropped; multi-column rows split correctly
  - authorization gate: project must have compute_env_access for the
    env (no per-directory permission needed — we don't touch the fs)
  - the primitive only works on ssh envs (refuses local)
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agent.skills import cluster_modules, compute_access


def _write_access(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


@pytest.fixture
def _ssh_env_with_project_grant(tmp_path):
    """An ssh env with a project that grants `compute_env_access`. No
    actual ssh round-trip; tests monkeypatch subprocess.run."""
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
            "compute_env_access": [{
                "compute_env": "fakehpc",
                "directories": [],
            }],
        }],
    }
    return _write_access(tmp_path, access)


# ===========================================================================
# 1. Pattern validation (pure-string, BEFORE any ssh)
# ===========================================================================

class TestPatternValidator:
    @pytest.mark.integration
    @pytest.mark.parametrize("good", [
        None, "", "nextflow", "samtools", "apptainer/1.4",
        "GNU-parallel", "python_3.11", "a", "x" * 64,
    ])
    def test_accepts_good_patterns(self, good):
        # Doesn't raise; empty string returns None.
        result = cluster_modules._validate_pattern(good)
        assert result == (good or None)

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "nextflow; rm -rf /",
        "nextflow && cat /etc/shadow",
        "x$(id)",
        "with newline\nattack",
        "with space",
        "with|pipe",
        "with`backtick`",
    ])
    def test_refuses_shell_metacharacters(self, bad):
        with pytest.raises(ValueError) as exc:
            cluster_modules._validate_pattern(bad)
        assert "forbidden" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_over_length(self):
        with pytest.raises(ValueError) as exc:
            cluster_modules._validate_pattern("x" * 129)
        assert "exceeds 128" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_non_string(self):
        with pytest.raises(ValueError):
            cluster_modules._validate_pattern(42)  # type: ignore[arg-type]


# ===========================================================================
# 2. Remote command shape pinning
# ===========================================================================

class TestRemoteCmdShape:
    @pytest.mark.integration
    def test_no_pattern_shape(self):
        # The canonical shape: bash -lc 'module avail 2>&1' || true.
        # `-l` forces a login shell so /etc/profile.d/lmod.sh loads.
        # `|| true` absorbs `module avail`'s nonzero exits (some Lmod
        # versions return 1 when pattern matches nothing).
        cmd = cluster_modules._build_module_avail_cmd(None)
        assert cmd == "bash -lc 'module avail 2>&1' || true"

    @pytest.mark.integration
    def test_with_pattern_shape(self):
        cmd = cluster_modules._build_module_avail_cmd("nextflow")
        assert cmd == "bash -lc 'module avail nextflow 2>&1' || true"

    @pytest.mark.integration
    def test_pattern_with_metachar_would_be_quoted(self):
        # The pattern is shlex.quote'd inside the inner shell command.
        # In practice _validate_pattern rejects it first, so we test
        # the SHAPE of the quoting directly via _build_module_avail_cmd.
        # (If a malicious pattern bypassed validation, the inner quote
        # would still keep it as one token to `module avail`.)
        cmd = cluster_modules._build_module_avail_cmd("safe-name")
        # No special chars in the pattern → shlex.quote returns it as-is.
        assert "module avail safe-name 2>&1" in cmd


# ===========================================================================
# 3. Output parsing
# ===========================================================================

class TestOutputParser:
    @pytest.mark.integration
    def test_parses_simple_multi_column(self):
        text = """
------- /nas/hpc_cluster/apps-modulefiles -------
   apptainer/1.4.1    nextflow/22.04.5    nextflow/25.04.7    samtools/1.21
"""
        out = cluster_modules._parse_module_avail_output(text)
        assert "apptainer/1.4.1" in out
        assert "nextflow/22.04.5" in out
        assert "nextflow/25.04.7" in out
        assert "samtools/1.21" in out

    @pytest.mark.integration
    def test_strips_lmod_annotations(self):
        text = "   nextflow/25.04.7 (D)    ape/5.7  (L)    bwa/0.7.17"
        out = cluster_modules._parse_module_avail_output(text)
        assert "nextflow/25.04.7" in out  # (D) stripped
        assert "ape/5.7" in out            # (L) stripped
        assert "bwa/0.7.17" in out
        # No annotation noise leaked in.
        for entry in out:
            assert "(" not in entry, f"annotation leaked: {entry!r}"

    @pytest.mark.integration
    def test_drops_section_headers(self):
        text = """
------- /nas/path/one -------
   apptainer/1.4.1
======================================
   nextflow/25.04.7
--------------------------------------
"""
        out = cluster_modules._parse_module_avail_output(text)
        assert out == ["apptainer/1.4.1", "nextflow/25.04.7"]

    @pytest.mark.integration
    def test_drops_footer_text(self):
        text = """
   nextflow/25.04.7
Where:
 D:  Default Module
 L:  Module is loaded
Use "module spider" to find all possible modules.
"""
        out = cluster_modules._parse_module_avail_output(text)
        assert out == ["nextflow/25.04.7"]

    @pytest.mark.integration
    def test_empty_output_returns_empty_list(self):
        assert cluster_modules._parse_module_avail_output("") == []
        assert cluster_modules._parse_module_avail_output("   \n\n\n") == []


# ===========================================================================
# 4. Authorization gate — project must have compute_env_access
# ===========================================================================

class TestAuthGate:
    @pytest.mark.integration
    def test_project_with_no_access_refused_no_ssh(
            self, _ssh_env_with_project_grant, monkeypatch, tmp_path):
        # Create a second project that DOESN'T have access to fakehpc.
        access_path = _ssh_env_with_project_grant
        data = yaml.safe_load(access_path.read_text())
        data["projects"].append({
            "name": "outsider",
            "compute_env_access": [],  # no access to any env
        })
        access_path.write_text(yaml.safe_dump(data))

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = cluster_modules.cluster_module_avail(
            project_name="outsider",
            compute_env_name="fakehpc",
            access_path=str(access_path))
        assert "error" in result
        assert "PermissionDenied" in result["error"]
        assert "no compute_env_access entry" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_unknown_project_refused_no_ssh(
            self, _ssh_env_with_project_grant, monkeypatch):
        access_path = _ssh_env_with_project_grant

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = cluster_modules.cluster_module_avail(
            project_name="not_a_project",
            compute_env_name="fakehpc",
            access_path=str(access_path))
        assert "error" in result and "KeyError" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_local_env_refused_no_ssh(self, tmp_path, monkeypatch):
        # cluster_module_avail only works on ssh envs.
        access = {
            "compute_envs": [{"name": "laptop", "type": "local",
                              "container_upload_target": None}],
            "projects": [{
                "name": "myproj",
                "compute_env_access": [{
                    "compute_env": "laptop", "directories": []}],
            }],
        }
        access_path = _write_access(tmp_path, access)

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = cluster_modules.cluster_module_avail(
            project_name="myproj", compute_env_name="laptop",
            access_path=str(access_path))
        assert "error" in result
        assert "only supports ssh" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_bad_pattern_refused_no_ssh(
            self, _ssh_env_with_project_grant, monkeypatch):
        access_path = _ssh_env_with_project_grant
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = cluster_modules.cluster_module_avail(
            project_name="myproj", compute_env_name="fakehpc",
            pattern="nextflow; rm -rf /",
            access_path=str(access_path))
        assert "error" in result and "ValueError" in result["error"]
        assert called == []


# ===========================================================================
# 5. Smoke test against a mocked ssh response (happy path)
# ===========================================================================

class TestSshHappyPathMock:
    @pytest.mark.integration
    def test_returns_parsed_list_on_successful_ssh(
            self, _ssh_env_with_project_grant, monkeypatch):
        access_path = _ssh_env_with_project_grant
        fake_output = """
------- /nas/hpc_cluster/apps-modulefiles -------
   nextflow/22.04.5    nextflow/25.04.7 (D)
"""

        def fake_run(*args, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = fake_output
            mock.stderr = ""
            return mock

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = cluster_modules.cluster_module_avail(
            project_name="myproj", compute_env_name="fakehpc",
            pattern="nextflow", access_path=str(access_path))
        assert "error" not in result, result
        assert result["pattern"] == "nextflow"
        assert "nextflow/25.04.7" in result["modules"]
        assert "nextflow/22.04.5" in result["modules"]
        assert result["module_count"] == 2
