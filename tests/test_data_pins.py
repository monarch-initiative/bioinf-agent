"""
reference_rebind — is the data this production run binds the data the workflow
was sealed against?

THE HOLE. `run_production_pipeline` re-anchored exactly one pin: the ENV, by
digest. The DATA was unpinned. A pipeline validated against gencode.v44 could be
production-run against v39 and the command rendered, launched, and reported
identically — the env half of "reproducible" enforced by a content digest, the
data half not enforced at all.

TWO THINGS THE OBVIOUS IMPLEMENTATION GETS WRONG. Both were found by running the
naive version against this repo's own sealed artifacts, and each would have
shipped a check that cries wolf on correct work:

  * DIRECTION. "Is every sealed reference still bound?" false-positives on
    `rnaseq_deseq2_chr22`, where the single `reference_databases` pin
    (gencode.v44.basic.annotation.gtf.gz) is consumed by NO step — step 4 used a
    chr22-subset GTF instead. A sealed pin this run does not use is not a
    divergence.

  * UNIVERSE. On that same workflow all 16 content anchors live in
    `authored_artifacts`; `reference_databases` holds only the pin nothing
    consumed. A check scoped to references would have inspected the one artifact
    that did not matter and ignored the sixteen that did.

And a third, found the same way once those were fixed: three of the five sealed
workflows pin `locus: cluster` references, so a LOCAL observer stats a cluster
path, finds nothing, and reports DIVERGED on four artifacts sitting exactly
where they were sealed.
"""
from __future__ import annotations

import glob

import pytest
import yaml

from agent.skills import data_pins as dp

REAL_SPECS = sorted(glob.glob("env_reports/*.workflow.yaml"))


def _spec(**over) -> dict:
    return {"workflow_name": "w", **over}


# ---------------------------------------------------------------------------
# The universe.
# ---------------------------------------------------------------------------

def test_the_universe_is_every_external_source_not_just_reference_databases():
    """Seal's own composition walk unions these four. A data check that looked at
    only one of them would miss most real pins — measurably so on this repo's
    flagship workflow, where 16 of 17 anchors are authored_artifacts."""
    spec = _spec(
        test_data={"r1": "/data/reads_1.fq.gz"},
        reference_databases=[{"name": "gencode", "local_path": "/refs/g.gtf",
                              "sha256": "a" * 64, "size_bytes": 10}],
        runtime_configs=[{"name": "cfg", "path": "/cfg/app.yaml", "sha256": "b" * 64}],
        authored_artifacts=[{"role": "driver", "path": "/work/run.R", "sha256": "c" * 64}])
    anchors = dp.sealed_anchors(spec)
    assert set(anchors) == {"/data/reads_1.fq.gz", "/refs/g.gtf",
                            "/cfg/app.yaml", "/work/run.R"}
    assert {a["source"] for a in anchors.values()} == {
        "test_data", "reference_databases", "runtime_configs", "authored_artifacts"}


def test_a_relative_or_missing_path_is_not_an_anchor():
    spec = _spec(reference_databases=[{"name": "x", "local_path": None},
                                      {"name": "y"}],
                 test_data={"note": "not-a-path"})
    assert dp.sealed_anchors(spec) == {}


# ---------------------------------------------------------------------------
# The direction — bound-input → sealed-anchor, never the reverse.
# ---------------------------------------------------------------------------

def test_a_sealed_pin_this_run_does_not_use_is_not_a_divergence(tmp_path):
    """THE FALSE POSITIVE THAT KILLED THE FIRST DESIGN. The exhibit workflow pins
    a reference no step consumed; asking 'is every pin still bound?' condemns it."""
    used = tmp_path / "used.gtf"
    used.write_text("x")
    import hashlib
    sha = hashlib.sha256(b"x").hexdigest()
    spec = _spec(
        authored_artifacts=[{"role": "gtf", "path": str(used), "sha256": sha}],
        reference_databases=[{"name": "never_used", "local_path": "/refs/nope.gtf",
                              "sha256": "f" * 64}])
    r = dp.check_bound_inputs(spec, {"GTF": str(used)})
    assert r["status"] == dp.VERIFIED, r
    assert r["counts"][dp.DIVERGED] == 0


def test_an_input_the_sealed_workflow_never_recorded_is_unanchored_not_diverged(tmp_path):
    """'Nothing pins this' and 'this is the wrong artifact' are different facts and
    must not collapse into one another."""
    p = tmp_path / "mystery.bam"
    p.write_bytes(b"BAM")
    r = dp.check_bound_inputs(_spec(), {"IN": str(p)})
    assert r["findings"][0]["verdict"] == dp.UNANCHORED
    assert r["status"] == dp.UNVERIFIED


# ---------------------------------------------------------------------------
# Content — the actual rebind.
# ---------------------------------------------------------------------------

def test_swapping_the_bytes_under_a_pinned_path_is_caught(tmp_path):
    """The headline case: same path, different artifact."""
    p = tmp_path / "genome.fa"
    p.write_text(">v44\n")
    import hashlib
    sealed = hashlib.sha256(b">v44\n").hexdigest()
    spec = _spec(reference_databases=[{"name": "genome", "local_path": str(p),
                                       "sha256": sealed}])
    assert dp.check_bound_inputs(spec, {"REF": str(p)})["status"] == dp.VERIFIED

    p.write_text(">v39\n")                       # the rebind
    r = dp.check_bound_inputs(spec, {"REF": str(p)})
    assert r["status"] == dp.DIVERGED
    assert "different artifact" in r["findings"][0]["reason"]
    assert r["findings"][0]["observed_sha256"] != sealed


def test_a_pinned_path_that_vanished_is_a_divergence(tmp_path):
    spec = _spec(reference_databases=[{"name": "g", "local_path": str(tmp_path / "gone.fa"),
                                       "sha256": "a" * 64}])
    r = dp.check_bound_inputs(spec, {"REF": str(tmp_path / "gone.fa")})
    assert r["status"] == dp.DIVERGED
    assert "not on disk" in r["findings"][0]["reason"]


def test_an_anchor_with_no_recorded_hash_cannot_be_compared(tmp_path):
    """Conservatism, copied from `versions_diverge`: a report crying a false
    mismatch is itself a lie. No anchor ⇒ unverified, never a verdict."""
    p = tmp_path / "x.txt"
    p.write_text("hi")
    spec = _spec(authored_artifacts=[{"role": "x", "path": str(p)}])
    r = dp.check_bound_inputs(spec, {"X": str(p)})
    assert r["findings"][0]["verdict"] == dp.UNVERIFIED
    assert "no content hash" in r["findings"][0]["reason"]


# ---------------------------------------------------------------------------
# Locus — an absolute path only means something where it was recorded.
# ---------------------------------------------------------------------------

def test_a_cluster_anchor_checked_locally_is_unverified_not_diverged():
    """Without this guard a local observer condemns four artifacts on
    talos_validate_moi and one on cluster_refdata_validation, all of which are
    exactly where they were sealed. Wrong namespace, not wrong bytes."""
    spec = _spec(reference_databases=[{"name": "g", "locus": "cluster",
                                       "local_path": "/work/users/x/g.fa",
                                       "sha256": "a" * 64}])
    r = dp.check_bound_inputs(spec, {"REF": "/work/users/x/g.fa"}, locus="local")
    assert r["findings"][0]["verdict"] == dp.UNVERIFIED
    assert "different namespace" in r["findings"][0]["reason"]
    assert r["findings"][0]["exists"] is None, "we did not look; don't imply we did"


def test_the_cluster_sidecar_makes_the_remote_check_more_than_existence():
    """Existence-only would be near-vacuous. The `<path>.source.sha256` sidecar is
    a `head -n1`, so content IS comparable at the cluster locus without ever
    hashing a reference on a head node."""
    spec = _spec(reference_databases=[{"name": "g", "locus": "cluster",
                                       "local_path": "/work/g.fa", "sha256": "a" * 64}])
    kw = dict(locus="cluster", remote_presence={"/work/g.fa": True})
    assert dp.check_bound_inputs(spec, {"R": "/work/g.fa"},
                                 remote_sha256={"/work/g.fa": "a" * 64},
                                 **kw)["status"] == dp.VERIFIED
    assert dp.check_bound_inputs(spec, {"R": "/work/g.fa"},
                                 remote_sha256={"/work/g.fa": "b" * 64},
                                 **kw)["status"] == dp.DIVERGED
    # no sidecar ⇒ say so, don't pass
    r = dp.check_bound_inputs(spec, {"R": "/work/g.fa"}, remote_sha256={}, **kw)
    assert r["status"] == dp.UNVERIFIED and "sidecar" in r["findings"][0]["reason"]


def test_a_cluster_path_that_is_gone_is_a_divergence():
    spec = _spec(reference_databases=[{"name": "g", "locus": "cluster",
                                       "local_path": "/work/g.fa", "sha256": "a" * 64}])
    r = dp.check_bound_inputs(spec, {"R": "/work/g.fa"}, locus="cluster",
                              remote_presence={"/work/g.fa": False})
    assert r["status"] == dp.DIVERGED


def test_an_unreachable_cluster_is_not_a_missing_file():
    """`exists: None` means we could not look. Converting that into a finding is
    the absent-vs-unchecked collapse this codebase keeps having to relearn."""
    spec = _spec(reference_databases=[{"name": "g", "locus": "cluster",
                                       "local_path": "/work/g.fa", "sha256": "a" * 64}])
    r = dp.check_bound_inputs(spec, {"R": "/work/g.fa"}, locus="cluster",
                              remote_presence={"/work/g.fa": None})
    assert r["status"] == dp.UNVERIFIED
    assert r["counts"][dp.DIVERGED] == 0


# ---------------------------------------------------------------------------
# Against the real artifacts on disk.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec_path", REAL_SPECS)
@pytest.mark.parametrize("locus", ["local", "cluster"])
def test_no_sealed_workflow_condemns_its_own_inputs(spec_path, locus):
    """THE REGRESSION GUARD, and the test that found both design errors. Re-bind
    each sealed workflow's own step-1 inputs and demand the checker not call them
    diverged — they are, by construction, exactly what it was sealed against."""
    spec = yaml.safe_load(open(spec_path))
    steps = spec.get("pipeline_steps") or []
    if not steps:
        pytest.skip("no steps")
    bound = {f"IN{i}": inp["path"]
             for i, inp in enumerate(steps[0].get("inputs") or [])}
    if not bound:
        pytest.skip("step 1 declares no inputs")
    presence = {p: True for p in bound.values()} if locus == "cluster" else None
    r = dp.check_bound_inputs(spec, bound, locus=locus, remote_presence=presence)
    diverged = [f for f in r["findings"] if f["verdict"] == dp.DIVERGED]
    assert not diverged, (
        f"{spec_path} at the {locus} locus: the checker condemns "
        f"{[f['path'] for f in diverged]}, which is what it was sealed with")


def test_the_flagship_workflow_really_does_verify_by_content():
    """The complement of the guard above: proof it is not vacuously green. This
    one has 16 authored anchors still on disk and must MATCH them, by sha256."""
    path = "env_reports/rnaseq_deseq2_chr22.workflow.yaml"
    spec = yaml.safe_load(open(path))
    step1 = (spec.get("pipeline_steps") or [])[0]
    bound = {f"IN{i}": inp["path"] for i, inp in enumerate(step1.get("inputs") or [])}
    r = dp.check_bound_inputs(spec, bound)
    assert r["status"] == dp.VERIFIED
    assert r["counts"][dp.MATCH] >= 10, r["counts"]
    assert all(f["observed_sha256"] for f in r["findings"]), \
        "a 'match' with no observed hash would be a claim, not an observation"
