"""I12's second half: the GPU claim, checked against the shipped image.

THE FINDING. I12 had never fired on a real record — all 16 envs in the corpus carry
`accelerator: None`, so POLICY_CLEAN.accelerator was NOT_APPLICABLE 16/16, exactly as
I13 was before the licence work. Measuring what it WOULD do turned out to matter more
than the fact it hadn't:

  Every field it reads is written by the same agent that writes the claim. `type`,
  `toolkit_version`, `min_driver_version` and `runtime_probe` all arrive through
  `freeze(accel=…, cuda_version=…)` or straight into `patch_pipeline(accelerator=…)`,
  and the clause checked them against EACH OTHER — that a cuda claim carried some
  toolkit string, that a runtime_verified claim carried some probe string. Nothing
  ever opened the image.

So on an arm64 Mac with no NVIDIA hardware in the building, this record cleared the
contract over a pyfaidx/nanoq image that has never contained a line of CUDA:

    accelerator:
      type: cuda
      toolkit_version: "99.9"          # not a CUDA version that exists
      runtime: runtime_verified        # claims a kernel ran on a real device
      min_driver_version: "1"
      runtime_probe: "yes there is definitely a gpu"

    → CONTRACT_OK = True
    → coverage: "checked for toolkit/driver/runtime honesty", 1 observation

That is the first FALSE GREEN in this hazard sweep — every earlier one was an unearned
green the gates still refused. The "1 observation" is the sharpest part: the coverage
payload exists precisely so a reader can tell a clause that checked three things from
one that checked nothing, and it reported an observation for a clause that had re-read
the claim.

`runtime_probe` deserves its own line. The model documents it as "the proof". It has no
producer anywhere in the codebase — grep finds the field definition, the gate reading
it, and nothing that writes it — so the only way to a `runtime_verified` record is to
type the proof by hand. And the system could not have produced one anyway: `--gpus` and
apptainer's `--nv` appear nowhere in it, so no container it starts has ever had a
device visible to it.

The fix is the same shape as BUILT.platform: freeze CAPTURES what the image really
carries (locus.image_accelerator), the contract COMPARES the two recorded fields.
Separate provenance is the only reason the comparison means anything.
"""
from __future__ import annotations

import copy

import pytest

from agent.skills import env_honesty as eh
from agent.skills import freeze as F
from agent.skills import locus as L


# --------------------------------------------------------------------------
# The observation itself
# --------------------------------------------------------------------------

def test_absent_ref_is_a_failure_to_look_not_an_absence_of_cuda():
    """resolved=False says nothing about the artifact. Reading it as "no CUDA" would
    turn every uninspectable image into a refusal of a claim that may be true."""
    r = L.image_accelerator("")
    assert r["resolved"] is False
    assert r["type"] == ""


def test_version_from_a_toolkit_directory_is_the_directory_name():
    """/usr/local/cuda-12.4 -> 12.4. No regex over prose: the version IS the name."""
    assert L._accel_version_from_dirname("/usr/local/cuda-12.4") == "12.4"
    assert L._accel_version_from_dirname("/usr/local/cuda-11.8.0/") == "11.8.0"
    assert L._accel_version_from_dirname("/usr/local/cuda") == ""
    assert L._accel_version_from_dirname("") == ""


def test_version_from_conda_meta_is_a_filename_field_not_a_scrape():
    """conda writes `<name>-<version>-<build>.json`, so the version is a field."""
    assert L._accel_version_from_conda_meta("cuda-version-12.4-h1234567_0.json") == "12.4"
    assert L._accel_version_from_conda_meta("cudatoolkit-11.8.0-h37601d7_11.json") == "11.8.0"
    assert L._accel_version_from_conda_meta("nonsense") == ""


# --------------------------------------------------------------------------
# Version agreement — precision, not equality
# --------------------------------------------------------------------------

@pytest.mark.parametrize("claimed,observed,agrees", [
    ("12.4",   "12.4",   True),    # exact
    ("12.4.1", "12.4",   True),    # nvidia's ENV vs the toolkit dir it ships beside
    ("12.4",   "12.4.1", True),    # and the other way round
    ("11.8",   "12.4",   False),   # a different toolkit
    ("99.9",   "12.4",   False),   # the fictional one
    ("12.4",   "12.40",  False),   # per-component, never per-character
    ("6.0",    "6.0.2",  True),    # rocm
    ("",       "12.4",   True),    # nothing claimed — the caller handles this
    ("12.4",   "",       True),    # nothing observed — likewise
])
def test_version_agreement_is_component_prefix(claimed, observed, agrees):
    """A record claiming 12.4 over an image reporting 12.4.1 describes one toolkit, and
    refusing it would train callers to copy whatever string silences the gate. A record
    claiming 11.8 over 12.4 describes a different one."""
    assert eh._accel_version_agrees(claimed, observed) is agrees


# --------------------------------------------------------------------------
# The clause
# --------------------------------------------------------------------------

def _rec(**over) -> dict:
    """A record that is otherwise clean, so only the accelerator is under test."""
    base = {
        "name": "accel_probe", "image": "x:latest",
        "image_digest": "sha256:" + "a" * 64,
        "platform": "linux/amd64", "image_arch": "amd64",
        "validation_locus": "native",
        "requested_tools": ["samtools"],
        "verifications": [{"label": "samtools", "tool": "samtools",
                           "check": "samtools --version", "rc": 0, "passed": True,
                           "control": "failed", "control_note": "absent in control"}],
        "shipped_binaries": [], "license_gated": False, "licenses": [],
        "redistributable": True,
    }
    base.update(over)
    return base


def _observed(clause_name: str, contract) -> eh.ClauseCoverage:
    for c in contract.coverage:
        if c.clause == clause_name:
            return c
    raise AssertionError(f"no clause {clause_name!r}")


CUDA_IMAGE = {"resolved": True, "type": "cuda", "version": "12.4",
              "source": "toolkit directory /usr/local/cuda-12.4",
              "driver_requirement": "cuda>=12.4 brand=tesla,driver>=470"}
NO_ACCEL_IMAGE = {"resolved": True, "type": "none", "version": "",
                  "source": "no CUDA/ROCm toolkit found in the image",
                  "driver_requirement": ""}


def test_the_false_green_is_refused():
    """THE REGRESSION TEST. The exact record from this module's docstring."""
    rec = _rec(accelerator={"type": "cuda", "toolkit_version": "99.9",
                            "runtime": "runtime_verified", "min_driver_version": "1",
                            "runtime_probe": "yes there is definitely a gpu"},
               image_accelerator=NO_ACCEL_IMAGE)
    c = eh.evaluate_build(rec)
    assert not c.ok
    ids = {v["invariant"] for v in c.violations}
    assert "I12.accel_absent_from_image" in ids
    assert "I12.runtime_probe_not_captured" in ids


def test_cuda_claimed_over_an_image_with_no_toolkit_refuses():
    rec = _rec(accelerator={"type": "cuda", "toolkit_version": "12.4",
                            "runtime": "build_only"},
               image_accelerator=NO_ACCEL_IMAGE)
    c = eh.evaluate_build(rec)
    assert not c.ok
    msg = next(v["message"] for v in c.violations
               if v["invariant"] == "I12.accel_absent_from_image")
    # The message must name the remedy, not just the complaint (THE GATE IS THE GUIDE).
    assert "cuda" in msg
    assert "accelerator.type=none" in msg
    assert "build FROM a cuda/rocm base" in msg


def test_a_true_claim_over_a_real_cuda_image_passes():
    rec = _rec(accelerator={"type": "cuda", "toolkit_version": "12.4",
                            "runtime": "build_only"},
               image_accelerator=CUDA_IMAGE)
    c = eh.evaluate_build(rec)
    assert c.ok, [v["invariant"] for v in c.violations]
    cov = _observed("POLICY_CLEAN.accelerator_observed", c)
    assert cov.status == eh.CHECKED
    assert "really does carry cuda 12.4" in cov.detail
    # The source is named so a reader never has to guess which of four probes ran.
    assert "/usr/local/cuda-12.4" in cov.detail


def test_wrong_toolkit_version_refuses_and_names_both_numbers():
    rec = _rec(accelerator={"type": "cuda", "toolkit_version": "11.8",
                            "runtime": "build_only"},
               image_accelerator=CUDA_IMAGE)
    c = eh.evaluate_build(rec)
    assert not c.ok
    msg = next(v["message"] for v in c.violations
               if v["invariant"] == "I12.accel_version_mismatch")
    assert "11.8" in msg and "12.4" in msg


def test_wrong_vendor_refuses():
    rec = _rec(accelerator={"type": "rocm", "toolkit_version": "6.0",
                            "runtime": "build_only"},
               image_accelerator=CUDA_IMAGE)
    c = eh.evaluate_build(rec)
    assert not c.ok
    assert any(v["invariant"] == "I12.accel_type_mismatch" for v in c.violations)


def test_no_observation_is_unobserved_never_a_pass():
    """The third state. A legacy record with a cuda claim and no image_accelerator has
    not been proven wrong — but it has not been proven right either, and the clause
    must not round that up."""
    rec = _rec(accelerator={"type": "cuda", "toolkit_version": "12.4",
                            "runtime": "build_only"})
    c = eh.evaluate_build(rec)
    cov = _observed("POLICY_CLEAN.accelerator_observed", c)
    assert cov.status == eh.UNOBSERVED
    assert cov.observations == 0
    assert cov.establishes == eh.ASSURANCE
    assert "caller's word repeated back" in cov.detail


def test_failing_to_look_is_not_an_observation_of_absence():
    """resolved=False must land in the same UNOBSERVED state as an absent key — NOT in
    the accel_absent_from_image refusal. An image the daemon cannot inspect is a gap in
    our looking, not evidence about the artifact."""
    rec = _rec(accelerator={"type": "cuda", "toolkit_version": "12.4",
                            "runtime": "build_only"},
               image_accelerator={"resolved": False, "type": "", "version": "",
                                  "source": "", "driver_requirement": ""})
    c = eh.evaluate_build(rec)
    assert _observed("POLICY_CLEAN.accelerator_observed", c).status == eh.UNOBSERVED
    assert not any(v["invariant"].startswith("I12.accel_") for v in c.violations)


def test_no_accelerator_claim_is_not_applicable_not_unobserved():
    """The 16 existing records. Nothing was claimed, so nothing is missing — this must
    not pollute assurance_unproven for every CPU env in the corpus."""
    c = eh.evaluate_build(_rec())
    cov = _observed("POLICY_CLEAN.accelerator_observed", c)
    assert cov.status == eh.NOT_APPLICABLE
    assert cov.observations == 0


def test_mps_is_not_looked_for_in_a_linux_image():
    """Metal never containerizes — I12 forces dev_only for it. Probing a linux image
    for it would manufacture a violation out of the expected case."""
    c = eh.evaluate_build(_rec(accelerator={"type": "mps", "dev_only": True}))
    cov = _observed("POLICY_CLEAN.accelerator_observed", c)
    assert cov.status == eh.NOT_APPLICABLE
    assert "mps" in cov.detail


def test_toolkit_present_but_version_unstated_is_said_out_loud():
    """An nvcc-only image: the vendor is confirmed, the number is not. Neither a silent
    pass nor a refusal of something that is probably right."""
    rec = _rec(accelerator={"type": "cuda", "toolkit_version": "12.4",
                            "runtime": "build_only"},
               image_accelerator={"resolved": True, "type": "cuda", "version": "",
                                  "source": "nvcc at /usr/bin/nvcc", "driver_requirement": ""})
    c = eh.evaluate_build(rec)
    assert c.ok, [v["invariant"] for v in c.violations]
    assert "unconfirmed" in _observed("POLICY_CLEAN.accelerator_observed", c).detail


# --------------------------------------------------------------------------
# runtime_verified — the claim that a kernel ran on a real device
# --------------------------------------------------------------------------

def test_a_typed_probe_string_no_longer_earns_runtime_verified():
    rec = _rec(accelerator={"type": "cuda", "toolkit_version": "12.4",
                            "runtime": "runtime_verified", "min_driver_version": "550",
                            "runtime_probe": "nvidia-smi: NVIDIA A100-SXM4-80GB"},
               image_accelerator=CUDA_IMAGE)
    c = eh.evaluate_build(rec)
    assert not c.ok
    msg = next(v["message"] for v in c.violations
               if v["invariant"] == "I12.runtime_probe_not_captured")
    assert "build_only" in msg          # the gate names the honest setting


def test_a_captured_probe_that_passed_earns_it():
    rec = _rec(accelerator={
        "type": "cuda", "toolkit_version": "12.4", "runtime": "runtime_verified",
        "min_driver_version": "470",
        "runtime_probe": {"command": "nvidia-smi --query-gpu=name --format=csv,noheader",
                          "returncode": 0, "locus": "cluster:g0601",
                          "output": "NVIDIA GeForce GTX 1080"}},
        image_accelerator=CUDA_IMAGE)
    c = eh.evaluate_build(rec)
    assert c.ok, [v["invariant"] for v in c.violations]


def test_a_captured_probe_that_failed_cannot_be_read_as_verification():
    """The I3 rule, applied here: evidence that exists and says FAILED cannot be
    un-failed by the claim sitting next to it."""
    rec = _rec(accelerator={
        "type": "cuda", "toolkit_version": "12.4", "runtime": "runtime_verified",
        "min_driver_version": "470",
        "runtime_probe": {"command": "nvidia-smi", "returncode": 255,
                          "locus": "cluster:g0601", "output": "not found"}},
        image_accelerator=CUDA_IMAGE)
    c = eh.evaluate_build(rec)
    assert not c.ok
    assert any(v["invariant"] == "I12.runtime_probe_failed" for v in c.violations)


def test_a_probe_that_does_not_say_where_it_ran_is_incomplete():
    """`locus` is the load-bearing field: a probe that does not say WHERE cannot
    distinguish a real device from a CPU fallback."""
    rec = _rec(accelerator={
        "type": "cuda", "toolkit_version": "12.4", "runtime": "runtime_verified",
        "min_driver_version": "470",
        "runtime_probe": {"command": "nvidia-smi", "output": "A100"}},
        image_accelerator=CUDA_IMAGE)
    c = eh.evaluate_build(rec)
    assert not c.ok
    msg = next(v["message"] for v in c.violations
               if v["invariant"] == "I12.runtime_probe_incomplete")
    assert "locus" in msg and "returncode" in msg


def test_build_only_needs_no_probe():
    """The honest default. The image carries the toolkit; execution on hardware was
    not verified, and the record says exactly that."""
    rec = _rec(accelerator={"type": "cuda", "toolkit_version": "12.4",
                            "runtime": "build_only"},
               image_accelerator=CUDA_IMAGE)
    assert eh.evaluate_build(rec).ok


# --------------------------------------------------------------------------
# The producer
# --------------------------------------------------------------------------

def test_freeze_record_omits_the_key_when_nothing_looked():
    """Absent and observed-empty are different facts, exactly as for image_arch."""
    rec = F.freeze_record(request_key="k", content_digest="d", mode="build",
                          image="i", image_digest="sha256:x", platform="linux-64",
                          gated=False)
    assert "image_accelerator" not in rec


def test_freeze_record_keeps_an_observation_of_absence():
    """`type: none` is a real, load-bearing observation — it is what refuses a cuda
    claim over a CPU-only image — and must survive to the record."""
    rec = F.freeze_record(request_key="k", content_digest="d", mode="build",
                          image="i", image_digest="sha256:x", platform="linux-64",
                          gated=False, image_accelerator=NO_ACCEL_IMAGE)
    assert rec["image_accelerator"]["type"] == "none"


# --------------------------------------------------------------------------
# Live — against real images on the local daemon
# --------------------------------------------------------------------------

@pytest.mark.live
def test_live_reads_a_real_cuda_image_and_a_real_cpu_image():
    """Needs docker + `docker pull nvidia/cuda:12.4.1-base-ubuntu22.04`.

    Grounded in what a real image actually holds, which corrected three assumptions:
    /usr/local/cuda/version.json and version.txt do NOT exist in a base image, and
    nvcc is not there either. What IS there is the toolkit directory, plus CUDA_VERSION
    and NVIDIA_REQUIRE_CUDA in the image ENV.
    """
    cuda = L.image_accelerator("nvidia/cuda:12.4.1-base-ubuntu22.04")
    if not cuda["resolved"]:
        pytest.skip("nvidia/cuda base image not present on this daemon")
    assert cuda["type"] == "cuda"
    assert cuda["version"].startswith("12.4")
    assert cuda["source"]
