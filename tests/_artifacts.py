"""Reading the repo's REAL generated artifacts from a test, safely.

`env_reports/` and `data/` are gitignored. On a developer machine that has run a
freeze or a seal they are full; on a fresh clone and in CI they are empty or absent.
A test that reads them directly therefore has two failure modes and both have now
happened in this repository:

  * a bare `open("env_reports/x.workflow.yaml")` — FileNotFoundError in CI, red for
    every run since it was written, unnoticed because the branch had not been pushed;
  * `parametrize(glob("env_reports/*.workflow.yaml"))` — zero parameters in CI, so
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

#: Directories holding generated artifacts. Gitignored — may be empty or absent.
SEALED_SPEC_GLOB = "env_reports/*.workflow.yaml"


def sealed_spec_paths() -> list[str]:
    """Every sealed workflow artifact on this machine. May be empty."""
    return sorted(_glob.glob(str(REPO / SEALED_SPEC_GLOB)))


def sealed_spec_params() -> list[Optional[str]]:
    """For `@pytest.mark.parametrize` — never empty, so the test is COLLECTED and can
    announce its own skip. An empty parametrize list silently collects nothing."""
    return sealed_spec_paths() or [None]


def load_or_skip(path: Optional[str]) -> Any:
    """The sealed spec at `path`, or a visible skip naming why it is absent."""
    if path is None:
        pytest.skip(
            "no sealed workflow artifacts on this machine (env_reports/ is "
            "gitignored) — this check only has force in a tree that has sealed "
            "something; it is not evidence of anything here")
    p = Path(path)
    if not p.is_absolute():
        p = REPO / p
    if not p.is_file():
        pytest.skip(f"{path} is not on this machine (generated artifact, gitignored) "
                    f"— run the pipeline that produces it to exercise this check")
    return yaml.safe_load(p.read_text())
