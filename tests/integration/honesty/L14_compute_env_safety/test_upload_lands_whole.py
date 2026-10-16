"""
L14 cheat-guards — a file under its final name is a complete one.

Every check that something "is already there" (the upload no-overwrite rule, a
staged .sif, a launch's inputs) trusts existence. That trust holds only if no
transport ever leaves a partial file under the final name, so on every wire an
upload goes to a hidden partial sibling and is renamed into place once its bytes
are verified:

  - scp: the partial is checked by sha256 on the far side, then `mv`'d; a failed
    or interrupted copy leaves nothing at the final name and its partial is cleared;
  - Globus: the task writes the partial and `globus rename` (which refuses an
    existing destination) moves it once the task SUCCEEDED; past the sync-wait cap
    the receipt says "still running" and names the rename, which globus_task_status
    makes when it sees the task succeed;
  - local copy: the same, with os.rename;
  - a failed download removes what it had written, so a retry is not refused and
    nothing reads a truncated file as the output;
  - a Globus task that cannot finish without someone acting is cancelled, so a
    retry cannot race it.

The scp tests run the real remote commands through bash on this machine.
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

_REAL_RUN = subprocess.run
_TASK_ID = "33333333-3333-3333-3333-333333333333"
_LOCAL_EP = "11111111-1111-1111-1111-111111111111"
_REMOTE_EP = "22222222-2222-2222-2222-222222222222"
_GLOBUS = {"local_endpoint_id": _LOCAL_EP, "local_endpoint_name": "laptop",
           "remote_endpoint_id": _REMOTE_EP, "remote_endpoint_name": "hpc"}


def _env_ssh() -> dict:
    return {"name": "hpc", "type": "ssh", "host": "hpc.example.edu", "user": "user1"}


def _fake_ssh_and_scp(calls: list, *, corrupt: bool = False, fail_scp: bool = False):
    """ssh runs its remote command through bash here; scp copies with `user@host:`
    stripped. `corrupt` flips the copy's bytes; `fail_scp` writes half the file and
    exits 1, as a dropped connection does."""
    def fake_run(argv, *a, **kw):
        calls.append(argv)
        if argv[0] == "ssh":
            return _REAL_RUN(["bash", "-c", argv[-1]], capture_output=True, text=True)
        src, dst = (s.split(":", 1)[1] if ":" in s else s for s in argv[-2:])
        data = Path(src).read_bytes()
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        if fail_scp:
            Path(dst).write_bytes(data[: len(data) // 2])
            return MagicMock(returncode=1, stdout="", stderr="Connection closed")
        Path(dst).write_bytes(data[::-1] if corrupt else data)
        return MagicMock(returncode=0, stdout="", stderr="")
    return fake_run


def _sha(p: Path) -> str:
    return transfer._compute_local_sha256(p)


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs sha256sum, as the cluster has")
class TestScpFile:
    @pytest.mark.integration
    def test_an_upload_lands_under_its_final_name_only_once_verified(self, tmp_path, monkeypatch):
        calls: list = []
        monkeypatch.setattr(subprocess, "run", _fake_ssh_and_scp(calls))
        src = tmp_path / "reads.fq"
        src.write_bytes(b"@r1\nACGT\n+\nIIII\n")
        dest = tmp_path / "far" / "reads.fq"
        out = transfer_providers.ScpHeadNodeProvider().upload_one(
            env=_env_ssh(), local_path=src, abs_remote_path=str(dest),
            local_sha256=_sha(src), timeout=60)
        assert out["outcome"] == "proven", out
        assert next(c for c in calls if c[0] == "scp")[-1].endswith("/far/.reads.fq.partial")
        assert dest.read_bytes() == src.read_bytes()
        assert not (tmp_path / "far" / ".reads.fq.partial").exists()

    @pytest.mark.integration
    @pytest.mark.parametrize("how", ["corrupt", "fail_scp"])
    def test_bytes_that_do_not_match_never_take_the_final_name(self, tmp_path, monkeypatch, how):
        monkeypatch.setattr(subprocess, "run", _fake_ssh_and_scp([], **{how: True}))
        src = tmp_path / "image.sif"
        src.write_bytes(b"0123456789" * 100)
        dest = tmp_path / "far" / "image.sif"
        out = transfer_providers.ScpHeadNodeProvider().upload_one(
            env=_env_ssh(), local_path=src, abs_remote_path=str(dest),
            local_sha256=_sha(src), timeout=60)
        assert out["outcome"] == "broke", out
        assert not dest.exists(), "a later existence check would read it as staged"
        assert not (tmp_path / "far" / ".image.sif.partial").exists()

    @pytest.mark.integration
    def test_a_failed_download_removes_what_it_wrote(self, tmp_path, monkeypatch):
        far = tmp_path / "far" / "out.bam"
        far.parent.mkdir()
        far.write_bytes(b"BAM\x01" * 50)
        monkeypatch.setattr(subprocess, "run", _fake_ssh_and_scp([], fail_scp=True))
        local = tmp_path / "back.bam"
        out = transfer_providers.ScpHeadNodeProvider().download_one(
            env=_env_ssh(), abs_remote_path=str(far), local_path=local, timeout=60)
        assert out["code"] == "transfer.scp_download_failed", out
        assert not local.exists(), "a truncated file would be validated as the output"

    @pytest.mark.integration
    def test_the_no_overwrite_check_looks_again_before_saying_missing(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(subprocess, "run", lambda argv, *a, **kw: (
            calls.append(argv), MagicMock(returncode=0, stdout="OK\n", stderr=""))[1])
        assert transfer._remote_existence_check(_env_ssh(), "/w/u/x.txt", 30) == {"exists": False}
        assert "seen /w/u/x.txt" in calls[0][-1]


class TestLocalCopy:
    @pytest.mark.integration
    def test_a_local_env_upload_leaves_no_partial(self, tmp_path):
        from tests.integration.honesty.L14_compute_env_safety.test_transfer_surface import _make_access
        access = _make_access(tmp_path)
        src = tmp_path / "x.txt"
        src.write_text("payload")
        dest = tmp_path / "fakescratch" / "demo_project" / "x.txt"
        out = transfer.upload("demo_project", "fakehpc", str(src), str(dest), access_path=str(access))
        assert out["outcome"] == "proven", out
        assert dest.read_text() == "payload"
        assert not (dest.parent / ".x.txt.partial").exists()


def _globus_access(tmp_path: Path) -> Path:
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump({
        "compute_envs": [{
            "name": "hpc", "type": "ssh", "host": "hpc.example.edu", "user": "u",
            "agent_scratch_target": {"path": "/w/scratch",
                                     "permissions": ["file_name_only", "upload", "download", "exec"]},
            "data_transfer": {"type": "globus", "globus": _GLOBUS}}],
        "projects": [{"name": "demo_project", "compute_envs": ["hpc"], "directories": []}]}))
    return p


class TestGlobus:
    @pytest.mark.integration
    def test_past_the_wait_cap_the_receipt_says_still_running_and_the_status_call_finishes_it(
            self, tmp_path, monkeypatch):
        clock = {"t": 0.0, "done": False}
        monkeypatch.setattr(transfer_providers.time, "perf_counter", lambda: clock["t"])
        monkeypatch.setattr(transfer_providers.time, "sleep",
                            lambda s: clock.__setitem__("t", clock["t"] + s))
        calls: list = []

        def fake_run(argv, *a, **kw):
            calls.append(argv)
            if argv[:2] == ["globus", "stat"]:
                return MagicMock(returncode=0, stderr="", stdout=json.dumps({"code": "NotFound"}))
            if argv[:2] == ["globus", "transfer"]:
                return MagicMock(returncode=0, stderr="", stdout=json.dumps({"task_id": _TASK_ID}))
            if argv[:2] == ["globus", "rename"]:
                return MagicMock(returncode=0, stderr="", stdout="")
            status = "SUCCEEDED" if clock["done"] else "ACTIVE"
            return MagicMock(returncode=0, stderr="", stdout=json.dumps(
                {"status": status, "nice_status": None, "files_transferred": 1}))
        monkeypatch.setattr(subprocess, "run", fake_run)
        src = tmp_path / "big.sif"
        src.write_bytes(b"x" * 64)

        out = transfer.upload("demo_project", "hpc", str(src), "/w/scratch/demo_project/big.sif",
                              access_path=str(_globus_access(tmp_path)))
        assert (out["outcome"], out["code"]) == ("loop", "transfer.globus_sync_wait_exceeded"), out
        assert out["task_id"] == _TASK_ID
        assert out["partial_path"] == "/w/scratch/demo_project/.big.sif.partial"
        assert not any(c[:2] == ["globus", "rename"] for c in calls), "nothing in place yet"
        rec = json.loads(Path(out["manifest"]).read_text())
        assert rec["outcome"] == "still running" and "Failed" not in rec["summary"]
        assert rec["pending_rename"] == {"from": "/w/scratch/demo_project/.big.sif.partial",
                                         "to": "/w/scratch/demo_project/big.sif"}

        clock["done"] = True
        env = {**_env_ssh(), "data_transfer": {"type": "globus", "globus": _GLOBUS}}
        st = transfer.resolve_globus_task("demo_project", env, _TASK_ID)
        assert (st["outcome"], st["renamed_to"]) == ("proven", "/w/scratch/demo_project/big.sif"), st
        assert calls[-1] == ["globus", "rename", _REMOTE_EP,
                             "/w/scratch/demo_project/.big.sif.partial",
                             "/w/scratch/demo_project/big.sif"]
        rec = json.loads(Path(out["manifest"]).read_text())
        assert rec["outcome"] == "uploaded" and "pending_rename" not in rec

        calls.clear()
        again = transfer.resolve_globus_task("demo_project", env, _TASK_ID)
        assert again["outcome"] == "proven" and "renamed_to" not in again
        assert not any(c[:2] == ["globus", "rename"] for c in calls), "renamed once"

    @pytest.mark.integration
    def test_a_task_that_cannot_finish_is_cancelled(self, tmp_path, monkeypatch):
        calls: list = []

        def fake_run(argv, *a, **kw):
            calls.append(argv)
            if argv[:2] == ["globus", "transfer"]:
                return MagicMock(returncode=0, stderr="", stdout=json.dumps({"task_id": _TASK_ID}))
            if argv[:3] == ["globus", "task", "cancel"]:
                return MagicMock(returncode=0, stderr="", stdout="")
            return MagicMock(returncode=0, stderr="", stdout=json.dumps(
                {"status": "INACTIVE", "nice_status": "Creds Expired"}))
        monkeypatch.setattr(subprocess, "run", fake_run)
        src = tmp_path / "x.txt"
        src.write_text("hi")
        out = transfer_providers.GlobusProvider(_GLOBUS).upload_one(
            env=_env_ssh(), local_path=src, abs_remote_path="/w/u/x.txt",
            local_sha256="0" * 64, timeout=60)
        assert out["code"] == "transfer.globus_task_inactive", out
        assert ["globus", "task", "cancel", _TASK_ID] in calls
        assert "cancelled, so a retry cannot race it" in out["error"]

    @pytest.mark.integration
    def test_a_directory_upload_mirrors_into_its_partial(self, tmp_path, monkeypatch):
        calls: list = []

        def fake_run(argv, *a, **kw):
            calls.append(argv)
            if argv[:2] == ["globus", "transfer"]:
                return MagicMock(returncode=0, stderr="", stdout=json.dumps({"task_id": _TASK_ID}))
            if argv[:2] == ["globus", "rename"]:
                return MagicMock(returncode=0, stderr="", stdout="")
            return MagicMock(returncode=0, stderr="", stdout=json.dumps(
                {"status": "SUCCEEDED", "nice_status": None, "files_transferred": 1}))
        monkeypatch.setattr(subprocess, "run", fake_run)
        src = tmp_path / "results"
        src.mkdir()
        (src / "a.txt").write_text("a")
        out = transfer_providers.GlobusProvider(_GLOBUS).upload_one(
            env=_env_ssh(), local_path=src, abs_remote_path="/w/u/results",
            local_sha256="", timeout=60, tree=transfer._scan_tree(src)[0])
        assert out["outcome"] == "proven", out
        submit = calls[0]
        assert submit[3] == f"{_REMOTE_EP}:/w/u/.results.partial"
        assert "--delete-destination-extra" in submit, \
            "files an earlier attempt left in the partial must not ride into place"
