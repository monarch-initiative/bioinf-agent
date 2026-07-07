"""L14 cheat-guards — stage_apptainer_image's refuse-to-emit + branch surface.

stage_apptainer_image picks the right HPC-delivery method from the
EnvCache record's `mode`. These tests pin:
  - missing freeze_request_key → clean error, no ssh
  - env without container_upload_target → fails LOUD (no fallback to
    agent_common_data_target — that zone is for reference data, per
    [[project-container-artifacts-routing]])
  - sif_subpath safety (no `..`, no absolute)
  - delivery = LOCAL build (apptainer-in-docker) + ship the finished .sif;
    NO `apptainer build`/`pull` on the head node
    ([[feedback-no-head-node-image-builds]])
  - skip-if-already-staged via a light read-only `test -f` ssh probe
  - the built .sif lands under container_upload_target (no spillover to
    common_data, per [[project-container-artifacts-routing]])
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agent.skills import local_sif, stage_apptainer
from agent.skills.outcomes import proven


def _install_local_build_mocks(monkeypatch, *, remote_exists=False, capture=None):
    """Stub the LOCAL-build-and-ship surface so the delivery flow is testable
    without real docker/ssh: (1) `local_sif.build_sif_locally` writes a fake .sif
    + returns proven, (2) `transfer.upload` captures the destination, (3)
    subprocess.run answers the `test -f` idempotency probe (EXISTS/MISSING) and
    any `docker image inspect` as present. Asserts NO ssh apptainer build/pull is
    ever constructed (the whole point of the fix)."""
    def fake_build(*, out_sif, tarball="", image_tag="", platform="linux/amd64",
                   builder_image="", timeout=1800):
        Path(out_sif).parent.mkdir(parents=True, exist_ok=True)
        Path(out_sif).write_bytes(b"FAKESIF")
        return proven("local_sif.built", success=True, sif_path=out_sif,
                      size_bytes=7, builder_image="kaczmarj/apptainer:test")
    monkeypatch.setattr(local_sif, "build_sif_locally", fake_build)

    def fake_upload(*, project_name, compute_env_name, local_path,
                    remote_abs_path, access_path=None, timeout=600):
        if capture is not None:
            capture["remote_abs_path"] = remote_abs_path
            capture["local_path"] = local_path
        return {"success": True, "zone": "container_upload",
                "remote_abs_path": remote_abs_path, "manifest": "<test>"}
    monkeypatch.setattr(stage_apptainer.transfer, "upload", fake_upload)

    def fake_run(argv, *a, **kw):
        joined = " ".join(argv) if isinstance(argv, (list, tuple)) else str(argv)
        assert "apptainer build" not in joined and "apptainer pull" not in joined, (
            f"head-node apptainer build/pull must never be constructed: {joined!r}")
        m = MagicMock(); m.returncode = 0; m.stderr = ""
        m.stdout = ("EXISTS\n" if remote_exists else "MISSING\n") \
            if "test -f" in joined else ""
        return m
    monkeypatch.setattr(subprocess, "run", fake_run)


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
# Idempotency: skip when the .sif is already staged (read-only `test -f`)
# ===========================================================================

class TestRemoteSifExists:
    @pytest.mark.integration
    def test_probe_is_read_only_and_parses_exists(self, monkeypatch):
        env = {"type": "ssh", "host": "h", "user": "u"}
        seen = {}
        def fake_run(argv, *a, **kw):
            seen["argv"] = argv
            m = MagicMock(); m.returncode = 0; m.stdout = "EXISTS\n"; m.stderr = ""
            return m
        monkeypatch.setattr(subprocess, "run", fake_run)
        assert stage_apptainer._remote_sif_exists(env, "/work/x.sif") is True
        joined = " ".join(seen["argv"])
        # read-only probe ONLY — never builds/pulls on the head node
        assert "test -f" in joined
        assert "apptainer build" not in joined and "apptainer pull" not in joined


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
        # Happy path: the locally-built .sif is UPLOADED directly under
        # CLAUDE_CONTAINERS (no `apptainer/` subdir prefix — the whole zone
        # IS the container zone).
        access_path = _good_access(tmp_path)
        cache = FakeEnvCache({"samtools|linux/amd64|none": {
            "mode": "adopt",
            "image": "quay.io/biocontainers/samtools@sha256:23cda",
            "image_digest": "sha256:23cda33a3a4212587276",
            "content_digest": "sha256:23cda33a3a4212587276"}})
        captured = {}
        _install_local_build_mocks(monkeypatch, capture=captured)

        r = stage_apptainer.stage_apptainer_image(
            project_name="demo", compute_env_name="fakehpc",
            freeze_request_key="samtools|linux/amd64|none",
            env_cache=cache, access_path=str(access_path))
        assert "error" not in r, r
        # Lands directly under CLAUDE_CONTAINERS (no apptainer/ prefix), and
        # THAT is where the finished .sif was uploaded.
        assert r["sif_path"] == \
            "/work/u/CLAUDE_CONTAINERS/samtools_23cda33a3a42.sif"
        assert captured["remote_abs_path"] == r["sif_path"]

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
# Local-build-and-ship happy path — adopt mode (NO head-node pull)
# ===========================================================================

class TestAdoptHappyPath:
    @pytest.mark.integration
    def test_adopt_builds_locally_and_ships(self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)
        cache = FakeEnvCache({"samtools|linux/amd64|none": {
            "mode": "adopt",
            "image": "quay.io/biocontainers/samtools@sha256:23cda",
            "image_digest": "sha256:23cda33a3a4212587276",
            "content_digest": "sha256:23cda33a3a4212587276"}})
        captured = {}
        _install_local_build_mocks(monkeypatch, remote_exists=False,
                                   capture=captured)

        r = stage_apptainer.stage_apptainer_image(
            project_name="demo", compute_env_name="fakehpc",
            freeze_request_key="samtools|linux/amd64|none",
            env_cache=cache, access_path=str(access_path))
        assert "error" not in r, r
        # delivery mode reflects the local build; the .sif was shipped, not
        # pulled on the head node (the fake_run asserts no apptainer pull).
        assert r["mode"] == "adopt_local_sif"
        assert r["sif_path"] == \
            "/work/u/CLAUDE_CONTAINERS/samtools_23cda33a3a42.sif"
        assert captured["remote_abs_path"] == r["sif_path"]
        assert r["skipped"] is False

    @pytest.mark.integration
    def test_skip_when_already_present(self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)
        cache = FakeEnvCache({"samtools|linux/amd64|none": {
            "mode": "adopt",
            "image": "quay.io/biocontainers/samtools@sha256:23cda",
            "image_digest": "sha256:23cda33a3a4212587276",
            "content_digest": "sha256:23cda33a3a4212587276"}})
        # A light read-only `test -f` says the .sif is already staged → the
        # whole local build + upload is skipped (no docker, no ship).
        _install_local_build_mocks(monkeypatch, remote_exists=True)

        r = stage_apptainer.stage_apptainer_image(
            project_name="demo", compute_env_name="fakehpc",
            freeze_request_key="samtools|linux/amd64|none",
            env_cache=cache, access_path=str(access_path))
        assert "error" not in r, r
        assert r["skipped"] is True


# ===========================================================================
# BUILD mode — the locally-built .sif ships to container_upload_target,
# NOT common_data (container-artifacts-routing), and NOTHING builds on the
# head node.
# ===========================================================================

class TestBuildShipsSifToContainerZone:
    """The point of [[project-container-artifacts-routing]]: container
    artifacts live in the container zone exclusively. With local-build-and-ship
    the ONLY artifact that moves is the finished .sif — pin that it lands under
    container_upload_target even when common_data is also declared."""

    @pytest.mark.integration
    def test_built_sif_ships_to_container_zone(self, tmp_path, monkeypatch):
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
        # A freeze docker-save tarball the record points at (build mode).
        fake_tar = tmp_path / "bioinf_samtools.tar"
        fake_tar.write_bytes(b"FAKE TAR BYTES FOR TEST")

        captured = {}
        _install_local_build_mocks(monkeypatch, remote_exists=False,
                                   capture=captured)

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
        # The finished .sif ships to the container zone, NOT common_data.
        assert r["sif_path"] == (
            "/work/u/CLAUDE_CONTAINERS/samtools_23cda33a3a42.sif")
        assert captured["remote_abs_path"] == r["sif_path"]
        assert "COMMON_DATA" not in captured["remote_abs_path"]
        assert r["mode"] == "build_local_sif"


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
