"""
Per-tier VERSION PROBES — the version an artifact reports about ITSELF, asked in the
shipped image.

WHY THIS FILE EXISTS. Two tiers shipped tools whose installed version rendered as `—`
in the ENV report and the build recipe, on a real frozen env (`tier_onehop`):

    Statistics::Descriptive   requested 3.0800   installed —
    BiocGenerics              requested 0.58.1   installed —

Both are real gaps in the ONE artifact the user reads to confirm they got what they
asked for. The cause was two different flavours of the same mistake:

  1. THE BANNER PROBE IS STRUCTURALLY BLIND HERE. `container_build.validate_in_image`
     captures `<tool> --version` for every tool token — which is the right question for
     a PATH command and a MEANINGLESS one for an R package or a perl module. There is
     no `BiocGenerics` binary; the probe could only ever return a shell error.
  2. THE VERSION RODE ALONG ON THE EVIDENCE COMMAND. `r_package`'s DEFAULT evidence
     printed `packageVersion()`, and a reader scraped the version back out of whatever
     the check happened to emit. But `evidence` is overridden the moment the caller
     passes a `functional_check` — which the install primitive actively encourages
     ("so validated means RAN, not merely imported"). So asking for BETTER evidence
     silently COST you the version. That is a tradeoff no user could see and none
     would choose.

The fix is the standing rule: the producer captures, the reader does not scrape. Each
tier that has its own way to interrogate an installed artifact declares it as a
`version_probe`; the probe runs in the SHIPPED image next to the evidence; the answer
is RECORDED on `shipped_binaries[].version`, which `_resolved_version` reads at rung 2
(recorded) rather than rung 3/4 (scraped).

Each test names what to break to watch it go red.
"""

from __future__ import annotations

import pytest

from agent.skills import install_commands as ic


# ── 1. the generators declare the right question ───────────────────────────────

def test_the_r_tier_asks_r_for_the_version():
    """Break it: drop `version_probe` from `r_package`'s return."""
    spec = ic.r_package("BiocGenerics", source="bioconductor")
    probe = spec["version_probe"]
    assert "packageVersion" in probe and "BiocGenerics" in probe
    assert probe.startswith("Rscript ")


def test_the_perl_tier_asks_perl_for_the_version():
    """Break it: drop `version_probe` from `perl_cpanm`'s return."""
    spec = ic.perl_cpanm("Statistics::Descriptive")
    probe = spec["version_probe"]
    assert "$Statistics::Descriptive::VERSION" in probe
    assert probe.startswith("perl -MStatistics::Descriptive ")


def test_a_functional_check_no_longer_costs_you_the_version():
    """THE REGRESSION THIS FILE EXISTS FOR. `tier_onehop` passed a real functional
    check for BiocGenerics (it dispatches `union()` and asserts the result) and got
    `installed: —` for its trouble, while a weaker env that took the default evidence
    got `0.58.1`. The probe must be INDEPENDENT of `evidence`, so the two can never
    trade off again.

    Break it: fold the probe back into the `evidence or (...)` expression."""
    functional = ('Rscript -e "library(BiocGenerics); '
                  'x <- BiocGenerics::union(c(1,2), c(2,3)); cat(\'ok\')"')
    weak = ic.r_package("BiocGenerics", source="bioconductor")
    strong = ic.r_package("BiocGenerics", source="bioconductor", evidence=functional)
    assert strong["evidence"] == functional            # the caller's check is honoured
    assert strong["evidence"] != weak["evidence"]      # ...and it really did override
    assert strong["version_probe"] == weak["version_probe"] != ""


@pytest.mark.parametrize("bad", ["foo; rm -rf /", "foo`whoami`", "foo bar", "", "2foo"])
def test_an_unsafe_name_gets_no_probe_rather_than_a_crafted_shell_line(bad):
    """The probe is synthesized into a shell line, so the name must be a plain
    identifier or there is NO probe — absence, never a crafted string. Same posture as
    `container_build._SAFE_TOOL`.

    Break it: drop the `_R_PACKAGE` / `_PERL_MODULE` guards."""
    assert ic.r_version_probe(bad) == ""
    assert ic.perl_version_probe(bad) == ""


def test_a_legitimate_dotted_r_name_and_nested_perl_name_still_probe():
    """The guards must not be so tight they refuse real names — `R.utils` and
    `Bio::SeqIO` are ordinary. Break it: tighten the patterns to `[A-Za-z]+`."""
    assert "R.utils" in ic.r_version_probe("R.utils")
    assert "Bio::SeqIO" in ic.perl_version_probe("Bio::SeqIO")


# ── 2. the probe rides the step into the build record ──────────────────────────

class _FakeSh:
    """Stands in for ContainerBuild's docker calls. Records what was run."""

    def __init__(self, responses: dict[str, tuple[int, str]]):
        self.responses, self.seen = responses, []

    def __call__(self, argv, timeout=0):
        cmd = argv[-1]
        self.seen.append(cmd)
        rc, out = self.responses.get(cmd, (0, ""))
        return {"returncode": rc, "stdout": out, "stderr": ""}


class _FakeEngine:
    name = "pixi"

    def run(self, cmd):
        return f"pixi run bash -c {cmd!r}"


def _cb():
    from agent.skills.container_build import ContainerBuild
    cb = ContainerBuild.__new__(ContainerBuild)
    cb.platform = "linux/amd64"
    cb.engine = _FakeEngine()
    cb.longtail = []
    cb.log = []
    return cb


def test_run_records_the_probe_on_the_step(monkeypatch):
    """`ContainerBuild.run` must carry `version_probe` onto the baked step — that is
    the only channel from the generator to the image-side probe.

    Break it: drop `version_probe` from `rec`."""
    cb = _cb()
    monkeypatch.setattr(cb, "exec", lambda c, timeout=0: {"returncode": 0, "stdout": "", "stderr": ""})
    monkeypatch.setattr(cb, "_emulated", lambda: False)
    r = cb.run("install it", "prove it", purpose="X (R cran)", tool="X",
               version_probe="Rscript -e 'cat(1)'")
    assert r["success"]
    assert cb.longtail[0]["version_probe"] == "Rscript -e 'cat(1)'"


def test_install_forwards_the_generators_probe(monkeypatch):
    """`install()` is the single entry point every tier goes through; a probe declared
    by a generator and dropped here would never reach the image.

    Break it: remove `version_probe=` from the `self.run(...)` call in `install`."""
    cb = _cb()
    monkeypatch.setattr(cb, "exec", lambda c, timeout=0: {"returncode": 0, "stdout": "", "stderr": ""})
    monkeypatch.setattr(cb, "_emulated", lambda: False)
    cb.install(ic.perl_cpanm("Statistics::Descriptive"))
    assert "$Statistics::Descriptive::VERSION" in cb.longtail[0]["version_probe"]


# ── 3. the probe's answer is VALIDATED, not searched ───────────────────────────

@pytest.mark.parametrize("stdout,want", [
    ("0.58.1", "0.58.1"),          # R packageVersion
    ("3.0800", "3.0800"),          # perl $VERSION — not a dotted triple, still a version
    ("1.2.3.9000", "1.2.3.9000"),  # an R devel version
    ("v2.1", "v2.1"),
    ("0.58.1\n", "0.58.1"),
    ("", None),                                    # nothing to report
    ("Loading required package: stats", None),     # prose, not a version
    ("Error in packageVersion: not found", None),
    ("bash: BiocGenerics: command not found", None),
])
def test_only_a_version_shaped_answer_is_recorded(monkeypatch, stdout, want):
    """The probe is OURS, so we know its output format and can VALIDATE it. A SEARCHING
    reader is what once reported htslib's version under bcftools' name — and would
    happily 'find' a version inside an R startup message.

    Break it: swap `_PROBE_VERSION.match` for `_extract_version`'s search."""
    cb = _cb()
    monkeypatch.setattr(cb, "_sh", _FakeSh({"probe": (0, stdout)}))
    got = cb.probe_versions("img", {"X": "probe"})
    assert got.get("X") == want


def test_a_probe_that_errors_records_nothing(monkeypatch):
    """rc != 0 means the question could not be asked. `version: None` — "not captured"
    — is the honest record; a stderr blob is not a version.

    Break it: drop the returncode check in `probe_versions`."""
    cb = _cb()
    monkeypatch.setattr(cb, "_sh", _FakeSh({"probe": (1, "0.58.1")}))
    assert cb.probe_versions("img", {"X": "probe"}) == {}


def test_probes_run_in_the_shipped_image_plain(monkeypatch):
    """Same locus and same posture as `validate_in_image`'s checks: plain exec in the
    self-activating image, which is how `apptainer exec <image> Rscript …` runs on HPC.
    A version captured in the BUILD container would not be a claim about the artifact
    that ships.

    Break it: probe `self.cid` (the builder) instead of `image`."""
    cb = _cb()
    sh = _FakeSh({"probe": (0, "1.0")})
    monkeypatch.setattr(cb, "_sh", sh)
    cb.probe_versions("shipimg:tag", {"X": "probe"})
    argv = [a for a in sh.seen]
    assert argv == ["probe"]


# ── 4. …and lands on the record the human reads ────────────────────────────────

def test_envbuild_threads_the_captured_version_onto_the_step(monkeypatch):
    """`EnvBuild.verify_in_image` asks; `EnvBuild.run` writes the answer onto
    `longtail_steps[].captured_version`, which is what `freeze` reads.

    Break it: drop `captured_version` from the longtail_steps comprehension."""
    from agent.skills import env_build as eb

    class FakeCB:
        platform = "linux/amd64"
        longtail = [{"command": "c", "purpose": "BiocGenerics (R bioconductor)",
                     "tool": "BiocGenerics", "version_probe": "ask-r",
                     "runtime_packages": []}]

        def validate_in_image(self, image, checks, probe_tools=None):
            return {"success": True, "checks": {checks[0]: {"rc": 0, "out": "ok"}},
                    "banners": {}}

        def probe_versions(self, image, probes):
            assert probes == {"BiocGenerics": "ask-r"}, "the probe must be forwarded"
            return {"BiocGenerics": "0.58.1"}

    monkeypatch.setattr("agent.skills.freeze_from_image._evidence_discriminates",
                        lambda platform, ev: ("discriminating", "stub"))
    inst = eb.EnvBuild.__new__(eb.EnvBuild)
    inst.cb = FakeCB()
    inst.captured_versions = {}
    inst.verifications = [{"label": "BiocGenerics (R bioconductor)", "tool": "BiocGenerics",
                           "check": "Rscript -e 'library(BiocGenerics)'",
                           "engine_coupled": True}]
    assert inst.verify_in_image("img")["success"]
    assert inst.captured_versions == {"BiocGenerics": "0.58.1"}


def test_the_env_report_stops_saying_dash_for_an_r_package():
    """END TO END over the reader chain, on the exact shape `tier_onehop` had. The
    captured version must reach `_resolved_version` at rung 2 (RECORDED) — which is
    what turns the ENV report's `installed: —` into `installed: 0.58.1`.

    Break it: pass `version=None` in `_shipped_binary_entry` again."""
    from agent.mcp_tools.freeze_tools import _shipped_binary_entry
    from agent.skills.env_report_helpers import _resolved_version, _verif_index

    step = {"command": "pixi run bash -c 'Rscript -e ...'",
            "purpose": "BiocGenerics (R bioconductor)", "tool": "BiocGenerics",
            "captured_version": "0.58.1",
            "provenance": {"tier": "r_install", "assurance": "repo_tofu", "verified": False},
            "runtime_packages": []}
    sb = [_shipped_binary_entry(step)]
    assert sb[0]["version"] == "0.58.1"
    # the verification is a FUNCTIONAL check that prints 'ok' and no version, and the
    # banner is the shell's own error — exactly the tier_onehop record. Rung 2 wins.
    vidx = _verif_index([{"tool": "BiocGenerics", "out": "ok",
                          "banner": "bash: line 1: BiocGenerics: command not found"}])
    assert _resolved_version("BiocGenerics", None,
                             vidx.get("biocgenerics"), sb) == "0.58.1"


def test_an_uncaptured_version_is_still_stated_as_absence():
    """The other half of the contract: no probe, or an empty one, must leave `None` —
    "we did not capture it" — never an empty string dressed up as a value.

    Break it: `version=step.get("captured_version", "")`."""
    from agent.mcp_tools.freeze_tools import _shipped_binary_entry
    assert _shipped_binary_entry({"tool": "seqtk", "command": "c", "purpose": "p"})["version"] is None
    assert _shipped_binary_entry({"tool": "seqtk", "command": "c", "purpose": "p",
                                  "captured_version": "  "})["version"] is None
