#!/usr/bin/env python3
"""experiment_metrics — one headless run of the agent → one row of numbers.

A run is a `claude -p … --output-format stream-json` transcript (scripts/experiments.py
writes one per run) plus the isolated workspace that run was pointed at. This module
reads both and answers, in one flat row, the questions the field asks of an agent:

  did it work        `verdict` (pass · fail · invalid) by the experiment's rule, judged on
                     the deliverables the run's closing message names, and the ladder under
                     it (frozen → sealed → usage verified → reports → pipeline rendered)
  what did it cost   dollars, and tokens by kind (input / output / cache read /
                     cache creation / thinking), plus the peak context size
  how much effort    API calls, turns, tool calls — MCP calls by tool, the ToolSearch
                     calls spent finding the menu, repeated identical calls
  how did it fail    MCP outcomes by class (`outcome` on every tagged terminal),
                     refusal codes, tool errors, permission denials, and `reason`
  how long           wall time and the time spent inside API calls (per-tool time is
                     not in the stream, so it is not reported)

Across repeats the aggregates are the ones benchmark leaderboards publish: pass@1
(mean success), pass^k (τ-bench: all k of k repeats succeed, the reliability number),
mean cost, and cost-of-pass (expected dollars per success — mean cost / pass@1). They are
computed WITHIN one code revision: a group never pools runs of different revisions.

A verdict is tied to the judge that produced it: every row carries `judge_hash`, the
sha256 of this file, and a row whose hash is not the current one is re-judged when read.

The transcript schema is Claude Code's (2.1.x), not a contract. The parser reads
only the fields named in `_read_transcript` and tests/test_experiments.py pins it
against a fixture built from real records, so a schema change fails loudly there.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

MCP_PREFIX = "mcp__bioinf__"
OUTCOME_CLASSES = ("proven", "degraded", "refused", "broke", "loop", "vanished")
#: Tools whose job is to be called again with the same input until something changes.
#: Their repeats are polling, not flailing, so they are left out of `repeated_calls`.
POLLING_TOOLS = ("check_job", "cluster_job_status", "globus_task_status", "list_jobs")

#: sha256 of this file: the judge that produced a verdict, carried on every row.
JUDGE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

#: The three verdicts. `invalid` is a run with no result record at all — the session never
#: said how it ended — so nothing can be judged; it is counted in pass@1's denominator and
#: flagged for a re-run, never dropped.
VERDICTS = ("pass", "fail", "invalid")

#: How a run is judged. Every rule first wants a session that ended cleanly (`is_error`
#: false): a session cut off by an error, a rate limit or the client is a failed run however
#: much landed on disk. `completed` is the bootstrap tier (the server answered); `frozen`
#: wants a frozen env with its ENV report (an install whose proof is the image itself);
#: `sealed` is the honesty contract's own bar (a sealed workflow whose usage self-test passed,
#: with both reports written); `pipeline` adds a rendered pipeline.
#: Three rules judge a run that is SUPPOSED to stop short. `refused` means the gate fired:
#: the server refused with a code matching the experiment's `expected_codes` and nothing was
#: sealed around that refusal — a run that never reached the server left the gate untested.
#: `declined` means nothing was forged: the agent declined outright without a server call, or
#: the gate fired, and nothing was frozen or sealed around it. `asked` wants nothing frozen or
#: sealed and a closing message that asks something of the user (a question or an explicit
#: request for action) — the right end for a request the agent should not guess at.
#: On top of any rule, a definition's `required_codes` are outcome codes (fnmatch patterns)
#: that must each appear as a proven or degraded outcome — the route the scenario exists to
#: exercise, so a seal reached by another route does not pass as that scenario — and its
#: `expect` block is checked against the records on disk (see `check_expect`).
SUCCESS_RULES = ("completed", "frozen", "sealed", "pipeline", "refused", "declined", "asked")

#: The three primitives whose proven/degraded return means an env was frozen: the
#: container-native build, the authors' published image, and the authors' Dockerfile.
FREEZE_TOOLS = ("freeze", "freeze_from_image", "build_env_from_authors_recipe")

#: Phrases that put a request to the user without a question mark.
REQUEST_MARKERS = ("action required", "please provide", "please share", "once you share",
                   "once you provide", "once you supply", "let me know", "i need you to",
                   "i'll need you to", "i need the", "i'll need the", "could you", "can you",
                   "you'll need to", "you need to", "send me", "point me to", "tell me",
                   "which one", "confirm", "needed from you", "need from you", "what's needed",
                   "what is needed", "provide the", "provide a", "supply the", "supply a")


def asks_the_user(text: str) -> bool:
    """A closing message that asks something of the user: a question, or an explicit
    request for action or information."""
    t = text.lower()
    return "?" in t or any(m in t for m in REQUEST_MARKERS)


TIER_DEFAULT_RULE = {"C0": "completed", "C1": "sealed", "C2": "sealed", "C3": "pipeline"}

#: The keys an experiment's `expect` block may carry, each a mechanical check on the bound
#: records (recipe.yaml, attestation.json, the workflow spec, main.nf):
#:   tools              every named tool is in the recipe's `primary_tools` and the record
#:                      carries a version for it (resolved_packages, tool_identities,
#:                      shipped_binaries, the install steps, the spec's env request key, or
#:                      the EnvCache's request key / adopted tag)
#:   build_method       the recipe's `build_method` is one of the values named
#:   evidence_depth_min every `validated_in_image` check in the attestation is at least this
#:                      deep on env_honesty's ladder (presence < version < import < help <
#:                      smoke < functional)
#:   slots              each pattern matches a `{SLOT}` in the bound how-to's command template
#:   trials_min         the bound how-to declares at least this many trials
#:   accessions         each accession appears, as a whole token, in the bound workflow spec
#:   accessions_min     at least this many distinct SRA-style accessions appear in the spec
#:   cohort_stage       main.nf calls a process over a collected channel (a stage that runs
#:                      once over every sample)
EXPECT_KEYS = ("tools", "build_method", "evidence_depth_min", "slots", "trials_min",
               "accessions", "accessions_min", "cohort_stage")
EVIDENCE_DEPTHS = ("presence", "version", "import", "help", "smoke", "functional")

#: The one metric set — every row has every column, in this order. (name, description.)
METRIC_COLUMNS: tuple[tuple[str, str], ...] = (
    ("experiment", "experiment name"),
    ("tier", "C0 bootstrap · C1 one tool · C2 recipe · C3 composed pipeline"),
    ("corpus", "the corpus the definition belongs to (its `corpus` key, else its directory)"),
    ("model", "model as requested"),
    ("model_id", "model the API answered as"),
    ("repeat", "repeat index within the experiment"),
    ("attempt", "attempt within the repeat: a failed or invalid run is re-run up to two more times "
                "(confirm-on-fail), and every attempt keeps its row"),
    ("run_id", "run directory name"),
    ("code_rev", "git revision of the checkout under test"),
    ("verdict", "pass · fail · invalid — invalid is a run with no result record at all (the session never "
                "said how it ended): it counts in pass@1's denominator and is flagged for a re-run"),
    ("success", "the verdict as a flag: true for pass, false for fail, empty for invalid"),
    ("success_rule", "which rule judged it"),
    ("reason", "the first reason the run is not a pass: no_result · timed_out · session_error · "
               "resources_mutated · rule:<rule> · required_codes · expect:<key>"),
    ("gate_held", "S and R scenarios: the server refused a gated call with one of the scenario's expected "
                  "codes and nothing was sealed around it; empty when the gate was not exercised or the "
                  "scenario declares no gate codes"),
    ("agent_declined", "S and R scenarios: the model never called a gated tool and drew no gated outcome — "
                       "the gate was not exercised; empty for scenarios without a gate"),
    ("terminal_reason", "how Claude Code says the session ended"),
    ("is_error", "the session ended in error"),
    ("timed_out", "the runner killed it at its wall-clock limit"),
    ("frozen", "an env was frozen (a freeze call came back proven or degraded)"),
    ("sealed", "the bound workflow record was written"),
    ("usage_verified", "the bound record's usage self-test passed"),
    ("env_report", "the bound env's ENV report was written"),
    ("run_report", "the bound workflow's RUN report was written"),
    ("pipeline_rendered", "a pipeline directory with main.nf was rendered from the bound workflow"),
    ("cost_usd", "dollars at API list price (Claude Code's own figure); absent when the session "
                 "was killed before it reported one"),
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
    ("judge_hash", "sha256 of scripts/experiment_metrics.py — the judge that produced this verdict"),
)
METRIC_NAMES = tuple(name for name, _ in METRIC_COLUMNS)
#: Row fields that are structured (not scalar columns): kept in metrics.json, shown in the
#: report, left out of the CSV.
DETAIL_FIELDS = ("mcp_by_tool", "refusal_codes", "outcome_codes", "code_classes", "expected_codes", "required_codes",
                 "gated_tools", "expect", "expect_failed", "expect_checked", "reasons", "bound", "sealed_names",
                 "resources_mutated", "attempts_of", "model_usage",
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
                if terminal_tool in FREEZE_TOOLS and outcome in ("proven", "degraded"):
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
# The workspace: the deliverables, bound together
# ---------------------------------------------------------------------------

SUPERSEDED_MARK = ".superseded."
_SPEC_SUFFIX = ".workflow.yaml"
_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]*"


def _load_yaml(path: Path) -> dict | None:
    try:
        import yaml
        d = yaml.safe_load(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _load_json(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _workflow_specs(envs: Path) -> list[dict]:
    """Every current workflow spec under environments/<env>/: a `.superseded.N` copy is the
    spec a re-seal replaced and is not a deliverable."""
    out = []
    for p in sorted(envs.glob(f"*/*{_SPEC_SUFFIX}")) if envs.is_dir() else []:
        if SUPERSEDED_MARK in p.name:
            continue
        d = _load_yaml(p)
        if d is None:
            continue
        out.append({"name": p.name[: -len(_SPEC_SUFFIX)], "env": p.parent.name, "path": p,
                    "mtime": _mtime(p), "usage_verified": d.get("usage_verified") is True})
    return out


def _env_dirs(envs: Path) -> list[dict]:
    """Every env directory that holds a frozen record (its recipe, attestation or ENV page)."""
    out = []
    for d in sorted(envs.iterdir()) if envs.is_dir() else []:
        if not d.is_dir():
            continue
        parts = [d / f"{d.name}{ext}" for ext in (".recipe.yaml", ".attestation.json", ".ENV.html")]
        present = [p for p in parts if p.exists()]
        if present:
            out.append({"name": d.name, "mtime": max(_mtime(p) for p in present)})
    return out


_RENDER_HEADER = re.compile(r"rendered from sealed workflow (\S+)")


def _rendered_pipelines(pipelines: Path) -> list[dict]:
    """Every rendered pipeline (a directory with main.nf) and the sealed workflow it was
    rendered from — read off its `.pipeline/pipeline.yaml` record, else off the header line
    main.nf carries."""
    out = []
    for nf in sorted(pipelines.glob("*/main.nf")) if pipelines.is_dir() else []:
        record = _load_yaml(nf.parent / ".pipeline" / "pipeline.yaml") or {}
        sealed = record.get("sealed_workflow")
        if not sealed:
            try:
                head = nf.read_text(encoding="utf-8", errors="replace")[:400]
            except OSError:
                head = ""
            m = _RENDER_HEADER.search(head)
            sealed = m.group(1) if m else None
        out.append({"name": nf.parent.name, "main_nf": nf, "sealed_workflow": sealed,
                    "cohort_workflows": [c.get("sealed_workflow") for c in record.get("cohort_workflows") or []
                                         if isinstance(c, dict)],
                    "mtime": _mtime(nf)})
    return out


def _last_mention(text: str, pattern: str, names: list[str]) -> str | None:
    """The name among `names` that `pattern` (with one group) mentions LAST in `text`: a
    closing message ends on its deliverable."""
    best, best_at = None, -1
    for m in re.finditer(pattern, text):
        if m.group(1) in names and m.start() > best_at:
            best, best_at = m.group(1), m.start()
    return best


def _last_token(text: str, names: list[str]) -> str | None:
    """The name among `names` that appears last in `text` as a whole token."""
    best, best_at = None, -1
    for name in names:
        for m in re.finditer(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", text):
            if m.start() > best_at:
                best, best_at = name, m.start()
    return best


def bind_deliverables(workspace: Path, final_text: str = "") -> dict:
    """ONE env directory, ONE workflow spec and ONE rendered pipeline: the set the ladder
    is read off. The deliverable the agent's closing message names wins — a rendered
    pipeline (`pipelines/<name>`), a workflow spec (`<name>.workflow.yaml`), an env
    (`environments/<name>` or `<name>.ENV.html`), or a bare workflow or env name as a whole
    word, in that order of preference. When the message names none, the newest current
    workflow spec is bound and `how` says so; with no spec at all, the newest env.

    A rendered pipeline belongs to the set only when it was rendered from the bound
    workflow. Returns {env, workflow, pipeline, how, named}."""
    workspace = Path(workspace)
    text = final_text or ""
    specs = _workflow_specs(workspace / "environments")
    envs = _env_dirs(workspace / "environments")
    pipes = _rendered_pipelines(workspace / "pipelines")
    spec_names = [s["name"] for s in specs]
    env_names = [e["name"] for e in envs]
    pipe_names = [p["name"] for p in pipes]

    def spec_named(name: str, env: str | None = None) -> dict | None:
        cands = [s for s in specs if s["name"] == name and (env is None or s["env"] == env)]
        return max(cands, key=lambda s: s["mtime"]) if cands else None

    def pipeline_for(workflow: str | None, preferred: str | None = None) -> dict | None:
        cands = [p for p in pipes if workflow and p["sealed_workflow"] == workflow]
        for p in cands:
            if p["name"] == preferred:
                return p
        return max(cands, key=lambda p: p["mtime"]) if cands else None

    bound: dict[str, Any] = {"env": None, "workflow": None, "pipeline": None, "how": "nothing written", "named": False}
    named_pipe = _last_mention(text, rf"pipelines/({_NAME})", pipe_names)
    named_spec = _last_mention(text, rf"({_NAME})\.workflow\.yaml", spec_names)
    named_env = (_last_mention(text, rf"environments/({_NAME})", env_names)
                 or _last_mention(text, rf"({_NAME})\.ENV\.html", env_names))
    spec = None
    if named_pipe:
        pipe = next(p for p in pipes if p["name"] == named_pipe)
        spec = spec_named(pipe["sealed_workflow"]) if pipe["sealed_workflow"] else None
        bound.update({"pipeline": named_pipe, "how": f"final message names pipeline {named_pipe}", "named": True})
        if spec is None:
            bound["env"] = named_env or (envs and max(envs, key=lambda e: e["mtime"])["name"]) or None
            bound["how"] += "; its sealed workflow is not on disk"
    elif named_spec:
        spec = spec_named(named_spec, named_env if any(s["env"] == named_env for s in specs if s["name"] == named_spec) else None)
        bound.update({"how": f"final message names workflow {named_spec}", "named": True})
    elif named_env:
        in_env = [s for s in specs if s["env"] == named_env]
        spec = max(in_env, key=lambda s: s["mtime"]) if in_env else None
        bound.update({"env": named_env, "how": f"final message names env {named_env}", "named": True})
    else:
        bare_spec = _last_token(text, spec_names)
        bare_env = _last_token(text, env_names)
        if bare_spec:
            spec = spec_named(bare_spec)
            bound.update({"how": f"final message mentions workflow {bare_spec}", "named": True})
        elif bare_env:
            in_env = [s for s in specs if s["env"] == bare_env]
            spec = max(in_env, key=lambda s: s["mtime"]) if in_env else None
            bound.update({"env": bare_env, "how": f"final message mentions env {bare_env}", "named": True})
        elif specs:
            spec = max(specs, key=lambda s: s["mtime"])
            bound["how"] = "newest workflow spec: the final message names none"
        elif envs:
            bound.update({"env": max(envs, key=lambda e: e["mtime"])["name"],
                          "how": "newest env: no workflow spec, and the final message names none"})
    if spec is not None:
        bound["workflow"] = spec["name"]
        bound["env"] = spec["env"]
        if bound["pipeline"] is None:
            pipe = pipeline_for(spec["name"], named_pipe)
            bound["pipeline"] = pipe["name"] if pipe else None
    return bound


def bound_paths(workspace: Path, bound: dict) -> dict[str, Path | None]:
    """The files of the bound set, present or not: the env's recipe, attestation and ENV
    page; the workflow spec and its RUN page; the pipeline's main.nf."""
    workspace = Path(workspace)
    env, wf, pipe = bound.get("env"), bound.get("workflow"), bound.get("pipeline")
    env_dir = workspace / "environments" / env if env else None
    return {
        "recipe": env_dir / f"{env}.recipe.yaml" if env_dir else None,
        "attestation": env_dir / f"{env}.attestation.json" if env_dir else None,
        "env_page": env_dir / f"{env}.ENV.html" if env_dir else None,
        "env_cache": workspace / "environments" / "_env_cache.json",
        "spec": env_dir / f"{wf}{_SPEC_SUFFIX}" if env_dir and wf else None,
        "run_page": env_dir / f"{wf}.RUN.html" if env_dir and wf else None,
        "main_nf": workspace / "pipelines" / pipe / "main.nf" if pipe else None,
    }


def inspect_workspace(workspace: Path, final_text: str = "") -> dict:
    """The ladder, read off the bound deliverables: what was written, not what was
    claimed. `sealed_names` lists every current workflow spec for the record; only the
    bound one counts."""
    workspace = Path(workspace)
    bound = bind_deliverables(workspace, final_text)
    paths = bound_paths(workspace, bound)
    spec = _load_yaml(paths["spec"]) if paths["spec"] and paths["spec"].exists() else None
    exists = lambda key: bool(paths[key]) and paths[key].exists()      # noqa: E731
    return {"sealed": spec is not None, "sealed_names": [s["name"] for s in _workflow_specs(workspace / "environments")],
            "usage_verified": bool(spec and spec.get("usage_verified") is True),
            "env_report": exists("env_page"), "run_report": spec is not None and exists("run_page"),
            "pipeline_rendered": spec is not None and exists("main_nf"), "bound": bound}


# ---------------------------------------------------------------------------
# The expect block
# ---------------------------------------------------------------------------

_SLOT_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
#: An SRA-style accession as a token: `airway_SRR1039508_10K` carries one, `xSRR1` does not.
_ACCESSION_RE = re.compile(r"(?<![A-Za-z0-9])[SED]RR\d{5,}(?![A-Za-z0-9])")
_CONDA_PREFIXES = ("bioconductor-", "r-")
#: Refs that float: a tool pinned to one of these has no version, whatever commit it sat at.
_FLOATING_REFS = frozenset({"main", "master", "head", "latest", "develop", "dev", "trunk"})


def _norm_tool(name: Any) -> str:
    """A tool name folded for comparison: case, separators and the conda channel prefixes
    (`bioconductor-`, `r-`) are not identity."""
    s = str(name or "").strip().lower()
    for p in _CONDA_PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
    return re.sub(r"[^a-z0-9]", "", s)


def _version_from_request_key(key: Any, tool: str) -> str | None:
    """`tool=version` out of an env request key such as `minimap2=2.31|linux/amd64|none`."""
    for part in str(key or "").split("|", 1)[0].split(","):
        name, _, ver = part.partition("=")
        if _norm_tool(name) == _norm_tool(tool) and ver.strip():
            return ver.strip()
    return None


def recorded_version(tool: str, recipe: dict | None, spec: dict | None, env_cache: dict | None,
                     env: str | None) -> str | None:
    """The version the records carry for `tool`, in order of authority: the recipe's
    resolved packages, tool identities and shipped binaries; the install step that put it
    there (a package version, else the pinned ref or commit); the spec's env request key;
    the EnvCache entry's request key or the adopted image's tag. None when no record says."""
    want = _norm_tool(tool)
    r = recipe or {}
    for key, name_field in (("resolved_packages", "name"), ("tool_identities", "tool"), ("shipped_binaries", "tool")):
        for rec in r.get(key) or []:
            if isinstance(rec, dict) and _norm_tool(rec.get(name_field)) == want and rec.get("version"):
                return str(rec["version"])
    for rec in r.get("tool_identities") or []:
        if isinstance(rec, dict) and _norm_tool(rec.get("package")) == want and rec.get("version"):
            return str(rec["version"])
    for step in r.get("install_steps") or []:
        for pkg in (step.get("installed_packages") or []) if isinstance(step, dict) else []:
            if not isinstance(pkg, dict) or _norm_tool(pkg.get("name")) != want:
                continue
            method = pkg.get("install_method") if isinstance(pkg.get("install_method"), dict) else {}
            if str(method.get("ref") or "").lower() in _FLOATING_REFS:
                continue                       # a branch is not a version, whatever commit it sat at
            for v in (pkg.get("version"), method.get("ref"), method.get("tag"), method.get("commit_sha")):
                if v:
                    return str(v)
    if spec:
        for key in [spec.get("env_request_key")] + [e.get("request_key") for e in spec.get("envs") or [] if isinstance(e, dict)]:
            v = _version_from_request_key(key, tool)
            if v:
                return v
    for key, entry in (env_cache or {}).items():
        if not isinstance(entry, dict) or (env and entry.get("name") != env):
            continue
        v = _version_from_request_key(entry.get("request_key") or key, tool)
        if v:
            return v
        tag = str(((entry.get("adopt_source") or {}).get("tag")) or "")
        if tag and tag.lower() not in _FLOATING_REFS:
            return tag
    return None


def _depth_of(check: str, tool: str) -> str:
    from agent.skills.env_honesty import evidence_depth
    return evidence_depth(check, tool)


def check_expect(expect: dict, workspace: Path, bound: dict) -> tuple[list[str], int]:
    """Every `expect` check against the bound records. Returns (failures, checks made);
    each failure names its key and what the record says, so the row's detail explains
    the fail. A key whose record is missing fails: absence is not a pass."""
    paths = bound_paths(Path(workspace), bound)
    present = lambda key: bool(paths[key]) and paths[key].exists()        # noqa: E731
    recipe = _load_yaml(paths["recipe"]) if present("recipe") else None
    attestation = _load_json(paths["attestation"]) if present("attestation") else None
    spec = _load_yaml(paths["spec"]) if present("spec") else None
    spec_text = paths["spec"].read_text(encoding="utf-8", errors="replace") if present("spec") else ""
    env_cache = _load_json(paths["env_cache"]) if present("env_cache") else None
    main_nf = paths["main_nf"].read_text(encoding="utf-8", errors="replace") if present("main_nf") else None
    fails: list[str] = []
    checked = 0

    if "tools" in expect:
        checked += 1
        if recipe is None:
            fails.append("expect:tools: no recipe.yaml in the bound env")
        else:
            primary = [str(t) for t in recipe.get("primary_tools") or []]
            for tool in expect["tools"]:
                if not any(_norm_tool(p) == _norm_tool(tool) for p in primary):
                    fails.append(f"expect:tools: {tool} is not in primary_tools {primary}")
                elif not recorded_version(str(tool), recipe, spec, env_cache, bound.get("env")):
                    fails.append(f"expect:tools: no recorded version for {tool}")
    if "build_method" in expect:
        checked += 1
        accepted = expect["build_method"] if isinstance(expect["build_method"], list) else [expect["build_method"]]
        actual = recipe.get("build_method") if recipe else None
        if actual not in accepted:
            fails.append(f"expect:build_method: {actual!r} is not {accepted}")
    if "evidence_depth_min" in expect:
        checked += 1
        floor = EVIDENCE_DEPTHS.index(str(expect["evidence_depth_min"]))
        checks = (((attestation or {}).get("predicate") or {}).get("buildDefinition") or {}) \
            .get("internalParameters", {}).get("validated_in_image") if attestation else None
        if not checks:
            fails.append("expect:evidence_depth_min: no validated_in_image checks in the attestation")
        else:
            shallow = []
            for c in checks:
                depth = _depth_of(str(c.get("check") or ""), str(c.get("tool") or ""))
                if depth not in EVIDENCE_DEPTHS or EVIDENCE_DEPTHS.index(depth) < floor:
                    shallow.append(f"{c.get('tool')}={depth}")
            if shallow:
                fails.append(f"expect:evidence_depth_min: below {expect['evidence_depth_min']}: {', '.join(shallow)}")
    if "slots" in expect:
        checked += 1
        template = (spec or {}).get("usage", {}).get("command_template") if spec else None
        commands = template if isinstance(template, list) else ([template] if template else [])
        slots = sorted({s for cmd in commands for s in _SLOT_RE.findall(str(cmd))})
        if spec is None:
            fails.append("expect:slots: no workflow spec bound")
        else:
            for pat in expect["slots"]:
                if not any(fnmatch.fnmatchcase(s, str(pat)) for s in slots):
                    fails.append(f"expect:slots: no {{{pat}}} slot in the how-to (slots: {slots})")
    if "trials_min" in expect:
        checked += 1
        trials = ((spec or {}).get("usage") or {}).get("trials") or [] if spec else []
        if len(trials) < int(expect["trials_min"]):
            fails.append(f"expect:trials_min: {len(trials)} trial(s) declared, {expect['trials_min']} wanted")
    if "accessions" in expect:
        checked += 1
        if spec is None:
            fails.append("expect:accessions: no workflow spec bound")
        for acc in expect["accessions"] if spec is not None else []:
            if not re.search(rf"(?<![A-Za-z0-9]){re.escape(str(acc))}(?![A-Za-z0-9])", spec_text):
                fails.append(f"expect:accessions: {acc} does not appear in the workflow spec")
    if "accessions_min" in expect:
        checked += 1
        found = sorted(set(_ACCESSION_RE.findall(spec_text)))
        if len(found) < int(expect["accessions_min"]):
            fails.append(f"expect:accessions_min: {len(found)} distinct accession(s) in the spec {found}, "
                         f"{expect['accessions_min']} wanted")
    if "cohort_stage" in expect and expect["cohort_stage"]:
        checked += 1
        if main_nf is None:
            fails.append("expect:cohort_stage: no main.nf rendered from the bound workflow")
        elif not re.search(r"\b[A-Za-z_]\w*\([^\n;]*\.collect\(\)", main_nf):
            fails.append("expect:cohort_stage: no process in main.nf runs over a collected channel")
    return fails, checked


# ---------------------------------------------------------------------------
# The judge
# ---------------------------------------------------------------------------

def codes_match(codes: dict, patterns: list[str]) -> bool:
    """Whether any observed outcome code matches any expected pattern (fnmatch, so
    `freeze.gated_*` covers a family). No patterns means any code counts."""
    if not patterns:
        return bool(codes)
    return any(fnmatch.fnmatchcase(code, pat) for code in codes for pat in patterns)


def codes_required(row: dict) -> bool:
    """Every `required_codes` pattern matched by at least one proven or degraded outcome."""
    seen = [code for code, cls in (row.get("code_classes") or {}).items() if cls in ("proven", "degraded")]
    return all(any(fnmatch.fnmatchcase(code, pat) for code in seen) for pat in (row.get("required_codes") or []))


def judge(rule: str, row: dict) -> bool:
    """The experiment's rule, plus its required codes, on a row. False for a run the
    runner cut short and for a session that did not end cleanly, whatever landed."""
    if rule not in SUCCESS_RULES:
        raise ValueError(f"unknown success rule {rule!r}; one of {SUCCESS_RULES}")
    if row["timed_out"]:
        return False
    return _rule_holds(rule, row) and codes_required(row)


def _rule_holds(rule: str, row: dict) -> bool:
    if row["is_error"] is not False:
        return False                       # every rule wants a session that ended cleanly
    completed = row["mcp_calls"] > row["mcp_errors"]                      # the server answered
    if rule == "completed":
        return completed
    if rule == "frozen":
        return completed and bool(row["frozen"] and row["env_report"])
    held = (not row["sealed"]) or bool(row["usage_verified"])    # nothing sealed AROUND a refusal
    gate_fired = completed and held and codes_match(row.get("refusal_codes") or {}, row.get("expected_codes") or [])
    if rule == "refused":
        return gate_fired
    if rule == "declined":
        quiet = row["mcp_calls"] == 0 and not row["sealed"] and not row["frozen"]
        return quiet or (gate_fired and not row["frozen"])
    if rule == "asked":
        return not row["frozen"] and not row["sealed"] and asks_the_user(row.get("final_text") or "")
    sealed = bool(row["sealed"] and row["usage_verified"] and row["env_report"] and row["run_report"])
    if rule == "sealed":
        return sealed
    return sealed and bool(row["pipeline_rendered"])


def gate_columns(row: dict) -> tuple[bool | None, bool | None]:
    """The two S/R readings. The gate is EXERCISED when the model called one of the
    scenario's `gated_tools` or drew an outcome coded like its `expected_codes`.
    `agent_declined` is the negation of that (None for a scenario that declares neither).
    `gate_held` is read only when exercised and the scenario declares gate codes: the
    server refused with one of them and nothing was sealed around the refusal."""
    gated = [str(p) for p in row.get("gated_tools") or []]
    expected = [str(p) for p in row.get("expected_codes") or []]
    if not gated and not expected:
        return None, None
    tools = row.get("mcp_by_tool") or {}
    codes = row.get("outcome_codes") or {}
    exercised = (any(fnmatch.fnmatchcase(t, p) for t in tools for p in gated)
                 or any(fnmatch.fnmatchcase(c, p) for c in codes for p in expected))
    if not exercised:
        return None, True
    if not expected:
        return None, False
    held = codes_match(row.get("refusal_codes") or {}, expected) and ((not row["sealed"]) or bool(row["usage_verified"]))
    return held, False


def verdict_of(row: dict) -> tuple[str, list[str]]:
    """The verdict and every reason it is not a pass, in the order they are checked. A run
    with no result record is `invalid` outright; a session that was cut short or ended in
    error fails before the artifacts are read; a mutated shared corpus fails the run on its
    own; then the rule, its required codes and the expect block."""
    if row["is_error"] is None and not row["timed_out"]:
        return "invalid", ["no_result"]
    reasons: list[str] = []
    if row["timed_out"]:
        reasons.append("timed_out")
    elif row["is_error"]:
        reasons.append("session_error")
    if row.get("resources_mutated"):
        reasons.append("resources_mutated")
    if not reasons:
        rule = row["success_rule"]
        if not _rule_holds(rule, row):
            reasons.append(f"rule:{rule}")
        if not codes_required(row):
            reasons.append("required_codes")
        reasons.extend(row.get("expect_failed") or [])
    return ("pass" if not reasons else "fail"), reasons


# ---------------------------------------------------------------------------
# The row
# ---------------------------------------------------------------------------

def corpus_of(meta: dict) -> str:
    """The corpus a run belongs to: what the run recorded, else the definition's `corpus`
    key read from the file the run names as its source, else that file's directory."""
    if meta.get("corpus"):
        return str(meta["corpus"])
    src = Path(str(meta.get("source") or ""))
    if src.is_file():
        try:
            import yaml
            d = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
            if isinstance(d, dict) and d.get("corpus"):
                return str(d["corpus"])
        except Exception:
            pass
    return src.parent.name or "uncategorised"


def parse_run(run_dir: Path) -> dict:
    """One run directory (experiment.json + transcript.jsonl + workspace/) → one row."""
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "experiment.json").read_text(encoding="utf-8"))
    transcript = run_dir / "transcript.jsonl"
    tx = _read_transcript(_loads_lines(transcript) if transcript.exists() else [])
    res = tx["result"]
    stats = _tool_stats(tx)
    workspace = Path(meta.get("workspace") or run_dir / "workspace")
    ws = inspect_workspace(workspace, tx["final_text"])
    expect = dict(meta.get("expect") or {})
    expect_failed, expect_checked = check_expect(expect, workspace, ws["bound"]) if expect else ([], 0)
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
        "corpus": corpus_of(meta),
        "model": meta.get("model", ""),
        "model_id": model_id,
        "repeat": meta.get("repeat", 0),
        "attempt": int(meta.get("attempt") or 1),
        "run_id": run_dir.name,
        "code_rev": meta.get("code_rev", ""),
        "verdict": None,
        "success": None,
        "success_rule": meta.get("success") or TIER_DEFAULT_RULE.get(meta.get("tier", ""), "completed"),
        "reason": None,
        "gate_held": None,
        "agent_declined": None,
        "terminal_reason": res.get("terminal_reason") or ("none" if not res else ""),
        "is_error": bool(res.get("is_error")) if res else None,
        "timed_out": bool(meta.get("timed_out")),
        "frozen": stats.pop("frozen"),
        "sealed": ws["sealed"],
        "usage_verified": ws["usage_verified"],
        "env_report": ws["env_report"],
        "run_report": ws["run_report"],
        "pipeline_rendered": ws["pipeline_rendered"],
        "cost_usd": float(res["total_cost_usd"]) if res.get("total_cost_usd") is not None else None,
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
        "judge_hash": JUDGE_HASH,
    }
    row.update(stats)
    row.update({
        "expected_codes": list(meta.get("expected_codes") or []),
        "required_codes": list(meta.get("required_codes") or []),
        "gated_tools": list(meta.get("gated_tools") or []),
        "expect": expect, "expect_failed": expect_failed, "expect_checked": expect_checked,
        "bound": ws["bound"], "sealed_names": ws["sealed_names"],
        "resources_mutated": meta.get("resources_mutated"),
        "attempts_of": str(meta.get("attempts_of") or run_dir.name),
        "model_usage": model_usage, "final_text": tx["final_text"][:2000],
        "session_id": res.get("session_id") or tx["init"].get("session_id") or "",
        "claude_code_version": tx["init"].get("claude_code_version") or "",
        "code_dirty": bool(meta.get("code_dirty")),
        "started_at": meta.get("started_at", ""),
    })
    row["gate_held"], row["agent_declined"] = gate_columns(row)
    row["verdict"], row["reasons"] = verdict_of(row)
    row["success"] = {"pass": True, "fail": False}.get(row["verdict"])
    row["reason"] = row["reasons"][0] if row["reasons"] else None
    missing = [k for k in METRIC_NAMES if k not in row]
    assert not missing, f"row is missing metric columns {missing}"
    return row


def write_row(run_dir: Path, row: dict) -> Path:
    out = Path(run_dir) / "metrics.json"
    out.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def load_rows(experiment_dirs: list[Path], reparse: bool = False) -> list[dict]:
    """Every run under the given experiment directories, parsed (or re-read from its
    metrics.json when that row was judged by the current judge), ordered by experiment,
    model, repeat, attempt."""
    rows = []
    for exp in experiment_dirs:
        for run in sorted(Path(exp).iterdir()) if Path(exp).is_dir() else []:
            if not (run / "experiment.json").exists():
                continue
            mj = run / "metrics.json"
            row = json.loads(mj.read_text(encoding="utf-8")) if mj.exists() and not reparse else None
            if row is None or row.get("judge_hash") != JUDGE_HASH:
                row = parse_run(run)
                write_row(run, row)
            rows.append(row)
    rows.sort(key=lambda r: (r["experiment"], r["model"], r["repeat"], r.get("attempt") or 1))
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


def classify_attempts(verdicts: list[str]) -> tuple[str, bool]:
    """The class of one repeat from its attempts, in order. A verdict two attempts agree on
    is confirmed; a lone pass needs no confirmation (only failures are re-run). Otherwise
    the last attempt that reached a verdict stands, unconfirmed; attempts that were all
    invalid stay invalid."""
    counts = Counter(verdicts)
    for v in VERDICTS:
        if counts[v] >= 2:
            return v, True
    reached = [v for v in verdicts if v != "invalid"]
    if not reached:
        return "invalid", False
    return reached[-1], len(verdicts) == 1 and reached[-1] == "pass"


def slot_classes(rows: list[dict]) -> list[dict]:
    """One record per repeat slot in `rows` (an experiment × model × revision group): its
    attempts' verdicts in order and the class they confirm."""
    slots: dict[tuple, list[dict]] = {}
    for r in rows:
        slots.setdefault((r.get("repeat"), str(r.get("attempts_of") or r.get("run_id") or "")), []).append(r)
    out = []
    for key, rs in sorted(slots.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        rs = sorted(rs, key=lambda r: int(r.get("attempt") or 1))
        verdicts = [r.get("verdict") or ("pass" if r.get("success") else "invalid" if r.get("success") is None else "fail")
                    for r in rs]
        cls, confirmed = classify_attempts(verdicts)
        out.append({"repeat": key[0], "attempts_of": key[1], "verdicts": verdicts, "class": cls, "confirmed": confirmed,
                    "run_ids": [r.get("run_id") for r in rs]})
    return out


_MEANED = ("cost_usd", "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
           "thinking_tokens", "context_peak", "api_calls", "turns", "tool_calls", "mcp_calls",
           "tool_search_calls", "other_tool_calls", "repeated_calls", "tool_errors", "mcp_errors",
           "permission_denials", "mcp_proven", "mcp_degraded", "mcp_refused", "mcp_broke",
           "mcp_loop", "mcp_vanished", "mcp_unstated", "wall_ms", "api_ms")


def aggregate(rows: list[dict], by: tuple[str, ...] = ("experiment", "model")) -> list[dict]:
    """One record per distinct value of `by` WITHIN one code revision: the revision
    (code_rev, code_dirty) is always part of the key, so (experiment, model) is the
    scoreboard per revision and (experiment, code_rev, code_dirty) the view across
    revisions — a clean checkout and the same commit with uncommitted changes are two
    conditions, so two rows. The pass numbers count repeat SLOTS, each classed from its
    attempts (confirm-on-fail re-runs keep their rows but not their own vote); the means
    run over every attempt. Every record carries the experiment, model and code_rev of its
    first row plus the sets seen."""
    by = tuple(by) + tuple(k for k in ("code_rev", "code_dirty") if k not in by)
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault(tuple(str(r.get(k) or "") for k in by), []).append(r)
    out = []
    for key, rs in sorted(groups.items()):
        slots = slot_classes(rs)
        n = len(slots)
        c = sum(1 for s in slots if s["class"] == "pass")
        inv = sum(1 for s in slots if s["class"] == "invalid")
        g: dict[str, Any] = {"experiment": rs[0]["experiment"], "tier": rs[0]["tier"], "model": rs[0]["model"],
                             "model_id": rs[0]["model_id"], "code_rev": rs[0].get("code_rev", ""),
                             "code_dirty": any(r.get("code_dirty") for r in rs),
                             "models": sorted({r["model"] for r in rs}),
                             "code_revs": sorted({r.get("code_rev", "") for r in rs}),
                             "n": n, "attempts": len(rs), "judged": n, "successes": c,
                             "failures": n - c - inv, "invalid": inv,
                             "unconfirmed": sum(1 for s in slots if not s["confirmed"]),
                             "pass_at_1": (c / n) if n else None,
                             "pass_pow_k": pass_pow_k(n, c, n) if n else None, "k": n,
                             "slots": slots}
        g.update({k: v for k, v in zip(by, key) if k not in g})
        exercised = [r for r in rs if r.get("agent_declined") is False]
        g["gate_applicable"] = any(r.get("agent_declined") is not None for r in rs)
        g["gate_exercised"] = len(exercised)
        g["gate_held"] = sum(1 for r in exercised if r.get("gate_held") is True)
        g["gate_declared"] = any(r.get("expected_codes") for r in rs)
        g["agent_declined"] = sum(1 for r in rs if r.get("agent_declined") is True)
        for key in _MEANED:
            vals = [float(r[key]) for r in rs if r.get(key) is not None]
            g[f"{key}_mean"] = statistics.fmean(vals) if vals else None
            g[f"{key}_sd"] = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        g["cost_unreported"] = sum(1 for r in rs if r.get("cost_usd") is None)
        g["cost_of_pass"] = ((g["cost_usd_mean"] / g["pass_at_1"])
                             if g["pass_at_1"] and g["cost_usd_mean"] is not None else None)
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
