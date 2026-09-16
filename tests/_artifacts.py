"""Reading the repo's REAL generated artifacts from a test, safely.

The workspace's `reports/` zone is outside the checkout entirely. On a developer
machine that has run a freeze or a seal it is full; on a fresh clone and in CI it is
empty or absent.
A test that reads them directly therefore has two failure modes and both have now
happened in this repository:

  * a bare `open("<reports>/x.workflow.yaml")` — FileNotFoundError in CI, red for
    every run since it was written, unnoticed because the branch had not been pushed;
  * `parametrize(glob("<reports>/*.workflow.yaml"))` — zero parameters in CI, so
    the test reports PASS having checked nothing, which is indistinguishable from
    coverage.

These artifacts are still worth checking: they are the only place the real, messy,
cluster-locus shapes exist, and checks against them have caught defects that no
synthetic fixture did. So the answer is not to stop reading them — it is to make
"they are not here" a VISIBLE SKIP rather than an error or a silent pass.

Anything that must hold on every machine belongs in a committed fixture under
tests/fixtures/ instead. See tests/test_cluster_chain_lineage.py for one that was
converted after a drive overwrote the working file it had been reading.
"""
from __future__ import annotations

import glob as _glob
from pathlib import Path
from typing import Any, Optional

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

#: The REAL reports zone. `conftest` captures the machine's workspace before it
#: redirects $BIOINF_WORKSPACE at a sandbox, and hands it over here — asking the
#: resolver at this point would return the sandbox, and every artifact check in the
#: suite would silently become a no-op against an empty directory.
#:
#: This is the one place in the suite that deliberately reads outside the sandbox,
#: and it only ever READS.
import os   # noqa: E402

REAL_WORKSPACE = Path(os.environ["BIOINF_REAL_WORKSPACE"])

#: The machine's real zones. A handful of checks are only meaningful against what
#: this machine has actually built — a freeze that really ran, a seal that really
#: sealed, the core_tools env that really exists — and the sandbox is empty by
#: construction. They ask HERE, so "the machine's real state" is spelled once.
#:
#: Everything else in the suite must use the redirected workspace. These are
#: READ-ONLY by rule: a test that writes into the real workspace is a test that
#: pollutes the user's audit trail.
REPORTS = REAL_WORKSPACE / "reports"
CONDA_ENVS = REAL_WORKSPACE / "environments" / "conda"
RESOURCES = REAL_WORKSPACE / "resources"
PROJECTS_ACCESS = REAL_WORKSPACE / "projects_access.yaml"


def real_dir_or_skip(path: Path, what: str) -> Path:
    """`path` if it exists and is non-empty, else a skip that names what to run.

    The skip has to say WHICH machine state is missing and how to produce it —
    "envs/ empty" was accurate for years and then quietly became wrong when the
    directory moved, and a skip that names a path nobody writes to any more reads
    as "not bootstrapped" on a fully bootstrapped machine.
    """
    if not path.is_dir() or not any(path.iterdir()):
        pytest.skip(f"{path} is empty — {what}")
    return path

#: Generated artifacts — may be empty or absent. Never in the checkout.
SEALED_SPEC_GLOB = "*.workflow.yaml"


def sealed_spec_paths() -> list[str]:
    """Every sealed workflow artifact on this machine. May be empty."""
    return sorted(_glob.glob(str(REPORTS / SEALED_SPEC_GLOB)))


def sealed_spec_params() -> list[Optional[str]]:
    """For `@pytest.mark.parametrize` — never empty, so the test is COLLECTED and can
    announce its own skip. An empty parametrize list silently collects nothing."""
    return sealed_spec_paths() or [None]


def load_or_skip(path: Optional[str]) -> Any:
    """The sealed spec at `path`, or a visible skip naming why it is absent."""
    if path is None:
        pytest.skip(
            f"no sealed workflow artifacts in {REPORTS} — this check only has "
            f"force on a machine that has sealed something; it is not evidence "
            f"of anything here")
    p = Path(path)
    if not p.is_absolute():
        p = REPORTS / p
    if not p.is_file():
        pytest.skip(f"{path} is not on this machine (generated artifact, gitignored) "
                    f"— run the pipeline that produces it to exercise this check")
    return yaml.safe_load(p.read_text())
