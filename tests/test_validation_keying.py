"""
Validation records are keyed by PATH, not basename.

A step's `validation` dict is the evidence I3 refuses on. It was keyed by
`Path(output).name`, so two outputs of one step that share a basename — the
ordinary shape of a per-sample fan-out — collided on one key and the second
write silently destroyed the first.

That is not a cosmetic loss. I3's `validation_passed` clause is deliberately
NON-OVERRIDABLE ("evidence that exists and says FAILED cannot be un-failed by an
assertion, only hidden by one"), and the collision was a way to hide it that
required no assertion at all: whenever the failing output was validated first,
its record was gone by seal time.

Reproduced before the fix: one step, `/out/a/result.bam` (truncated → passed
False) and `/out/b/result.bam` (passed True). The runtime recorded a single
`{"passed": true}` and `check_workflow_invariants` returned no I3 violation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.skills import spec_writer
from agent.skills.pipeline_state import PipelineState, validation_covers, validation_key


@pytest.fixture
def store(tmp_path):
    """A PipelineState writing entirely inside tmp_path (see tests/conftest.py's
    repo-leakage guard)."""
    from agent import mcp_server as _ms
    (tmp_path / "drafts").mkdir(exist_ok=True)
    (tmp_path / "reports").mkdir(exist_ok=True)
    return PipelineState({**_ms.config,
                          "paths": {**_ms.config.get("paths", {}),
                                    "drafts_dir": str(tmp_path / "drafts"),
                                    "pipelines_dir": str(tmp_path / "reports")}})


def _step(outputs, validation=None, **over):
    s = {
        "step": 1, "tool": "samtools", "command": "samtools view -b in.bam",
        "returncode": 0,
        "inputs": [{"path": "/data/in.bam", "source": "test_data"}],
        "detected_outputs": list(outputs),
        "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 10.0, "peak_cpu_percent": 5.0},
    }
    if validation is not None:
        s["validation"] = validation
    s.update(over)
    return s


def _i3(step) -> list[str]:
    return sorted(v["invariant"] for v in
                  spec_writer.check_workflow_invariants({"pipeline_steps": [step]})
                  if v["invariant"].startswith("I3"))


# ---------------------------------------------------------------------------
# The store no longer loses a record.
# ---------------------------------------------------------------------------

def test_same_basename_outputs_keep_separate_records(tmp_path, store):
    """THE BUG. Two outputs, same name, different directories: both records survive,
    and the failing one is still there at seal time."""
    ps = store
    pid = ps.start("p", "d")["pipeline_id"]
    a, b = tmp_path / "a" / "result.bam", tmp_path / "b" / "result.bam"
    for p in (a, b):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    ps.add_step(pid, _step([str(a), str(b)]))

    ps.add_validation(pid, 1, str(a), {"passed": False, "error": "truncated BAM"})
    ps.add_validation(pid, 1, str(b), {"passed": True})

    val = ps.get_draft(pid)["pipeline_steps"][0]["validation"]
    assert len(val) == 2, f"a record was overwritten: {val}"
    assert val[validation_key(str(a))]["passed"] is False
    assert val[validation_key(str(b))]["passed"] is True


def test_the_erased_failure_now_reaches_i3(tmp_path):
    """End of the chain: because both records survive, the non-overridable
    `I3.validation_passed` clause sees the failure and the seal is refused."""
    a, b = "/out/a/result.bam", "/out/b/result.bam"
    assert "I3.validation_passed" in _i3(_step([a, b], {
        validation_key(a): {"passed": False, "error": "truncated BAM"},
        validation_key(b): {"passed": True}}))

    # ...and the pre-fix shape (one basename key, PASS last) is exactly the silence
    # that motivated this. Kept as a written-down record of the defect.
    assert _i3(_step([a, b], {"result.bam": {"passed": True}})) == []


def test_revalidating_the_same_file_updates_rather_than_duplicates(tmp_path, store):
    """Resolution means `./out/x.bam` and the absolute path are ONE key, so a re-run
    replaces its own record instead of accumulating a second, stale one."""
    ps = store
    pid = ps.start("p", "d")["pipeline_id"]
    f = tmp_path / "out" / "x.bam"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")
    ps.add_step(pid, _step([str(f)]))

    ps.add_validation(pid, 1, str(f), {"passed": False})
    ps.add_validation(pid, 1, f"{tmp_path}/out/./x.bam", {"passed": True})
    val = ps.get_draft(pid)["pipeline_steps"][0]["validation"]
    assert len(val) == 1 and list(val.values())[0]["passed"] is True


# ---------------------------------------------------------------------------
# Coverage: the read side is per-path, and legacy records still work.
# ---------------------------------------------------------------------------

def test_one_output_is_not_covered_by_a_different_file_of_the_same_name():
    """The read-side half of the same defect: `Path(o).name in keys` let
    `/out/a/result.bam` count as validated because `/out/b/result.bam` had a record."""
    a, b = "/out/a/result.bam", "/out/b/result.bam"
    assert "I3.outputs_validated" in _i3(_step([a, b], {validation_key(b): {"passed": True}}))


def test_legacy_basename_keyed_records_still_validate():
    """Drafts and sealed specs written before path keys are keyed by basename. They keep
    working — an invariant must not start failing over a naming change the record had no
    say in ([[feedback-existing-installs-not-precious]] covers re-earning an artifact, not
    retroactively condemning one)."""
    assert _i3(_step(["/out/a/result.bam"], {"result.bam": {"passed": True}})) == []


@pytest.mark.parametrize("key,path,covered", [
    ("/out/a/x.bam", "/out/a/x.bam", True),      # canonical
    ("x.bam",        "/out/a/x.bam", True),      # legacy basename
    ("/out/b/x.bam", "/out/a/x.bam", False),     # different file, same name
    ("y.bam",        "/out/a/x.bam", False),     # unrelated
])
def test_validation_covers_truth_table(key, path, covered):
    assert validation_covers({key: {"passed": True}}, path) is covered


def test_validation_covers_is_false_for_an_empty_record():
    assert validation_covers({}, "/out/x.bam") is False
    assert validation_covers(None, "/out/x.bam") is False


def test_validation_key_survives_an_unresolvable_path():
    """A broken symlink or an odd mount must not raise inside the store — the key
    degrades to the string it was given rather than losing the record entirely."""
    assert validation_key("relative/x.bam").endswith("relative/x.bam")
    assert validation_key("\0bad") == "\0bad"


def test_the_store_owns_the_key_so_a_caller_cannot_get_it_wrong(tmp_path, store):
    """Five call sites used to compute this key themselves and all five chose the
    basename. `add_validation` normalizes it now, so a caller passing a raw path, a
    relative path, or a bare name all land where the reader looks."""
    ps = store
    pid = ps.start("p", "d")["pipeline_id"]
    f = tmp_path / "x.bam"
    f.write_bytes(b"x")
    ps.add_step(pid, _step([str(f)]))
    ps.add_validation(pid, 1, str(f), {"passed": True})
    val = ps.get_draft(pid)["pipeline_steps"][0]["validation"]
    assert list(val) == [validation_key(str(f))]
    assert Path(list(val)[0]).is_absolute()
