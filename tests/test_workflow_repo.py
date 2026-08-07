"""A repo can be a WORKFLOW rather than a tool, and until 2026-08-04 nothing could see it.

`authors_sources` walked sixteen filenames looking for a build — Dockerfile, environment.yml,
Makefile — and nf-core pipelines ship none of them. So the assessment came back "no recipe in
the repo", the registry had no entry either, and routing fell through to the one tier whose
availability test is verbatim `gh["repo_exists"]`:

    availability["synthesis"] = {"available": gh["repo_exists"], **gh}

The result was not a near-miss. `synth_fetch("https://github.com/nf-core/rnaseq")` returns
`outcome: proven` over a 41,673-char corpus whose top-ranked "most authoritative recipe" is
the CI check that PRs target `dev` instead of `master`, which also contains the pipeline's
release-announcement TWEET workflow and a `pyproject.toml` holding `[tool.black] line-length
= 120` — and which does NOT contain main.nf or nextflow.config. The agent was handed
everything about the repo except the pipeline, and asked how the pipeline installs.

Meanwhile `main.nf`, `nextflow.config` and `nextflow_schema.json` were all 200s. Every signal
saying "I am a pipeline" was on the wire; the same shape as the licence that both the registry
and the shipped image published while nothing read it.

These tests pin the three halves: the manifest is DETECTED, the detection is NARROW (a repo
that is both a workflow and installable keeps its install tiers), and the refusal it produces
names the world rather than our investigation.
"""
from __future__ import annotations

import pytest

from agent.skills import authors_sources as A
from agent.skills.authors_sources import assess_tool_sources, parse_nextflow_manifest
from agent.skills.resolver import REFUSAL_REASONS, rank_decision


# --------------------------------------------------------------------------- the parser
#: the real block, copied from nf-core/rnaseq's nextflow.config
_RNASEQ_CONFIG = """
params {
    version = 'THIS-IS-NOT-THE-MANIFEST-VERSION'
    input   = null
}

manifest {
    name            = 'nf-core/rnaseq'
    author          = \"\"\"Harshil Patel, Phil Ewels, Rickard Hammaren\"\"\"
    homePage        = 'https://github.com/nf-core/rnaseq'
    description     = \"\"\"RNA sequencing analysis pipeline\"\"\"
    mainScript      = 'main.nf'
    nextflowVersion = '!>=23.04.0'
    version         = '3.14.0'
    doi             = 'https://doi.org/10.5281/zenodo.1400710'
}

process {
    version = 'ALSO-NOT-IT'
}
"""


def test_manifest_reads_the_pipelines_own_name_and_version():
    m = parse_nextflow_manifest(_RNASEQ_CONFIG)
    assert m["name"] == "nf-core/rnaseq"
    assert m["version"] == "3.14.0"
    assert m["nextflowVersion"] == "!>=23.04.0"


def test_manifest_does_not_reach_outside_its_block():
    """The bcftools-reports-htslib's-version defect, re-run on a config file.

    `params` and `process` both declare a `version` here, and `params` comes FIRST. A regex
    that scanned the whole file would return 'THIS-IS-NOT-THE-MANIFEST-VERSION' under the
    pipeline's name — a confident lie, which is strictly worse than the empty string.
    """
    assert parse_nextflow_manifest(_RNASEQ_CONFIG)["version"] == "3.14.0"


@pytest.mark.parametrize("text", ["", "// just a comment\n", "params { x = 1 }", None])
def test_no_manifest_block_yields_no_claims(text):
    """Absence is stated as absence. The caller still has the file's PRESENCE, which is the
    load-bearing fact — a guessed declaration would add nothing and could subtract."""
    assert parse_nextflow_manifest(text) == {}


def test_a_manifest_key_present_but_empty_is_not_recorded():
    assert "version" not in parse_nextflow_manifest("manifest { version = '' }")


# --------------------------------------------------------------- discovery + the verdict
def _sources(paths: dict[str, str]):
    """A fake repo: path -> text. Injected, so no network and no monkeypatching of the
    function under test (the discipline that keeps the REAL assess path exercised)."""
    return lambda url, **kw: paths.get(url.rsplit("/HEAD/", 1)[-1])


def _assess(paths: dict[str, str], tool: str = "rnaseq"):
    return assess_tool_sources(tool, owner="nf-core", repo=tool,
                               get_text=_sources(paths),
                               get_json=lambda *a, **k: None,
                               probe_image=lambda *a, **k: None)


def test_an_nfcore_shaped_repo_is_workflow_only():
    v = _assess({"main.nf": "workflow { }", "nextflow.config": _RNASEQ_CONFIG})
    assert v["workflow_only"] is True
    assert v["workflow_manifest"]["engine"] == "nextflow"
    assert v["workflow_manifest"]["declares"]["version"] == "3.14.0"
    assert "nextflow.config" in v["workflow_manifest"]["paths"]
    assert "NOT AN INSTALLABLE TOOL" in v["recommendation"]


def test_a_snakemake_repo_is_detected_without_a_manifest_to_parse():
    """Snakemake ships no manifest block. Presence alone settles WHAT the repo is; the
    declaration is a bonus, and its absence must not un-detect the workflow."""
    v = _assess({"workflow/Snakefile": "rule all:\n    input: []\n"})
    assert v["workflow_only"] is True
    assert v["workflow_manifest"]["engine"] == "snakemake"
    assert v["workflow_manifest"]["declares"] == {}


@pytest.mark.parametrize("extra", ["Dockerfile", "environment.yml", "Makefile"])
def test_a_repo_that_is_both_keeps_its_install_tiers(extra):
    """WORKFLOW-ONLY, not workflow-ISH. A tool may legitimately ship a Snakefile for its own
    CI while still being an installable thing; withholding synthesis from it would trade one
    wrong answer for another."""
    v = _assess({"main.nf": "workflow { }", "nextflow.config": _RNASEQ_CONFIG,
                 extra: "FROM debian\nRUN apt-get install -y libfoo-dev\n"})
    assert v["workflow_manifest"] is not None, "still a workflow…"
    assert v["workflow_only"] is False, "…but not ONLY a workflow"
    assert "NOT AN INSTALLABLE TOOL" not in v["recommendation"]


def test_a_plain_tool_repo_is_untouched():
    v = _assess({"Dockerfile": "FROM debian\n"}, tool="samtools")
    assert v["workflow_manifest"] is None
    assert v["workflow_only"] is False


# ------------------------------------------------------------------------- the routing
def test_synthesis_and_source_are_withheld_deliberately_not_found_missing():
    """The distinction is the whole point of the message. `rank_decision` is pure, so this
    drives the real ranking against the availability the gate produces."""
    d = rank_decision({
        "synthesis": {"available": False, "repo_exists": True,
                      "workflow_repo": {"engine": "nextflow", "paths": ["main.nf"],
                                        "declares": {}}},
        "source": {"available": False, "repo_exists": True},
    })
    assert d["chosen"] is None
    assert "withheld deliberately, not found missing" in d["rationale"]
    assert "pass a github_repo" not in d["rationale"], (
        "the caller already passed one — advice aimed at the wrong problem is worse than "
        "none, and here it invites re-running the exact route we just ruled out")


def test_the_stock_no_tier_advice_survives_for_a_non_workflow():
    d = rank_decision({"conda": {"available": False}})
    assert d["chosen"] is None
    assert "pass a github_repo" in d["rationale"]


def test_the_refusal_names_the_world_not_our_investigation():
    """The other four reasons all describe how far WE got looking. This is the first that
    describes what the artifact IS — the gap G3 phase 1 named and left open."""
    assert "artifact_is_a_workflow" in REFUSAL_REASONS
    from agent.skills.resolver import _classify_refusal
    assert _classify_refusal({"workflow_repo": {"engine": "nextflow"}}) == \
        "artifact_is_a_workflow"


def test_an_unreached_probe_still_outranks_the_workflow_verdict():
    """A pipeline repo can ALSO be packaged, so a registry probe that never answered leaves
    that open. 'We did not finish looking' stays the strongest thing we can say."""
    from agent.skills.resolver import _classify_refusal
    assert _classify_refusal({"workflow_repo": {"engine": "nextflow"},
                              "unchecked_tiers": {"conda": "rate_limited"}}) == \
        "investigation_incomplete"


def test_the_workflow_verdict_outranks_a_bare_empty_and_a_weak_discovery():
    from agent.skills.resolver import _classify_refusal
    assert _classify_refusal({"workflow_repo": {"engine": "nextflow"},
                              "discovered_repos": [{"repo": "x/y"}]}) == \
        "artifact_is_a_workflow"
    assert _classify_refusal({}) == "investigation_empty"


# ------------------------------------------------------- the pin, end-to-end via resolve
_HEAD_ASSESSMENT = {
    "author_image": None, "authors_recipe": None, "env_specs": [], "build_scripts": [],
    "reconstruction_incomplete": False, "workflow_only": True,
    "workflow_manifest": {"engine": "nextflow", "paths": ["nextflow.config", "main.nf"],
                          # HEAD has moved on; the caller asked for 3.14.0
                          "declares": {"name": "nf-core/rnaseq", "version": "3.26.0",
                                       "nextflowVersion": "!>=25.04.3"}},
    "recommendation": "…",
}


def _resolve_workflow(monkeypatch, **kw):
    import agent.skills.resolver as R
    for fn in ("probe_conda", "probe_pypi", "probe_cran", "probe_bioconductor"):
        monkeypatch.setattr(R, fn, lambda *a, **k: {"available": False})
    monkeypatch.setattr(R, "probe_github",
                        lambda *a, **k: {"repo_exists": True, "assets": [],
                                         "has_release_assets": False, "is_fork": False,
                                         "full_name": "nf-core/rnaseq",
                                         "default_branch": "master", "tag": "3.26.0"})
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: _HEAD_ASSESSMENT)
    # A requested version sends the binary tier off to enumerate that tag's release; the
    # hermetic guard rightly refuses the socket. We only need the DECISION here.
    monkeypatch.setattr(R, "_release_for_version",
                        lambda *a, **k: {"status": "no_asset", "tag": kw.get("version", ""),
                                         "assets": []})
    monkeypatch.setattr(R, "probe_github_search",
                        lambda *a, **k: {"found": False, "candidates": []})
    return R.resolve("rnaseq", github_repo="nf-core/rnaseq", **kw)


def test_the_revision_to_run_is_the_callers_pin_not_heads(monkeypatch):
    """The somalier 0.2.15→v0.3.3 class, reproduced in the message that announces we caught
    a wrong answer — and caught before it shipped.

    The manifest is read at `ref="HEAD"` on purpose: a tag guessed wrong ('3.14.0' vs
    'v3.14.0') 404s and loses the DETECTION, which matters more than the figure. That makes
    HEAD's `version` an observation of the default branch, and spending it as the `-r`
    operand hands a caller who asked for 3.14.0 a command that runs 3.26.0.
    """
    d = _resolve_workflow(monkeypatch, version="3.14.0")
    assert d["refusal_reason"] == "artifact_is_a_workflow"
    assert "-r 3.14.0`" in d["rationale"], "the run command must carry the CALLER's pin"
    assert "-r 3.26.0" not in d["rationale"]
    # …and HEAD's figure is still reported, labelled as HEAD's rather than dropped.
    assert "3.26.0 on its default branch" in d["rationale"]


def test_with_no_pin_requested_heads_revision_is_offered(monkeypatch):
    d = _resolve_workflow(monkeypatch)
    assert "-r 3.26.0`" in d["rationale"]


def test_the_walk_actually_requests_the_manifest_paths():
    """A detection that never fetches the file cannot fire. Pins the four filenames as a
    behaviour, so deleting one from the tuple fails here rather than silently in production
    eighteen months later."""
    asked: list[str] = []

    def spy(url, **kw):
        asked.append(url.rsplit("/HEAD/", 1)[-1])
        return None

    A.discover_authors_sources("nf-core", "rnaseq", get_text=spy,
                               get_json=lambda *a, **k: None,
                               probe_image=lambda *a, **k: None)
    for want in ("main.nf", "nextflow.config", "Snakefile", "workflow/Snakefile"):
        assert want in asked, f"the discovery walk never looks for {want}"


# ------------------------------------------------------- the gate AT THE TOOL, not the router
#
# Everything above this line tests the ROUTER. The module docstring has narrated the
# `synth_fetch` failure since the day it was written, and no test drove it — so the fix
# landed on one side of the door and the prose described the other. Measured 2026-08-07
# against the live repo, at the commit those paragraphs were written for:
#
#     synth_fetch("https://github.com/nf-core/rnaseq")  ->  outcome: proven
#         19 files, 66,816 chars, top-ranked recipe `.devcontainer/setup.sh`,
#         main.nf and nextflow.config both absent from the corpus
#     assess_tool_sources("rnaseq", owner="nf-core", repo="rnaseq")  ->  workflow_only: True
#
# Same repo, same session: the verdict existed and the tool that needed it read nothing.
# `resolve()` meanwhile returned `available: []`, `chosen: None`,
# `refusal_reason: artifact_is_a_workflow` — the router is genuinely well protected, which
# is what made the tool-level hole the only door left open.
#
# These drive the TOOLS. Network is injected, so they run in the normal suite.

def _workflow_assessment(**over):
    """A stand-in for `assess_tool_sources` that answers as it does for nf-core/rnaseq."""
    def _fn(tool, **kw):
        return {"workflow_only": True,
                "workflow_manifest": {"engine": "nextflow",
                                      "paths": ["nextflow.config", "main.nf"],
                                      "declares": {"name": "nf-core/rnaseq",
                                                   "version": "3.14.0"}},
                "recommendation": "THIS REPO IS A NEXTFLOW WORKFLOW, NOT AN INSTALLABLE TOOL",
                **over}
    return _fn


def test_the_gate_reports_workflow_only_for_a_pipeline_repo():
    g = A.workflow_gate("https://github.com/nf-core/rnaseq", assess=_workflow_assessment())
    assert g["state"] == "workflow_only"
    assert g["manifest"]["engine"] == "nextflow"


def test_the_gate_reports_installable_for_a_tool_repo():
    g = A.workflow_gate("https://github.com/samtools/samtools",
                        assess=_workflow_assessment(workflow_only=False))
    assert g["state"] == "installable"


@pytest.mark.parametrize("url", [
    "https://gitlab.com/owner/repo",
    "https://bitbucket.org/owner/repo",
    "https://example.org/releases/tool-1.2.tar.gz",
])
def test_a_non_github_source_is_unchecked_and_never_silently_installable(url):
    """THREE STATES, NOT TWO. The manifest walk reads raw.githubusercontent.com and nothing
    else, so for a GitLab / self-hosted / archive source there is no verdict to be had.
    Returning `installable` there would report an unrun check as a clean bill of health —
    the absence-rounded-up-into-a-verdict shape refused everywhere else in this codebase."""
    g = A.workflow_gate(url, assess=_workflow_assessment())
    assert g["state"] == "unchecked"
    assert "NOT checked" in g["reason"]


def test_a_probe_that_raises_is_unchecked_not_installable():
    def _boom(*a, **k):
        raise RuntimeError("network is down")
    g = A.workflow_gate("https://github.com/nf-core/rnaseq", assess=_boom)
    assert g["state"] == "unchecked"
    assert "RuntimeError" in g["reason"]


def test_synth_fetch_refuses_a_workflow_repo(monkeypatch):
    """THE TOOL, not the router. This is the call the docstring above narrates."""
    from agent.mcp_tools import env_tools
    monkeypatch.setattr(A, "assess_tool_sources", _workflow_assessment())

    def _must_not_fetch(*a, **k):
        raise AssertionError("synth_fetch cloned the repo before consulting the gate")
    monkeypatch.setattr(env_tools._ms._env_mgr, "fetch_build_source", _must_not_fetch)

    r = env_tools.synth_fetch("https://github.com/nf-core/rnaseq")
    assert r["outcome"] == "refused"
    assert r["code"] == "install.synth_artifact_is_a_workflow"
    assert r["workflow_repo"]["engine"] == "nextflow"
    assert "NOT AN INSTALLABLE TOOL" in r["error"]


def test_synth_build_refuses_a_workflow_repo_independently(monkeypatch):
    """synth_build is gated SEPARATELY on purpose: an agent can call it without ever calling
    synth_fetch, or holding a corpus fetched before the gate existed. It is the call that
    WRITES an install_method the freeze then replays into the shipped image, so it is the one
    that must not be skippable."""
    from agent.mcp_tools import env_tools
    monkeypatch.setattr(A, "assess_tool_sources", _workflow_assessment())

    def _must_not_fetch(*a, **k):
        raise AssertionError("synth_build re-fetched before consulting the gate")
    monkeypatch.setattr(env_tools._ms._env_mgr, "fetch_build_source", _must_not_fetch)

    r = env_tools.synth_build(
        env_name="x", repo_url="https://github.com/nf-core/rnaseq", tool_name="rnaseq",
        commands=[{"command": "nextflow run nf-core/rnaseq", "source": "agent_authored"}],
        evidence="command -v nextflow")
    assert r["outcome"] == "refused"
    assert r["code"] == "install.synth_artifact_is_a_workflow"


def test_a_tool_repo_still_fetches_and_says_the_gate_ran(monkeypatch):
    """The gate must not become a wall. A normal repo proceeds — and the corpus SAYS the
    check ran, so `installable` is a statement rather than the silence of a check nobody
    performed."""
    from agent.mcp_tools import env_tools
    monkeypatch.setattr(A, "assess_tool_sources", _workflow_assessment(workflow_only=False))
    monkeypatch.setattr(env_tools._ms._env_mgr, "fetch_build_source",
                        lambda *a, **k: {"success": True, "files": [
                            {"path": "Dockerfile", "text": "FROM debian\n"}]})
    r = env_tools.synth_fetch("https://github.com/samtools/samtools")
    assert r["success"] is True
    assert r["workflow_gate"]["state"] == "installable"


def test_validate_submission_refuses_the_origin_file_shape_synth_fetch_returns():
    """`synth_fetch`'s `ranked_sources` is `[{category, path}]`, and both synth docstrings
    say to "pass its path as origin_file". An agent that passed the ENTRY instead of its
    `path` got `TypeError: unhashable type: 'dict'` out of the validator — a traceback reads
    as a bug in the runtime, not as a mistake the caller can fix. Refuse, and name the fix."""
    from agent.skills import synthesis
    out = synthesis.validate_submission(
        {"files": [{"path": "install.sh", "text": "make install\n", "sha256": "x"}]},
        [{"command": "make install", "source": "extracted",
          "origin_file": {"category": "install_script", "path": "install.sh"}}])
    assert out["ok"] is False
    assert "must be the file's PATH" in out["violations"][0]["reason"]
