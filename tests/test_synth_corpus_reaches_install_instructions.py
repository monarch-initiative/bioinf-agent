"""The synthesis corpus excluded the file holding the install instructions — and said
otherwise.

WHAT WENT WRONG (sea trial S4a, measured against a real academic repo at a pinned tag):

    synth_fetch("…/Genome_Assembly_Booster", ref="v1")
      -> outcome: proven, corpus_chars: 6302
      -> ranked_sources: [{category: "readme", path: "README.md"}]      # ONE file

The repo's tree at that exact commit contains `HIC_ASSEMBLER/packageInstallCommands.txt`
— every conda line the tool needs, and the file the README explicitly points at:
*"installing the relevant packages provided in the packageInstallCommands.txt file"*.
It matched none of the six predicates, so it was not fetched.

The cost is reachability, not honesty — `synth_build` re-verifies against these bytes, so
an `extracted` command has no file entry to anchor to and is REFUSED, and an
`agent_authored` one is graded against a corpus that is just the README. It fails safe.
But the class it locks out — academic repos whose install steps live in a plain
`packageInstallCommands.txt` / `INSTALL` / `requirements.txt` / `environment.yml` — is
exactly the long tail this tier exists to serve.

AND THE DOCSTRING PROMISED THE CATEGORY THE CODE LACKED. `is_build_relevant` read:

    "Should synth_fetch pull this file? The build sources above PLUS the files where
     install URLs/instructions live (so the grounding corpus is complete)."

There was no PLUS. A second, hand-written account of something the machine already
knows, drifting from it — sitting on the function whose entire job is corpus
completeness. So the fix is two-sided: the predicates gain the category, and the account
of the categories is GENERATED from the predicate table, into the tool description the
model is actually served. The tests below hold both halves.
"""
from __future__ import annotations

import asyncio

import pytest

from agent.skills import synthesis as s


# ---------------------------------------------------------------------------
# the category that was missing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "HIC_ASSEMBLER/packageInstallCommands.txt",   # the S4a specimen, verbatim
    "INSTALL",
    "INSTALL.md",
    "docs/INSTALLATION.rst",
    "install_commands.txt",
])
def test_a_file_that_holds_install_instructions_is_in_the_corpus(path):
    """The whole class, not the one file. Nothing here keys on a tool or repo name."""
    assert s.is_build_relevant(path), f"{path} would not be fetched"


@pytest.mark.parametrize("path", [
    "requirements.txt", "requirements-dev.txt",
    "environment.yml", "env.yaml", "conda_env.yml",
    "DESCRIPTION",                                 # the R/Bioconductor dependency list
])
def test_a_declarative_dependency_list_is_in_the_corpus(path):
    """For a repo whose "build" is a conda env, WHAT to install is most of the job, and
    an `agent_authored` command is grounded against exactly these bytes."""
    assert s.is_build_relevant(path), f"{path} would not be fetched"


@pytest.mark.parametrize("path", [
    "src/installer.cpp",       # source code that merely has the word in its name
    "python/install_hooks.py",
    "scripts/uninstall.sh",    # the opposite instruction
    "data/sample.bam",
    "src/main.c",
])
def test_the_widening_does_not_swallow_the_tree(path):
    """`is_build_relevant` keeps the fetch to the build surface. A name-contains rule
    with no extension gate would pull source files into the grounding corpus, which
    makes an `agent_authored` command easier to ground on text that is not an
    instruction to anyone."""
    assert not s.is_build_relevant(path), f"{path} should not be fetched"


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------

def test_install_notes_outrank_the_readme_that_points_at_them():
    """S4a's README says "the packages provided in packageInstallCommands.txt". The
    file it defers to must not rank below it."""
    ranked = s.rank_build_sources(["README.md", "HIC_ASSEMBLER/packageInstallCommands.txt"])
    cats = [r["category"] for r in ranked]
    assert cats.index("install_notes") < cats.index("readme")


def test_a_dockerfile_still_beats_everything(tmp_path):
    ranked = s.rank_build_sources(
        ["README.md", "INSTALL.md", "requirements.txt", "Dockerfile", "Makefile"])
    assert ranked[0]["category"] == "dockerfile"
    assert ranked[-1]["category"] == "readme"


def test_a_path_is_ranked_once_at_its_best_category():
    """`install.sh` is an install_script and also matches install_notes by name. A file
    listed twice in a ranking reads as two different files to whoever is scanning it for
    what to extract from."""
    ranked = s.rank_build_sources(["install.sh"])
    assert [r["category"] for r in ranked] == ["install_script"]


# ---------------------------------------------------------------------------
# the docstring half: ONE account of the categories, and it is generated
# ---------------------------------------------------------------------------

def _served_synth_fetch() -> str:
    from agent.mcp_server import mcp
    tools = {t.name: (t.description or "") for t in asyncio.run(mcp.list_tools())}
    return tools["synth_fetch"]


def test_the_served_contract_carries_the_generated_category_list():
    """THE load-bearing test, and the same shape as `test_tool_surface`'s guardrail
    check: the substitution happens by reaching into a decorator chain, and if it ever
    stops happening it stops SILENTLY — the server starts, the tool works, and the model
    is served a description with a raw `{BUILD_SOURCES}` in it or, worse, a stale list
    someone pasted back in. Read the description the model actually receives."""
    desc = _served_synth_fetch()
    assert "{BUILD_SOURCES}" not in desc, "the catalog substitution did not run"
    assert s.catalog_sentence() in desc, (
        "synth_fetch's served description no longer carries the GENERATED category list "
        "— if it has been replaced by a hand-typed one, that is the defect this test "
        "exists to catch")


def test_every_category_the_code_has_is_named_in_the_contract():
    """The direction the old docstring failed in was the other one (it named a category
    the code lacked), but both directions are drift. A category the corpus filters on
    and the description never mentions is a fetch rule nobody can see."""
    desc = _served_synth_fetch()
    for entry in s.catalog():
        assert entry["category"] in desc, entry


def test_the_catalog_is_the_predicate_table_and_not_a_copy_of_it():
    """`catalog()` must be derived from `BUILD_SOURCES` in order — a second list, even a
    correct one on the day it was written, is the thing that drifted."""
    assert [e["category"] for e in s.catalog()] == [b.category for b in s.BUILD_SOURCES]
    assert all(e["is_what"] for e in s.catalog()), "a category with no served account"


def test_every_fetch_reports_what_it_looked_for(monkeypatch):
    """A corpus of one README is ambiguous on its own: it could mean the repo has only a
    README, or that the file holding the commands matched no pattern. S4a was the second
    and read as the first. The categories come back on every call so the reader can
    tell — and can say "the instructions are somewhere I was not looking"."""
    from agent.mcp_tools import env_tools
    from agent.skills import authors_sources as A
    monkeypatch.setattr(A, "assess_tool_sources",
                        lambda *a, **k: {"workflow_only": False, "workflow_manifest": None})
    monkeypatch.setattr(env_tools._ms._env_mgr, "fetch_build_source",
                        lambda *a, **k: {"success": True, "files": [
                            {"path": "README.md", "text": "see packageInstallCommands.txt\n"}]})
    r = env_tools.synth_fetch("https://github.com/owner/repo")
    assert r["corpus_categories"] == s.catalog()
