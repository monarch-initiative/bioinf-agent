"""No test may read a generated, gitignored artifact directly.

`env_reports/` and `data/` hold artifacts the agent produces. They are gitignored, so
they are full on a developer machine and empty in CI. Reading them directly fails in
one of two ways, and BOTH have happened here:

  * a bare `open("env_reports/x.workflow.yaml")` errors in CI. One such test was red
    on every CI run from the day it was written and nobody saw it, because the branch
    it lived on had never been pushed.
  * `parametrize(glob("env_reports/*.workflow.yaml"))` collects ZERO parameters in
    CI, so the test reports PASS having checked nothing. That is the worse of the two:
    an error is loud, a vacuous pass looks exactly like coverage.

A third variant is a test pinned to a working file the agent itself rewrites — three
tests read a pipeline draft under `data/`, and the next cluster run replaced it with a
better one and broke all three. What must hold everywhere belongs in a committed
fixture under `tests/fixtures/`.

Go through `tests/_artifacts.py`, which turns absence into a VISIBLE SKIP. This lint
is the thing that makes that a rule rather than a suggestion — the class kept coming
back one file at a time.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

#: The generated, gitignored trees. A string literal that STARTS with one of these
#: (trailing slash required) is a repo-relative read of an artifact. The slash is what
#: distinguishes it from `tmp_path / "env_reports"`, which is a sandbox and always fine.
GENERATED = ("env_reports/", "data/pipeline_drafts/", "docker_images/",
             "job_submissions/", "transfer_history/")

#: `_artifacts.py` is the helper that does the skipping, so it must name these paths.
#: This file quotes the forbidden shapes twice over — in the docstring that teaches
#: them and in the self-test that proves the pattern matches them — and a lint that
#: cannot state what it forbids is worse than one that exempts itself.
EXEMPT = {"_artifacts.py", "test_generated_artifact_reads.py"}

_LITERAL = re.compile(r"""['"](%s)[^'"]*['"]""" % "|".join(re.escape(g) for g in GENERATED))


def _test_files() -> list[Path]:
    return sorted(p for p in TESTS.rglob("*.py") if p.name not in EXEMPT)


@pytest.mark.parametrize("path", _test_files(), ids=lambda p: str(p.name))
def test_no_test_file_reads_a_generated_artifact_directly(path):
    hits = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or not _LITERAL.search(line):
            continue
        # Docstrings and comments may DISCUSS these paths — that is how the lesson is
        # recorded. Only flag a literal that is being handed to something.
        if re.search(r"(open|glob|glob\.glob|read_text|safe_load|Path)\s*\(", line):
            hits.append(f"  {path.name}:{n}: {stripped[:100]}")

    assert not hits, (
        f"{len(hits)} direct read(s) of a generated, gitignored artifact:\n"
        + "\n".join(hits)
        + "\n\nThese are empty in CI: a bare open() errors, and a parametrize over an "
          "empty glob PASSES having checked nothing. Use tests/_artifacts.py "
          "(`sealed_spec_params` / `load_or_skip`), which makes absence a visible skip "
          "— or move what must hold everywhere into a committed tests/fixtures/ file.")


def test_the_lint_actually_matches_the_shape_it_is_meant_to_catch():
    """A lint nobody has seen fire is a lint nobody knows the shape of."""
    bad = [
        'spec = yaml.safe_load(open("env_reports/x.workflow.yaml"))',
        '@pytest.mark.parametrize("p", glob.glob("env_reports/*.workflow.yaml"))',
        'd = yaml.safe_load(open("data/pipeline_drafts/x.draft.yaml"))',
    ]
    for line in bad:
        assert _LITERAL.search(line) and re.search(
            r"(open|glob|glob\.glob|read_text|safe_load|Path)\s*\(", line), line

    ok = [
        '    * a bare `open("env_reports/x.workflow.yaml")` errors in CI.',   # prose
        'out = tmp_path / "env_reports"',                                     # sandbox
        'assert "env_reports/" not in rendered',                              # assertion
    ]
    for line in ok:
        flagged = bool(_LITERAL.search(line)) and bool(re.search(
            r"(open|glob|glob\.glob|read_text|safe_load|Path)\s*\(", line))
        assert not flagged or line.strip().startswith("*"), line
