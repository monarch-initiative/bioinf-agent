"""backgroundable — the ONE way a long-running MCP tool gets detached.

The agent's stream-watchdog kills a tool call that produces no stdout for ~600 s.
Several primitives here legitimately run far longer than that: a CUDA freeze
downloads multi-GB layers and builds under emulation, a Bioconductor install
compiles from source, a real alignment step's own default timeout is 1800 s. In
every case the WORK succeeds inside its container and the tool call dies on the
way home — no EnvCache record, no draft step, no env report. The build happened
and the system has no memory of it.

`@backgroundable(...)` appends `background: bool = False` to a tool's PUBLISHED
signature. When the agent passes True, the kwargs are written to a JSON file, a
detached subprocess (`agent.skills.job_runner`) re-enters the SAME tool with
`background=False`, and the parent returns a `job_id` immediately.

Why a decorator and not a per-tool branch: this repo's diagnosed defect is "one
truth in N places, drifting in N−1". The detachment protocol has five moving
parts that must agree (the job_id shape, the args file, the runner's recursion
guard, the result file's path, the re-entry). Hand-copying that per tool is how
four of them drift. The single spelling lives here, the sixth part (reading the
result) lives on JobManager, and every tool that wants the flag adds one line.

Adding the flag to a tool:

    @mcp.tool()
    @backgroundable("env_name")        # params tried, in order, for a readable job_id
    def install_conda_packages(env_name: str, ...) -> dict:

Order matters — `@mcp.tool()` must be OUTERMOST so FastMCP publishes the widened
signature. `tests/test_background_detach.py` fails the build if a decorated tool
does not publish `background`.
"""
from __future__ import annotations

import functools
import inspect
import json
import re
import shlex
import sys
import uuid
from typing import Any, Callable

from agent.skills.outcomes import broke, refused

#: tool name -> the parameter names tried, in order, to build a readable job_id.
#: Populated by @backgroundable at import time, so it is DERIVED from the
#: decoration rather than restated beside it. `job_runner` treats it as an
#: ALLOWLIST: a name that was never decorated cannot be spawned, which keeps the
#: runner's `getattr(mcp_server, name)` closed over the decorated set instead of
#: being an arbitrary-callable surface keyed by a string off the wire.
BACKGROUNDABLE: dict[str, tuple[str, ...]] = {}

# Appended to every decorated tool's docstring. The agent reads this text as the
# tool's contract, so the promise made here is the promise check_job keeps: the
# outcome is read in ONE place, and the spawn receipt is not it.
_DOC = """

    `background=True` DETACHES this call. The arguments are handed to a fresh
    subprocess and the tool returns immediately with a `job_id` — no result, and
    nothing has been produced yet. Use it whenever the work could plausibly run
    silently past the agent's ~600 s stream-watchdog. Poll `check_job(job_id)`;
    once `state` is `'exited'`, that same response carries this tool's real
    return value inline under `result`. There is no second file to read."""


def backgroundable(*name_from: str,
                   preflight: Callable[[], Any] | None = None) -> Callable:
    """Give a tool a `background: bool = False` flag.

    `name_from` names the parameters tried, in order, to build a human-readable
    job_id (`freeze.talos.a1b2c3d4`) — the first one holding a non-empty string
    wins. They are checked against the real signature AT IMPORT, so a typo is a
    startup error rather than a silently ugly job_id at 3 a.m.

    `preflight` is a zero-arg callable run BEFORE the spawn (background path
    only); a truthy return is handed straight back to the caller as the tool's
    result and nothing is detached. Use it for the cheap machine-level checks a
    tool already makes in its body — docker up, disk free — because a detached
    run turns an instant, correctly-named refusal into a job that fails minutes
    later inside a log the agent has to go read. It runs again in the child, as
    part of the body; these checks are pure reads, so twice is free.
    """
    def deco(fn: Callable) -> Callable:
        sig = inspect.signature(fn)
        if "background" in sig.parameters:
            raise TypeError(f"{fn.__name__} already declares `background`")
        for p in sig.parameters.values():
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                # *args/**kwargs would nest under one key in bind().arguments,
                # and the child re-enters by keyword only. Refuse at import.
                raise TypeError(
                    f"{fn.__name__} takes {p.kind.description}; @backgroundable "
                    "needs an explicit keyword signature to cross the subprocess")
        for want in name_from:
            if want not in sig.parameters:
                raise TypeError(
                    f"@backgroundable({want!r}) on {fn.__name__}: no such parameter. "
                    f"Choose from {sorted(sig.parameters)}")

        widened = sig.replace(parameters=[
            *sig.parameters.values(),
            inspect.Parameter("background", inspect.Parameter.KEYWORD_ONLY,
                              default=False, annotation=bool),
        ])

        @functools.wraps(fn)
        def wrapper(*a, background: bool = False, **kw):
            if not background:
                return fn(*a, **kw)
            if preflight is not None:
                stop = preflight()
                if stop:
                    return stop
            # bind against the ORIGINAL signature: `background` is consumed here
            # and must never reach the args file, or the child would re-detach.
            bound = sig.bind(*a, **kw)
            return spawn_detached(fn.__name__, dict(bound.arguments), name_from)

        # FastMCP builds the published input schema from __signature__ (proven
        # against fastmcp 3.2.4 — every original param survives byte-identical
        # to the undecorated control, including Annotated/BeforeValidator
        # aliases like StrList). __annotations__ is widened to match so any
        # other reader agrees with the signature.
        wrapper.__signature__ = widened          # type: ignore[attr-defined]
        wrapper.__annotations__ = {**getattr(fn, "__annotations__", {}),
                                   "background": bool}
        wrapper.__doc__ = (fn.__doc__ or "").rstrip() + _DOC
        BACKGROUNDABLE[fn.__name__] = tuple(name_from)
        return wrapper
    return deco


def spawn_detached(tool: str, kwargs: dict, name_from: tuple[str, ...] = ()) -> dict:
    """Write the kwargs, spawn `job_runner`, return the receipt. Never blocks."""
    # Lazy: agent.skills.* must not import the server at module load (the tool
    # modules import THIS at decoration time, which happens during that import).
    from agent import mcp_server as _ms
    jm = _ms._job_manager

    bad = _unserializable(kwargs)
    if bad:
        # A silently stringified argument means the child runs a DIFFERENT call
        # than the one the agent asked for — the exact class of quiet lie this
        # codebase exists to prevent. json.dumps(default=str) would have done
        # that; refuse instead, and name the parameter.
        return refused(
            "background.unserializable_argument",
            success=False, tool=tool, parameter=bad,
            error=f"argument {bad!r} ({type(kwargs[bad]).__name__}) is not JSON-"
                  f"serializable, so it cannot cross into the detached subprocess",
            fix=f"pass {bad!r} as a plain str/int/bool/list/dict, or call {tool} "
                f"synchronously (background=False)")

    job_id = _job_id(tool, kwargs, name_from)
    args_path, result_path = jm.args_path(job_id), jm.result_path(job_id)
    args_path.parent.mkdir(parents=True, exist_ok=True)
    if result_path.exists():
        result_path.unlink()      # a stale result must never fool a poller
    args_path.write_text(json.dumps(kwargs))

    # sys.executable, not bare `python`: the child must be the SAME interpreter
    # the server runs under, or `import agent.mcp_server` can resolve to a
    # different (or no) install.
    cmd = " ".join(shlex.quote(x) for x in (
        sys.executable, "-m", "agent.skills.job_runner",
        tool, str(args_path), str(result_path)))
    job = jm.start(cmd, job_id=job_id)
    if "error" in job:
        # `stage` is the key every background failure shares — the parent's
        # spawn refusal and the child's four in-runner failure modes all set it,
        # so one read tells you how far the detachment got.
        return broke("background.spawn_failed",
                     **{"success": False, "stage": "background_spawn",
                        "tool": tool, "job_id": job_id, **job})
    return {
        # The SPAWN succeeded. Whether the tool succeeds is unknown and lives on
        # check_job's `result`; the note says so in as many words.
        "success":     True,
        "background":  True,
        "tool":        tool,
        "job_id":      job_id,
        "state":       "running",
        "result_path": str(result_path),
        "log_path":    job.get("log_path", ""),
        "done_marker": str(jm.done_path(job_id)),
        "note": (f"{tool} is running detached — NOTHING has been produced yet and "
                 f"this receipt is not a result. Poll check_job('{job_id}'); when "
                 f"state=='exited' that response carries {tool}'s real return value "
                 f"inline under `result`. `log_tail` shows progress meanwhile."),
    }


def _unserializable(kwargs: dict) -> str:
    """Name of the first argument that cannot cross the JSON boundary, else "".

    Checked one key at a time so the refusal can name the culprit rather than
    reporting that "the arguments" failed.
    """
    for k, v in kwargs.items():
        try:
            json.dumps({k: v})
        except (TypeError, ValueError):
            return k
    return ""


def _job_id(tool: str, kwargs: dict, name_from: tuple[str, ...]) -> str:
    """`{tool}.{slug}.{8-hex}` — readable in list_jobs AND unique across repeated
    background runs of the same tool on the same target."""
    slug = ""
    for p in name_from:
        v = kwargs.get(p)
        if isinstance(v, str) and v.strip():
            slug = v.strip()
            break
    # The job_id becomes a set of FILENAMES (.args.json/.log/.status.json/.done),
    # so anything path-hostile is folded out before it reaches the filesystem.
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", slug).strip("-.")[:40]
    tail = uuid.uuid4().hex[:8]
    return f"{tool}.{slug}.{tail}" if slug else f"{tool}.{tail}"


def _describe(kwargs: dict[str, Any], limit: int = 3) -> str:
    """A short one-line rendering of the call, for the runner's log header."""
    parts = []
    for k, v in list(kwargs.items())[:limit]:
        s = repr(v)
        parts.append(f"{k}={s[:60] + '…' if len(s) > 60 else s}")
    return " ".join(parts)
