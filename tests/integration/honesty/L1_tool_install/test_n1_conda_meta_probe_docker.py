"""
N1 (batch-3 Apollo3 stress): the conda-meta bin probe must find the actual
installed binary even when the conda package name differs from the binary
name. Examples: nodejs→node, mongodb→mongod, openjdk→java, mysql→mysqld,
postgresql→postgres, python→python3.

The fix: a shell probe (`_conda_pkg_bin_check_sh`) that reads the conda-
meta JSON's `files: ["bin/X", ...]` list and tests the first bin/ basename
via `command -v`. Shell-only — no python required.

----------------------------------------------------------------
History: this test file surfaced a real production bug (2026-05-28). The
ORIGINAL probe shape was `sh -c '<body>'` where the body contained an
internal `'s|...|p'` sed expression. Production invokes evidence via
`bash -c "...; <evidence>"` (container_build.validate_in_image), so bash
re-parsed the outer string and the internal single quote closed bash's
outer single-quote mid-body — leaving sed's `(`/`)` characters unquoted
to bash → `syntax error near unexpected token ')'`. The C4 fix shipped
without an end-to-end integration test; Apollo3 end-to-end was never re-
run after C4 so the bug was live but unexercised. THIS test caught it.

The fix (chosen from three options surfaced by the test author): drop the
`sh -c '...'` wrap entirely and use a plain bash subshell `(...)`. The
body parses cleanly because the outer bash is the only shell parsing the
string; the subshell scopes `exit 0/exit 1` so the probe still short-
circuits the `||` chain without killing the parent.

Tests passed `xfail(strict=True)` before the fix — `xpass(strict)` failed
the suite when the fix landed, prompting the markers to flip. They are
ordinary `@pytest.mark.integration_docker` now.
----------------------------------------------------------------

Why integration_docker, not unit: a unit test grepping the probe STRING
proves it reads `/opt/conda/...` paths but cannot prove the probe ACTUALLY
RESOLVES nodejs→node end-to-end. The bug class WAS the shell-quoting
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
    a miss (exit non-zero), not lie."""
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
    non-zero."""
    probe = _conda_pkg_bin_check_sh("nodejs")
    # No conda-meta files at all. The binary IS on PATH — proves the probe
    # doesn't just check PATH (it also requires the package record).
    _write_stub_binary(tmp_path / "local_bin", "node")
    assert _run_probe(tmp_path, probe) != 0, \
        "probe claimed presence with no conda-meta record"


@pytest.mark.integration_docker
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
