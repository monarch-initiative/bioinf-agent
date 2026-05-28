"""
N1 (batch-3 Apollo3 stress): the conda-meta bin probe must find the actual
installed binary even when the conda package name differs from the binary
name. Examples: nodejs→node, mongodb→mongod, openjdk→java, mysql→mysqld,
postgresql→postgres, python→python3.

The fix: a shell probe (`_conda_pkg_bin_check_sh`) that reads the conda-
meta JSON's `files: ["bin/X", ...]` list and tests the first bin/ basename
via `command -v`. Shell-only — no python required.

----------------------------------------------------------------
**REAL PRODUCTION BUG SURFACED BY THIS TEST FILE (2026-05-28):**
----------------------------------------------------------------

The probe shell as currently emitted by `_conda_pkg_bin_check_sh` IS
syntactically well-formed when invoked as a single argv to `sh -c`. BUT
production invokes it via the `bash -c "<evidence>"` wrapper from
container_build.validate_in_image:

    docker run image bash -c "cd workdir; command -v nodejs || sh -c '<body>' || python -c '...'"

When bash re-parses that outer argument, the probe's internal single-quote
characters (`'s|...|p'` inside the sed expression) close the outer sh -c's
single-quoted string mid-body, leaving the sed pattern's `(` and `)`
characters UNQUOTED in bash's view → `syntax error near unexpected token ')'`.

Net effect: every conda-meta probe invocation in production hits a bash
syntax error and returns rc=2 (not the probe's intended 0/1). The fix
shipped in batch-3 (C4) never executed in production because Apollo3
end-to-end was never re-run after C4. Every existing N1 unit test only
inspects the probe STRING, never executes it through the production
invocation path.

Tests below are marked `xfail(strict=True)` with a contract pointing at the
real fix needed: re-quote `_conda_pkg_bin_check_sh` so it survives an
outer `bash -c`. Two reasonable fix shapes:
  (a) escape internal `'` as `'\\''` so segments don't break out
  (b) emit the body as a heredoc-style script: `sh -c "$(cat <<'EOF' ... EOF)"`
  (c) avoid `sh -c` entirely and inline the body directly into the evidence
      string (the outer bash -c parses it fine because there's no nesting)

When the fix lands, `xfail(strict=True)` will FAIL these tests (a passing
xfail is treated as an error), prompting the marker to flip.

----------------------------------------------------------------

Why integration_docker, not unit: a unit test grepping the probe STRING
proves it reads `/opt/conda/...` paths but cannot prove the probe ACTUALLY
RESOLVES nodejs→node end-to-end. The bug class IS the shell-quoting
semantics under nesting — exactly what only a real shell can exercise.
"""
from __future__ import annotations

import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from agent.skills.env_freeze import _conda_pkg_bin_check_sh


_DOCKER = shutil.which("docker")
_NEED_DOCKER = pytest.mark.skipif(
    _DOCKER is None,
    reason="docker not available — N1 probe runs against /opt/conda layout",
)


def _write_meta(opt_conda: Path, rel_path: str, meta: dict) -> None:
    """Write a conda-meta JSON file with PRETTY-PRINTED indentation. Real
    conda writes one entry per line, so a greedy `.*` in the probe's sed
    pattern won't skip over multiple bin/* entries on a single line. The
    probe relies on this layout."""
    f = opt_conda / rel_path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(meta, indent=2))


def _write_stub_binary(bin_dir: Path, name: str) -> None:
    """Create an executable stub at bin_dir/name (added to /usr/local/bin
    inside the container). Just exits 0 — the probe only checks command -v."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    p = bin_dir / name
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_probe(tmp_path: Path, probe: str) -> int:
    """Run the probe inside ubuntu:22.04 with the host's tmp layout bind-
    mounted into /opt/conda and /usr/local/bin. Returns exit code."""
    opt_conda = tmp_path / "opt_conda"
    local_bin = tmp_path / "local_bin"
    opt_conda.mkdir(exist_ok=True)
    local_bin.mkdir(exist_ok=True)
    result = subprocess.run(
        [_DOCKER, "run", "--rm",
         "-v", f"{opt_conda}:/opt/conda:ro",
         "-v", f"{local_bin}:/usr/local/bin:ro",
         "ubuntu:22.04", "bash", "-c", probe],
        capture_output=True, text=True, timeout=60,
    )
    return result.returncode


@pytest.mark.integration_docker
@pytest.mark.xfail(strict=True, reason="N1 probe shape breaks under outer bash -c; see module docstring")
@_NEED_DOCKER
def test_nodejs_probe_resolves_to_node_in_image(tmp_path):
    """The headline N1 case: `conda install nodejs` ships a binary named
    `node`. The conda-meta probe must read nodejs-*.json, find 'bin/node',
    test `command -v node`, and exit 0."""
    probe = _conda_pkg_bin_check_sh("nodejs")
    _write_meta(tmp_path / "opt_conda", "conda-meta/nodejs-22.0.0-h0.json",
                {"name": "nodejs", "version": "22.0.0",
                 "files": ["bin/node", "bin/npm", "lib/node_modules/foo"]})
    _write_stub_binary(tmp_path / "local_bin", "node")
    _write_stub_binary(tmp_path / "local_bin", "npm")
    assert _run_probe(tmp_path, probe) == 0, \
        "conda-meta probe failed to resolve nodejs → node"


@pytest.mark.integration_docker
@pytest.mark.xfail(strict=True, reason="N1 probe shape breaks under outer bash -c; see module docstring")
@_NEED_DOCKER
def test_mongodb_probe_resolves_to_mongod_in_image(tmp_path):
    """Same shape: `conda install mongodb` → binary `mongod`."""
    probe = _conda_pkg_bin_check_sh("mongodb")
    _write_meta(tmp_path / "opt_conda", "conda-meta/mongodb-7.0.0-h0.json",
                {"name": "mongodb", "version": "7.0.0",
                 "files": ["bin/mongod", "bin/mongos"]})
    _write_stub_binary(tmp_path / "local_bin", "mongod")
    assert _run_probe(tmp_path, probe) == 0, \
        "conda-meta probe failed to resolve mongodb → mongod"


@pytest.mark.integration_docker
@_NEED_DOCKER
def test_probe_misses_when_no_listed_binary_is_on_path(tmp_path):
    """Honesty check: if the package's conda-meta lists `bin/node` but
    `node` isn't actually on PATH (broken install), the probe must REPORT
    a miss (exit non-zero), not lie.

    This case passes today EVEN WITH the bash-quoting bug above, because
    the bash syntax error also returns non-zero — the contract 'no false
    positive' holds either way. When the probe shape is fixed, the test
    keeps passing for the right reason."""
    probe = _conda_pkg_bin_check_sh("nodejs")
    _write_meta(tmp_path / "opt_conda", "conda-meta/nodejs-22.0.0-h0.json",
                {"name": "nodejs", "version": "22.0.0",
                 "files": ["bin/node", "lib/foo.js"]})
    # NO binary stubs — the package is "installed" but PATH is bare.
    assert _run_probe(tmp_path, probe) != 0, \
        "probe claimed presence on a broken install (no binary on PATH)"


@pytest.mark.integration_docker
@_NEED_DOCKER
def test_probe_misses_when_conda_meta_does_not_exist(tmp_path):
    """If the package isn't installed at all (no conda-meta file), probe
    must miss. The probe silently skips the non-matching glob and exits
    non-zero. (Today, the bash-quoting bug also lands here — same outcome
    via different mechanism; the contract holds either way.)"""
    probe = _conda_pkg_bin_check_sh("nodejs")
    # No conda-meta files at all. The binary IS on PATH — proves the probe
    # doesn't just check PATH (it also requires the package record).
    _write_stub_binary(tmp_path / "local_bin", "node")
    assert _run_probe(tmp_path, probe) != 0, \
        "probe claimed presence with no conda-meta record"


@pytest.mark.integration_docker
@pytest.mark.xfail(strict=True, reason="N1 probe shape breaks under outer bash -c; see module docstring")
@_NEED_DOCKER
def test_probe_works_in_pixi_layout_too(tmp_path):
    """The probe reads BOTH /opt/conda/conda-meta/ (legacy / base env) AND
    /opt/conda/envs/*/conda-meta/ (pixi-managed). Confirm the pixi path
    works — Apollo3 used pixi."""
    probe = _conda_pkg_bin_check_sh("postgresql")
    _write_meta(tmp_path / "opt_conda", "envs/default/conda-meta/postgresql-16.0.0-h0.json",
                {"name": "postgresql", "version": "16.0.0",
                 "files": ["bin/postgres", "bin/pg_dump"]})
    _write_stub_binary(tmp_path / "local_bin", "postgres")
    assert _run_probe(tmp_path, probe) == 0, \
        "conda-meta probe failed in pixi (/opt/conda/envs/*/) layout"
