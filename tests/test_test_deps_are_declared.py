"""Every third-party module a test imports must be declared, or CI dies on import.

THREE TIMES IN ONE DAY, which is what earns this file:

  * `coverage` — a new test assumed the analysis tool was installed. Red in CI.
  * `pytest-xdist` — pytest.ini grew `addopts = -n auto` and nothing declared the
    plugin, so every invocation would have died on `unrecognized arguments: -n`
    before collecting a single test.
  * `tiktoken` — a test imported it to compare two payload sizes. Red in CI, and the
    dependency was not even load-bearing: a character count answered the same question.

The shape is always identical and always invisible locally. A developer machine that
has been used for analysis accumulates tools; the test passes there, forever, and the
failure only ever appears on a clean checkout. It is the same blind spot as the
gitignored-artifact class (`tests/test_generated_artifact_reads.py`) — the environment
that runs the check is richer than the environment that must pass it.

A lint is the only thing that closes it, because "remember to declare it" demonstrably
does not — I forgot three times in a row while actively fixing the previous instance.

WHAT THIS DOES NOT DO: it will not notice a dependency of a dependency, and it does not
check versions. It catches the direct `import X` in a test file, which is the whole of
what went wrong all three times.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
PYPROJECT = ROOT / "pyproject.toml"

#: Import name -> distribution name, where they differ.
_ALIASES = {"yaml": "pyyaml", "dotenv": "python-dotenv", "PIL": "pillow"}

#: Local test helpers. tests/ has no __init__.py, so these are imported bare — the
#: convention documented at tests/conftest.py. They are files in this directory, not
#: packages, and requiring them in requirements.txt would be nonsense.
_LOCAL = {"env_records", "workflow_records", "_artifacts", "conftest", "real_inputs"}


def _declared() -> set[str]:
    """Distribution names declared in pyproject.toml — `dependencies` plus every
    optional-dependency extra.

    Reads pyproject because it is now the SINGLE dependency truth: requirements.txt
    was its self-declared mirror and is gone (setup.sh and CI both install
    `-e ".[dev]"`). The extraction is deliberately SCOPED to those two places rather
    than sweeping every array in the file — `classifiers`, `keywords` and
    `build-system.requires` are also arrays of strings, and folding them in would
    silently widen the declared set and make this lint stop firing."""
    text = PYPROJECT.read_text()
    arrays: list[str] = []
    m = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", text, re.S | re.M)
    if m:
        arrays.append(m.group(1))
    m = re.search(r"^\[project\.optional-dependencies\](.*?)(?=^\[|\Z)", text, re.S | re.M)
    if m:
        arrays += re.findall(r"=\s*\[(.*?)\]", m.group(1), re.S)
    names: set[str] = set()
    for block in arrays:
        for item in re.findall(r'"([^"]+)"', block):
            spec = re.match(r"^([A-Za-z0-9._-]+)", item.strip())
            if spec:
                names.add(spec.group(1).lower().replace("_", "-"))
    return names


def _top_level_imports(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:                                        # pragma: no cover
        return set()
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            out.add(n.module.split(".")[0])
    return out


def _is_third_party(mod: str) -> bool:
    if mod in sys.stdlib_module_names or mod in _LOCAL:
        return False
    return mod not in {"agent", "scripts", "tests"}


@pytest.mark.parametrize("path", sorted(TESTS.rglob("*.py")), ids=lambda p: p.name)
def test_every_third_party_import_in_a_test_is_declared(path):
    declared = _declared()
    undeclared = sorted(
        m for m in _top_level_imports(path)
        if _is_third_party(m)
        and _ALIASES.get(m, m).lower().replace("_", "-") not in declared)
    assert not undeclared, (
        f"{path.name} imports {undeclared}, which no requirements file declares.\n\n"
        f"This passes on any machine where the module happens to be installed and "
        f"fails on a clean checkout — the whole test module errors at COLLECTION, so "
        f"it takes every test in the file with it.\n"
        f"Either add it to pyproject.toml's [project.optional-dependencies] dev list, "
        f"or — better, if it is a dev/analysis tool — do not depend on it: "
        f"`pytest.importorskip` for an optional check, or drop it entirely if "
        f"something in the stdlib answers the same question.")


def test_the_lint_reads_a_dependency_list_that_exists():
    """If the parse stopped matching, `_declared()` would return an empty set and every
    import would look undeclared — noisy, not silent, so that half is a convenience. The
    real risk is the reverse: a parse that silently widens (sweeping `classifiers` in,
    say) and reports nothing because everything looks declared. So assert the shape —
    the runtime deps and the dev extra both reached the set, and nothing that is only a
    classifier or a keyword did."""
    declared = _declared()
    assert PYPROJECT.is_file(), f"{PYPROJECT} does not exist"
    assert {"fastmcp", "pyyaml", "pydantic"} <= declared, (
        f"runtime dependencies missing from the parse: {declared}")
    assert {"pytest", "pytest-xdist"} <= declared, (
        f"the [dev] extra did not reach the parse: {declared}")
    assert not declared & {"bioinformatics", "setuptools", "posix"}, (
        f"the parse swept in keywords/classifiers/build-requires, which would widen the "
        f"declared set and stop this lint firing: {declared}")


def test_the_lint_catches_a_planted_undeclared_import(tmp_path):
    """A lint nobody has seen fire is a lint nobody knows the shape of."""
    f = tmp_path / "t_x.py"
    f.write_text("import tiktoken\nimport json\nfrom agent.skills import resources\n")
    third = {m for m in _top_level_imports(f) if _is_third_party(m)}
    assert third == {"tiktoken"}, (
        f"expected only the third-party import to be flagged, got {third} — `json` is "
        f"stdlib and `agent` is first-party")
