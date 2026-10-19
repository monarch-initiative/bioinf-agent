"""
L14 cheat-guards — a directory moves as one transfer.

`upload` / `download` take a directory as well as a file. Pinned here, on every
wire (local copy, scp, Globus):

  - a directory arrives whole: every file is checked against the source (sha256
    on both ends for scp and local copy; Globus checksums each file itself, and
    must have moved every one), and its checksums are written beside the receipt
    in the form `sha256sum -c` reads;
  - symlinks are never followed (one could reach outside the zone the transfer was
    authorized for): an upload holding one is refused before a byte moves, an scp
    download is refused at its source listing, and a Globus task that left one
    behind is not a whole copy;
  - a directory nothing can be read from is refused, never read as empty;
  - a detached transfer (`background=True`) waits past the cap a tool call
    keeps under the stream watchdog.

The scp tests run the real remote listing command through bash on this machine;
the cluster side is a directory in tmp_path.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agent.skills import transfer, transfer_providers
from agent.skills.backgroundable import DETACHED_ENV
from tests.integration.honesty.L14_compute_env_safety.test_transfer_surface import _make_access

_REAL_RUN = subprocess.run
_TASK_ID = "33333333-3333-3333-3333-333333333333"


def _tree(root: Path) -> Path:
    (root / "sub" / "deeper").mkdir(parents=True)
    (root / "a.txt").write_text("alpha\n")
    (root / "sub" / "b.tsv").write_text("x\ty\n1\t2\n")
    (root / "sub" / "deeper" / "c.bin").write_bytes(bytes(range(256)) * 8)
    return root


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _env_ssh() -> dict:
    return {"name": "hpc", "type": "ssh", "host": "hpc.example.edu", "user": "user1"}


# ===========================================================================
# Through the primitive, on a local-mode env
# ===========================================================================

class TestThroughThePrimitive:
    @pytest.mark.integration
    def test_a_directory_round_trips_with_its_checksums(self, tmp_path):
        access = _make_access(tmp_path, scratch_path=str(tmp_path / "scratch"))
        src = _tree(tmp_path / "results")
        remote = str(tmp_path / "scratch" / "demo_project" / "results")

        up = transfer.upload(project_name="demo_project", compute_env_name="fakehpc",
                             local_path=str(src), remote_abs_path=remote,
                             access_path=str(access))
        assert up["outcome"] == "proven", up
        assert up["files"] == 3 and up["bytes"] == sum(map(len, _files(src).values()))
        assert _files(Path(remote)) == _files(src)

        back = tmp_path / "back"
        down = transfer.download(project_name="demo_project", compute_env_name="fakehpc",
                                 remote_abs_path=remote, local_path=str(back),
                                 access_path=str(access))
        assert down["outcome"] == "proven", down
        assert _files(back) == _files(src)

        # The checksums are the list `sha256sum -c` reads from inside the directory,
        # and the receipt names them.
        sums = Path(down["checksums"]).read_text().splitlines()
        assert [line.split("  ", 1)[1] for line in sums] == ["a.txt", "sub/b.tsv", "sub/deeper/c.bin"]
        assert all(len(line.split("  ", 1)[0]) == 64 for line in sums)
        receipt = json.loads(Path(down["manifest"]).read_text())
        assert receipt["files"] == 3 and receipt["checksums"] == down["checksums"]
        assert "3 files" in receipt["summary"]

    @pytest.mark.integration
    def test_a_directory_holding_a_symlink_is_refused_before_any_byte_moves(self, tmp_path):
        access = _make_access(tmp_path, scratch_path=str(tmp_path / "scratch"))
        src = _tree(tmp_path / "results")
        (src / "sub" / "elsewhere").symlink_to("/etc/hosts")
        remote = tmp_path / "scratch" / "demo_project" / "results"

        out = transfer.upload(project_name="demo_project", compute_env_name="fakehpc",
                              local_path=str(src), remote_abs_path=str(remote),
                              access_path=str(access))
        assert (out["outcome"], out["code"]) == ("refused", "transfer.validation_error"), out
        assert "symlink" in out["error"]
        assert not remote.exists()

    @pytest.mark.integration
    def test_an_empty_directory_moves_as_zero_files(self, tmp_path):
        access = _make_access(tmp_path, scratch_path=str(tmp_path / "scratch"))
        (tmp_path / "empty" / "nested").mkdir(parents=True)
        remote = tmp_path / "scratch" / "demo_project" / "e"
        out = transfer.upload(project_name="demo_project", compute_env_name="fakehpc",
                              local_path=str(tmp_path / "empty"), remote_abs_path=str(remote),
                              access_path=str(access))
        assert out["outcome"] == "proven" and out["files"] == 0, out
        assert (remote / "nested").is_dir()

    @pytest.mark.integration
    def test_a_directory_that_cannot_be_read_is_refused_never_read_as_empty(self, tmp_path):
        access = _make_access(tmp_path, scratch_path=str(tmp_path / "scratch"))
        src = _tree(tmp_path / "results")
        (src / "sub").chmod(0)
        try:
            out = transfer.upload(project_name="demo_project", compute_env_name="fakehpc",
                                  local_path=str(src),
                                  remote_abs_path=str(tmp_path / "scratch" / "demo_project" / "r"),
                                  access_path=str(access))
        finally:
            (src / "sub").chmod(0o755)
        assert (out["outcome"], out["code"]) == ("refused", "transfer.validation_error"), out
        assert "can't be read" in out["error"]


# ===========================================================================
# scp: `scp -r` plus the sha256 of every file on both ends
# ===========================================================================

def _fake_ssh_and_scp(calls: list, *, drop: str = ""):
    """ssh runs its remote command through bash here; scp copies, with `user@host:`
    stripped. `drop` names a relative path scp 'loses' on the way."""
    def fake_run(argv, *a, **kw):
        calls.append(argv)
        if argv[0] == "ssh":
            return _REAL_RUN(["bash", "-c", argv[-1]], capture_output=True, text=True)
        src, dst = (s.split(":", 1)[1] if ":" in s else s for s in argv[-2:])
        if Path(src).is_dir():
            shutil.copytree(src, dst)
            if drop:
                (Path(dst) / drop).unlink()
        else:
            shutil.copy2(src, dst)
        return MagicMock(returncode=0, stdout="", stderr="")
    return fake_run


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs sha256sum, as the cluster has")
class TestScp:
    @pytest.mark.integration
    def test_an_upload_checks_every_file_on_the_far_side(self, tmp_path, monkeypatch):
        calls: list = []
        monkeypatch.setattr(subprocess, "run", _fake_ssh_and_scp(calls))
        src = _tree(tmp_path / "results")
        out = transfer_providers.ScpHeadNodeProvider().upload_one(
            env=_env_ssh(), local_path=src, abs_remote_path=str(tmp_path / "far" / "results"),
            local_sha256="", timeout=60, tree=transfer._scan_tree(src)[0])
        assert out["outcome"] == "proven", out
        scp = next(c for c in calls if c[0] == "scp")
        assert "-r" in scp and scp[-1].endswith("/far/.results.partial"), "bytes go to the partial name"
        assert out["checksums"] == transfer._scan_tree(src)[0]
        assert all(c[-1].startswith("bash -c ") for c in calls[calls.index(scp) + 1:]), \
            "a compound remote command runs in bash, not the login shell"
        assert _files(tmp_path / "far" / "results") == _files(src), "renamed into place once verified"
        assert not (tmp_path / "far" / ".results.partial").exists()

    @pytest.mark.integration
    def test_an_upload_names_the_file_that_did_not_arrive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _fake_ssh_and_scp([], drop="sub/b.tsv"))
        src = _tree(tmp_path / "results")
        out = transfer_providers.ScpHeadNodeProvider().upload_one(
            env=_env_ssh(), local_path=src, abs_remote_path=str(tmp_path / "far" / "results"),
            local_sha256="", timeout=60, tree=transfer._scan_tree(src)[0])
        assert out["code"] == "transfer.scp_upload_tree_mismatch", out
        assert "1 file(s) missing (first: sub/b.tsv)" in out["error"]
        assert not (tmp_path / "far" / "results").exists(), "an unverified copy never takes the final name"
        assert not (tmp_path / "far" / ".results.partial").exists(), "and its partial is cleared"

    @pytest.mark.integration
    def test_a_partial_an_earlier_attempt_left_is_cleared_first(self, tmp_path, monkeypatch):
        stale = tmp_path / "far" / ".results.partial" / "results"
        stale.mkdir(parents=True)
        (stale / "old.txt").write_text("from an interrupted attempt")
        monkeypatch.setattr(subprocess, "run", _fake_ssh_and_scp([]))
        src = _tree(tmp_path / "results")
        out = transfer_providers.ScpHeadNodeProvider().upload_one(
            env=_env_ssh(), local_path=src, abs_remote_path=str(tmp_path / "far" / "results"),
            local_sha256="", timeout=60, tree=transfer._scan_tree(src)[0])
        assert out["outcome"] == "proven", out
        assert _files(tmp_path / "far" / "results") == _files(src)

    @pytest.mark.integration
    def test_a_destination_that_appears_during_the_transfer_is_not_overwritten(self, tmp_path, monkeypatch):
        dest = tmp_path / "far" / "results"
        fake = _fake_ssh_and_scp([])
        def racing(argv, *a, **kw):
            out = fake(argv, *a, **kw)
            if argv[0] == "scp":
                dest.mkdir(parents=True)
                (dest / "theirs.txt").write_text("someone else's")
            return out
        monkeypatch.setattr(subprocess, "run", racing)
        src = _tree(tmp_path / "results")
        out = transfer_providers.ScpHeadNodeProvider().upload_one(
            env=_env_ssh(), local_path=src, abs_remote_path=str(dest),
            local_sha256="", timeout=60, tree=transfer._scan_tree(src)[0])
        assert (out["outcome"], out["code"]) == ("refused", "transfer.remote_exists"), out
        assert [p.name for p in dest.iterdir()] == ["theirs.txt"]
        assert not (tmp_path / "far" / ".results.partial").exists()

    @pytest.mark.integration
    def test_a_download_finds_the_directory_at_the_source(self, tmp_path, monkeypatch):
        calls: list = []
        monkeypatch.setattr(subprocess, "run", _fake_ssh_and_scp(calls))
        far = _tree(tmp_path / "far" / "results")
        out = transfer_providers.ScpHeadNodeProvider().download_one(
            env=_env_ssh(), abs_remote_path=str(far), local_path=tmp_path / "back", timeout=60)
        assert out["outcome"] == "proven", out
        assert calls[0][-1].startswith("bash -c ")
        assert "-r" in calls[1] and _files(tmp_path / "back") == _files(far)
        assert set(out["checksums"]) == {"a.txt", "sub/b.tsv", "sub/deeper/c.bin"}

    @pytest.mark.integration
    def test_a_download_of_a_tree_holding_a_symlink_is_refused_before_scp(self, tmp_path, monkeypatch):
        calls: list = []
        monkeypatch.setattr(subprocess, "run", _fake_ssh_and_scp(calls))
        far = _tree(tmp_path / "far" / "results")
        (far / "outside").symlink_to("/etc/hosts")
        out = transfer_providers.ScpHeadNodeProvider().download_one(
            env=_env_ssh(), abs_remote_path=str(far), local_path=tmp_path / "back", timeout=60)
        assert (out["outcome"], out["code"]) == ("refused", "transfer.scp_download_tree_not_files"), out
        assert "outside" in out["error"]
        assert [c[0] for c in calls] == ["ssh"], "nothing is copied once the listing says no"

    @pytest.mark.integration
    def test_that_refusal_reaches_the_caller_as_a_refusal(self, tmp_path, monkeypatch):
        far = _tree(tmp_path / "scratch" / "demo_project" / "results")
        (far / "outside").symlink_to("/etc/hosts")
        access = tmp_path / "projects_access.yaml"
        access.write_text(yaml.safe_dump({
            "compute_envs": [{"name": "hpc", "type": "ssh", "host": "hpc.example.edu", "user": "u",
                              "agent_scratch_target": {"path": str(tmp_path / "scratch"),
                                                       "permissions": ["file_name_only", "upload", "download", "exec"]}}],
            "projects": [{"name": "demo_project", "compute_envs": ["hpc"], "directories": []}]}))
        monkeypatch.setattr(subprocess, "run", _fake_ssh_and_scp([]))
        out = transfer.download(project_name="demo_project", compute_env_name="hpc",
                                remote_abs_path=str(far), local_path=str(tmp_path / "back"),
                                access_path=str(access))
        assert (out["outcome"], out["code"]) == ("refused", "transfer.provider_refused"), out
        assert json.loads(Path(out["manifest"]).read_text())["outcome"] == "refused"


# ===========================================================================
# Globus: it checksums each file it moves; it must have moved every one
# ===========================================================================

def _fake_globus(task: dict, *, land: tuple[Path, Path] | None = None):
    """`globus transfer` answers with a task id (and, for a download, lands the tree);
    `globus task show` answers SUCCEEDED with the counters given."""
    def fake_run(argv, *a, **kw):
        if argv[:2] == ["globus", "transfer"]:
            if land:
                shutil.copytree(*land)
            return MagicMock(returncode=0, stderr="", stdout=json.dumps({"task_id": _TASK_ID}))
        return MagicMock(returncode=0, stderr="",
                         stdout=json.dumps({"status": "SUCCEEDED", "nice_status": None, **task}))
    return fake_run


def _globus() -> transfer_providers.GlobusProvider:
    return transfer_providers.GlobusProvider({
        "local_endpoint_id": "11111111-1111-1111-1111-111111111111", "local_endpoint_name": "laptop",
        "remote_endpoint_id": "22222222-2222-2222-2222-222222222222", "remote_endpoint_name": "hpc"})


class TestGlobus:
    @pytest.mark.integration
    def test_an_upload_must_have_moved_every_file(self, tmp_path, monkeypatch):
        src = _tree(tmp_path / "results")
        tree = transfer._scan_tree(src)[0]
        monkeypatch.setattr(subprocess, "run", _fake_globus({"files_transferred": 3}))
        ok = _globus().upload_one(env=_env_ssh(), local_path=src, abs_remote_path="/w/u/results",
                                  local_sha256="", timeout=60, tree=tree)
        assert ok["outcome"] == "proven" and ok["checksums"] == tree, ok

        monkeypatch.setattr(subprocess, "run", _fake_globus({"files_transferred": 2}))
        short = _globus().upload_one(env=_env_ssh(), local_path=src, abs_remote_path="/w/u/results",
                                     local_sha256="", timeout=60, tree=tree)
        assert short["code"] == "transfer.globus_tree_incomplete", short
        assert "moved 2 file(s) and the directory holds 3" in short["error"]

    @pytest.mark.integration
    def test_a_download_records_what_landed_and_a_symlink_left_behind_is_not_a_whole_copy(
            self, tmp_path, monkeypatch):
        far = _tree(tmp_path / "far" / "results")
        monkeypatch.setattr(subprocess, "run", _fake_globus(
            {"files_transferred": 3}, land=(far, tmp_path / "back")))
        ok = _globus().download_one(env=_env_ssh(), abs_remote_path="/w/u/results",
                                    local_path=tmp_path / "back", timeout=60)
        assert ok["outcome"] == "proven", ok
        assert ok["checksums"] == transfer._scan_tree(far)[0]

        monkeypatch.setattr(subprocess, "run", _fake_globus(
            {"files_transferred": 3, "symlinks": 1}, land=(far, tmp_path / "again")))
        short = _globus().download_one(env=_env_ssh(), abs_remote_path="/w/u/results",
                                       local_path=tmp_path / "again", timeout=60)
        assert short["code"] == "transfer.globus_tree_incomplete", short
        assert "1 symlink(s)" in short["error"] and str(tmp_path / "again") in short["error"]

    @pytest.mark.integration
    def test_task_status_does_not_call_a_copy_with_a_symlink_left_behind_whole(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _fake_globus({"files_transferred": 5, "symlinks": 2}))
        env = {**_env_ssh(), "data_transfer": {"type": "globus", "globus": {
            "local_endpoint_id": "11111111-1111-1111-1111-111111111111", "local_endpoint_name": "laptop",
            "remote_endpoint_id": "22222222-2222-2222-2222-222222222222", "remote_endpoint_name": "hpc"}}}
        out = transfer_providers.globus_task_status(env, _TASK_ID)
        assert (out["outcome"], out["code"]) == ("broke", "transfer.task_incomplete"), out
        assert out["success"] is False and out["symlinks"] == 2

    @pytest.mark.integration
    def test_a_detached_transfer_waits_past_the_cap_a_tool_call_keeps(self, tmp_path, monkeypatch):
        """In a tool call the wait stops at 540 s, under the stream watchdog; a
        `background=True` job has no watchdog over it and waits for the task."""
        clock = {"t": 0.0}
        monkeypatch.setattr(transfer_providers.time, "perf_counter", lambda: clock["t"])
        monkeypatch.setattr(transfer_providers.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

        def fake_run(argv, *a, **kw):
            if argv[:2] == ["globus", "transfer"]:
                return MagicMock(returncode=0, stderr="", stdout=json.dumps({"task_id": _TASK_ID}))
            status = "SUCCEEDED" if clock["t"] >= 700 else "ACTIVE"
            return MagicMock(returncode=0, stderr="", stdout=json.dumps(
                {"status": status, "nice_status": None, "files_transferred": 1}))
        monkeypatch.setattr(subprocess, "run", fake_run)
        src = tmp_path / "big.bin"
        src.write_bytes(b"x")

        def up():
            return _globus().upload_one(env=_env_ssh(), local_path=src, abs_remote_path="/w/u/big.bin",
                                        local_sha256="", timeout=24 * 3600)
        assert up()["code"] == "transfer.globus_sync_wait_exceeded"
        clock["t"] = 0.0
        monkeypatch.setenv(DETACHED_ENV, "upload")
        assert up()["outcome"] == "proven"
