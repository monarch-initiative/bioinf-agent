"""Regression tests for the download local-path absolutization (bugs #8/#8b).

Anchored to the real Talos cluster seal (2026-07-06): run_step_on_cluster fetched
a validated output via Globus, but a RELATIVE download_local_dir broke it two ways:
  #8  transfer.download hashed the fetched file against the process CWD while
      Globus delivered it against the local-endpoint root — SUCCEEDED-but-not-found.
  #8b the recorded detected_output stayed relative → I6 (absolute-paths) refused
      the seal even though the run was clean.
Both fixes absolutize the local path so the transfer target, the sha256 check, and
the recorded output path all agree — and satisfy I6.
"""
from pathlib import Path


def test_download_validator_absolutizes_relative_path(tmp_path, monkeypatch):
    from agent.skills.transfer import _validate_local_path_for_download
    monkeypatch.chdir(tmp_path)
    (tmp_path / "some_run").mkdir()          # parent must exist (dest must not)
    p = _validate_local_path_for_download("some_run/out.txt")
    assert p.is_absolute(), f"download dest not absolutized: {p}"
    assert p == (tmp_path / "some_run" / "out.txt").resolve()


def test_upload_validator_absolutizes_relative_path(tmp_path, monkeypatch):
    from agent.skills import transfer
    # a real relative file to upload
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    f = tmp_path / "sub" / "art.txt"
    f.write_text("x")
    p = transfer._validate_local_path_for_upload("sub/art.txt")
    assert p.is_absolute(), f"upload src not absolutized: {p}"
    assert p == f.resolve()


def test_upload_validator_still_rejects_symlink_after_absolutize(tmp_path, monkeypatch):
    """Absolutizing must NOT resolve symlinks away — the redirect-attack guard
    still has to see the symlink."""
    from agent.skills.transfer import _validate_local_path_for_upload, TransferError
    monkeypatch.chdir(tmp_path)
    real = tmp_path / "real.txt"
    real.write_text("x")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    try:
        _validate_local_path_for_upload("link.txt")
        assert False, "symlink should have been rejected"
    except TransferError as e:
        assert "symlink" in str(e)


def test_run_cluster_step_absolutizes_download_dir_source():
    """#8b — run_cluster_step must resolve download_local_dir so detected_outputs
    are absolute (I6). Pin the code shape so the fix can't silently revert."""
    src = Path("agent/skills/run_cluster_step.py").read_text()
    assert "Path(download_local_dir).expanduser().resolve()" in src, (
        "download_dir must be absolutized for I6-compliant detected_outputs")
