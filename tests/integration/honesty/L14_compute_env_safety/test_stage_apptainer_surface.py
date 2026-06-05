"""
L14 cheat-guards — stage_apptainer_image's refuse-to-emit + branch surface.

stage_apptainer_image picks the right HPC-delivery method from the
EnvCache record's `mode`. These tests pin:
  - missing freeze_request_key → clean error, no ssh
  - env without agent_common_data_target → clean error
  - sif_subpath safety (no `..`, no absolute)
  - mode=adopt → apptainer pull command shape
  - mode=adopt skip-if-exists branch detection
  - mode=build (.tar) + bulk_transfer.type != scp_head_node → declines
    cleanly so a future DataMover/Globus author has a single branch
    to add
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agent.skills import stage_apptainer


def _write_access(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _good_access(tmp_path: Path) -> Path:
    return _write_access(tmp_path, {
        "compute_envs": [{
            "name": "fakehpc", "type": "ssh",
            "host": "fake.example.edu", "user": "u",
            "container_upload_target": None,
            "agent_common_data_target": {
                "path": "/work/u/COMMON_DATA",
                "permissions": ["file_name_only", "upload", "download", "exec"],
            },
        }],
        "projects": [{
            "name": "demo",
            "compute_env_access": [{"compute_env": "fakehpc",
                                     "directories": []}],
        }],
    })


class FakeEnvCache:
    def __init__(self, records: dict):
        self._records = records
    def lookup(self, key):
        return self._records.get(key)


# ===========================================================================
# Short-digest + default-subpath helpers
# ===========================================================================

class TestPathHelpers:
    @pytest.mark.integration
    def test_short_digest_truncates_to_12(self):
        d = stage_apptainer._short_digest(
            "sha256:23cda33a3a42125872766df9aaf1d2db67cdb8c8")
        assert d == "23cda33a3a42"

    @pytest.mark.integration
    def test_short_digest_handles_garbage(self):
        assert stage_apptainer._short_digest("") == "unknown"
        assert stage_apptainer._short_digest("nope") == "unknown"

    @pytest.mark.integration
    def test_default_sif_subpath_shape(self):
        out = stage_apptainer._default_sif_subpath(
            "samtools_view_demo",
            "sha256:23cda33a3a42125872766df9aaf1d2db67")
        assert out == "apptainer/samtools_view_demo_23cda33a3a42.sif"


# ===========================================================================
# apptainer-pull remote command shape
# ===========================================================================

class TestApptainerPullCmd:
    @pytest.mark.integration
    def test_canonical_pull_shape(self):
        cmd = stage_apptainer._build_apptainer_pull_cmd(
            "/work/u/COMMON_DATA/apptainer/samtools.sif",
            "docker://quay.io/biocontainers/samtools@sha256:abc")
        # Login shell so `module load apptainer` works on Lmod systems.
        assert "bash -lc " in cmd
        # Idempotent: SKIP if file exists.
        assert "SKIP_ALREADY_STAGED" in cmd
        # mkdir -p the parent before pulling.
        assert "mkdir -p" in cmd
        # The pull line itself.
        assert "apptainer pull" in cmd
        assert "/work/u/COMMON_DATA/apptainer/samtools.sif" in cmd
        assert "docker://quay.io/biocontainers/samtools@sha256:abc" in cmd


# ===========================================================================
# Freeze-record lookup failures
# ===========================================================================

class TestRecordLookupFailures:
    @pytest.mark.integration
    def test_missing_key_returns_error(self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)
        cache = FakeEnvCache({})
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        r = stage_apptainer.stage_apptainer_image(
            project_name="demo", compute_env_name="fakehpc",
            freeze_request_key="nope|linux/amd64|none",
            env_cache=cache, access_path=str(access_path))
        assert "error" in r and "not in EnvCache" in r["error"]
        assert called == []

    @pytest.mark.integration
    def test_adopt_record_missing_image_field_refused(
            self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)
        cache = FakeEnvCache({"samtools|linux/amd64|none": {
            "mode": "adopt",
            "image_digest": "sha256:abc",
            # missing "image"
        }})
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        r = stage_apptainer.stage_apptainer_image(
            project_name="demo", compute_env_name="fakehpc",
            freeze_request_key="samtools|linux/amd64|none",
            env_cache=cache, access_path=str(access_path))
        assert "error" in r and "missing `image`" in r["error"]
        assert called == []


# ===========================================================================
# Env-level auth gate
# ===========================================================================

class TestAuthGate:
    @pytest.mark.integration
    def test_env_missing_both_targets_refused(
            self, tmp_path, monkeypatch):
        # Neither container_upload_target nor agent_common_data_target
        # declared with `upload` — nothing for the .sif to land on.
        access_path = _write_access(tmp_path, {
            "compute_envs": [{
                "name": "fakehpc", "type": "ssh",
                "host": "fake.example.edu", "user": "u",
                "container_upload_target": None,
                # No agent_common_data_target either.
            }],
            "projects": [{
                "name": "demo",
                "compute_env_access": [{
                    "compute_env": "fakehpc", "directories": []}],
            }],
        })
        cache = FakeEnvCache({"samtools|linux/amd64|none": {
            "mode": "adopt", "image": "quay.io/x@sha256:abc",
            "image_digest": "sha256:abc"}})
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        r = stage_apptainer.stage_apptainer_image(
            project_name="demo", compute_env_name="fakehpc",
            freeze_request_key="samtools|linux/amd64|none",
            env_cache=cache, access_path=str(access_path))
        assert "error" in r
        assert "container_upload_target" in r["error"]
        assert "agent_common_data_target" in r["error"]
        assert called == []

    @pytest.mark.integration
    def test_container_upload_target_preferred_over_common_data(
            self, tmp_path, monkeypatch):
        # When both targets are declared, container_upload_target
        # wins — that's semantically what .sifs are for, and it
        # doesn't compete with reference data for namespace.
        access_path = _write_access(tmp_path, {
            "compute_envs": [{
                "name": "fakehpc", "type": "ssh",
                "host": "fake.example.edu", "user": "u",
                "container_upload_target": {
                    "path": "/work/u/CLAUDE_CONTAINERS",
                    "permissions": ["file_name_only", "upload"],
                },
                "agent_common_data_target": {
                    "path": "/work/u/COMMON_DATA",
                    "permissions": ["file_name_only", "upload",
                                     "download", "exec"],
                },
            }],
            "projects": [{
                "name": "demo",
                "compute_env_access": [{
                    "compute_env": "fakehpc", "directories": []}],
            }],
        })
        cache = FakeEnvCache({"samtools|linux/amd64|none": {
            "mode": "adopt",
            "image": "quay.io/biocontainers/samtools@sha256:23cda",
            "image_digest": "sha256:23cda33a3a4212587276",
            "content_digest": "sha256:23cda33a3a4212587276"}})

        def fake_run(*args, **kwargs):
            mock = MagicMock(); mock.returncode = 0
            mock.stdout = ""; mock.stderr = ""
            return mock
        monkeypatch.setattr(subprocess, "run", fake_run)

        r = stage_apptainer.stage_apptainer_image(
            project_name="demo", compute_env_name="fakehpc",
            freeze_request_key="samtools|linux/amd64|none",
            env_cache=cache, access_path=str(access_path))
        assert "error" not in r, r
        # Lands under CLAUDE_CONTAINERS, NOT under COMMON_DATA/apptainer.
        assert r["sif_path"] == "/work/u/CLAUDE_CONTAINERS/samtools_23cda33a3a42.sif"

    @pytest.mark.integration
    def test_only_common_data_target_uses_apptainer_subdir(
            self, tmp_path, monkeypatch):
        # When container_upload_target isn't declared at all,
        # fall back to agent_common_data_target with the apptainer/
        # subdir prefix.
        access_path = _good_access(tmp_path)  # common_data only
        cache = FakeEnvCache({"samtools|linux/amd64|none": {
            "mode": "adopt", "image": "quay.io/x@sha256:abc",
            "image_digest": "sha256:23cda33a3a4212",
            "content_digest": "sha256:23cda33a3a4212"}})

        def fake_run(*a, **kw):
            m = MagicMock(); m.returncode = 0; m.stdout = ""; m.stderr = ""
            return m
        monkeypatch.setattr(subprocess, "run", fake_run)

        r = stage_apptainer.stage_apptainer_image(
            project_name="demo", compute_env_name="fakehpc",
            freeze_request_key="samtools|linux/amd64|none",
            env_cache=cache, access_path=str(access_path))
        assert "error" not in r, r
        # Falls back to common_data + the apptainer/ subdir.
        assert "/COMMON_DATA/apptainer/samtools_23cda33a3a42.sif" in r["sif_path"]

    @pytest.mark.integration
    def test_env_common_data_missing_upload_perm_refused(
            self, tmp_path, monkeypatch):
        access_path = _write_access(tmp_path, {
            "compute_envs": [{
                "name": "fakehpc", "type": "ssh",
                "host": "fake.example.edu", "user": "u",
                "container_upload_target": None,
                "agent_common_data_target": {
                    "path": "/work/u/COMMON_DATA",
                    "permissions": ["file_name_only"],  # no upload!
                },
            }],
            "projects": [{
                "name": "demo",
                "compute_env_access": [{
                    "compute_env": "fakehpc", "directories": []}],
            }],
        })
        cache = FakeEnvCache({"samtools|linux/amd64|none": {
            "mode": "adopt", "image": "quay.io/x@sha256:abc",
            "image_digest": "sha256:abc"}})
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        r = stage_apptainer.stage_apptainer_image(
            project_name="demo", compute_env_name="fakehpc",
            freeze_request_key="samtools|linux/amd64|none",
            env_cache=cache, access_path=str(access_path))
        assert "error" in r and "upload" in r["error"]
        assert called == []

    @pytest.mark.integration
    def test_project_without_env_access_refused(
            self, tmp_path, monkeypatch):
        access_path = _write_access(tmp_path, {
            "compute_envs": [{
                "name": "fakehpc", "type": "ssh",
                "host": "fake.example.edu", "user": "u",
                "container_upload_target": None,
                "agent_common_data_target": {
                    "path": "/work/u/COMMON_DATA",
                    "permissions": ["upload", "download", "exec"]},
            }],
            "projects": [{
                "name": "outsider",
                "compute_env_access": [],  # no env grant
            }],
        })
        cache = FakeEnvCache({"samtools|linux/amd64|none": {
            "mode": "adopt", "image": "quay.io/x@sha256:abc",
            "image_digest": "sha256:abc"}})
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        r = stage_apptainer.stage_apptainer_image(
            project_name="outsider", compute_env_name="fakehpc",
            freeze_request_key="samtools|linux/amd64|none",
            env_cache=cache, access_path=str(access_path))
        assert "error" in r and "PermissionDenied" in r["error"]
        assert called == []


# ===========================================================================
# sif_subpath validation
# ===========================================================================

class TestSifSubpathSafety:
    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "/abs/path",            # absolute (must be relative under cd_path)
        "../escape",            # `..` traversal
        "x/../../etc/shadow",
    ])
    def test_refuses_bad_subpath(self, bad, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)
        cache = FakeEnvCache({"samtools|linux/amd64|none": {
            "mode": "adopt", "image": "quay.io/x@sha256:abc",
            "image_digest": "sha256:abc"}})
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        r = stage_apptainer.stage_apptainer_image(
            project_name="demo", compute_env_name="fakehpc",
            freeze_request_key="samtools|linux/amd64|none",
            sif_subpath=bad,
            env_cache=cache, access_path=str(access_path))
        assert "error" in r
        assert called == []


# ===========================================================================
# Mocked-ssh happy path — adopt mode
# ===========================================================================

class TestAdoptHappyPath:
    @pytest.mark.integration
    def test_adopt_pull_returns_success(self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)
        cache = FakeEnvCache({"samtools|linux/amd64|none": {
            "mode": "adopt",
            "image": "quay.io/biocontainers/samtools@sha256:23cda",
            "image_digest": "sha256:23cda33a3a4212587276",
            "content_digest": "sha256:23cda33a3a4212587276"}})

        def fake_run(*args, **kwargs):
            mock = MagicMock(); mock.returncode = 0
            mock.stdout = "INFO:    Downloading container ...\n"
            mock.stderr = ""
            return mock
        monkeypatch.setattr(subprocess, "run", fake_run)

        r = stage_apptainer.stage_apptainer_image(
            project_name="demo", compute_env_name="fakehpc",
            freeze_request_key="samtools|linux/amd64|none",
            env_cache=cache, access_path=str(access_path))
        assert "error" not in r, r
        assert r["mode"] == "adopt"
        assert r["sif_path"].endswith(
            "apptainer/samtools_23cda33a3a42.sif")
        assert r["skipped"] is False

    @pytest.mark.integration
    def test_adopt_skip_when_already_present(self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)
        cache = FakeEnvCache({"samtools|linux/amd64|none": {
            "mode": "adopt",
            "image": "quay.io/biocontainers/samtools@sha256:23cda",
            "image_digest": "sha256:23cda33a3a4212587276",
            "content_digest": "sha256:23cda33a3a4212587276"}})

        def fake_run(*args, **kwargs):
            mock = MagicMock(); mock.returncode = 0
            mock.stdout = "SKIP_ALREADY_STAGED\n"
            mock.stderr = ""
            return mock
        monkeypatch.setattr(subprocess, "run", fake_run)

        r = stage_apptainer.stage_apptainer_image(
            project_name="demo", compute_env_name="fakehpc",
            freeze_request_key="samtools|linux/amd64|none",
            env_cache=cache, access_path=str(access_path))
        assert "error" not in r, r
        assert r["skipped"] is True


# ===========================================================================
# Build-archive: bulk-transfer extension hook
# ===========================================================================

class TestBulkTransferExtensionHook:
    @pytest.mark.integration
    def test_unknown_bulk_transfer_type_declines_cleanly(
            self, tmp_path, monkeypatch):
        # When env declares a not-yet-implemented bulk_transfer.type
        # (e.g. datamover, globus), stage_apptainer_image refuses with
        # a clear error pointing at the ONE branch to add. This is the
        # extension hook contract.
        access_path = _write_access(tmp_path, {
            "compute_envs": [{
                "name": "fakehpc", "type": "ssh",
                "host": "fake.example.edu", "user": "u",
                "container_upload_target": None,
                "agent_common_data_target": {
                    "path": "/work/u/COMMON_DATA",
                    "permissions": ["upload", "download", "exec"]},
                "bulk_transfer": {"type": "datamover"},
            }],
            "projects": [{
                "name": "demo",
                "compute_env_access": [{
                    "compute_env": "fakehpc", "directories": []}],
            }],
        })
        # A non-adopt record so we hit the build-archive path.
        cache = FakeEnvCache({"samtools|linux/amd64|none": {
            "mode": "build",
            "tarball": "docker_images/x/x.tar",
            "image_digest": "sha256:abc",
            "content_digest": "sha256:abc"}})
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        r = stage_apptainer.stage_apptainer_image(
            project_name="demo", compute_env_name="fakehpc",
            freeze_request_key="samtools|linux/amd64|none",
            env_cache=cache, access_path=str(access_path))
        assert "error" in r
        assert "datamover" in r["error"]
        assert "scp_head_node" in r["error"]
        assert called == []
