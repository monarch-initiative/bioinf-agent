"""The runtime env's nextflow, for the tests that run a rendered directory for real.

`./scripts/setup.sh` installs nextflow (with its own Java) into `./.conda_runtime`; a
checkout without it skips these tests naming the command that installs it.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parents[1] / ".conda_runtime"
NEXTFLOW = RUNTIME / "bin" / "nextflow"
JVM = RUNTIME / "lib" / "jvm"

needs_nextflow = pytest.mark.skipif(
    not (NEXTFLOW.is_file() and os.access(NEXTFLOW, os.X_OK) and (JVM / "bin" / "java").is_file()),
    reason="the runtime env carries no nextflow binary (./scripts/setup.sh installs it)")


def run_nextflow(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """The runtime env's nextflow in `cwd`, offline, with NXF_HOME inside the directory
    (as launcher.sh does) so nothing touches ~/.nextflow. The launcher honours
    JAVA_HOME / JAVA_CMD over the JDK beside it, so both name the bundled JVM."""
    env = dict(os.environ, JAVA_HOME=str(JVM), JAVA_CMD=str(JVM / "bin" / "java"),
               NXF_HOME=str(cwd / ".nextflow_home"), NXF_OFFLINE="true", NXF_ANSI_LOG="false")
    return subprocess.run([str(NEXTFLOW), *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=300)
