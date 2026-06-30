"""
L14 cheat-guards — the unified transfer surface.

Replaces the old scratch / common_data / project_path command-surface tests.
Pins the public contract of agent/skills/transfer.py:

  - Zone classification routes by where remote_abs_path falls.
  - scratch zone enforces the per-project-isolation prefix.
  - common_data zone allows shared writes (no prefix).
  - project_path zone requires longest-prefix-match in directories[].
  - _ad_hoc project synthesizes scratch + common_data on every env, no
    YAML edit needed; project_path zone is refused for _ad_hoc (empty
    directories[] by design).
  - The manifest is ALWAYS written, success or failure, under
    transfer_history/<project>/<YYYY-MM-DD>/<stamp>_<dir>_<hash>.json
    with a replay_args block.
  - Pure-string validators (path, project name, local file) reject
    every metacharacter / traversal / oversize input BEFORE any I/O.

These are pure tests — no ssh, no real Globus, no real cluster. Local-
mode envs only (compute_env type: local) so we exercise the routing
logic without provider-level mocking.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from agent.skills import transfer, compute_access


# ---------------------------------------------------------------------------
# Helpers: build a minimal YAML on disk
# ---------------------------------------------------------------------------

def _make_access(tmp_path: Path, *,
                 scratch_path: str = "",
                 common_path: str = "",
                 project_dirs: list[dict] | None = None) -> Path:
    """Write a projects_access.yaml stub for tests. The compute env is
    `type: local` so the primitives do shutil.copy + no ssh."""
    scratch_path = scratch_path or str(tmp_path / "fakescratch")
    common_path = common_path or str(tmp_path / "fakecommon")
    Path(scratch_path).mkdir(parents=True, exist_ok=True)
    Path(common_path).mkdir(parents=True, exist_ok=True)

    env_block = {
        "name":        "fakehpc",
        "type":        "local",
        "agent_scratch_target": {
            "path":        scratch_path,
            "permissions": ["file_name_only", "upload", "download", "exec"],
        },
        "agent_common_data_target": {
            "path":        common_path,
            "permissions": ["file_name_only", "upload", "download", "exec"],
        },
    }

    project_block = {
        "name":         "demo_project",
        "compute_env_access": [{
            "compute_env": "fakehpc",
            "directories": project_dirs or [],
        }],
    }
    access = {"compute_envs": [env_block], "projects": [project_block]}
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(access, sort_keys=False))
    return p


def _src_file(tmp_path: Path, name: str = "src.txt",
              content: bytes = b"payload") -> Path:
    p = tmp_path / name
    p.write_bytes(content)
    return p


# ===========================================================================
# Validators — pure-string, no I/O
# ===========================================================================

class TestPathValidator:
    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "relative/path.txt",            # not absolute
        "/has/../traversal",            # .. component
        "/has spaces.txt",              # forbidden whitespace
        "/has;semicolon",               # shell metachar
        "/has\nnewline",                # newline injection
        "/has$dollar",                  # shell metachar
        "",                             # empty
    ])
    def test_rejects_unsafe(self, bad):
        with pytest.raises(transfer.TransferError):
            transfer._validate_remote_abs_path(bad)

    @pytest.mark.integration
    def test_accepts_clean_absolute(self):
        out = transfer._validate_remote_abs_path("/work/u/file.txt")
        assert out == "/work/u/file.txt"

    @pytest.mark.integration
    def test_normalizes_double_slashes(self):
        out = transfer._validate_remote_abs_path("/work/u//file.txt")
        assert out == "/work/u/file.txt"


class TestProjectNameValidator:
    @pytest.mark.integration
    def test_ad_hoc_allowed(self):
        # _ad_hoc is the one name with a literal underscore prefix; the
        # validator MUST allow it (it's the synthesized one-off project).
        assert transfer._validate_project_name_token("_ad_hoc") == "_ad_hoc"

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "has space", "has;meta", "../etc", "x" * 100, ""])
    def test_rejects_unsafe(self, bad):
        with pytest.raises(transfer.TransferError):
            transfer._validate_project_name_token(bad)

    @pytest.mark.integration
    def test_accepts_normal(self):
        for ok in ["demo", "p_1", "my-project"]:
            assert transfer._validate_project_name_token(ok) == ok


# ===========================================================================
# Zone classification + auth
# ===========================================================================

class TestZoneRouting:
    @pytest.mark.integration
    def test_scratch_zone_with_project_prefix(self, tmp_path):
        access_path = _make_access(tmp_path,
                                    scratch_path=str(tmp_path / "scratch"))
        src = _src_file(tmp_path)
        # Path under scratch root + project name prefix → scratch zone.
        remote = str(tmp_path / "scratch" / "demo_project" / "x.txt")
        out = transfer.upload(
            project_name="demo_project",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path=remote,
            access_path=str(access_path))
        assert "error" not in out, out
        assert out["zone"] == "scratch"

    @pytest.mark.integration
    def test_scratch_zone_refuses_wrong_project_prefix(self, tmp_path):
        # Multi-project isolation: a path under scratch root that DOESN'T
        # start with the calling project's name must be refused. This is
        # the core safety property of the scratch zone.
        access_path = _make_access(tmp_path,
                                    scratch_path=str(tmp_path / "scratch"))
        src = _src_file(tmp_path)
        remote = str(tmp_path / "scratch" / "OTHER_PROJECT" / "x.txt")
        out = transfer.upload(
            project_name="demo_project",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path=remote,
            access_path=str(access_path))
        assert "error" in out
        assert "PermissionDenied" in out["error"]
        assert "multi-project isolation" in out["error"]

    @pytest.mark.integration
    def test_common_data_zone_no_prefix_required(self, tmp_path):
        access_path = _make_access(tmp_path,
                                    common_path=str(tmp_path / "common"))
        src = _src_file(tmp_path)
        # No project prefix needed — common_data is shared.
        remote = str(tmp_path / "common" / "reference" / "hg38.fa")
        out = transfer.upload(
            project_name="demo_project",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path=remote,
            access_path=str(access_path))
        assert "error" not in out, out
        assert out["zone"] == "common_data"

    @pytest.mark.integration
    def test_project_path_zone_directories_match(self, tmp_path):
        dirs_root = tmp_path / "project_workspace"
        dirs_root.mkdir(parents=True)
        access_path = _make_access(tmp_path, project_dirs=[{
            "path":        str(dirs_root),
            "permissions": ["file_name_only", "upload", "download", "exec"],
        }])
        src = _src_file(tmp_path)
        remote = str(dirs_root / "subdir" / "out.txt")
        out = transfer.upload(
            project_name="demo_project",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path=remote,
            access_path=str(access_path))
        assert "error" not in out, out
        assert out["zone"] == "project_path"

    @pytest.mark.integration
    def test_no_zone_match_refuses(self, tmp_path):
        # Path outside scratch, common_data, AND directories[] → no zone
        # matches → PermissionDenied. The unified primitive must NOT
        # silently fall through to a zone-less write.
        access_path = _make_access(tmp_path, project_dirs=[])
        src = _src_file(tmp_path)
        remote = "/somewhere/not/declared.txt"
        out = transfer.upload(
            project_name="demo_project",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path=remote,
            access_path=str(access_path))
        assert "error" in out
        assert "PermissionDenied" in out["error"]


# ===========================================================================
# _ad_hoc project — scratch + common_data on every env, NO directories[]
# ===========================================================================

class TestAdHocProject:
    @pytest.mark.integration
    def test_ad_hoc_can_write_scratch_without_yaml_edit(self, tmp_path):
        access_path = _make_access(tmp_path,
                                    scratch_path=str(tmp_path / "scratch"))
        src = _src_file(tmp_path)
        # _ad_hoc lands under <scratch>/_ad_hoc/...
        remote = str(tmp_path / "scratch" / "_ad_hoc" / "ad_hoc_drop.txt")
        out = transfer.upload(
            project_name="_ad_hoc",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path=remote,
            access_path=str(access_path))
        assert "error" not in out, out
        assert out["zone"] == "scratch"
        assert out["project"] == "_ad_hoc"
        assert Path(remote).read_bytes() == src.read_bytes()

    @pytest.mark.integration
    def test_ad_hoc_can_write_common_data(self, tmp_path):
        access_path = _make_access(tmp_path,
                                    common_path=str(tmp_path / "common"))
        src = _src_file(tmp_path)
        remote = str(tmp_path / "common" / "ad_hoc_ref" / "x.bed")
        out = transfer.upload(
            project_name="_ad_hoc",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path=remote,
            access_path=str(access_path))
        assert "error" not in out, out
        assert out["zone"] == "common_data"

    @pytest.mark.integration
    def test_ad_hoc_refused_for_project_path_zone(self, tmp_path):
        # _ad_hoc has NO directories[] by design — a path outside
        # scratch/common_data must be refused even though _ad_hoc has
        # access to every env. The synthesized project is not a back
        # door into the user's real workspaces.
        access_path = _make_access(tmp_path, project_dirs=[])
        src = _src_file(tmp_path)
        remote = "/work/u/real_project/sneaky.txt"
        out = transfer.upload(
            project_name="_ad_hoc",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path=remote,
            access_path=str(access_path))
        assert "error" in out
        assert "PermissionDenied" in out["error"]


# ===========================================================================
# Manifest — every transfer (success OR failure) leaves a record
# ===========================================================================

class TestManifest:
    @pytest.mark.integration
    def test_manifest_written_on_success(self, tmp_path):
        access_path = _make_access(tmp_path,
                                    scratch_path=str(tmp_path / "scratch"))
        src = _src_file(tmp_path)
        remote = str(tmp_path / "scratch" / "demo_project" / "x.txt")
        out = transfer.upload(
            project_name="demo_project",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path=remote,
            access_path=str(access_path))
        assert "manifest" in out
        mpath = Path(out["manifest"])
        assert mpath.exists()
        record = json.loads(mpath.read_text())
        assert record["result"] == "success"
        assert record["direction"] == "upload"
        assert record["zone"] == "scratch"
        assert record["project"] == "demo_project"
        assert record["replay_args"]["project_name"] == "demo_project"
        assert record["replay_args"]["remote_abs_path"] == remote
        assert record["replay_args"]["direction"] == "upload"
        assert record["local_sha256"]
        assert record["bytes"] > 0

    @pytest.mark.integration
    def test_manifest_written_on_failure(self, tmp_path):
        # PermissionDenied is the most common failure path; manifest must
        # land regardless. This is the audit trail.
        access_path = _make_access(tmp_path, project_dirs=[])
        src = _src_file(tmp_path)
        out = transfer.upload(
            project_name="demo_project",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path="/unauthorized/path.txt",
            access_path=str(access_path))
        assert "error" in out
        assert "manifest" in out
        mpath = Path(out["manifest"])
        assert mpath.exists()
        record = json.loads(mpath.read_text())
        assert record["result"] == "error"
        assert record["error"]

    @pytest.mark.integration
    def test_manifest_path_uses_date_bucket(self, tmp_path):
        access_path = _make_access(tmp_path,
                                    scratch_path=str(tmp_path / "scratch"))
        src = _src_file(tmp_path)
        remote = str(tmp_path / "scratch" / "demo_project" / "y.txt")
        out = transfer.upload(
            project_name="demo_project",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path=remote,
            access_path=str(access_path))
        mpath = Path(out["manifest"])
        # transfer_history/demo_project/<YYYY-MM-DD>/<stamp>_<dir>_<hash>.json
        parts = mpath.parts
        assert "transfer_history" in parts
        assert "demo_project" in parts
        # Date bucket — 10 chars, format YYYY-MM-DD
        date_part = mpath.parent.name
        assert len(date_part) == 10 and date_part[4] == "-" and date_part[7] == "-"


# ===========================================================================
# Local-mode round-trip — upload then download
# ===========================================================================

class TestRoundTrip:
    @pytest.mark.integration
    def test_upload_then_download_byte_identical(self, tmp_path):
        access_path = _make_access(tmp_path,
                                    scratch_path=str(tmp_path / "scratch"))
        # Use a non-trivial payload so sha256 means something
        payload = b"hello world\n" * 1024
        src = tmp_path / "in.bin"
        src.write_bytes(payload)
        remote = str(tmp_path / "scratch" / "demo_project" / "round.bin")
        out_up = transfer.upload(
            project_name="demo_project",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path=remote,
            access_path=str(access_path))
        assert "error" not in out_up, out_up

        local_dst = tmp_path / "out.bin"
        out_dl = transfer.download(
            project_name="demo_project",
            compute_env_name="fakehpc",
            remote_abs_path=remote,
            local_path=str(local_dst),
            access_path=str(access_path))
        assert "error" not in out_dl, out_dl
        assert local_dst.read_bytes() == payload
        assert out_dl["local_sha256"] == out_up["local_sha256"]


# ===========================================================================
# Overwrite refusal — discrete capability "upload" means write NEW only
# ===========================================================================

class TestOverwriteRefusal:
    @pytest.mark.integration
    def test_upload_refuses_existing_remote(self, tmp_path):
        access_path = _make_access(tmp_path,
                                    scratch_path=str(tmp_path / "scratch"))
        src = _src_file(tmp_path)
        remote = str(tmp_path / "scratch" / "demo_project" / "z.txt")
        # First upload — fine.
        out1 = transfer.upload(
            project_name="demo_project",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path=remote,
            access_path=str(access_path))
        assert "error" not in out1
        # Second upload to the same path — refused.
        out2 = transfer.upload(
            project_name="demo_project",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path=remote,
            access_path=str(access_path))
        assert "error" in out2
        assert "already exists" in out2["error"]

    @pytest.mark.integration
    def test_download_refuses_existing_local(self, tmp_path):
        access_path = _make_access(tmp_path,
                                    scratch_path=str(tmp_path / "scratch"))
        src = _src_file(tmp_path)
        remote = str(tmp_path / "scratch" / "demo_project" / "src.txt")
        transfer.upload(
            project_name="demo_project",
            compute_env_name="fakehpc",
            local_path=str(src),
            remote_abs_path=remote,
            access_path=str(access_path))
        local_dst = tmp_path / "existing.txt"
        local_dst.write_text("already here")
        out = transfer.download(
            project_name="demo_project",
            compute_env_name="fakehpc",
            remote_abs_path=remote,
            local_path=str(local_dst),
            access_path=str(access_path))
        assert "error" in out
        assert "already exists" in out["error"]
