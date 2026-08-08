"""F8 + F1/F13 — two arguments that punished the caller for holding the truth.

Both are `resolve_tool` inputs whose most natural form was the one that failed.

**F8 — `github_repo` as a URL.** Documented as `owner/repo`; a URL is how a human
holds a repo. A URL contains "/", so it flowed through the owner/repo branch
verbatim and downstream probes built
`api.github.com/repos/https://github.com/owner/repo` -> 404 -> `repo_exists:
false` for a repo that exists, refused as `investigation_empty` ("we looked and
found nothing") when the truth was "we mangled the identifier". Measured on the
same repo in one session: the bare form resolved a tag and emitted an
install_call; the URL form refused. Worse, the two disagreed WITHIN one call —
`author_image`/`authors_recipe` accepted the URL while binary/synthesis/source
reported the repo absent. Two readings of one input.

This is the zero-registry case, where `github_repo` is the ONLY identity anchor
there is, so the one input that must work for a hostile-install tool was the one
that silently failed on its most natural form.

**F1/F13 — `user_said` as a sentence.** Contracted as the user's WORD. A caller
holding free text passes the sentence, because that is what they have; the
sentence failed the package-family comparison and produced
`investigation_contradicted` — a claim that the investigation found a conflict —
with the remedy *"Resolve '<the whole sentence>' as typed and read the family"*,
which cannot be done.

F13 is why that is not cosmetic: **the guard inverted.** Passing more truth about
the user's intent produced a refusal; omitting `user_said` entirely produced a
clean, confident answer. A guard that punishes disclosure teaches callers to
withhold it.
"""
from __future__ import annotations

import re

import pytest

from agent.skills.resolver import _normalize_github_repo


# -- F8 ----------------------------------------------------------------------

@pytest.mark.parametrize("given", [
    "baumannlab/Genome_Assembly_Booster",
    "https://github.com/baumannlab/Genome_Assembly_Booster",
    "http://github.com/baumannlab/Genome_Assembly_Booster",
    "https://github.com/baumannlab/Genome_Assembly_Booster/",
    "https://github.com/baumannlab/Genome_Assembly_Booster.git",
    "git@github.com:baumannlab/Genome_Assembly_Booster.git",
    "https://www.github.com/baumannlab/Genome_Assembly_Booster",
    "  baumannlab/Genome_Assembly_Booster  ",
])
def test_every_way_a_human_holds_this_repo_normalizes_to_one_form(given):
    assert _normalize_github_repo(given) == "baumannlab/Genome_Assembly_Booster"


def test_a_path_inside_the_repo_is_not_part_of_its_name():
    for suffix in ("/tree/main", "/releases/tag/v1", "/blob/main/README.md"):
        assert _normalize_github_repo(f"https://github.com/a/b{suffix}") == "a/b"


def test_a_non_github_url_is_returned_untouched():
    """The normalizer fixes a SPELLING; it must not invent a repo. Stripping the
    scheme first and slicing to two segments turned `https://gitlab.com/a/b` into
    `gitlab.com/a` — a repo that does not exist, manufactured by the cleanup.
    A caller who passed something else must see their own input in the refusal."""
    for url in ("https://gitlab.com/a/b", "https://bitbucket.org/x/y",
                "https://codeberg.org/x/y/z"):
        assert _normalize_github_repo(url) == url


def test_things_that_are_not_repo_references_pass_through():
    for s in ("", "notarepo", "https://github.com/onlyowner"):
        assert _normalize_github_repo(s) == s.strip()


def test_resolve_normalizes_before_anything_reads_the_argument():
    """The structural half. Fixing the three readers that were wrong would leave
    a fourth free to be added; normalizing at the boundary is what makes the
    disagreement impossible."""
    import inspect
    from agent.skills import resolver
    src = inspect.getsource(resolver.resolve)
    body = src.split('"""', 2)[-1]
    norm = body.index("_normalize_github_repo(github_repo)")
    first_read = min((body.index(m) for m in ("probe_github(github_repo",
                                              "if github_repo:") if m in body),
                     default=len(body))
    assert norm < first_read, (
        "github_repo is read before it is normalized, so some readers see the raw "
        "form and some the clean one — which is exactly F8")


# -- F1 / F13 ----------------------------------------------------------------

def _decide(monkeypatch, tool, user_said):
    """Run resolve() with every network probe stubbed to a clean conda hit, so
    what is under test is the user_said guard and nothing else."""
    from agent.skills import resolver

    for name in ("probe_conda", "probe_pypi", "probe_cran", "probe_bioconductor"):
        monkeypatch.setattr(resolver, name,
                            lambda t, timeout=12, **kw: {"available": False}, raising=False)
    monkeypatch.setattr(resolver, "probe_conda",
                        lambda t, timeout=12, **kw: {"available": True, "version": "1.0",
                                                     "channel": "bioconda"}, raising=False)
    monkeypatch.setattr(resolver, "probe_github_search",
                        lambda *a, **kw: {"available": False, "repos": []}, raising=False)
    return resolver.resolve(tool=tool, user_said=user_said, timeout=1)


def test_free_text_that_names_the_tool_no_longer_refuses(monkeypatch):
    """THE inversion, asserted directly. The campaign's very first call was
    `resolve_tool(tool="fastp", user_said="install fastp to QC and trim
    paired-end exome reads")` and it refused, while dropping the argument
    resolved cleanly."""
    d = _decide(monkeypatch, "fastp",
                "install fastp to QC and trim paired-end exome reads")
    assert d.get("refusal_reason") is None, d.get("rationale", "")[:300]
    assert d.get("chosen"), "more truth about the user's intent still costs the answer"


def test_free_text_is_named_as_free_text_rather_than_silently_dropped(monkeypatch):
    d = _decide(monkeypatch, "fastp", "install fastp to QC reads")
    assert d.get("user_said_shape") == "free_text"
    assert "free text" in d.get("rationale", "")


def test_the_note_names_an_action_that_can_actually_be_taken(monkeypatch):
    """The rule this codebase already holds: a gate should teach. `Resolve '<a
    whole sentence>' as typed` is not an instruction anyone can follow."""
    d = _decide(monkeypatch, "fastp", "install fastp to QC reads")
    r = d.get("rationale", "")
    assert "interpret_request" in r
    assert "as typed and read the family" not in r


def test_free_text_that_never_mentions_the_tool_still_refuses(monkeypatch):
    """The guard's real job survives. If the sentence does not name the tool, a
    substitution may genuinely be happening and nothing here can tell."""
    d = _decide(monkeypatch, "bowtie2", "please set up the aligner we discussed")
    assert d.get("chosen") is None
    assert d.get("refusal_reason") == "needs_user_input"
    assert "interpret_request" in d.get("rationale", "")


def test_a_word_level_substitution_is_still_caught(monkeypatch):
    """The case the guard was built for — an actual different tool passed as a
    normalization — must be untouched by the free-text branch."""
    d = _decide(monkeypatch, "bwa", "samtools")
    assert d.get("chosen") is None
    assert d.get("refusal_reason") == "investigation_contradicted"


def test_a_major_version_rename_is_still_accepted(monkeypatch):
    """`user_said='gatk'` with `tool='gatk4'` is the documented worked example."""
    d = _decide(monkeypatch, "gatk4", "gatk")
    assert d.get("refusal_reason") != "investigation_contradicted"


def test_the_mention_test_is_a_word_boundary_not_a_substring(monkeypatch):
    """`bwa` must not be found inside `bwa-mem2` or `subwaysurfer`, or the guard
    would clear substitutions it exists to catch."""
    d = _decide(monkeypatch, "bwa", "please install the subway mapper thing")
    assert d.get("chosen") is None, d.get("rationale", "")[:200]
    assert re.search(r"(?<![a-z0-9])bwa(?![a-z0-9])", "please install the subway mapper thing") is None
