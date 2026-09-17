"""The setup surface stays quiet about other people's upgrades, and the doctor
states what it could not check.

Round 2 of the cold-start drive found the first thing a new user reads mid-setup
is conda telling them to update BASE conda, and the first thing the server prints
is fastmcp telling them to upgrade a deliberately pinned dependency (CS11/CS12/
CS50/CS51). Both suppressions are environment knobs, and an env knob is exactly
the kind of fix that silently falls off in a refactor — so each one is pinned
here at the layer it lives on. The doctor's bridge-prerequisite rows are pinned
too, including the failure shape found in audit: a stray stderr line from the
probing child turned a declared bridge into ZERO rows, absence unstated.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENV_SH = ROOT / "scripts" / "_env.sh"
LAUNCHER = ROOT / "scripts" / "start_mcp_server.sh"


# --- the two nag suppressions -------------------------------------------------

def _sourced_var(name: str, preset: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k != name}
    if preset is not None:
        env[name] = preset
    p = subprocess.run(
        ["bash", "-c", f'source "{ENV_SH}" && printf %s "${name}"'],
        capture_output=True, text=True, timeout=30, env=env)
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_env_sh_silences_condas_update_base_banner():
    """Every script that sources _env.sh (setup, the launcher, the bootstrap)
    runs conda with the outdated-conda notice off — the notice's remedy is
    `conda update -n base`, the exact mutation the setup story keeps users
    away from."""
    assert _sourced_var("CONDA_NOTIFY_OUTDATED_CONDA") == "false"


def test_env_sh_respects_a_users_explicit_choice():
    """Suppression is a default, not a decree."""
    assert _sourced_var("CONDA_NOTIFY_OUTDATED_CONDA", preset="true") == "true"


def test_python_m_agent_sets_both_knobs_and_its_own_bin_on_path():
    """`python -m agent` — the documented production launch — sources no shell
    file, so agent/__main__.py must carry the same environment itself: the
    fastmcp update banner off (before fastmcp loads), conda's nag off (for
    EnvManager's child runs), and the interpreter's bin/ on PATH (globus-cli
    lives there). Checked in a subprocess so this test's own env stays clean."""
    code = (
        "import agent.__main__, os, sys; "
        "from pathlib import Path; "
        "print(os.environ['FASTMCP_CHECK_FOR_UPDATES']); "
        "print(os.environ['CONDA_NOTIFY_OUTDATED_CONDA']); "
        "print(str(Path(sys.executable).resolve().parent) in "
        "os.environ['PATH'].split(os.pathsep))")
    env = {k: v for k, v in os.environ.items()
           if k not in ("FASTMCP_CHECK_FOR_UPDATES", "CONDA_NOTIFY_OUTDATED_CONDA")}
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=60, cwd=str(ROOT), env=env)
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == ["off", "false", "True"]


def test_the_launcher_puts_the_runtime_envs_bin_on_path():
    """transfer_providers invokes `globus` off PATH; setup installs globus-cli
    into .conda_runtime. The launcher is what joins those two facts."""
    assert 'PATH="$BIOINF_RUNTIME/bin:' in LAUNCHER.read_text()


def test_the_hpc_extra_carries_the_globus_cli():
    """setup.sh installs `.[dev,hpc]`; if the extra loses globus-cli the install
    line keeps succeeding while the wire silently loses its CLI."""
    import tomllib
    py = tomllib.loads((ROOT / "pyproject.toml").read_text())
    hpc = py["project"]["optional-dependencies"]["hpc"]
    assert any(dep.startswith("globus-cli") for dep in hpc), hpc
    assert "[dev,hpc]" in (ROOT / "scripts" / "setup.sh").read_text()


# --- the doctor's bridge-prerequisite rows -------------------------------------

@pytest.fixture()
def doctor(monkeypatch):
    spec = importlib.util.spec_from_file_location("_doctor", ROOT / "scripts" / "doctor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROWS.clear()
    return mod


ENVS = [{"name": "hpc", "type": "ssh", "host": "cluster-alias", "user": "me",
         "wire": "globus"},
        {"name": "laptop", "type": "local", "host": None, "user": None, "wire": None}]


def _fake_run(json_out: str, ssh_rc: int, globus_rc: int):
    def run(argv, timeout=15, cwd=None):
        if argv[0] == "ssh":
            return ssh_rc, ""
        if argv[-1] == "version":
            return globus_rc, ""
        return 0, json_out
    return run


def test_a_live_control_master_is_a_pass_and_a_dead_one_names_the_exact_command(
        doctor, monkeypatch):
    monkeypatch.setattr(doctor, "run", _fake_run(json.dumps(ENVS), ssh_rc=0, globus_rc=0))
    monkeypatch.setattr(doctor.shutil, "which", lambda *_: None)
    doctor.check_bridge_prerequisites(Path("/tmp/pa.yaml"))
    verdicts = {(v, n) for v, n, *_ in doctor.ROWS}
    assert ("PASS", "ssh session (hpc)") in verdicts
    assert ("PASS", "globus CLI") in verdicts

    doctor.ROWS.clear()
    monkeypatch.setattr(doctor, "run", _fake_run(json.dumps(ENVS), ssh_rc=255, globus_rc=0))
    doctor.check_bridge_prerequisites(Path("/tmp/pa.yaml"))
    skip = next(r for r in doctor.ROWS if r[1] == "ssh session (hpc)")
    assert skip[0] == "SKIP"
    # The fix must name the TARGET the check probed (`ssh me@cluster-alias`),
    # not a bare host a differently-usered socket would never match.
    assert "ssh me@cluster-alias" in skip[2]


def test_a_declared_globus_wire_with_no_cli_is_a_fail_that_names_setup(doctor, monkeypatch):
    monkeypatch.setattr(doctor, "run", _fake_run(json.dumps(ENVS), ssh_rc=0, globus_rc=127))
    monkeypatch.setattr(doctor.shutil, "which", lambda *_: None)
    doctor.check_bridge_prerequisites(Path("/tmp/pa.yaml"))
    fail = next(r for r in doctor.ROWS if r[1] == "globus CLI")
    assert fail[0] == "FAIL" and "setup.sh" in fail[3]


def test_stderr_noise_around_the_probe_json_does_not_erase_the_rows(doctor, monkeypatch):
    """run() returns stdout+stderr concatenated; a warning line from the child
    python must not turn a declared bridge into zero rows (the audit's exact
    reproduction: JSONDecodeError -> envs=[] -> silence)."""
    noisy = "SomeWarning: yaml is feeling chatty\n" + json.dumps(ENVS) + "\ntrailer"
    monkeypatch.setattr(doctor, "run", _fake_run(noisy, ssh_rc=0, globus_rc=0))
    monkeypatch.setattr(doctor.shutil, "which", lambda *_: None)
    doctor.check_bridge_prerequisites(Path("/tmp/pa.yaml"))
    assert any(n == "ssh session (hpc)" for _, n, *_ in doctor.ROWS)


def test_an_unreadable_probe_is_a_stated_skip_never_silence(doctor, monkeypatch):
    """Absence is stated: when the env list cannot be read at all, the doctor
    says the rows were NOT CHECKED rather than printing nothing."""
    monkeypatch.setattr(doctor, "run", _fake_run("total garbage", ssh_rc=0, globus_rc=0))
    doctor.check_bridge_prerequisites(Path("/tmp/pa.yaml"))
    assert len(doctor.ROWS) == 1
    verdict, name, detail, _ = doctor.ROWS[0]
    assert verdict == "SKIP" and "not checked, not passed" in detail
