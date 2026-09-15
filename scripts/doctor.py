#!/usr/bin/env python3
"""doctor.py — the bioinf-agent systems check.

One row per requirement, PASS/FAIL/SKIP, and every FAIL names its own fix —
"the gate is the guide", applied to setup. Run via `./scripts/setup.sh --check`
(preferred: uses the runtime env's python) or directly with any python3.

Deliberately stdlib-only: this script must be able to DIAGNOSE a machine where
the repo's dependencies are not installed yet, so it may not import them at
module level. Checks that need a dependency probe it in a subprocess of the
runtime env's python, which is the interpreter whose health is in question.

Exit code = number of FAIL rows (0 == healthy).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_SH = ROOT / "scripts" / "_env.sh"

ROWS: list[tuple[str, str, str, str]] = []   # (verdict, name, detail, fix)


def row(verdict: str, name: str, detail: str, fix: str = "") -> None:
    ROWS.append((verdict, name, detail, fix))


def run(argv: list[str], timeout: int = 15, cwd: str | None = None) -> tuple[int, str]:
    """rc + combined output; never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd or str(ROOT))
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


_RUNTIME_PY_FALLBACK = ROOT / ".conda_runtime" / "bin" / "python"


def _runtime_py() -> Path:
    """The runtime interpreter, per scripts/_env.sh — asked, not re-spelled, for the
    same reason check_conda delegates. The CLI prints the path whether or not it is
    usable, so a missing runtime env can still be reported BY NAME. If _env.sh is
    absent or answers something that is not an interpreter path we keep the literal:
    check_runtime_env then reports the missing env, which is the true finding."""
    if not ENV_SH.is_file():
        return _RUNTIME_PY_FALLBACK
    _, out = run(["bash", str(ENV_SH), "runtime-python"])
    line = out.splitlines()[-1].strip() if out else ""
    return Path(line) if line.endswith("bin/python") else _RUNTIME_PY_FALLBACK


RUNTIME_PY = _runtime_py()


# --- conda -------------------------------------------------------------------
def check_conda() -> None:
    """Ask scripts/_env.sh, never a second path list.

    The doctor used to carry its own copy of the search, and it had already
    diverged from setup.sh's on ORDER (repo-local .miniforge first here, last
    there) — so on a machine with both a private and a system conda the check
    could PASS on a conda setup never used. A systems check that reports on a
    different thing than the one that ran is the defect this repo exists to
    refuse; delegating is the only structural fix."""
    if not ENV_SH.is_file():
        row("FAIL", "conda", "scripts/_env.sh missing — cannot resolve conda",
            "restore it from git; it is the single conda/interpreter resolver")
        return
    rc, conda = run(["bash", str(ENV_SH), "conda"])
    if rc != 0 or not conda:
        row("FAIL", "conda", "not found ($CONDA_EXE, PATH, ./.miniforge, usual locations)",
            "run ./scripts/setup.sh — it offers to install a private miniforge at ./.miniforge "
            "(or install miniforge yourself: https://github.com/conda-forge/miniforge)")
        return
    conda = conda.splitlines()[-1].strip()
    rc, out = run([conda, "--version"])
    row("PASS" if rc == 0 else "FAIL", "conda",
        f"{out.splitlines()[0] if out else conda} ({conda})",
        "" if rc == 0 else "conda exists but won't run — reinstall miniforge")


# --- the runtime env ---------------------------------------------------------
def check_runtime_env() -> None:
    if not RUNTIME_PY.exists():
        row("FAIL", "runtime env", ".conda_runtime/ missing — the MCP server has no interpreter",
            "run ./scripts/setup.sh (creates it and installs the agent into it)")
        return
    rc, out = run([str(RUNTIME_PY), "-c",
                   "import sys, fastmcp; print(sys.version.split()[0], fastmcp.__version__)"])
    if rc != 0:
        row("FAIL", "runtime env",
            f".conda_runtime exists but can't import the deps: {out.splitlines()[-1] if out else 'unknown'}",
            "re-run ./scripts/setup.sh (re-installs the agent into the runtime env)")
        return
    pyver, mcpver = (out.split() + ["?", "?"])[:2]
    row("PASS", "runtime env", f".conda_runtime — Python {pyver}, fastmcp {mcpver}")


def check_agent_import() -> None:
    if not RUNTIME_PY.exists():
        row("SKIP", "agent import", "no runtime env (see above)")
        return
    rc, out = run([str(RUNTIME_PY), "-c",
                   "import agent, os; print(os.path.dirname(os.path.abspath(agent.__file__)))"])
    if rc != 0:
        row("FAIL", "agent import", out.splitlines()[-1] if out else "import failed",
            "re-run ./scripts/setup.sh (editable-installs this checkout into the runtime env)")
        return
    where = Path(out.splitlines()[-1])
    if ROOT in where.parents or where == ROOT / "agent":
        row("PASS", "agent import", f"agent @ {where} (this checkout)")
    else:
        row("FAIL", "agent import", f"agent resolves to {where}, not this checkout",
            "a site-packages copy is shadowing the editable install — "
            "pip uninstall bioinf-agent in the runtime env, then re-run ./scripts/setup.sh")


# --- docker ------------------------------------------------------------------
def check_docker() -> None:
    rc, out = run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
    if rc == 0 and out:
        row("PASS", "docker", f"daemon up (server {out.splitlines()[-1]})")
    elif rc == 127:
        row("FAIL", "docker", "docker CLI not found",
            "install Docker Desktop (macOS) or docker-ce (Linux); freeze/validate need the daemon")
    else:
        row("FAIL", "docker", "daemon not reachable",
            "start Docker Desktop / `systemctl start docker`, then re-run --check")


# --- MCP registration --------------------------------------------------------
def check_mcp_registration() -> None:
    mcp_json = ROOT / ".mcp.json"
    launcher = ROOT / "scripts" / "start_mcp_server.sh"
    if not mcp_json.exists():
        row("FAIL", "mcp register", ".mcp.json missing from the repo root",
            "restore it from git — MCP clients discover the server through this file")
        return
    try:
        servers = json.loads(mcp_json.read_text()).get("mcpServers", {})
    except Exception as e:
        row("FAIL", "mcp register", f".mcp.json unparseable: {e!r}",
            "restore it from git")
        return
    if "bioinf" not in servers:
        row("FAIL", "mcp register", f".mcp.json has servers {sorted(servers)} — no 'bioinf'",
            "restore .mcp.json from git")
        return
    if not os.access(launcher, os.X_OK):
        row("FAIL", "mcp register", "scripts/start_mcp_server.sh is not executable",
            "chmod +x scripts/start_mcp_server.sh")
        return
    row("PASS", "mcp register", ".mcp.json → bioinf via scripts/start_mcp_server.sh")


# --- core data ---------------------------------------------------------------
def check_core_data() -> None:
    chr22 = ROOT / "data" / "core_test_data_hg38" / "genome" / "chr22.fa"
    core_env = ROOT / "envs" / "bioinf_core_tools"
    missing = [str(p.relative_to(ROOT)) for p in (chr22, core_env) if not p.exists()]
    if missing:
        row("FAIL", "core data", f"missing: {', '.join(missing)}",
            "run ./scripts/setup.sh (bootstraps the core_tools env + chr22 test data)")
        return
    # manifest.yaml is written by the FULL bootstrap (it enumerates the read-
    # dataset corpus) — its absence after --minimal is a state, not a failure.
    manifest = ROOT / "data" / "core_test_data_hg38" / "manifest.yaml"
    corpus = ("read-dataset corpus present" if manifest.exists()
              else "minimal bootstrap (no read-dataset corpus — ./scripts/setup.sh --full adds it)")
    row("PASS", "core data", f"chr22 reference + core_tools env present; {corpus}")


# --- HPC bridge (optional) ---------------------------------------------------
def check_hpc_config() -> None:
    cfg = ROOT / "projects_access.yaml"
    if not cfg.exists():
        row("SKIP", "hpc bridge", "no projects_access.yaml — local-only mode "
            "(fine; see README 'HPC bridge' to add a cluster)")
        return
    if not RUNTIME_PY.exists():
        row("SKIP", "hpc bridge", "projects_access.yaml present, but no runtime env to parse it with")
        return
    rc, out = run([str(RUNTIME_PY), "-c", (
        "import yaml, pathlib; "
        f"d = yaml.safe_load(pathlib.Path({str(cfg)!r}).read_text()) or {{}}; "
        "print(len(d.get('compute_envs') or []), len(d.get('projects') or []))")])
    if rc != 0:
        row("FAIL", "hpc bridge", f"projects_access.yaml does not parse: {out.splitlines()[-1] if out else '?'}",
            "fix the YAML — compare against agent/skills/projects_access.yaml.example")
        return
    n_envs, n_projects = (out.split() + ["0", "0"])[:2]
    row("PASS", "hpc bridge", f"projects_access.yaml — {n_envs} compute env(s), "
        f"{n_projects} project(s) declared (reachability is probed at drive time)")


def main() -> int:
    print(f"bioinf-agent systems check — {ROOT}")
    check_conda()
    check_runtime_env()
    check_agent_import()
    check_docker()
    check_mcp_registration()
    check_core_data()
    check_hpc_config()

    width = max(len(n) for _, n, _, _ in ROWS)
    fails = 0
    for verdict, name, detail, fix in ROWS:
        print(f"[{verdict}] {name.ljust(width)}  {detail}")
        if verdict == "FAIL":
            fails += 1
            if fix:
                print(f"{' ' * (width + 8)}fix: {fix}")
    n_pass = sum(1 for v, *_ in ROWS if v == "PASS")
    n_skip = sum(1 for v, *_ in ROWS if v == "SKIP")
    print(f"{n_pass} passed, {fails} failed, {n_skip} skipped")
    return fails


if __name__ == "__main__":
    sys.exit(main())
