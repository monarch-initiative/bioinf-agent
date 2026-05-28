"""
N8 (batch-3 Apollo3 stress): the `output_types` lookup chain in
run_pipeline_step must accept absolute paths, basenames, AND extension
keys — and surface unmatched keys back to the caller so a typo doesn't
silently fall through to expected_type="any" (an I3 violation at seal).

Pre-fix, the lookup was effectively `output_types.get(basename) or
output_types.get(ext)`. Every agent reaches for full-path keys first
(the path it just constructed); they all fell through to inference,
which yielded "any". Seal then refused (I3) AFTER the run, not before.

The fix added a five-tier lookup chain:
  1. absolute resolved path
  2. as-detected path (in case of non-canonical form)
  3. basename
  4. ".ext" (with leading dot)
  5. "ext" (without leading dot)
  6. fall through to extension inference

Why integration, not unit: the lookup chain consults `Path(p).resolve()`
which is filesystem-sensitive (symlinks, /tmp → /private/tmp on macOS).
A constructed dict test cannot exercise resolution. We use real touched
files in tmp_path, mock the subprocess + validator, and let the lookup
chain run unmodified.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent import mcp_server as m


@pytest.fixture
def _mock_step(monkeypatch, tmp_path):
    """A run_pipeline_step where the run + validation are stubbed, but the
    output_types lookup chain runs for real. Returns a callable that takes
    an output_types dict and the relative output filename, runs the step,
    and returns the (etype_consumed, response) pair."""
    # Need a registered draft so add_step / add_validation succeed.
    pid = "p_n8"
    m._pipeline_state.start(pid, "n8 test")

    consumed_etypes: list[tuple[str, str]] = []   # (basename, etype)

    def _fake_validate(self, path, etype, env_name=""):
        consumed_etypes.append((Path(path).name, etype))
        return {"valid": True, "expected_type": etype, "path": path}

    # Use a patched validator that records the etype the lookup chain
    # picked (the thing we actually want to assert on).
    monkeypatch.setattr(type(m._validator), "validate", _fake_validate)

    def _run(output_types: dict, out_relpath: str = "out.bam"):
        outp = tmp_path / out_relpath
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text("BAM")

        def _fake_run_in_env(self, env_name, command, working_dir=None,
                             timeout=1800, inputs=None, watch_dir=None):
            return {
                "returncode": 0, "stdout": "", "stderr": "",
                "command": command, "env_name": env_name,
                "detected_outputs": [str(outp)],
                "resource_usage": {"wall_seconds": 0.01,
                                   "peak_rss_mb": 1.0,
                                   "peak_cpu_pct": 1.0},
                "inputs": inputs or [],
            }
        monkeypatch.setattr(type(m._env_mgr), "run_in_env", _fake_run_in_env)

        consumed_etypes.clear()
        return m.run_pipeline_step(
            env_name="dummy", command="ignored",
            pipeline_id=pid,
            inputs=[],
            output_types=output_types,
            watch_dir=str(tmp_path),
        ), consumed_etypes
    return _run


@pytest.mark.integration
def test_absolute_resolved_path_key_wins(_mock_step, tmp_path):
    """The N8 headline fix: a full absolute path key MUST match the
    detected output's resolved path."""
    outp = tmp_path / "myout.bam"
    abs_key = str(outp.resolve())
    resp, consumed = _mock_step({abs_key: "bam"}, out_relpath="myout.bam")
    assert resp["validation_count"] == 1
    assert consumed == [("myout.bam", "bam")], \
        f"absolute-path key did not match: consumed={consumed}, resp_unmatched={resp.get('output_types_unmatched')}"
    assert "output_types_unmatched" not in resp


@pytest.mark.integration
def test_basename_key_matches(_mock_step):
    resp, consumed = _mock_step({"out.bam": "bam"})
    assert consumed == [("out.bam", "bam")]
    assert "output_types_unmatched" not in resp


@pytest.mark.integration
def test_dot_extension_key_matches(_mock_step):
    resp, consumed = _mock_step({".bam": "bam"})
    assert consumed == [("out.bam", "bam")]


@pytest.mark.integration
def test_extension_no_dot_key_matches(_mock_step):
    resp, consumed = _mock_step({"bam": "bam"})
    assert consumed == [("out.bam", "bam")]


@pytest.mark.integration
def test_typo_or_unmatched_key_surfaces_in_response(_mock_step):
    """A key that doesn't bind to any produced output must appear in
    `output_types_unmatched` — pre-N8 it silently failed; the response
    payload now tells the agent its key didn't take effect."""
    resp, _ = _mock_step({"typo.vcf": "vcf"})
    assert "output_types_unmatched" in resp
    assert resp["output_types_unmatched"] == ["typo.vcf"]


@pytest.mark.integration
def test_lookup_chain_precedence_absolute_beats_basename(_mock_step, tmp_path):
    """When both an absolute path AND a basename key are provided, the
    absolute path key wins (chain order 1 > 3). The other key surfaces in
    output_types_unmatched — that key did not bind to anything produced."""
    outp = tmp_path / "kind.vcf"
    abs_key = str(outp.resolve())
    resp, consumed = _mock_step({abs_key: "vcf", "kind.vcf": "json"},
                                out_relpath="kind.vcf")
    assert consumed == [("kind.vcf", "vcf")], \
        f"absolute-path key did NOT win against basename key: consumed={consumed}"
    # The basename key never matched (the absolute key shorted out first).
    assert resp.get("output_types_unmatched") == ["kind.vcf"]
