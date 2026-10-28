"""Conftest for L15 (real-build) — the ONE tier that drives a genuine Docker
build end-to-end (every other build test mocks the container layer).

A real `freeze()` / `build_env_image()` writes to two singletons that are
bound to the working tree at import time:
  - `_env_cache`  → the workspace's environments/_env_cache.json (register on a
    successful build)
  - `_pipeline_state` → the drafts under the workspace's scratch zone (if a draft is
    created)

Both are isolated to tmp here (mirrors the L4 / L5 conftests) so a real build in
a test never pollutes the user's production cache or leaks a draft — the leak
class the L4 conftest documents (a test env_name flowing into a real .sif name).
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

#: Every image this tier builds carries this in its repository name.
IMAGE_NAME_MARK = "l15_"


@pytest.fixture(autouse=True)
def _isolate_singletons(monkeypatch, tmp_path):
    from agent import mcp_server as m
    from agent.skills.pipeline_state import PipelineState

    monkeypatch.setattr(m._env_cache, "path", tmp_path / "_env_cache.json")

    monkeypatch.setattr(m, "_pipeline_state", PipelineState(dict(m.config)))
    yield


@pytest.fixture(scope="session", autouse=True)
def _remove_the_images_this_tier_built():
    """A real build leaves its image in the daemon; the tier removes what it built when
    the session ends, so a run of the suite is not a growing collection of test images."""
    yield
    if not shutil.which("docker"):
        return
    listed = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                            capture_output=True, text=True, timeout=60)
    built = [t for t in listed.stdout.split() if IMAGE_NAME_MARK in t.split(":")[0]]
    if built:
        subprocess.run(["docker", "rmi", "-f", *built], capture_output=True, timeout=600)
