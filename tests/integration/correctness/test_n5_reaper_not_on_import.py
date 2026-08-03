"""
N5 (batch-3 Apollo3 stress): the orphan-service-PID reaper must run ONLY in
the actual MCP server process (the __main__ entrypoint), NOT at module
import. Pre-fix it ran at module-import; the W1 background subprocess
(`from agent.mcp_server import freeze`) imported it on startup, which then
reaped PID files belonging to the PARENT's still-running services (because
start_service writes the wrapper bash's PID, not the daemon's, and by the
time the runner imported the wrapper bash had often exited). The
parent's later stop_service() then found no PID file and orphaned the real
daemon. Apollo3 left a real mongod running on port 27027 because of this.

Why integration, not unit: this is a side-effect of MODULE IMPORT. A unit
test using `import agent.mcp_server` in the test process can't tell whether
the reaper ran or not — the module's already been imported (it gets reused
across tests). The only way to actually prove the no-import-side-effect is
to spawn a fresh Python subprocess that imports the module and check the
filesystem AFTER. That's what this does.

WHY BOTH TESTS ARE PINNED TO ONE XDIST WORKER. They share the real global
`/tmp/bioinf_services` — and they must: test 1's whole method is a COLD
subprocess, which reads the hardcoded path in env_manager and cannot see a
monkeypatch made in this process. Test 2 then calls the reaper, which walks
that entire directory and removes every orphan it finds.

Run on separate workers, test 2 deletes the file test 1 planted, and test 1
fails with "reaper ran on module import" — accusing the code of the exact
defect this file exists to catch, when nothing of the sort occurred. A flake
that invents a plausible bug report is worse than a flake that just fails,
so this is a correctness fix, not a speed accommodation. `xdist_group` keeps
them on one worker, where they run in file order and cannot interleave.

(The underlying sharpness is that `/tmp/bioinf_services` is spelled out four
separate times in env_manager.py. Making it one constant would let a test
inject a private directory — but not for test 1, whose subprocess is cold by
design. Grouping is the honest fix for these two.)
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest


@pytest.mark.integration
@pytest.mark.xdist_group("service_pid_registry")
def test_import_does_not_reap_orphan_service_pids(tmp_path):
    """The acid test: drop an obviously-orphan PID file under the shared
    service-registry dir, spawn a Python subprocess that imports
    agent.mcp_server, and assert the file is still there after."""
    pid_dir = Path("/tmp/bioinf_services")
    pid_dir.mkdir(parents=True, exist_ok=True)
    # Unique name so we don't collide with any other tests / real services.
    fake = pid_dir / f"_test_n5_no_reap_{uuid.uuid4().hex[:8]}.pid"
    # PID 999999 won't exist on any sane macOS/linux box (kernel.pid_max is
    # typically 32768-99999). os.kill(p, 0) will raise ProcessLookupError,
    # which is the EXACT case the reaper would clean up.
    fake.write_text("999999")
    try:
        # Spawn a totally fresh interpreter so the import is from cold —
        # no shared module state from this test process.
        repo_root = Path(__file__).resolve().parents[2]
        code = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(repo_root)!r})
            import agent.mcp_server   # noqa: F401
            print("imported")
        """)
        env = dict(os.environ)
        env["BIOINF_MCP_AUTO_RELOAD"] = "0"   # avoid any startup hooks
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=30, env=env,
        )
        assert result.returncode == 0, \
            f"subprocess import failed: stdout={result.stdout!r} stderr={result.stderr!r}"
        assert "imported" in result.stdout

        # The acid check: orphan PID file must STILL exist. If module import
        # ran the reaper, this file would be gone.
        assert fake.exists(), \
            f"reaper ran on module import — orphan PID file {fake} was removed"
    finally:
        fake.unlink(missing_ok=True)


@pytest.mark.integration
@pytest.mark.xdist_group("service_pid_registry")
def test_explicit_reaper_call_still_works(tmp_path):
    """Sibling guarantee: the no-import-side-effect MUST NOT also disable
    the reaper. Calling _reap_orphan_service_pids() explicitly (as the MCP
    server's __main__ does at startup) must still clean orphan PIDs."""
    from agent.mcp_server import _reap_orphan_service_pids
    from agent.skills.env_manager import EnvManager

    pid_dir = Path("/tmp/bioinf_services")
    pid_dir.mkdir(parents=True, exist_ok=True)
    fake = pid_dir / f"_test_n5_explicit_{uuid.uuid4().hex[:8]}.pid"
    fake.write_text("999999")
    try:
        # cleanup_orphan_service_pids walks the whole pid_dir; we filter on
        # our own file so cohabiting test artifacts don't affect the result.
        result = EnvManager.cleanup_orphan_service_pids()
        assert fake.name in result.get("removed", []), \
            f"explicit reaper call did not remove the orphan PID file: {result}"
        assert not fake.exists()
    finally:
        fake.unlink(missing_ok=True)
