"""L14 cheat-guards — stage_apptainer_image's refuse-to-emit + branch surface.

stage_apptainer_image picks the right HPC-delivery method from the
EnvCache record's `mode`. These tests pin:
  - missing freeze_request_key → clean error, no ssh
  - env without container_upload_target → fails LOUD (no fallback to
    agent_common_data_target — that zone is for reference data, per
    [[project-container-artifacts-routing]])
  - sif_subpath safety (no `..`, no absolute)
  - mode=adopt → apptainer pull command shape + .sif lands under
    container_upload_target
  - mode=adopt skip-if-exists branch detection
  - mode=build_archive → .tar lands under
    container_upload_target/apptainer_sources/ (same zone as the .sif,
    no spillover to common_data)
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
    """An access manifest with a properly-declared container_upload_target.
    Use this for tests that need to reach beyond the env-auth gate (sif
    safety, mode dispatch, happy paths)."""
    return _write_access(tmp_path, {
        "compute_envs": [{
            "name": "fakehpc", "type": "ssh",
            "host": "fake.example.edu", "user": "u",
            "container_upload_target": {
                "path": "/work/u/CLAUDE_CONTAINERS",
                "permissions": ["file_name_only", "upload"],
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
# Short-digest helper
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


# ===========================================================================
# apptainer-pull remote command shape
# ===========================================================================

class TestApptainerPullCmd:
    @pytest.mark.integration
    def test_canonical_pull_shape(self):
        cmd = stage_apptainer._build_apptainer_pull_cmd(
            "/work/u/CLAUDE_CONTAINERS/samtools.sif",
            "docker://quay.io/biocontainers/samtools@sha256:abc")
        # Login shell so `module load apptainer` works on Lmod systems.
        assert "bash -lc " in cmd
        # Idempotent: SKIP if file exists.
        assert "SKIP_ALREADY_STAGED" in cmd
        # mkdir -p the parent before pulling.
        assert "mkdir -p" in cmd
        # The pull line itself.
        assert "apptainer pull" in cmd
        assert "/work/u/CLAUDE_CONTAINERS/samtools.sif" in cmd
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
# Env-level auth gate — container_upload_target is REQUIRED, no fallback
# ===========================================================================

class TestAuthGate:
    @pytest.mark.integration
    def test_env_without_container_upload_target_refused(
            self, tmp_path, monkeypatch):
        # No container_upload_target declared at all. Container
        # artifacts (.sif + .tar) have nowhere legal to land; the
        # primitive must refuse — NOT silently fall back to
        # agent_common_data_target. The user defines the rails.
        access_path = _write_access(tmp_path, {
            "compute_envs": [{
                "name": "fakehpc", "type": "ssh",
                "host": "fake.example.edu", "user": "u",
                "container_upload_target": None,
                "agent_common_data_target": {
                    "path": "/work/u/COMMON_DATA",
                    "permissions": ["file_name_only", "upload",
                                     "download", "exec"]},
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
        # And the error EXPLICITLY says common_data is NOT a fallback —
        # we don't want a future contributor to add one quietly.
        assert "do NOT fall back" in r["error"]
        assert called == []

    @pytest.mark.integration
    def test_container_upload_target_missing_upload_perm_refused(
            self, tmp_path, monkeypatch):
        # Declared but no `upload` token — same refusal as missing.
        access_path = _write_access(tmp_path, {
            "compute_envs": [{
                "name": "fakehpc", "type": "ssh",
                "host": "fake.example.edu", "user": "u",
                "container_upload_target": {
                    "path": "/work/u/CLAUDE_CONTAINERS",
                    "permissions": ["file_name_only"],  # no upload!
                },
            }],
            "projects": [{
                "name": "demo",
                "compute_env_access": [{
                    "compute_env": "fakehpc", "directories": []}],
            }],
        })
        # The schema validator should reject this manifest at load time
        # because `container_upload_target` requires `upload` per
        # _validate_compute_env(must_include=["upload"]).
        from agent.skills import compute_access
        with pytest.raises(compute_access.ConfigError,
                           match="container_upload_target.*upload"):
            compute_access.load_access(access_path)

    @pytest.mark.integration
    def test_sif_lands_in_container_upload_target(
            self, tmp_path, monkeypatch):
        # Happy path: container_upload_target is declared, .sif lands
        # there directly (no `apptainer/` subdir prefix — the whole zone
        # IS the container zone).
        access_path = _good_access(tmp_path)
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
        # Lands directly under CLAUDE_CONTAINERS (no apptainer/ prefix).
        assert r["sif_path"] == \
            "/work/u/CLAUDE_CONTAINERS/samtools_23cda33a3a42.sif"

    @pytest.mark.integration
    def test_project_without_env_access_refused(
            self, tmp_path, monkeypatch):
        # Container target is present, but the project has no
        # compute_env_access entry for this env. PermissionDenied —
        # the auth gate fires BEFORE the env-target zone gate.
        access_path = _write_access(tmp_path, {
            "compute_envs": [{
                "name": "fakehpc", "type": "ssh",
                "host": "fake.example.edu", "user": "u",
                "container_upload_target": {
                    "path": "/work/u/CLAUDE_CONTAINERS",
                    "permissions": ["upload"]},
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
        "/abs/path",            # absolute (must be relative under container target)
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
        assert r["sif_path"] == \
            "/work/u/CLAUDE_CONTAINERS/samtools_23cda33a3a42.sif"
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
# BUILD-archive — .tar lands in container_upload_target, NOT common_data
# ===========================================================================

class TestBuildArchiveTarRouting:
    """The point of [[project-container-artifacts-routing]]: container
    artifacts (.tar + .sif) live in the container zone exclusively. Pins
    the .tar landing path so a future regression that quietly re-routes
    to agent_common_data_target gets caught at CI."""

    @pytest.mark.integration
    def test_tar_uploads_to_container_upload_target(
            self, tmp_path, monkeypatch):
        # Build the manifest with BOTH container_upload_target AND
        # agent_common_data_target declared on the env — to prove the
        # .tar goes to container, NOT common_data, when both are present.
        access_path = _write_access(tmp_path, {
            "compute_envs": [{
                "name": "fakehpc", "type": "local",
                "container_upload_target": {
                    "path": str(tmp_path / "CLAUDE_CONTAINERS"),
                    "permissions": ["upload"]},
                "agent_common_data_target": {
                    "path": str(tmp_path / "COMMON_DATA"),
                    "permissions": ["upload", "download", "exec"]},
            }],
            "projects": [{
                "name": "demo",
                "compute_env_access": [{
                    "compute_env": "fakehpc", "directories": []}],
            }],
        })
        # Pre-create the zone roots so transfer.upload's mkdir-parent
        # has a real place to write into.
        (tmp_path / "CLAUDE_CONTAINERS").mkdir()
        (tmp_path / "COMMON_DATA").mkdir()

        # Synthesize the tarball the freeze record claims to point at.
        fake_tar = tmp_path / "bioinf_samtools.tar"
        fake_tar.write_bytes(b"FAKE TAR BYTES FOR TEST")

        # type=local skips the apptainer build ssh hop (env_type check
        # at the top of stage_apptainer_image refuses non-ssh envs). So
        # we can't test the FULL build path through a local env. Drive
        # this via monkeypatching: track what transfer.upload was called
        # with, then short-circuit.
        captured = {}
        def fake_upload(*, project_name, compute_env_name, local_path,
                         remote_abs_path, access_path=None, timeout=600):
            captured["remote_abs_path"] = remote_abs_path
            captured["local_path"] = local_path
            return {"success": True, "zone": "container_upload",
                    "remote_abs_path": remote_abs_path,
                    "manifest": "<test>"}
        # The build path needs an ssh env to run the apptainer-build
        # remote command. Switch the env type to ssh + intercept the
        # build subprocess.
        access_path = _write_access(tmp_path, {
            "compute_envs": [{
                "name": "fakehpc", "type": "ssh",
                "host": "fake.example.edu", "user": "u",
                "container_upload_target": {
                    "path": "/work/u/CLAUDE_CONTAINERS",
                    "permissions": ["upload"]},
                "agent_common_data_target": {
                    "path": "/work/u/COMMON_DATA",
                    "permissions": ["upload", "download", "exec"]},
            }],
            "projects": [{
                "name": "demo",
                "compute_env_access": [{
                    "compute_env": "fakehpc", "directories": []}],
            }],
        })

        monkeypatch.setattr(stage_apptainer.transfer, "upload", fake_upload)
        def fake_run(*a, **kw):
            m = MagicMock(); m.returncode = 0
            m.stdout = ""; m.stderr = ""
            return m
        monkeypatch.setattr(subprocess, "run", fake_run)

        cache = FakeEnvCache({"samtools|linux/amd64|none": {
            "mode": "build",
            "tarball": str(fake_tar),
            "image_digest": "sha256:23cda33a3a4212587276",
            "content_digest": "sha256:23cda33a3a4212587276"}})

        r = stage_apptainer.stage_apptainer_image(
            project_name="demo", compute_env_name="fakehpc",
            freeze_request_key="samtools|linux/amd64|none",
            env_cache=cache, access_path=str(access_path))
        assert "error" not in r, r
        # The .tar lands under container_upload_target, in the
        # `apptainer_sources/` subdir — NOT under COMMON_DATA.
        assert captured["remote_abs_path"] == (
            "/work/u/CLAUDE_CONTAINERS/apptainer_sources/bioinf_samtools.tar")
        assert "COMMON_DATA" not in captured["remote_abs_path"]
        # And the .sif itself lands under container_upload_target too.
        assert r["sif_path"].startswith("/work/u/CLAUDE_CONTAINERS/")
        assert "COMMON_DATA" not in r["sif_path"]
        assert r["mode"] == "build_archive"
        assert r["tar_remote_path"] == captured["remote_abs_path"]


# ===========================================================================
# C2 — inspect_staged_sif: fingerprint the .sif that will actually run.
# Parses ONE ssh hop's output: sha256 line, ---INSPECT--- marker, JSON body.
# ===========================================================================

_ENV = {"type": "ssh", "host": "fake.example.edu", "user": "u"}


def _cp(stdout: str, rc: int = 0):
    return subprocess.CompletedProcess([], returncode=rc, stdout=stdout, stderr="")


class TestInspectStagedSif:
    @pytest.mark.integration
    def test_parses_sha_and_inspect_json(self, monkeypatch):
        # sha256sum's real output is `<hash>  <filename>` — the parser must
        # extract just the hash token, not the whole line.
        out = ("a"*64 + "  /work/u/CLAUDE_CONTAINERS/x.sif\n---INSPECT---\n"
               '{"data": {"attributes": {"labels": {"org.opencontainers.image.title": "samtools"}}}}\n')
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _cp(out))
        r = stage_apptainer.inspect_staged_sif(_ENV, "/work/u/CLAUDE_CONTAINERS/x.sif")
        assert "error" not in r
        assert r["sif_sha256"] == "a"*64
        assert r["apptainer_inspect_ok"] is True
        assert isinstance(r["inspect"], dict)

    @pytest.mark.integration
    def test_missing_sif_is_error(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _cp("SIF_MISSING\n", rc=3))
        r = stage_apptainer.inspect_staged_sif(_ENV, "/work/u/CLAUDE_CONTAINERS/absent.sif")
        assert "error" in r and "not found on cluster" in r["error"]

    @pytest.mark.integration
    def test_inspect_failed_still_returns_sha(self, monkeypatch):
        """apptainer inspect can fail (old apptainer, odd .sif) while the file
        is real. We still surface the sha256 — but apptainer_inspect_ok=False,
        so run_step_on_cluster will NOT set cluster_image_verified."""
        out = "b"*64 + "\n---INSPECT---\nINSPECT_FAILED\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _cp(out))
        r = stage_apptainer.inspect_staged_sif(_ENV, "/work/u/CLAUDE_CONTAINERS/x.sif")
        assert r["sif_sha256"] == "b"*64
        assert r["apptainer_inspect_ok"] is False


class TestExtractSourceDigests:
    """C2 strong tie: pull the source image digest out of a biocontainer .sif's
    apptainer-inspect labels. Shape verified against real HPC-cluster output —
    `org.label-schema.usage.singularity.deffile.from` carries `repo@sha256:…`."""

    _REAL_DIGEST = "sha256:" + "23cda33a3a42125872766df9aaf1d2db67cdb8c85314b793465188435af31ba6"[:64]

    @pytest.mark.integration
    def test_extracts_deffile_from_digest(self):
        inspect = {"data": {"attributes": {"labels": {
            "org.label-schema.usage.singularity.deffile.from":
                f"quay.io/biocontainers/samtools@{self._REAL_DIGEST}",
            "org.label-schema.build-arch": "amd64",
        }}}}
        got = stage_apptainer._extract_source_digests(inspect)
        assert self._REAL_DIGEST in got

    @pytest.mark.integration
    def test_no_digest_in_labels_returns_empty(self):
        # build_archive .sif from a local docker-archive — no source digest.
        inspect = {"data": {"attributes": {"labels": {
            "org.label-schema.usage.singularity.deffile.bootstrap": "docker-archive",
            "org.label-schema.build-arch": "amd64",
        }}}}
        assert stage_apptainer._extract_source_digests(inspect) == set()

    @pytest.mark.integration
    def test_none_inspect_is_safe(self):
        assert stage_apptainer._extract_source_digests(None) == set()
