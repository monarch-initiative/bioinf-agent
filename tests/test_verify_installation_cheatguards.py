"""C1 — false-green certification of EnvManager.verify (the anti-cheat gate).

`verify(env, package, check_command)` is the honesty gate that decides whether
an install actually happened. Its green (`env_manager.verified`) is only honest
if it CANNOT be faked by a check_command that prints a plausible version string
without anything installed. Three independent gates must all hold:
  (a) the command exits 0,
  (b) it references the package as a word-boundary token (not `echo "fake"`),
  (c) the package is present per an anchor the agent can't influence
      (`which` OR the conda/pip/R registry).

These drive the whole method with the three runtime seams mocked
(run_in_env / evidence.cli_which / _package_in_registry), so every branch is
deterministic. The false-green attacks — echo-cheat and library-only-cheat —
must NOT reach `proven`.
"""
from __future__ import annotations

import yaml
from pathlib import Path

import pytest

from agent.skills import evidence
from agent.skills.env_manager import EnvManager


def _em() -> EnvManager:
    cfg = yaml.safe_load((Path(__file__).parent.parent / "config" / "agent_config.yaml").read_text())
    return EnvManager(cfg)


def _wire(monkeypatch, em, *, rc: int, which_anchored: bool, in_registry: bool, out: str = ""):
    """Mock the three runtime truth-sources verify() consults."""
    monkeypatch.setattr(em, "run_in_env",
                        lambda env, cmd, timeout=30: {"stdout": out, "stderr": "", "returncode": rc})
    monkeypatch.setattr(evidence, "cli_which",
                        lambda mgr, env, name: {"anchored": which_anchored,
                                                "detail": f"/env/bin/{name}" if which_anchored else ""})
    monkeypatch.setattr(em, "_package_in_registry", lambda env, name: in_registry)


def test_verify_proven_when_all_three_gates_hold(monkeypatch):
    """The honest green: command exits 0, names the tool, and the tool is present
    per an independent anchor → env_manager.verified."""
    em = _em()
    _wire(monkeypatch, em, rc=0, which_anchored=True, in_registry=False, out="samtools 1.21")
    res = em.verify("bioinf_x", "samtools", "samtools --version")
    assert res.get("outcome") == "proven", res
    assert res.get("code") == "env_manager.verified", res
    assert res.get("success") is True


def test_verify_rejects_echo_cheat(monkeypatch):
    """FALSE-GREEN ATTACK (echo cheat): `echo '1.21'` exits 0 but does NOT name
    the package as a token → must be refused, never proven, even if the package
    happened to be present."""
    em = _em()
    _wire(monkeypatch, em, rc=0, which_anchored=True, in_registry=True, out="1.21")
    res = em.verify("bioinf_x", "samtools", "echo '1.21'")
    assert res.get("outcome") != "proven", res
    assert res.get("code") == "env_manager.verify_rejected", res
    assert res.get("success") is False
    assert res.get("rejection_reason")


def test_verify_rejects_library_only_cheat(monkeypatch):
    """FALSE-GREEN ATTACK (library-only cheat): the command names the package and
    exits 0, but NOTHING is installed (no `which`, not in the registry) — e.g.
    `python -c "import numpy; print('numpy 2.4')"` against an empty env. The
    registry/which anchor the agent can't fake must reject it."""
    em = _em()
    _wire(monkeypatch, em, rc=0, which_anchored=False, in_registry=False, out="numpy 2.4")
    res = em.verify("bioinf_x", "numpy", 'python -c "import numpy; print(numpy.__version__)"')
    assert res.get("outcome") != "proven", res
    assert res.get("code") == "env_manager.verify_rejected", res
    assert res.get("success") is False
    assert "not present" in (res.get("rejection_reason") or "")


def test_verify_broke_on_real_tool_failure(monkeypatch):
    """A genuine verify failure: the command names the tool and the tool IS
    present, but the check exits nonzero (tool errors) → broke verify_failed,
    NOT a rejection (nothing was cheated) and NOT a green."""
    em = _em()
    _wire(monkeypatch, em, rc=1, which_anchored=True, in_registry=False, out="segfault")
    res = em.verify("bioinf_x", "samtools", "samtools --version")
    assert res.get("outcome") == "broke", res
    assert res.get("code") == "env_manager.verify_failed", res
    assert res.get("success") is False
