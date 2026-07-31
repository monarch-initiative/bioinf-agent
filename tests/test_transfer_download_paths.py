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


def test_run_cluster_step_absolutizes_a_relative_download_dir(tmp_path, monkeypatch):
    """#8b — run_cluster_step must resolve download_local_dir so detected_outputs are
    absolute (I6).

    This used to read run_cluster_step.py and assert the literal expression
    `Path(download_local_dir).expanduser().resolve()` appeared in it. That passes if
    the expression is sitting in a comment and fails if someone splits the line: it
    pinned the SPELLING, not the behaviour, and its two siblings above already showed
    the better shape. The absolutization is now a named function, so ask it directly.
    """
    from agent.skills.run_cluster_step import absolutize_download_dir
    monkeypatch.chdir(tmp_path)
    p = absolutize_download_dir("run1/outputs")
    assert p.is_absolute(), f"download dir not absolutized: {p}"
    assert p == (tmp_path / "run1" / "outputs").resolve()


def test_download_dir_absolutization_does_not_require_the_dir_to_exist(tmp_path, monkeypatch):
    """The caller mkdirs immediately after, so the normal case is a path that is not
    there yet. `resolve()` is lexical for a missing dir — pinned, because a switch to
    `strict=True` would raise here and only on the cluster path, where it is expensive
    to find out."""
    from agent.skills.run_cluster_step import absolutize_download_dir
    monkeypatch.chdir(tmp_path)
    p = absolutize_download_dir("does/not/exist/yet")
    assert p.is_absolute() and not p.exists()


def test_home_relative_download_dir_is_expanded(tmp_path, monkeypatch):
    """`~/scratch` must become a real path, not a literal directory named '~'."""
    from agent.skills.run_cluster_step import absolutize_download_dir
    monkeypatch.setenv("HOME", str(tmp_path))
    p = absolutize_download_dir("~/scratch/out")
    assert p == (tmp_path / "scratch" / "out").resolve()
    assert "~" not in str(p)
