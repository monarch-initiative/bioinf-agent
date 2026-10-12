"""A served tool description describes the code that serves it.

The descriptions are the only contract a client sees, so a sentence that outlived the
code steers the agent into a refusal it cannot read. Pinned here: no description cites an
invariant the registry has retired, `patch_pipeline` names exactly the keys its state
object accepts and blocks, and the sentences the 2026-10 review found false stay false-free.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from agent.skills import invariants as reg


@pytest.fixture(scope="module")
def descriptions() -> dict[str, str]:
    from agent.mcp_server import mcp
    return {t.name: (t.description or "") for t in asyncio.run(mcp.list_tools())}


def test_no_description_cites_a_retired_invariant(descriptions):
    retired = {i.id for i in reg.REGISTRY.values() if i.status == reg.RETIRED}
    stale = {name: sorted(hits) for name, text in descriptions.items()
             if (hits := {f"I{n}" for n in re.findall(r"\bI(\d{1,2})\b", text)} & retired)}
    assert not stale, f"descriptions cite retired invariants: {stale}"


def test_patch_pipeline_names_the_keys_it_accepts_and_blocks(descriptions):
    from agent.skills.pipeline_state import PipelineState
    text = descriptions["patch_pipeline"]
    missing = sorted(k for k in PipelineState.PATCHABLE_KEYS | PipelineState.BLOCKED_PATCH_KEYS
                     if k not in text)
    assert not missing, f"patch_pipeline's description does not name {missing}"


def test_the_corrected_sentences_stay_corrected(descriptions):
    assert "user guide" not in descriptions["seal_workflow"], "the seal renders the RUN dashboard"
    assert "no cap" not in descriptions["upload"], "the upload cap applies on every wire"
    assert "single quotes" not in descriptions["run_step_on_cluster"], "a single quote is carried faithfully"
    assert "list_installed_pipelines" not in descriptions["run_production_pipeline"], \
        "list_installed_pipelines lists envs and sealed workflows, not rendered pipelines"
    naming = sorted(n for n, t in descriptions.items() if "compute_env_access" in t)
    assert not naming, f"the loader rejects a compute_env_access key; named by {naming}"
