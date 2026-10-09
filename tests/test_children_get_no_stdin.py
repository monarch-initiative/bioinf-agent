"""Child processes never see this server's stdin, and a timeout kills the whole tree.

The server's fd 0 is the MCP transport. A child that inherits it and reads — a tool run
with no file argument, or ssh forwarding stdin to the remote — waits on the protocol
socket for ever, and the timeout around it only helps if the kill reaches every
descendant rather than the wrapper alone. These put a pipe nobody writes to on fd 0,
which is exactly what the transport socket looks like to a child.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time

import pytest

from agent.skills import _proc
from agent.skills.env_manager import EnvManager
from agent.skills.snapshot import _ssh_argv


@contextlib.contextmanager
def _stdin_that_never_speaks(release_after: float = 8.0):
    """A pipe nobody writes to on fd 0. Its write end closes on exit, and after
    `release_after` seconds regardless, so a child that was wrongly handed this
    stdin reads EOF late and the test fails instead of hanging the suite."""
    r, w = os.pipe()
    saved = os.dup(0)
    os.dup2(r, 0)
    lock = threading.Lock()
    open_w = [True]

    def release():
        with lock:
            if open_w[0]:
                open_w[0] = False
                os.close(w)

    timer = threading.Timer(release_after, release)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        os.dup2(saved, 0)
        os.close(saved)
        os.close(r)
        release()


def _monitored(cmd, timeout, cwd):
    em = EnvManager.__new__(EnvManager)          # _run_monitored reads nothing off self
    return EnvManager._run_monitored(em, cmd, cwd=cwd, timeout=timeout)


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    psutil = pytest.importorskip("psutil")
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_a_monitored_child_that_reads_stdin_sees_eof_not_the_transport(tmp_path):
    with _stdin_that_never_speaks():
        t0 = time.monotonic()
        r = _monitored(["bash", "-c", "cat; echo alive"], timeout=5, cwd=str(tmp_path))
    assert r["returncode"] == 0 and "alive" in r["stdout"]
    assert time.monotonic() - t0 < 4


def test_a_monitored_timeout_kills_the_grandchildren_too(tmp_path):
    pidfile = tmp_path / "inner.pid"
    inner = f"echo $$ > {pidfile}; exec sleep 60"
    t0 = time.monotonic()
    r = _monitored(["bash", "-c", f"bash -c '{inner}'; true"], timeout=1, cwd=str(tmp_path))
    wall = time.monotonic() - t0
    assert r["returncode"] == -1 and "timed out" in r["stderr"]
    assert wall < 8, f"communicate() waited on orphans for {wall:.0f}s"
    pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 3
    while not _gone(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _gone(pid), "the inner sleep outlived the timeout"


def test_the_argv_runner_gives_its_child_no_stdin(tmp_path):
    with _stdin_that_never_speaks():
        t0 = time.monotonic()
        r = _proc.run_argv(["bash", "-c", "cat; echo alive"], 5, cwd=str(tmp_path))
    assert r["returncode"] == 0 and "alive" in r["stdout"]
    assert time.monotonic() - t0 < 4


def test_ssh_never_forwards_this_process_stdin():
    argv = _ssh_argv({"host": "h", "user": "u"}, "true")
    assert argv[0] == "ssh" and "-n" in argv[: argv.index("u@h")]
