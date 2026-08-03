"""job_runner — the child half of `@backgroundable`.

Invoked by `backgroundable.spawn_detached` as:

    python -m agent.skills.job_runner <tool_name> <args_path> <result_path>

`args_path`   — JSON kwargs the parent wrote BEFORE spawning.
`result_path` — where the tool's real return value lands, for `check_job` to
                inline once the job exits.

Generalized from `freeze_runner`, which did exactly this for one tool. Its
lessons are kept verbatim and now apply to every backgroundable tool:

  * A script, not `python -c`. Results are deeply nested dicts; serializing one
    back through shell quoting is fragile, and a script gives a place to catch a
    top-level exception into a STRUCTURED failure rather than a subprocess that
    dies with no record.
  * `background=False` is forced. Without it a caller-side bug that let
    background=True through would make the child spawn ANOTHER child and exit
    immediately — a job that always "succeeds" and never runs.
  * `os._exit()` at the end. `import agent.mcp_server` pulls in FastMCP and its
    providers, which spawn NON-DAEMON threads (Docker SDK pools, component
    watchers). Those keep the interpreter alive after the work is done, and the
    parent keys job state off `proc.poll()` — a 0.3 s adopt-by-digest freeze was
    once observed lingering ~9.5 min with check_job stuck on 'running'. The
    result file is closed and both streams are flushed first, so nothing is lost.

Progress markers go to stdout, which is the JobManager log — `check_job`'s
`log_tail` is the only visibility into a detached run while it is in flight.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback


def main() -> int:
    if len(sys.argv) != 4:
        sys.stderr.write("usage: python -m agent.skills.job_runner "
                         "<tool_name> <args_path> <result_path>\n")
        return 2

    tool, args_path, result_path = sys.argv[1], sys.argv[2], sys.argv[3]
    print(f"[job_runner] start {time.strftime('%Y-%m-%d %H:%M:%S')} tool={tool}", flush=True)
    print(f"[job_runner] args_path={args_path}", flush=True)
    print(f"[job_runner] result_path={result_path}", flush=True)

    try:
        with open(args_path) as f:
            args = json.load(f)
        if not isinstance(args, dict):
            raise TypeError(f"args file holds {type(args).__name__}, expected an object")
    except Exception as e:
        print(f"[job_runner] FAILED to read args: {e!r}", flush=True)
        _write_result(result_path, {
            "success": False,
            "stage": "background_args_read",
            "tool": tool,
            "error": repr(e),
            "traceback": traceback.format_exc(),
        })
        return 1

    # Force synchronous mode in the child — see the module docstring.
    args["background"] = False

    try:
        # Lazy import: the tool surface pulls in Docker SDKs and conda tooling.
        # Doing it inside main() means a misconfigured environment still gets a
        # structured result instead of an import-time crash with no record.
        # Importing the server is also what POPULATES the allowlist below —
        # BACKGROUNDABLE is filled by the @backgroundable decorators as the
        # tool modules import, so the allowlist cannot drift from the decoration.
        import agent.mcp_server as _ms
        from agent.skills.backgroundable import BACKGROUNDABLE
    except BaseException as e:
        print(f"[job_runner] EXCEPTION importing the tool surface: {e!r}", flush=True)
        traceback.print_exc()
        _write_result(result_path, {
            "success": False,
            "stage": "background_import",
            "tool": tool,
            "error": repr(e),
            "traceback": traceback.format_exc(),
        })
        return 1

    if tool not in BACKGROUNDABLE:
        # Closed over the decorated set: the runner never resolves an arbitrary
        # attribute name into a callable.
        print(f"[job_runner] REFUSED unknown tool {tool!r}", flush=True)
        _write_result(result_path, {
            "success": False,
            "stage": "background_unknown_tool",
            "tool": tool,
            "error": f"{tool!r} is not decorated with @backgroundable",
            "known": sorted(BACKGROUNDABLE),
        })
        return 1

    fn = getattr(_ms, tool, None)
    if not callable(fn):
        # Decorated but not re-exported on mcp_server — a wiring bug, not a
        # caller error. Name it precisely so it is fixed rather than retried.
        print(f"[job_runner] REFUSED {tool!r} not importable from agent.mcp_server", flush=True)
        _write_result(result_path, {
            "success": False,
            "stage": "background_tool_not_exported",
            "tool": tool,
            "error": f"{tool!r} is @backgroundable but agent.mcp_server has no "
                     f"callable of that name — add it to the back-compat "
                     f"re-exports at the bottom of agent/mcp_server.py",
        })
        return 1

    from agent.skills.backgroundable import _describe
    print(f"[job_runner] calling {tool}({_describe(args)})", flush=True)

    try:
        t0 = time.time()
        result = fn(**args)
        elapsed = time.time() - t0
        if not isinstance(result, dict):
            result = {"success": True, "result": result}
        print(f"[job_runner] {tool}() returned in {elapsed:.1f}s "
              f"success={result.get('success')} code={result.get('code','-')}",
              flush=True)
    except BaseException as e:
        # BaseException so KeyboardInterrupt / SystemExit also land in the result
        # file. A subprocess that exits silently is the failure this whole
        # feature exists to fix.
        print(f"[job_runner] EXCEPTION in {tool}(): {e!r}", flush=True)
        traceback.print_exc()
        _write_result(result_path, {
            "success": False,
            "stage": "background_exception",
            "tool": tool,
            "error": repr(e),
            "traceback": traceback.format_exc(),
        })
        return 1

    _write_result(result_path, result)
    print(f"[job_runner] wrote result_path={result_path}", flush=True)
    return 0 if result.get("success") else 1


def _write_result(path: str, result: dict) -> None:
    """Serialize the result to JSON, atomically.

    tempfile + rename so a `check_job` landing mid-write never reads a truncated
    object and reports it as the tool's outcome. `default=str` here is a
    LAST-RESORT stringify on the way OUT — unlike the args side (which refuses,
    because a mangled argument changes what runs), a mangled field in a report
    is better than no report at all.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(result, f, default=str, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
