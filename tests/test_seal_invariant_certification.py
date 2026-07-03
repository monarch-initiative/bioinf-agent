"""C1 — adversarial certification of the honesty-critical SEAL invariants.

`seal_workflow` refuses to write a WorkflowSpec on any I0/I3/I6/I7/I8/I13
violation. A false green here is catastrophic under full-auto: a workflow with a
relative path, a lineage input that can't be verified, a vanished authored
artifact, or a gated-but-license-less artifact would ship as sealed/trustworthy.

These attack the previously-uncertified invariant BRANCHES directly — each
crafts a spec that MUST trip its invariant, and names the invariant code so the
gate is proven to fire on exactly that attack.
"""
from __future__ import annotations

import hashlib
import pathlib

import pytest

from agent.skills.spec_writer import check_invariants


def _base_spec():
    """A minimal spec that PASSES the invariants — each test perturbs one thing so
    the ONLY violation is the one under test."""
    return {
        "pipeline_name": "cert",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "test_data": {"bam": "/abs/in.bam"},
        "pipeline_steps": [{
            "step": 1, "tool": "samtools", "command": "samtools view /abs/in.bam",
            "returncode": 0,
            "inputs": [{"path": "/abs/in.bam"}],
            "detected_outputs": ["/abs/out.sam"],
            "validation": {"out.sam": {"passed": True, "expected_type": "sam"}},
            "validation_status": "passed",
            "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 12.3},
        }],
    }


def test_i6_fires_on_relative_output_path():
    """I6: a RELATIVE detected_output (not just input) must trip I6.absolute_paths.
    An absolute-path contract is what lets the sealed spec be re-run anywhere;
    a relative output silently binds to wherever it happened to run."""
    spec = _base_spec()
    spec["pipeline_steps"][0]["detected_outputs"] = ["relative/out.sam"]  # <- relative
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I6.absolute_paths"
               and "output" in v["message"] for v in violations), violations


def test_i8_fires_on_unreadable_authored_artifact(tmp_path, monkeypatch):
    """I8.authored_artifact_unreadable: an authored artifact that exists on disk
    (so it's not 'missing') but cannot be re-hashed at seal time (permission / I/O
    error) must trip the gate — seal can't attest to bytes it can't read."""
    script = tmp_path / "run.R"
    script.write_text("cat('ok')\n")
    sha = hashlib.sha256(script.read_bytes()).hexdigest()

    real_read = pathlib.Path.read_bytes
    def _boom(self):
        if str(self) == str(script):
            raise PermissionError("simulated unreadable artifact")
        return real_read(self)
    monkeypatch.setattr(pathlib.Path, "read_bytes", _boom)

    spec = _base_spec()
    spec["authored_artifacts"] = [{
        "path": str(script), "role": "driver_script", "description": "d",
        "sha256": sha, "size_bytes": 5, "created_at": "2026-07-03T00:00:00",
    }]
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I8.authored_artifact_unreadable"
               for v in violations), violations


def test_i8_fires_on_unreadable_lineage_input(tmp_path, monkeypatch):
    """I8.lineage_unreadable: a consumer step reads a file a prior step produced
    (recorded in output_sha256); the file exists but can't be re-hashed at seal.
    The lineage chain can't be verified, so the gate must fire."""
    produced = tmp_path / "reads.bam"
    produced.write_bytes(b"BAM\x01 produced bytes")
    sha = hashlib.sha256(produced.read_bytes()).hexdigest()

    real_open = pathlib.Path.open
    def _boom(self, *a, **k):
        if str(self) == str(produced):
            raise OSError("simulated unreadable lineage input")
        return real_open(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "open", _boom)

    spec = _base_spec()
    spec["pipeline_steps"] = [
        {  # producer
            "step": 1, "tool": "bwa", "command": "bwa ... > reads.bam",
            "returncode": 0, "inputs": [{"path": "/abs/in.fq"}],
            "detected_outputs": [str(produced)],
            "output_sha256": {str(produced): sha},
            "validation": {"reads.bam": {"passed": True, "expected_type": "bam"}},
            "validation_status": "passed",
            "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 12.3},
        },
        {  # consumer of the produced file
            "step": 2, "tool": "samtools", "command": f"samtools view {produced}",
            "returncode": 0, "inputs": [{"path": str(produced)}],
            "detected_outputs": ["/abs/out.sam"],
            "validation": {"out.sam": {"passed": True, "expected_type": "sam"}},
            "validation_status": "passed",
            "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 12.3},
        },
    ]
    spec["test_data"] = {"fq": "/abs/in.fq"}
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I8.lineage_unreadable" for v in violations), violations


def test_i13_early_gate_refuses_gated_without_license(monkeypatch):
    """I13.gated_license_recorded: freeze must refuse a GATED build with an empty
    licenses[] BEFORE any docker work (the early gate) — republishing a gated
    artifact with no recorded license is the license-firewall's core attack."""
    from agent.mcp_tools import freeze_tools
    from agent import mcp_server as _ms
    monkeypatch.setattr(_ms, "_check_disk_failsafe", lambda: None)
    res = freeze_tools.freeze(env_name="bioinf_gated_zzz", tools=["cellranger"],
                              gated=True, licenses=[])
    assert res.get("outcome") == "refused", res
    assert res.get("code") == "freeze.gated_no_license", res
    assert any(v.get("invariant") == "I13.gated_license_recorded"
               for v in res.get("honesty_violations", [])), res


# ---------------------------------------------------------------------------
# I10 (service health) — restored at Layer 2. The services probe (row 7) found
# it advertised in every ServiceDependency docstring but enforced NOWHERE: the
# respine retired I10 as an env-build invariant and it was never re-added to the
# workflow checker. A workflow depending on a service that never became healthy
# would seal GREEN. These certify the restored gate fires on exactly that attack.
# ---------------------------------------------------------------------------
def _svc(status, probes):
    return {"type": "cache", "name": "redis", "start_command": "redis-server",
            "stop_command": "redis-cli shutdown", "status": status,
            "health_check_log": probes}


def test_i10_fires_on_service_never_healthy():
    """A declared service with ZERO healthy probes (status=failed) must trip
    I10.service_never_healthy — seal cannot bless a workflow whose service never
    came up (the advertised-but-absent firewall the probe exposed)."""
    spec = _base_spec()
    spec["service_dependencies"] = [_svc("failed", [{"healthy": False, "returncode": 1}])]
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I10.service_never_healthy" for v in violations), violations


def test_i10_passes_service_that_was_healthy_then_stopped():
    """A service that reached healthy then cleanly stopped PASSES — the check is
    'did it ever come up', not 'is it up now' (the probe's valid fixture)."""
    spec = _base_spec()
    spec["service_dependencies"] = [_svc("stopped",
        [{"healthy": True, "returncode": 0}, {"healthy": True, "returncode": 0}])]
    violations = check_invariants(spec)
    assert not any(v["invariant"].startswith("I10") for v in violations), violations


def test_i10_flows_through_workflow_entry_point():
    """The gate must fire via check_workflow_invariants (the actual seal path), not
    only check_invariants — i.e. I10 is in _WORKFLOW_INVARIANT_TIERS."""
    from agent.skills.spec_writer import check_workflow_invariants
    spec = _base_spec()
    spec["service_dependencies"] = [_svc("running", [])]     # zero probes at all
    wv = check_workflow_invariants(spec)
    assert any(v["invariant"] == "I10.service_never_healthy" for v in wv), wv


def test_i10_noop_without_services():
    """No service_dependencies → no I10 violation (the common case is untouched)."""
    violations = check_invariants(_base_spec())
    assert not any(v["invariant"].startswith("I10") for v in violations)
