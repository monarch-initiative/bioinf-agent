"""Conftest for L15 (real-build) — the ONE tier that drives a genuine Docker
build end-to-end (every other build test mocks the container layer).

A real `freeze()` / `build_env_from_tools()` writes to two singletons that are
bound to the working tree at import time:
  - `_env_cache`  → env_reports/_env_cache.json  (register on a successful build)
  - `_pipeline_state` → data/pipeline_drafts/     (if a draft is created)

Both are isolated to tmp here (mirrors the L4 / L5 conftests) so a real build in
a test never pollutes the user's production cache or leaks a draft — the leak
class the L4 conftest documents (a test env_name flowing into a real .sif name).
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_singletons(monkeypatch, tmp_path):
    from agent import mcp_server as m
    from agent.skills.pipeline_state import PipelineState

    monkeypatch.setattr(m._env_cache, "path", tmp_path / "_env_cache.json")

    drafts = tmp_path / "pipeline_drafts"; drafts.mkdir()
    cfg = {**m.config, "paths": {**m.config.get("paths", {}),
                                 "drafts_dir": str(drafts),
                                 "pipelines_dir": str(tmp_path / "env_reports")}}
    monkeypatch.setattr(m, "_pipeline_state", PipelineState(cfg))
    yield
