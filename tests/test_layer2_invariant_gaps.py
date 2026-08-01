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
from real_inputs import real_input


# ---------------------------------------------------------------------------
# I8 — sidecar tolerance, done as stated.
# ---------------------------------------------------------------------------

#: A REAL file, because I8's test-data clause stats what the spec declares. The fixture
#: used to say `/data/in.fq`, which passed only while nothing looked.
_IN_FQ = real_input("in.fq")


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
    return {"pipeline_steps": [_s(1, [_IN_FQ], ["/run1/sample.bam"]), s2],
            "test_data": {"r1": _IN_FQ}}


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


# ---------------------------------------------------------------------------
# I10 — a clause the sealed artifact carried no data for.
#
# The same shape as the three above, one layer out. I10 ran at seal against the
# DRAFT and refused an unhealthy service correctly; the field was then dropped
# from the constructed WorkflowSpec, so seal's own self-verify — which validates
# the artifact, not the draft — re-checked the clause against nothing. A workflow
# that genuinely depends on a running service read, standalone, as one that
# depends on no service at all: the reproducibility hole the clause exists to
# close, reintroduced by the writer immediately after the clause passed.
# ---------------------------------------------------------------------------

import ast
from pathlib import Path

import yaml

from agent.models.core_data import WorkflowSpec

ROOT = Path(__file__).resolve().parents[1]


def _i10(spec) -> list[str]:
    return [v["invariant"] for v in sw.check_workflow_invariants(spec)
            if v["invariant"].startswith("I10")]


def _service(*, healthy: bool) -> dict:
    return {"type": "database", "name": "redis",
            "start_command": "redis-server", "stop_command": "redis-cli shutdown",
            "health_check_command": "redis-cli ping",
            "status": "running" if healthy else "failed",
            "health_check_log": [{"timestamp": "2026-07-31T00:00:00Z",
                                  "command": "redis-cli ping",
                                  "returncode": 0 if healthy else 1,
                                  "healthy": healthy}]}


def _minimal_spec(**over) -> dict:
    return {"workflow_name": "w", "description": "d",
            "created_at": "2026-07-31T00:00:00Z", "env_request_key": "k",
            "env_content_digest": "sha256:c", "env_image": "img@sha256:c",
            "pipeline_status": "fully_validated", **over}


def test_dropping_the_field_is_what_made_the_check_vacuous():
    """The bug, stated as an equality: the clause refuses the draft and passes the
    artifact, for no reason other than which keys the writer copied across."""
    assert _i10({"service_dependencies": [_service(healthy=False)]}), \
        "I10 must refuse a service that never came up"
    assert _i10({}) == [], \
        "…and with the field dropped it has nothing to refuse — the vacuity, exactly"


def test_the_sealed_model_carries_services_with_their_probes_intact():
    """The typed-record trap: `health_check_log` is what I10 reads, so a model that
    declared the field but dropped the log would be worse than not carrying it at
    all — every carried service would become a spurious refusal at re-verify."""
    spec = WorkflowSpec.model_validate(
        _minimal_spec(service_dependencies=[_service(healthy=True)]))
    on_disk = yaml.safe_load(spec.to_yaml())
    assert on_disk["service_dependencies"][0]["health_check_log"], \
        "the probes did not survive the typed round trip"
    assert _i10(on_disk) == []


def test_an_unhealthy_service_refuses_the_artifact_standalone():
    """The point of carrying it: the refusal now survives into the artifact, so a
    reader who never saw the draft reaches the same verdict."""
    spec = WorkflowSpec.model_validate(
        _minimal_spec(service_dependencies=[_service(healthy=False)]))
    assert _i10(yaml.safe_load(spec.to_yaml()))


#: Fields the sealed artifact must carry because its OWN invariants read them.
#: Both halves are required and neither implies the other: a model field seal
#: never populates is dead, and a key seal writes that the model drops is lost at
#: `to_yaml`. This ratchet is the join.
_CARRIED_FOR_SELF_VERIFICATION = (
    "test_data", "reference_databases", "runtime_configs",
    "authored_artifacts", "service_dependencies",
)


def test_seal_writes_every_field_the_artifact_re_verifies_against():
    """THE GENERAL RATCHET. `service_dependencies` was declared nowhere and copied
    nowhere, and nothing noticed for as long as the clause existed, because a
    missing external source cannot fail an invariant — it can only silence one.
    Adding a run-side clause that reads a new top-level field now means adding it
    here, or the build breaks."""
    tree = ast.parse((ROOT / "agent" / "mcp_tools" / "workflow_tools.py").read_text())
    written: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(getattr(t, "id", None) == "wf" for t in node.targets)):
            written |= {k.value for k in node.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    assert written, "could not find seal's WorkflowSpec dict — the AST walk has drifted"

    for field in _CARRIED_FOR_SELF_VERIFICATION:
        assert field in WorkflowSpec.model_fields, \
            f"WorkflowSpec has no '{field}' — seal may write it, but to_yaml will drop it"
        assert field in written, \
            f"seal never copies '{field}' into the spec — the artifact re-verifies " \
            f"that clause against nothing"


# ---------------------------------------------------------------------------
# usage_status — ONE reading of a three-state fact stored in two fields.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec,expected", [
    # the stated verdict always wins
    ({"usage_verification": {"status": "not_attempted"}, "usage_verified": False},
     "not_attempted"),
    ({"usage_verification": {"status": "failed"}, "usage_verified": False}, "failed"),
    # ...even against a bool that disagrees: the producer STATED one, the bool is derived
    ({"usage_verification": {"status": "verified"}, "usage_verified": False}, "verified"),
    # pre-three-state artifacts fall back to the bool
    ({"usage_verified": True}, "verified"),
    # ...and a bare False resolves to "", NOT "failed". seal REFUSES a real I4 failure,
    # so false on a sealed spec never meant the command was tested and broken. Reading it
    # as "failed" would be the same fabrication in the opposite direction.
    ({"usage_verified": False}, ""),
    ({}, ""),
    (None, ""),
])
def test_usage_status_is_one_reading_of_the_three_state_verdict(spec, expected):
    from agent.models.core_data import usage_status
    assert usage_status(spec) == expected


def test_no_rendered_artifact_prints_a_bare_usage_bool():
    """THE RATCHET behind the leaf, asserted on RENDERED OUTPUT rather than on imports.

    Both artifacts a user opens — the RUN dashboard and the markdown guide — used to
    derive this verdict independently, so one printed "not attempted — <reason>" while
    the other printed `False` about the same workflow.

    An earlier version of this test checked that each module MENTIONED usage_status. Its
    guard did not fire: reverting the guide to print the bare bool left the import in
    place and the substring check passed. A test that a name appears in a file says
    nothing about what the file renders, so this renders both artifacts and reads them.
    (This one is about DISPLAY. GATING was a separate hole and is covered by the test
    below — the parenthetical here used to say gating on the bool was "still fine",
    which held only while the two fields agreed.)"""
    from agent.skills.run_dashboard_html import render_run_dashboard_html
    from agent.skills.user_guide import render_user_guide

    spec = {
        "workflow_name": "never_tested", "description": "d",
        "created_at": "2026-01-01T00:00:00+00:00",
        "env_request_key": "fake=1.0|linux/amd64|none",
        "env_image": "fake:1.0", "env_content_digest": "sha256:" + "a" * 64,
        "usage_verified": False,
        "usage_verification": {"status": "not_attempted",
                               "reason": "no usage block was authored"},
        "pipeline_steps": [{"step": 1, "tool": "fake", "command": "fake --go",
                            "returncode": 0, "validation_status": "passed"}],
    }
    for name, page in (("RUN.html", render_run_dashboard_html(spec)),
                       ("GUIDE.md", render_user_guide(spec))):
        assert "not attempted" in page.lower(), (
            f"{name} does not state the three-state verdict for a workflow whose how-to "
            f"was never executed")
        assert "usage_verified" not in page, (
            f"{name} prints the raw field name/bool — `usage_verified: False` tells a "
            f"reader the command was TESTED and does not work, and seal refuses that "
            f"case, so it has only ever meant nobody ran it")


def test_the_guide_does_not_call_a_command_self_tested_when_the_two_usage_fields_disagree():
    """The GATING half, and the reason it is not covered by the display test above.

    `usage_verified: True` beside `usage_verification.status: "not_attempted"` is not a
    hypothetical — samtools_cluster_rung3 carries exactly that on disk, from the window
    before the producer always stated the three-state field. The guide gated its
    "(self-tested)" label on the raw bool, so for that spec it printed a command as
    self-tested while the record beside it said nobody ever ran one.

    That is the failure mode the whole honesty contract exists to prevent: the report is
    the only thing most people read, and here it would assert a verification that did
    not happen. `usage_status` treats the explicit status as authoritative and falls back
    to the bool only for specs sealed before the field existed, so it resolves the
    contradiction the way the evidence does.
    """
    from agent.skills.user_guide import render_user_guide

    contradictory = {
        "workflow_name": "disagreeing_fields", "description": "d",
        "created_at": "2026-01-01T00:00:00+00:00",
        "env_request_key": "fake=1.0|linux/amd64|none",
        "env_image": "fake:1.0", "env_content_digest": "sha256:" + "a" * 64,
        "usage_verified": True,                                   # the stale bool
        "usage_verification": {"status": "not_attempted",         # the real verdict
                               "reason": "no usage block was authored"},
        "usage": {"description": "how to run it",
                  "command_template": "fake --go {INPUT} -o {OUTPUT_DIR}/r.txt"},
        "pipeline_steps": [{"step": 1, "tool": "fake", "command": "fake --go",
                            "returncode": 0, "validation_status": "passed"}],
    }
    TEMPLATE = "fake --go {INPUT} -o {OUTPUT_DIR}/r.txt"
    page = render_user_guide(contradictory)

    # What a reader actually sees. The guide presents the command_template under
    # "the validated command sequence" and repeats it in the TL;DR — so printing it
    # is the claim, and the provenance row two screens down already said
    # `usage self-tested: not attempted` (that row reads the leaf). One page, two
    # answers, the prominent one wrong: the same contradiction the inventory row had.
    assert TEMPLATE not in page, (
        "the guide offered an unverified command_template as the validated command "
        "sequence, on the strength of the stale `usage_verified` bool, while "
        "usage_verification.status says nobody ran it")
    assert "not attempted" in page.lower(), (
        "having withheld the command, the guide must still SAY the how-to was never run")
    assert "fake --go" in page, (
        "the guide must still show what DID run — withholding the unproven how-to is "
        "not a reason to drop the executed step, which is real evidence")

    agreeing = {**contradictory,
                "usage_verification": {"status": "verified", "reason": "I4 passed"}}
    verified_page = render_user_guide(agreeing)
    assert TEMPLATE in verified_page, (
        "the fix must not silence a how-to that WAS self-tested — a gate that never "
        "opens is not an improvement on one that never closes")
    assert "usage self-tested: `yes`" in verified_page
