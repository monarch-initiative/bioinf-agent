"""
freeze_runner — small driver script that invokes freeze() synchronously in a
detached subprocess. The W1 mitigation companion to mcp_server._freeze_in_
background.

Invoked as:
    python -m agent.skills.freeze_runner <args_path> <result_path>

`args_path`   — JSON file with the freeze() kwargs (env_name, tools, ...). The
                parent's _freeze_in_background writes this before spawn.
`result_path` — where to drop the freeze result JSON when done. The parent's
                check_job-polling loop reads this once the subprocess exits.

Why a script (not a python -c one-liner): freeze's result can be a deeply
nested dict; serializing it inline through shell quoting is fragile. A script
also gives us a clean place to capture top-level exceptions into a structured
failure result rather than crashing the subprocess with no record.

The script prints high-level progress markers to stdout so check_job(job_id)'s
log_tail surfaces useful state ('start', 'mode', 'result_path', exception
traceback if any). The detailed docker build output remains internal — adding
streaming there is a separate piece of work.
"""

from __future__ import annotations

import json
import sys
import time
import traceback


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write("usage: python -m agent.skills.freeze_runner "
                         "<args_path> <result_path>\n")
        return 2

    args_path, result_path = sys.argv[1], sys.argv[2]
    print(f"[freeze_runner] start {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"[freeze_runner] args_path={args_path}", flush=True)
    print(f"[freeze_runner] result_path={result_path}", flush=True)

    try:
        with open(args_path) as f:
            args = json.load(f)
    except Exception as e:
        print(f"[freeze_runner] FAILED to read args: {e!r}", flush=True)
        _write_result(result_path, {
            "success": False,
            "stage": "background_args_read",
            "error": repr(e),
            "traceback": traceback.format_exc(),
        })
        return 1

    print(f"[freeze_runner] env={args.get('env_name')} tools={args.get('tools')} "
          f"platform={args.get('platform')} accel={args.get('accel')}", flush=True)

    # Force synchronous mode in the subprocess — otherwise the runner would
    # recursively spawn ANOTHER background job and exit immediately, with no
    # actual freeze ever running. Defensive against caller-side bugs that
    # accidentally pass background=True through.
    args["background"] = False

    try:
        # Lazy import: the freeze() side imports a lot (Docker SDKs, conda
        # tooling). Doing it inside main() keeps a misconfigured environment
        # from failing the runner before we can even write a structured result.
        from agent.mcp_server import freeze
        t0 = time.time()
        result = freeze(**args)
        elapsed = time.time() - t0
        print(f"[freeze_runner] freeze() returned in {elapsed:.1f}s "
              f"mode={result.get('mode','?')} success={result.get('success')}",
              flush=True)
    except BaseException as e:
        # Catch BaseException so KeyboardInterrupt / SystemExit also land in
        # the result file. A subprocess that exits silently is exactly the
        # failure mode this whole feature is trying to fix.
        print(f"[freeze_runner] EXCEPTION in freeze(): {e!r}", flush=True)
        traceback.print_exc()
        _write_result(result_path, {
            "success": False,
            "stage": "background_exception",
            "error": repr(e),
            "traceback": traceback.format_exc(),
        })
        return 1

    _write_result(result_path, result)
    print(f"[freeze_runner] wrote result_path={result_path}", flush=True)
    return 0 if result.get("success") else 1


def _write_result(path: str, result: dict) -> None:
    """Serialize the result to JSON. Falls back to repr() for non-serializable
    objects (we never want the result file to be unreadable)."""
    with open(path, "w") as f:
        json.dump(result, f, default=str, indent=2)


if __name__ == "__main__":
    sys.exit(main())
