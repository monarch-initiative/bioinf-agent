"""Real on-disk files for invariant fixtures — importable as `from real_inputs import …`
from anywhere in the suite (tests/ is on sys.path; see tests/conftest.py).

WHY THIS EXISTS. Layer-2's external-source clauses do not merely read the record, they
LOOK: I5 stats every reference_database, I8 re-hashes every authored_artifact, and
`I8.test_data_*` now does both for test_data. A fixture that declares `/abs/x.bam` is
therefore describing a workflow whose declared input is not there — a shape the seal is
specifically built to refuse.

Before the test_data clause existed those fixtures passed, because test_data was the one
external source nobody ever looked at. That is not a reason to keep them: a fixture that
can only pass while a check is missing is the fixture that lets the check go missing.

The files are tiny and live in one session-scoped temp dir, deliberately NOT under the
repo (tests/conftest.py fails the run on repo-anchored leakage)."""
from __future__ import annotations

import tempfile
from pathlib import Path

_DIR: Path | None = None


def _dir() -> Path:
    global _DIR
    if _DIR is None:
        _DIR = Path(tempfile.mkdtemp(prefix="bioinf_fixture_inputs_"))
    return _DIR


def real_input(name: str, content: str = "fixture input\n") -> str:
    """An absolute path to a real, non-empty file. Stable across calls with the same
    name, so a fixture can declare it as both `test_data.r1` and a step's input and the
    two compare equal — which is what I8's composition walk is checking."""
    p = _dir() / name
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(p)
