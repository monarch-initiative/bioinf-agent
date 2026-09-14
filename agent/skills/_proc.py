"""
The ONE argv subprocess runner.

Measured 2026-09-14: seven generic runners existed across agent/, in THREE error
dialects — {rc:124/127} (local_sif, freeze_from_image, container_build),
{-1 for everything} (locus, docker_builder, env_manager), and a semantic
tool_found flag (output_validator) — with local_sif._run and freeze_from_image._sh
byte-identical. A missing daemon surfaced differently depending on which module
happened to make the call (mcp_server._check_docker_available documents exactly
this). This module is the consolidation: one subprocess invocation, one timeout
discipline (rc 124 with partial-output salvage), one missing-binary discipline
(rc 127), errors="replace" everywhere (a tool's banner can emit non-UTF-8 —
pigz writes gzip magic to stdout — and strict decoding crashed the whole build).

DELIBERATELY NOT CONSOLIDATED — each is a documented contract, not a duplicate:
  * locus._sh          — never-fatal by design; every failure is rc -1 because a
                         locus probe must not distinguish "daemon down" from
                         "docker absent" (its callers treat both as unobserved).
  * output_validator._run_tool — returns CompletedProcess + tool_found; the H1
                         honesty fix that stops a MISSING validator being
                         laundered into a pass. Flattening it reopens that bug.
  * env_manager._run_monitored — the psutil process-tree poller; the SOLE
                         producer of pipeline_step resource_usage (I7).

Two result spellings, ONE implementation: run_argv is canonical
({returncode, stdout, stderr}); run_argv_rc remaps to the {rc, out, err}
dialect local_sif/freeze_from_image read. The remap cannot drift — it
delegates — which is the property the byte-identical pair lacked.
"""
from __future__ import annotations

import subprocess
from typing import Any, Optional


def run_argv(argv: list[str], timeout: int, *,
             cwd: Optional[str] = None,
             env: Optional[dict] = None) -> dict[str, Any]:
    """Run argv (never a shell string); return {returncode, stdout, stderr}.

    Timeout → rc 124 with whatever partial output the process produced (salvaged
    off the exception, bytes-or-str normalised). Missing binary → rc 127. Any
    other exception propagates — a wrapper that needs never-fatal semantics says
    so at ITS seam (env_manager/docker_builder keep theirs, documented there).
    """
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           errors="replace", timeout=timeout,
                           cwd=cwd, env=env)
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
        err = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode("utf-8", "replace")
        return {"returncode": 124, "stdout": out or "",
                "stderr": ((err or "") + f"\n[_proc] command timed out after {timeout}s").strip()}
    except FileNotFoundError as e:
        return {"returncode": 127, "stdout": "", "stderr": str(e)}
    return {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}


def run_argv_rc(argv: list[str], timeout: int = 300, *,
                cwd: Optional[str] = None,
                env: Optional[dict] = None) -> dict[str, Any]:
    """run_argv, spelled {rc, out, err} for the modules that read that dialect."""
    r = run_argv(argv, timeout, cwd=cwd, env=env)
    return {"rc": r["returncode"], "out": r["stdout"] or "", "err": r["stderr"] or ""}
