#!/usr/bin/env python3
"""experiment_metrics — one headless run of the agent → one row of numbers.

A run is a `claude -p … --output-format stream-json` transcript (scripts/experiments.py
writes one per run) plus the isolated workspace that run was pointed at. This module
reads both and answers, in one flat row, the questions the field asks of an agent:

  did it work        `success` by the experiment's rule, and the ladder under it
                     (frozen → sealed → usage verified → reports → pipeline rendered)
  what did it cost   dollars, and tokens by kind (input / output / cache read /
                     cache creation / thinking), plus the peak context size
  how much effort    API calls, turns, tool calls — MCP calls by tool, the ToolSearch
                     calls spent finding the menu, repeated identical calls
  how did it fail    MCP outcomes by class (`outcome` on every tagged terminal),
                     refusal codes, tool errors, permission denials
  how long           wall time and the time spent inside API calls (per-tool time is
                     not in the stream, so it is not reported)

Across repeats the aggregates are the ones benchmark leaderboards publish: pass@1
(mean success), pass^k (τ-bench: all k of k repeats succeed, the reliability number),
mean cost, and cost-of-pass (expected dollars per success — mean cost / pass@1).

The transcript schema is Claude Code's (2.1.x), not a contract. The parser reads
only the fields named in `_read_transcript` and tests/test_experiments.py pins it
against a fixture built from real records, so a schema change fails loudly there.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import statistics
from itertools import combinations
from pathlib import Path
from typing import Any

MCP_PREFIX = "mcp__bioinf__"
OUTCOME_CLASSES = ("proven", "degraded", "refused", "broke", "loop", "vanished")
#: Tools whose job is to be called again with the same input until something changes.
#: Their repeats are polling, not flailing, so they are left out of `repeated_calls`.
POLLING_TOOLS = ("check_job", "cluster_job_status", "globus_task_status", "list_jobs")

#: How a run is judged. `completed` is the bootstrap tier (the session finished and the
#: server answered); `sealed` is the honesty contract's own bar (a sealed workflow whose
#: usage self-test passed, with both reports written); `pipeline` adds a rendered pipeline.
#: Two rules judge a run that is SUPPOSED to stop short: `refused` wants the server to have
#: refused (with a code matching the experiment's `expected_codes`, when it names any) and
#: nothing sealed around that refusal; `asked` wants nothing frozen or sealed and a closing
#: message that puts a question to the user — the right end for a request the agent should
#: not guess at.
SUCCESS_RULES = ("completed", "sealed", "pipeline", "refused", "asked")
TIER_DEFAULT_RULE = {"C0": "completed", "C1": "sealed", "C2": "sealed", "C3": "pipeline"}

#: The one metric set — every row has every column, in this order. (name, description.)
METRIC_COLUMNS: tuple[tuple[str, str], ...] = (
    ("experiment", "experiment name"),
    ("tier", "C0 bootstrap · C1 one tool · C2 recipe · C3 composed pipeline"),
    ("model", "model as requested"),
    ("model_id", "model the API answered as"),
    ("repeat", "repeat index within the experiment"),
    ("run_id", "run directory name"),
    ("code_rev", "git revision of the checkout under test"),
    ("success", "the experiment's success rule, judged on the workspace"),
    ("success_rule", "which rule judged it"),
    ("terminal_reason", "how Claude Code says the session ended"),
    ("is_error", "the session ended in error"),
    ("timed_out", "the runner killed it at its wall-clock limit"),
    ("frozen", "an env was frozen (a freeze call came back proven or degraded)"),
    ("sealed", "a workflow record was written"),
    ("usage_verified", "the sealed record's usage self-test passed"),
    ("env_report", "an ENV report was written"),
    ("run_report", "a RUN report was written"),
    ("pipeline_rendered", "a pipeline directory with main.nf was rendered"),
    ("cost_usd", "dollars at API list price (Claude Code's own figure)"),
    ("input_tokens", "uncached input tokens"),
    ("output_tokens", "output tokens"),
    ("cache_read_tokens", "input tokens served from the prompt cache"),
    ("cache_creation_tokens", "input tokens written to the prompt cache"),
    ("thinking_tokens", "thinking tokens, where the API reports them (read off the streamed messages)"),
    ("context_peak", "largest single request (input + cache read + cache creation)"),
    ("api_calls", "API round trips (distinct assistant messages)"),
    ("turns", "turns as Claude Code counts them"),
    ("tool_calls", "every tool call"),
    ("mcp_calls", "calls to the bioinf server"),
    ("tool_search_calls", "ToolSearch calls — the cost of finding the deferred menu"),
    ("other_tool_calls", "calls to Claude Code's own tools (Bash, Read, …)"),
    ("repeated_calls", "calls identical to an earlier one (same tool, same input), polling excluded"),
    ("tool_errors", "tool results flagged is_error"),
    ("mcp_errors", "bioinf results flagged is_error — the call itself failed, no outcome tag"),
    ("permission_denials", "calls the harness refused to run"),
    ("mcp_proven", "bioinf results with outcome proven"),
    ("mcp_degraded", "… degraded"),
    ("mcp_refused", "… refused"),
    ("mcp_broke", "… broke"),
    ("mcp_loop", "… loop"),
    ("mcp_vanished", "… vanished"),
    ("mcp_unstated", "bioinf results with no outcome tag (query tools, and polls still running)"),
    ("wall_ms", "wall-clock time of the whole run"),
    ("api_ms", "time inside API calls, as Claude Code reports it"),
)
METRIC_NAMES = tuple(name for name, _ in METRIC_COLUMNS)
#: Row fields that are structured (not scalar columns): kept in metrics.json, shown in the
#: report, left out of the CSV.
DETAIL_FIELDS = ("mcp_by_tool", "refusal_codes", "outcome_codes", "code_classes", "expected_codes", "sealed_names", "model_usage",
                 "final_text", "session_id", "claude_code_version", "code_dirty", "started_at")


# ---------------------------------------------------------------------------
# The transcript
# ---------------------------------------------------------------------------

def _loads_lines(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                out.append(d)
    return out


def _result_payload(block: dict, record: dict) -> Any:
    """The tool result as data. MCP tools answer with a JSON document in the text;
    Claude Code's own tools answer with text or structured content. Returns the parsed
    document when there is one, else the raw content."""
    content = block.get("content")
    if isinstance(content, list):
        texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        content = "\n".join(t for t in texts if t) if texts else content
    if isinstance(content, str):
        s = content.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return content
        return content
    tur = record.get("tool_use_result")
    if isinstance(tur, dict) and isinstance(tur.get("content"), str):
        try:
            return json.loads(tur["content"])
        except json.JSONDecodeError:
            return tur["content"]
    return content


def _read_transcript(records: list[dict]) -> dict:
    """Everything the row needs from the stream, read once."""
    init: dict = {}
    result: dict = {}
    seen_msgs: set[str] = set()
    usage_sum = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0, "thinking_tokens": 0}
    context_peak = 0
    model_ids: list[str] = []
    calls: dict[str, dict] = {}          # tool_use_id → {name, input}
    call_order: list[str] = []
    results: dict[str, dict] = {}        # tool_use_id → {is_error, payload}
    final_text = ""
    rate_limit_events = 0

    for d in records:
        t = d.get("type")
        if t == "system" and d.get("subtype") == "init":
            init = d
        elif t == "rate_limit_event":
            rate_limit_events += 1
        elif t == "assistant":
            m = d.get("message") or {}
            mid = m.get("id")
            # stream-json repeats the message once per content block, each carrying the
            # usage as it stood when the message STARTED: input and cache counts are final
            # there, output_tokens is a placeholder. Summed once per message id, these give
            # the request sizes (context_peak); the token totals come from the result
            # record, which has the finished numbers, with this sum as the fallback.
            if mid and mid not in seen_msgs:
                seen_msgs.add(mid)
                u = m.get("usage") or {}
                for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                          "cache_creation_input_tokens"):
                    usage_sum[k] += int(u.get(k) or 0)
                usage_sum["thinking_tokens"] += int(
                    ((u.get("output_tokens_details") or {}).get("thinking_tokens")) or 0)
                ctx = (int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                       + int(u.get("cache_creation_input_tokens") or 0))
                context_peak = max(context_peak, ctx)
                if m.get("model"):
                    model_ids.append(m["model"])
            for b in m.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("id"):
                    calls[b["id"]] = {"name": b.get("name") or "", "input": b.get("input")}
                    call_order.append(b["id"])
                elif b.get("type") == "text" and b.get("text"):
                    final_text = b["text"]
        elif t == "user":
            m = d.get("message") or {}
            for b in (m.get("content") or []) if isinstance(m.get("content"), list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                    results[b["tool_use_id"]] = {"is_error": bool(b.get("is_error")),
                                                 "payload": _result_payload(b, d)}
        elif t == "result":
            result = d

    return {"init": init, "result": result, "usage": usage_sum, "context_peak": context_peak,
            "api_calls": len(seen_msgs), "model_ids": model_ids, "calls": calls,
            "call_order": call_order, "results": results, "final_text": final_text,
            "rate_limit_events": rate_limit_events}


def _call_fingerprint(name: str, inp: Any) -> str:
    return hashlib.sha1((name + "\x00" + json.dumps(inp, sort_keys=True, default=str)).encode()).hexdigest()


def _tool_stats(tx: dict) -> dict:
    by_tool: dict[str, int] = {}
    outcomes = {f"mcp_{c}": 0 for c in OUTCOME_CLASSES}
    outcomes["mcp_unstated"] = 0
    refusal_codes: dict[str, int] = {}
    outcome_codes: dict[str, int] = {}
    code_classes: dict[str, str] = {}
    seen: set[str] = set()
    n_mcp = n_search = n_other = n_repeat = n_err = n_mcp_err = 0
    frozen = False
    for cid in tx["call_order"]:
        c = tx["calls"][cid]
        name = c["name"]
        short = name[len(MCP_PREFIX):] if name.startswith(MCP_PREFIX) else name
        fp = _call_fingerprint(name, c["input"])
        if fp in seen and short not in POLLING_TOOLS:
            n_repeat += 1
        seen.add(fp)
        r = tx["results"].get(cid)
        if r and r["is_error"]:
            n_err += 1
        if name.startswith(MCP_PREFIX):
            n_mcp += 1
            if r and r["is_error"]:
                n_mcp_err += 1
            by_tool[short] = by_tool.get(short, 0) + 1
            payload = r["payload"] if r else None
            terminal_tool = short
            # A backgrounded primitive answers through check_job: the tool's own return
            # value rides inline under `result` once the job has exited, and the job id
            # names the tool it belongs to. That is the terminal, not the poll.
            if (short == "check_job" and isinstance(payload, dict)
                    and isinstance(payload.get("result"), dict) and payload["result"].get("outcome")):
                job_id = str((c["input"] or {}).get("job_id", "")) if isinstance(c["input"], dict) else ""
                terminal_tool = job_id.split(".", 1)[0] or short
                payload = payload["result"]
            outcome = payload.get("outcome") if isinstance(payload, dict) else None
            code = payload.get("code") if isinstance(payload, dict) else None
            if outcome in OUTCOME_CLASSES:
                outcomes[f"mcp_{outcome}"] += 1
                if isinstance(code, str):
                    outcome_codes[code] = outcome_codes.get(code, 0) + 1
                    code_classes[code] = outcome
                    if outcome == "refused":
                        refusal_codes[code] = refusal_codes.get(code, 0) + 1
                if terminal_tool == "freeze" and outcome in ("proven", "degraded"):
                    frozen = True
            else:
                outcomes["mcp_unstated"] += 1
        elif name == "ToolSearch":
            n_search += 1
        else:
            n_other += 1
    return {"mcp_calls": n_mcp, "tool_search_calls": n_search, "other_tool_calls": n_other,
            "repeated_calls": n_repeat, "tool_errors": n_err, "mcp_errors": n_mcp_err, "mcp_by_tool": by_tool,
            "refusal_codes": refusal_codes, "outcome_codes": outcome_codes, "code_classes": code_classes,
            "frozen": frozen, **outcomes}


# ---------------------------------------------------------------------------
# The workspace
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict | None:
    try:
        import yaml
        d = yaml.safe_load(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def inspect_workspace(workspace: Path) -> dict:
    """The ladder, read off the artifacts: what was written, not what was claimed."""
    envs = workspace / "environments"
    sealed_names: list[str] = []
    usage_verified = False
    for p in sorted(envs.glob("*/*.workflow.yaml")) if envs.is_dir() else []:
        d = _load_yaml(p)
        if d is None:
            continue
        name = p.name[: -len(".workflow.yaml")]
        sealed_names.append(name)
        if d.get("usage_verified") is True:
            usage_verified = True
    env_report = any(envs.glob("*/*.ENV.html")) if envs.is_dir() else False
    run_report = any(envs.glob("*/*.RUN.html")) if envs.is_dir() else False
    pipelines = workspace / "pipelines"
    rendered = any(pipelines.glob("*/main.nf")) if pipelines.is_dir() else False
    return {"sealed": bool(sealed_names), "sealed_names": sealed_names,
            "usage_verified": usage_verified, "env_report": env_report,
            "run_report": run_report, "pipeline_rendered": rendered}


def codes_match(codes: dict, patterns: list[str]) -> bool:
    """Whether any observed outcome code matches any expected pattern (fnmatch, so
    `freeze.gated_*` covers a family). No patterns means any code counts."""
    if not patterns:
        return bool(codes)
    return any(fnmatch.fnmatchcase(code, pat) for code in codes for pat in patterns)


def judge(rule: str, row: dict) -> bool:
    if rule not in SUCCESS_RULES:
        raise ValueError(f"unknown success rule {rule!r}; one of {SUCCESS_RULES}")
    if row["timed_out"]:
        return False      # the runner cut it short; whatever landed, the run did not finish
    completed = (row["is_error"] is False and row["mcp_calls"] > row["mcp_errors"])   # the server answered
    if rule == "completed":
        return completed
    if rule == "refused":
        # the gate held: the server refused as expected, and anything sealed after that
        # was sealed properly (a self-test that passed), never around the refusal
        held = (not row["sealed"]) or bool(row["usage_verified"])
        return completed and held and codes_match(row.get("refusal_codes") or {}, row.get("expected_codes") or [])
    if rule == "asked":
        return (row["is_error"] is False and not row["frozen"] and not row["sealed"]
                and "?" in (row.get("final_text") or ""))
    sealed = bool(row["sealed"] and row["usage_verified"] and row["env_report"] and row["run_report"])
    if rule == "sealed":
        return sealed
    return sealed and bool(row["pipeline_rendered"])


# ---------------------------------------------------------------------------
# The row
# ---------------------------------------------------------------------------

def parse_run(run_dir: Path) -> dict:
    """One run directory (experiment.json + transcript.jsonl + workspace/) → one row."""
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "experiment.json").read_text(encoding="utf-8"))
    transcript = run_dir / "transcript.jsonl"
    tx = _read_transcript(_loads_lines(transcript) if transcript.exists() else [])
    res = tx["result"]
    stats = _tool_stats(tx)
    ws = inspect_workspace(Path(meta.get("workspace") or run_dir / "workspace"))
    usage = tx["usage"]
    final = res.get("usage") if isinstance(res.get("usage"), dict) else None
    if final:
        usage = {**usage, **{k: int(final.get(k) or 0) for k in (
            "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}}
    model_usage = {}
    for mid, mu in (res.get("modelUsage") or {}).items():
        if isinstance(mu, dict):
            model_usage[mid] = {"input_tokens": mu.get("inputTokens"), "output_tokens": mu.get("outputTokens"),
                                "cache_read_tokens": mu.get("cacheReadInputTokens"),
                                "cache_creation_tokens": mu.get("cacheCreationInputTokens"),
                                "cost_usd": mu.get("costUSD")}
    model_id = tx["model_ids"][0] if tx["model_ids"] else (tx["init"].get("model") or "")
    row: dict[str, Any] = {
        "experiment": meta.get("name", ""),
        "tier": meta.get("tier", ""),
        "model": meta.get("model", ""),
        "model_id": model_id,
        "repeat": meta.get("repeat", 0),
        "run_id": run_dir.name,
        "code_rev": meta.get("code_rev", ""),
        "success": None,
        "success_rule": meta.get("success") or TIER_DEFAULT_RULE.get(meta.get("tier", ""), "completed"),
        "terminal_reason": res.get("terminal_reason") or ("none" if not res else ""),
        "is_error": bool(res.get("is_error")) if res else None,
        "timed_out": bool(meta.get("timed_out")),
        "frozen": stats.pop("frozen"),
        "sealed": ws["sealed"],
        "usage_verified": ws["usage_verified"],
        "env_report": ws["env_report"],
        "run_report": ws["run_report"],
        "pipeline_rendered": ws["pipeline_rendered"],
        "cost_usd": float(res.get("total_cost_usd") or 0.0),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_read_tokens": usage["cache_read_input_tokens"],
        "cache_creation_tokens": usage["cache_creation_input_tokens"],
        "thinking_tokens": usage["thinking_tokens"],
        "context_peak": tx["context_peak"],
        "api_calls": tx["api_calls"],
        "turns": int(res.get("num_turns") or 0),
        "tool_calls": len(tx["call_order"]),
        "permission_denials": len(res.get("permission_denials") or []),
        "wall_ms": int(meta.get("wall_ms") or res.get("duration_ms") or 0),
        "api_ms": int(res.get("duration_api_ms") or 0),
    }
    row.update(stats)
    row.update({
        "expected_codes": list(meta.get("expected_codes") or []),
        "sealed_names": ws["sealed_names"], "model_usage": model_usage, "final_text": tx["final_text"][:2000],
        "session_id": res.get("session_id") or tx["init"].get("session_id") or "",
        "claude_code_version": tx["init"].get("claude_code_version") or "",
        "code_dirty": bool(meta.get("code_dirty")),
        "started_at": meta.get("started_at", ""),
    })
    row["success"] = judge(row["success_rule"], row) if res or row["timed_out"] else None
    missing = [k for k in METRIC_NAMES if k not in row]
    assert not missing, f"row is missing metric columns {missing}"
    return row


def write_row(run_dir: Path, row: dict) -> Path:
    out = Path(run_dir) / "metrics.json"
    out.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def load_rows(experiment_dirs: list[Path], reparse: bool = False) -> list[dict]:
    """Every run under the given experiment directories, parsed (or re-read from its
    metrics.json), ordered by experiment, model, repeat."""
    rows = []
    for exp in experiment_dirs:
        for run in sorted(Path(exp).iterdir()) if Path(exp).is_dir() else []:
            if not (run / "experiment.json").exists():
                continue
            mj = run / "metrics.json"
            if mj.exists() and not reparse:
                rows.append(json.loads(mj.read_text(encoding="utf-8")))
            else:
                row = parse_run(run)
                write_row(run, row)
                rows.append(row)
    rows.sort(key=lambda r: (r["experiment"], r["model"], r["repeat"]))
    return rows


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------

def pass_pow_k(n: int, c: int, k: int) -> float | None:
    """τ-bench pass^k: the probability that k draws without replacement from n runs
    with c successes are all successes — C(c,k)/C(n,k). None when k > n."""
    if k > n or n == 0:
        return None
    if k > c:
        return 0.0
    return math.comb(c, k) / math.comb(n, k)


_MEANED = ("cost_usd", "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
           "thinking_tokens", "context_peak", "api_calls", "turns", "tool_calls", "mcp_calls",
           "tool_search_calls", "other_tool_calls", "repeated_calls", "tool_errors", "mcp_errors",
           "permission_denials", "mcp_proven", "mcp_degraded", "mcp_refused", "mcp_broke",
           "mcp_loop", "mcp_vanished", "mcp_unstated", "wall_ms", "api_ms")


def aggregate(rows: list[dict], by: tuple[str, ...] = ("experiment", "model")) -> list[dict]:
    """One record per distinct value of `by`: (experiment, model) is the scoreboard,
    (experiment, code_rev) the view across code revisions. Every record carries the
    experiment, model and code_rev of its first row plus the sets seen."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault(tuple(str(r.get(k) or "") for k in by), []).append(r)
    out = []
    for key, rs in sorted(groups.items()):
        judged = [r for r in rs if r["success"] is not None]
        n = len(judged)
        c = sum(1 for r in judged if r["success"])
        g: dict[str, Any] = {"experiment": rs[0]["experiment"], "tier": rs[0]["tier"], "model": rs[0]["model"],
                             "model_id": rs[0]["model_id"], "code_rev": rs[0].get("code_rev", ""),
                             "code_dirty": any(r.get("code_dirty") for r in rs),
                             "models": sorted({r["model"] for r in rs}),
                             "code_revs": sorted({r.get("code_rev", "") for r in rs}),
                             "n": len(rs), "judged": n, "successes": c,
                             "pass_at_1": (c / n) if n else None,
                             "pass_pow_k": pass_pow_k(n, c, n) if n else None, "k": n}
        g.update(dict(zip(by, key)))
        for key in _MEANED:
            vals = [float(r[key]) for r in rs]
            g[f"{key}_mean"] = statistics.fmean(vals) if vals else None
            g[f"{key}_sd"] = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        g["cost_of_pass"] = (g["cost_usd_mean"] / g["pass_at_1"]) if g["pass_at_1"] else None
        codes: dict[str, int] = {}
        for r in rs:
            for code, k in (r.get("refusal_codes") or {}).items():
                codes[code] = codes.get(code, 0) + k
        g["refusal_codes"] = codes
        all_codes: dict[str, int] = {}
        for r in rs:
            for code, k in (r.get("outcome_codes") or {}).items():
                all_codes[code] = all_codes.get(code, 0) + k
        g["outcome_codes"] = all_codes
        classes: dict[str, str] = {}
        for r in rs:
            classes.update(r.get("code_classes") or {})
        g["code_classes"] = classes
        tools: dict[str, int] = {}
        for r in rs:
            for tool, k in (r.get("mcp_by_tool") or {}).items():
                tools[tool] = tools.get(tool, 0) + k
        g["mcp_by_tool"] = tools
        out.append(g)
    return out


def rows_to_csv(rows: list[dict]) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(METRIC_NAMES)
    for r in rows:
        w.writerow(["" if r.get(k) is None else r.get(k) for k in METRIC_NAMES])
    return buf.getvalue()
