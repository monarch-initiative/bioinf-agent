"""Tests for local_sif.build_sif_locally — build an Apptainer .sif LOCALLY
(apptainer-in-docker) so image -> .sif conversion never touches the cluster head
node ([[feedback-no-head-node-image-builds]]).

The command-construction tests monkeypatch the subprocess runner so they need no
docker; the functional test is docker-gated and skipped when docker is absent.
"""
import shutil

import pytest

from agent.skills import local_sif


def test_rejects_both_or_neither_source():
    r1 = local_sif.build_sif_locally(out_sif="/tmp/x.sif")
    assert r1["outcome"] == "refused" and r1["code"] == "local_sif.bad_args"
    r2 = local_sif.build_sif_locally(out_sif="/tmp/x.sif",
                                     tarball="a.tar", image_tag="img:1")
    assert r2["outcome"] == "refused" and r2["code"] == "local_sif.bad_args"


def test_image_tag_mode_saves_then_builds_in_privileged_container(tmp_path, monkeypatch):
    calls = []
    def fake_run(argv, timeout):
        calls.append(argv)
        # docker save -> touch the tar so the "tar inside bind" check passes
        if argv[:2] == ["docker", "save"]:
            open(argv[argv.index("-o") + 1], "w").close()
            return {"rc": 0, "out": "", "err": ""}
        # apptainer build -> touch the .sif so the output check passes
        if "build" in argv:
            (tmp_path / "out.sif").write_bytes(b"SIF")
            return {"rc": 0, "out": "", "err": ""}
        return {"rc": 0, "out": "", "err": ""}
    monkeypatch.setattr(local_sif, "_run", fake_run)

    out = local_sif.build_sif_locally(
        out_sif=str(tmp_path / "out.sif"), image_tag="talos-authors:11.0.0")
    assert out["outcome"] == "proven", out
    assert out["sif_path"].endswith("out.sif")

    save = next(c for c in calls if c[:2] == ["docker", "save"])
    assert "talos-authors:11.0.0" in save
    build = next(c for c in calls if "build" in c)
    # heavy conversion runs in a PRIVILEGED linux container, never on a cluster
    assert build[0] == "docker" and "--privileged" in build
    assert "--platform" in build and "linux/amd64" in build
    assert local_sif.APPTAINER_BUILDER_IMAGE in build
    # docker-archive source lives under the single bind dir mounted at /work
    assert any(a.startswith("docker-archive:/work/") for a in build)
    # no ssh / cluster hop anywhere
    assert not any("ssh" in a or "apptainer build" == a for c in calls for a in c)


def test_build_failure_surfaces_broke(tmp_path, monkeypatch):
    def fake_run(argv, timeout):
        if argv[:2] == ["docker", "save"]:
            open(argv[argv.index("-o") + 1], "w").close()
            return {"rc": 0, "out": "", "err": ""}
        return {"rc": 255, "out": "", "err": "FATAL: mksquashfs blew up"}
    monkeypatch.setattr(local_sif, "_run", fake_run)
    out = local_sif.build_sif_locally(out_sif=str(tmp_path / "x.sif"),
                                      image_tag="img:1")
    assert out["outcome"] == "broke" and out["code"] == "local_sif.build_failed"


@pytest.mark.skipif(not shutil.which("docker"), reason="docker required")
def test_functional_build_from_tiny_local_image(tmp_path):
    """docker-gated: build a real .sif from a locally-built tiny image."""
    import subprocess
    tag = "localsif-pytest:1"
    build = subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "-t", tag, "-"],
        input="FROM alpine:3.19\nRUN echo hi > /hi\n", text=True,
        capture_output=True, timeout=300)
    if build.returncode != 0:
        pytest.skip(f"docker build unavailable: {build.stderr[-200:]}")
    try:
        out = local_sif.build_sif_locally(
            out_sif=str(tmp_path / "tiny.sif"), image_tag=tag, timeout=300)
        assert out["outcome"] == "proven", out
        assert (tmp_path / "tiny.sif").is_file()
        assert out["size_bytes"] > 0
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
