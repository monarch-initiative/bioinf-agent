"""
Three Layer-2 clauses that passed without checking anything.

Each was reproduced against the live checker before being fixed, and each has the
same shape: the clause ran, found nothing to object to, and its silence was read
as a verdict.

  I4  `usage.outputs == []` — the trial is judged on its outputs, so with none
      declared both judgement loops iterate zero times and the trial returns
      ok=True having established only that the command exited 0. `touch` passes.
  I8  the "suffix" fallback was an ANY-DIRECTORY basename match: it refused the
      sidecar case its own comment described, and accepted an input anywhere on
      the filesystem that merely shared a name with some prior output.
  validated == shipped — the step stamped the EnvCache's NOMINAL digest, and the
      seal earned the badge by comparing it to the same record's digest. The
      codebase's headline claim compared a value to itself.
"""
from __future__ import annotations

import pytest

from agent.skills import spec_writer as sw


# ---------------------------------------------------------------------------
# I8 — sidecar tolerance, done as stated.
# ---------------------------------------------------------------------------

def _two_step(step2_input: str, *, locus: str = "") -> dict:
    """Step 1 produces /run1/sample.bam from test data; step 2 consumes something."""
    def _s(n, ins, outs):
        return {"step": n, "tool": f"t{n}", "command": f"c{n}", "returncode": 0,
                "inputs": [{"path": p, "source": "x"} for p in ins],
                "detected_outputs": outs,
                "validation": {o: {"passed": True} for o in outs},
                "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 1.0,
                                   "peak_cpu_percent": 1.0}}
    s2 = _s(2, [step2_input], ["/run1/out.vcf"])
    if locus:
        s2["validation_locus"] = locus
    return {"pipeline_steps": [_s(1, ["/data/in.fq"], ["/run1/sample.bam"]), s2],
            "test_data": {"r1": "/data/in.fq"}}


def _i8(spec) -> list[str]:
    return [v["invariant"] for v in sw.check_workflow_invariants(spec)
            if v["invariant"].startswith("I8")]


@pytest.mark.parametrize("path", [
    "/run1/sample.bam",          # the prior output itself
    "/run1/sample.bam.bai",      # the sidecar the old comment named — and REFUSED
    "/run1/sample.bam.csi",
    "/run1/sample.bam.gz",       # the other case the old comment named
])
def test_i8_accepts_a_prior_output_and_its_companions(path):
    assert _i8(_two_step(path)) == [], path


@pytest.mark.parametrize("path,why", [
    ("/somewhere/else/entirely/sample.bam",
     "THE HOLE: an unrelated tree sharing only a basename traced cleanly to /run1/sample.bam"),
    ("/run1/nope.bam.bai",
     "a sidecar is only tolerated when its PARENT is a real prior output"),
    ("/run1/never_produced.vcf",
     "a plain orphan"),
])
def test_i8_refuses_an_input_with_no_producing_source(path, why):
    assert "I8.composition_coherence" in _i8(_two_step(path)), why


# ---------------------------------------------------------------------------
# I8 — cross-locus staging: what the old fallback was ACTUALLY load-bearing for.
# ---------------------------------------------------------------------------

def test_a_cluster_step_may_consume_the_staged_copy_of_a_declared_external_source():
    """A cluster step's input lives under the compute env's scratch; every declared
    external source is recorded as a LOCAL path. Those two absolute paths are in
    different namespaces, so comparing them is a category error and the basename is the
    strongest handle the record carries.

    This is a real, sealed, proven flow — `samtools_cluster_rung3.workflow.yaml` consumes
    `/work/.../CLAUDE_SCRATCH/.../airway_..._R1.fastq.gz`, the upload of the local
    `data/core_test_data_hg38/.../airway_..._R1.fastq.gz`. Removing the unscoped basename
    fallback broke it, which is how the fallback's real purpose was found."""
    staged = "/work/users/x/SCRATCH/proj/wf/in.fq"
    assert _i8(_two_step(staged, locus="cluster")) == []


def test_the_staging_allowance_does_not_extend_to_a_host_step():
    """Within one filesystem, paths ARE comparable, so there is nothing to excuse. This
    is the half of the old rule that was pure hole."""
    assert "I8.composition_coherence" in _i8(_two_step("/elsewhere/in.fq"))


def test_the_staging_allowance_matches_external_sources_only_not_prior_outputs():
    """Two steps on the same cluster share a filesystem, so their paths compare directly
    and need no fallback. Widening the allowance to prior outputs would reopen the hole
    one locus over."""
    assert "I8.composition_coherence" in _i8(
        _two_step("/work/users/x/SCRATCH/proj/wf/sample.bam", locus="cluster"))


@pytest.mark.parametrize("step,off_host", [
    ({"validation_locus": "cluster"}, True),
    ({"resource_usage": {"locus": "cluster"}}, True),
    ({"validation_locus": "host"}, False),
    ({"validation_locus": "container"}, False),
    ({}, False),
    ("not a dict", False),
])
def test_off_host_is_read_from_runtime_evidence(step, off_host):
    """Read from what the runtime stamped, never from a caller-supplied flag — otherwise
    a step could claim cross-locus staging to excuse a genuine orphan."""
    assert sw._ran_off_host(step) is off_host


def test_sidecar_parent_is_full_path_not_basename():
    """The parent is looked up by FULL path. Matching on basename is what let an input
    in any directory satisfy I8, and it is the same basename-as-identity mistake that
    let a validation PASS erase a FAIL (see test_validation_keying)."""
    assert sw._sidecar_parent("/run/x.bam.bai") == "/run/x.bam"
    assert sw._sidecar_parent("/run/x.bam") is None
    assert sw._sidecar_parent(".bai") is None          # nothing left after stripping
    assert sw._sidecar_parent(None) is None


# ---------------------------------------------------------------------------
# I4 — a how-to with nothing declared is not a verified how-to.
# ---------------------------------------------------------------------------

def _usage(**over) -> dict:
    u = {"description": "d", "command_template": "tool {IN} -o {OUTPUT_DIR}/x.bam",
         "inputs": [{"name": "IN"}], "outputs": [{"name": "O", "files": ["*.bam"]}]}
    u.update(over)
    return {"usage": u, "conda_env": "bioinf_x"}


def test_empty_usage_outputs_is_not_attempted_not_verified():
    """THE HOLE. Before: ok=True, status='verified', and `usage_verified: True` reached
    the sealed spec and the run dashboard over a how-to whose results nobody checked."""
    r = sw.self_test_usage(_usage(outputs=[]), object())
    assert r["ok"] is False and r["status"] == "not_attempted"
    assert "usage.outputs is empty" in r["reason"]
    # ...and the reason must be ACTIONABLE — it names the field and the slot convention.
    assert "OUTPUT_DIR" in r["reason"]


def test_the_declaration_check_precedes_the_environment_excuses():
    """With no runner AND no declared outputs, the actionable reason is the one the
    author can fix from where they are. A missing runner is our problem; a missing
    declaration is theirs."""
    r = sw.self_test_usage({"usage": dict(_usage(outputs=[])["usage"])}, None)
    assert "usage.outputs is empty" in r["reason"], r["reason"]


def test_not_attempted_does_not_refuse_a_seal():
    """Deliberate: `not_attempted` is missing evidence, not a broken how-to. It records
    `usage_verified: False` plus a reason. Turning this hole into a refusal would have
    made legitimately unsealable-here workflows permanently unsealable."""
    assert sw.self_test_usage(_usage(outputs=[]), object())["status"] == "not_attempted"


# ---------------------------------------------------------------------------
# validated == shipped — the digest must be OBSERVED.
# ---------------------------------------------------------------------------

def test_step_records_the_observed_digest_not_the_one_it_was_handed(monkeypatch, tmp_path):
    """The badge is earned by comparing the step's digest to the frozen env's. When the
    step copies that same digest out of the same record, the comparison cannot fail —
    including in the one case it exists to catch, where the tag has been rebuilt and the
    daemon's image is no longer the one the record describes."""
    from agent import mcp_server as m

    rec = {"image": "env:latest", "image_digest": "sha256:NOMINAL", "platform": "linux-64"}
    monkeypatch.setattr(m._env_cache, "lookup_verified", lambda k: (rec, []))
    monkeypatch.setattr(m, "_check_docker_available", lambda: None)
    monkeypatch.setattr(m._locus, "daemon_is_remote", lambda: False)
    monkeypatch.setattr(m._docker, "_run", lambda *a, **k: {"returncode": 0, "stdout": "", "stderr": ""})
    # the daemon says the tag now points at DIFFERENT bytes than the record claims
    monkeypatch.setattr(m._docker, "image_digest", lambda img: "sha256:OBSERVED")
    monkeypatch.setattr(m._docker, "run_in_container",
                        lambda *a, **k: {"returncode": 0, "stdout": "", "stderr": "",
                                         "resource_usage": {"wall_seconds": 1.0}})
    monkeypatch.setattr(m._env_mgr, "hash_outputs", lambda outs: {})

    captured: dict = {}
    monkeypatch.setattr(m._pipeline_state, "add_step",
                        lambda pid, data, replace_step=None: captured.update(data) or 1)
    monkeypatch.setattr(m._pipeline_state, "get_draft", lambda pid: {"pipeline_steps": [{}]})

    m.run_step_in_container(freeze_request_key="k", command="echo hi", pipeline_id="p",
                            inputs=[], data_dir=str(tmp_path))

    assert captured["container_image_digest"] == "sha256:OBSERVED", \
        "the step must record what the daemon is about to run, not what the cache claims"
    assert captured["container_digest_nominal"] == "sha256:NOMINAL", \
        "a divergence must be RECORDED, not merely absent — absence reads as 'not checked'"


def test_an_unobservable_digest_is_recorded_as_absent_not_as_the_nominal_one(monkeypatch, tmp_path):
    """Falling back to the nominal digest when the inspect fails would restore exactly
    the tautology being removed: the badge would be earned again by copying."""
    from agent import mcp_server as m

    rec = {"image": "env:latest", "image_digest": "sha256:NOMINAL", "platform": "linux-64"}
    monkeypatch.setattr(m._env_cache, "lookup_verified", lambda k: (rec, []))
    monkeypatch.setattr(m, "_check_docker_available", lambda: None)
    monkeypatch.setattr(m._locus, "daemon_is_remote", lambda: False)
    monkeypatch.setattr(m._docker, "_run", lambda *a, **k: {"returncode": 0, "stdout": "", "stderr": ""})
    monkeypatch.setattr(m._docker, "image_digest", lambda img: "")        # inspect failed
    monkeypatch.setattr(m._docker, "run_in_container",
                        lambda *a, **k: {"returncode": 0, "stdout": "", "stderr": "",
                                         "resource_usage": {"wall_seconds": 1.0}})
    monkeypatch.setattr(m._env_mgr, "hash_outputs", lambda outs: {})
    captured: dict = {}
    monkeypatch.setattr(m._pipeline_state, "add_step",
                        lambda pid, data, replace_step=None: captured.update(data) or 1)
    monkeypatch.setattr(m._pipeline_state, "get_draft", lambda pid: {"pipeline_steps": [{}]})

    m.run_step_in_container(freeze_request_key="k", command="echo hi", pipeline_id="p",
                            inputs=[], data_dir=str(tmp_path))
    assert captured.get("container_image_digest") is None
