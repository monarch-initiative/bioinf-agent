"""F16 — a build container that outlives the process that made it.

`ContainerBuild.close()` removes the container, and `EnvBuild`'s `finally:`
makes that survive an exception. Neither survives the process being KILLED:
the ~600 s stream-watchdog, or `BIOINF_MCP_AUTO_RELOAD` reloading the MCP
server mid-freeze. No Python runs after SIGKILL, so the container stays,
holding its entire install layer. Ten of them — 4.485 GB, the oldest two
weeks old — were found on this machine during the 2026-08-07 disk cleanup,
which is the whole reason this file exists.

The fix is a labelled container plus a sweep at the next build's start, and
the property that MATTERS is not "leaks get reaped" but "a CONCURRENT build
does not get reaped". Different envs freeze fully in parallel, so a sweep
that removed every labelled container would murder a live sibling build.
Ownership is what separates them, and both directions are asserted here.
"""
from __future__ import annotations

import os

import agent.skills.container_build as cbmod
from agent.skills.container_build import (
    _BUILD_LABEL,
    _BUILD_OWNER_LABEL,
    ContainerBuild,
    _owner_is_alive,
    sweep_leaked_build_containers,
)


class _FakeRun:
    """Records every docker argv and answers `docker ps` from a fixture."""

    def __init__(self, listing: str):
        self.listing = listing
        self.calls: list[list[str]] = []

    def __call__(self, args, **kw):
        self.calls.append(list(args))

        class R:
            returncode = 0
            stderr = ""

        R.stdout = self.listing if (len(args) > 1 and args[1] == "ps") else ""
        return R

    def removed(self) -> list[str]:
        return [a[3] for a in self.calls if a[:3] == ["docker", "rm", "-f"]]


def _dead_pid() -> int:
    """A pid that certainly names no live process. 2**22 is above every
    platform's default pid_max, so it cannot be recycled onto a real one."""
    return 4194303


# -- ownership ---------------------------------------------------------------

def test_our_own_process_is_alive():
    assert _owner_is_alive(str(os.getpid())) is True


def test_a_dead_pid_is_not_alive():
    assert _owner_is_alive(str(_dead_pid())) is False


def test_an_unreadable_owner_label_is_treated_as_alive():
    """An unlabelled or garbled container predates the sweep, or came from
    somewhere else entirely. The sweep declines to guess: it must fail toward
    LEAVING a container alone, because the opposite failure kills a live build."""
    for junk in ("", "not-a-pid", "-1", "0", None):
        assert _owner_is_alive(junk) is True, junk


# -- the sweep ---------------------------------------------------------------

def test_sweep_removes_a_container_whose_owner_is_gone(monkeypatch):
    fake = _FakeRun(f"aaaaaaaaaaaa\t{_dead_pid()}\n")
    monkeypatch.setattr(cbmod.subprocess, "run", fake)

    out = sweep_leaked_build_containers()

    assert out["swept"] == ["aaaaaaaaaaaa"], out
    assert fake.removed() == ["aaaaaaaaaaaa"], fake.calls


def test_sweep_leaves_a_concurrent_builds_container_alone(monkeypatch):
    """THE load-bearing case. A parallel freeze into a different env owns a live
    container carrying the same label; reaping it would destroy that build's
    work mid-install and the failure would surface as an unrelated docker error."""
    fake = _FakeRun(f"bbbbbbbbbbbb\t{os.getpid()}\n")
    monkeypatch.setattr(cbmod.subprocess, "run", fake)

    out = sweep_leaked_build_containers()

    assert out["swept"] == [], out
    assert fake.removed() == [], "a live sibling build's container was reaped"
    assert out["kept"][0]["reason"] == "owner_alive", out


def test_sweep_reaps_only_the_dead_when_both_are_present(monkeypatch):
    fake = _FakeRun(f"dddddddddddd\t{_dead_pid()}\nllllllllllll\t{os.getpid()}\n")
    monkeypatch.setattr(cbmod.subprocess, "run", fake)

    out = sweep_leaked_build_containers()

    assert out["swept"] == ["dddddddddddd"], out
    assert fake.removed() == ["dddddddddddd"], fake.calls


def test_sweep_asks_docker_only_for_our_own_labelled_containers(monkeypatch):
    """Scoped by label, so a sweep can never touch a container this project did
    not create — the cleanup stays restricted to this project's own leavings."""
    fake = _FakeRun("")
    monkeypatch.setattr(cbmod.subprocess, "run", fake)

    sweep_leaked_build_containers()

    ps = fake.calls[0]
    assert "--filter" in ps and f"label={_BUILD_LABEL}=1" in ps, ps


def test_a_docker_failure_reports_itself_rather_than_claiming_a_clean_sweep(monkeypatch):
    def boom(args, **kw):
        raise OSError("docker daemon not running")

    monkeypatch.setattr(cbmod.subprocess, "run", boom)

    out = sweep_leaked_build_containers()

    assert out["swept"] == []
    assert "error" in out, "a sweep that could not run must say so, not return empty"


# -- the label that makes all of the above possible --------------------------

def test_start_stamps_the_container_with_its_owner(monkeypatch):
    """Without both labels the sweep has nothing to find and nothing to compare,
    so this asserts the producer side of the contract the sweep consumes."""
    cb = ContainerBuild()
    seen: list[list[str]] = []

    def fake_sh(args, timeout=1800):
        seen.append(list(args))
        return {"returncode": 0, "stdout": "cid123", "stderr": ""}

    monkeypatch.setattr(cb, "_sh", fake_sh)
    monkeypatch.setattr(cbmod, "sweep_leaked_build_containers", lambda: {"swept": [], "kept": []})

    cb.start()

    run = next(a for a in seen if a[:3] == ["docker", "run", "-d"])
    assert f"{_BUILD_LABEL}=1" in run, run
    assert f"{_BUILD_OWNER_LABEL}={os.getpid()}" in run, run


def test_start_sweeps_before_it_builds(monkeypatch):
    """The sweep has to run at the START of a build, not the end: a leak is only
    ever discovered by the NEXT build, since the one that leaked is dead."""
    cb = ContainerBuild()
    order: list[str] = []

    monkeypatch.setattr(cbmod, "sweep_leaked_build_containers",
                        lambda: (order.append("sweep"), {"swept": [], "kept": []})[1])

    def fake_sh(args, timeout=1800):
        if args[:3] == ["docker", "run", "-d"]:
            order.append("run")
        return {"returncode": 0, "stdout": "cid123", "stderr": ""}

    monkeypatch.setattr(cb, "_sh", fake_sh)

    cb.start()

    assert order[:2] == ["sweep", "run"], order


# ---------------------------------------------------------------------------
# THE CLASS, not the instance.
#
# F16 was fixed at ONE launcher (ContainerBuild.start). An audit then found a
# second, `docker_builder.py`'s `docker run -d`, launching an unlabelled detached
# container holding a whole step's working set — one the sweep can never reap,
# because the sweep is scoped by label. Fixing a leak at one site and leaving
# another is how a class survives its own repair.
#
# So this is a SOURCE SWEEP rather than another per-site test: every detached
# `docker run -d` in agent/ must carry the labels, including ones written after
# this file. That is the difference between a guard against an instance and a
# guard against a class, and it is the property the whole fix phase was judged on.
# ---------------------------------------------------------------------------

import ast
from pathlib import Path

_AGENT = Path(__file__).resolve().parents[1] / "agent"


def _detached_run_sites():
    """Every `docker run -d ...` argv list literal in agent/, with its labels."""
    out = []
    for py in sorted(_AGENT.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.List):
                continue
            head = [e.value for e in node.elts[:3]
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if head[:3] != ["docker", "run", "-d"]:
                continue
            literals = [e.value for e in node.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            fstrings = [e for e in node.elts if isinstance(e, ast.JoinedStr)]
            has_label_flag = "--label" in literals
            # the owner label is an f-string (`f"{LABEL}={os.getpid()}"`), so its
            # presence is checked structurally rather than by value
            out.append({"file": str(py.relative_to(_AGENT.parent)), "line": node.lineno,
                        "has_label_flag": has_label_flag,
                        "fstring_count": len(fstrings)})
    return out


def test_every_detached_container_in_the_codebase_is_labelled():
    sites = _detached_run_sites()
    assert sites, "no `docker run -d` found — this sweep has stopped looking at anything"
    unlabelled = [s for s in sites if not s["has_label_flag"]]
    assert not unlabelled, (
        "detached container launch(es) with no bioinf_agent label: "
        + ", ".join(f"{s['file']}:{s['line']}" for s in unlabelled)
        + ". sweep_leaked_build_containers() is scoped by label, so an unlabelled "
          "container is one it can never reap. Add both labels (see "
          "container_build.ContainerBuild.start) or the leak F16 fixed comes back "
          "through this door.")


def test_every_labelled_launch_also_records_its_owner():
    """The label alone makes a container findable; the OWNER PID is what makes it
    safe to reap. A site with the first and not the second would be swept while a
    live build was using it."""
    for s in _detached_run_sites():
        assert s["fstring_count"] >= 2, (
            f"{s['file']}:{s['line']} labels its container but interpolates fewer than "
            f"two values — the owner pid label looks to be missing")
