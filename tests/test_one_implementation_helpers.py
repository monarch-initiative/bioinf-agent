"""
The 2026-09-14 cleanup-pass consolidations, pinned by IDENTITY.

Measured before acting (the banked review claims were stale in both
directions): SEVEN generic subprocess runners in three error dialects — with
local_sif._run and freeze_from_image._sh byte-identical — and FIVE named
file-hash loops plus inline copies. Each family now has one implementation;
these tests assert the aliases ARE the shared function, so a re-forked copy
fails even if it starts out byte-identical (the property the original pair
lacked: a copy that cannot drift).

Deliberately NOT consolidated, and NOT asserted here: locus._sh (never-fatal
contract), output_validator._run_tool (tool_found honesty semantics),
env_manager._run_monitored (I7's sole resource_usage producer). See
_proc's module docstring.
"""
from __future__ import annotations

import subprocess
import sys

from agent.skills import _proc


# ───────────────────────── subprocess: one implementation ──────────────────

def test_the_byte_identical_pair_is_now_one_function():
    from agent.skills import local_sif, freeze_from_image
    assert local_sif._run is _proc.run_argv_rc
    assert freeze_from_image._sh is _proc.run_argv_rc


def test_run_argv_timeout_and_missing_binary_are_conventional_codes():
    r = _proc.run_argv([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
    assert r["returncode"] == 124
    assert "timed out" in r["stderr"]
    r = _proc.run_argv(["definitely-not-a-binary-xyz"], timeout=5)
    assert r["returncode"] == 127


def test_run_argv_salvages_partial_output_on_timeout():
    r = _proc.run_argv(
        [sys.executable, "-u", "-c",
         "print('partial', flush=True); import time; time.sleep(5)"],
        timeout=2)
    assert r["returncode"] == 124
    assert "partial" in r["stdout"], (
        "the timeout branch must salvage what the process printed — this is "
        "the container_build semantics the consolidation promoted, not lost")


def test_rc_dialect_is_a_remap_of_the_canonical_not_a_second_runner():
    r = _proc.run_argv_rc([sys.executable, "-c", "print('x')"], timeout=30)
    assert set(r) == {"rc", "out", "err"}
    assert r["rc"] == 0 and r["out"].strip() == "x"


def test_no_module_greww_its_own_subprocess_runner_back():
    # The three delegating shells may still CALL subprocess for other jobs;
    # what must not return is a private capture_output runner in the two
    # modules that had the byte-identical copies.
    import inspect
    from agent.skills import local_sif, freeze_from_image
    for mod in (local_sif, freeze_from_image):
        src = inspect.getsource(mod)
        assert "subprocess.run(" not in src, (
            f"{mod.__name__} re-grew a private runner; use _proc")


# ───────────────────────── sha256: one implementation ──────────────────────

def test_data_pins_alias_is_the_shared_tolerant_hash():
    from agent.skills import data_pins
    from agent.models import core_data
    assert data_pins._sha256_file is core_data.sha256_file_or_none


def test_every_delegate_hashes_identically(tmp_path):
    from agent.models.core_data import sha256_file
    from agent.skills.core_test_data import _sha256_file as ctd_hash
    from agent.skills.transfer import _compute_local_sha256
    from agent.skills.env_manager import EnvManager
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00\x01bioinf\xff" * 1000)
    expect = sha256_file(p)
    assert ctd_hash(p) == expect
    assert _compute_local_sha256(p) == expect
    assert EnvManager._sha256_file(p) == expect


def test_raising_and_tolerant_spellings_differ_only_on_the_missing_file(tmp_path):
    from agent.models.core_data import sha256_file, sha256_file_or_none
    import pytest
    missing = tmp_path / "nope"
    assert sha256_file_or_none(missing) is None
    with pytest.raises(OSError):
        sha256_file(missing)


def test_anchor_for_path_uses_the_shared_hash(tmp_path):
    # anchor_for_path's own docstring warns that two implementations of
    # "what are these bytes" end up comparing a capped observation with an
    # uncapped pin — it must ride the shared implementation.
    import inspect
    from agent.models import core_data
    src = inspect.getsource(core_data.anchor_for_path)
    assert "sha256_file(" in src and "hashlib" not in src


# ───────────────────────── user guide / dashboard parity (F21 leaf) ────────

def _staged_spec() -> dict:
    return {
        "workflow_name": "parity_probe",
        "created_at": "2026-09-14T00:00:00+00:00",
        "usage": {"command_template": "tool {R1} > {OUTPUT_DIR}/x",
                  "description": "d", "inputs": [], "outputs": []},
        "pipeline_steps": [{
            "step": 1, "tool": "tool", "returncode": 0,
            "validation_locus": "cluster",
            "cluster_job_id": "1", "cluster_node": "c1",
            "cluster_sif_sha256": "ab" * 32,
            "container_image": "/work/CLAUDE_CONTAINERS/x.sif",
            "cluster_slurm": {"time": "00:05:00", "mem": "1g"},
            "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 1.0,
                               "max_cpu_percent": 1.0, "locus": "cluster",
                               "i7_authoritative": True},
            "validation": {"x": {"passed": True}},
        }],
    }


_STALE_ADVICE = {"hpc_delivery": {
    "get_image": "# transfer x.tar to the cluster (scp/rsync), then on the HPC:\n"
                 "apptainer build x.sif docker-archive://x.tar",
    "source_note": "registry-free transfer"}}


def test_guide_and_dashboard_agree_a_staged_sif_outranks_stored_advice():
    # The measured defect: the guide printed the head-node build advice for
    # the same workflow whose dashboard said "never built on the head node".
    # Both now consult core_data.staged_sif_steps.
    from agent.skills.user_guide import render_user_guide
    from agent.skills.run_dashboard_html import render_run_dashboard_html
    spec = _staged_spec()
    spec["env_hpc_delivery"] = dict(_STALE_ADVICE["hpc_delivery"])
    guide = render_user_guide(spec, freeze_record=dict(_STALE_ADVICE))
    dash = render_run_dashboard_html(spec)
    for page, name in ((guide, "guide"), (dash, "dashboard")):
        assert "then on the HPC" not in page, (
            f"the {name} still shows the stored head-node build advice "
            f"over a step-recorded staged .sif (F21)")
    assert "already staged on the cluster" in guide
    assert "Staged .sif (cluster)" in dash


def test_without_staged_evidence_the_stored_advice_still_reaches_the_guide():
    from agent.skills.user_guide import render_user_guide
    spec = _staged_spec()
    step = spec["pipeline_steps"][0]
    for k in ("cluster_sif_sha256", "container_image"):
        step.pop(k, None)
    guide = render_user_guide(spec, freeze_record=dict(_STALE_ADVICE))
    assert "Get the container" in guide
