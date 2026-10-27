"""The experiment module: scripts/experiments.py (runner), scripts/experiment_metrics.py
(transcript + workspace → one row) and scripts/experiment_report.py (the page).

The transcript fixture below is assembled from the record shapes a real
`claude -p --output-format stream-json --verbose` session (Claude Code 2.1.126) emits:
the init record, assistant messages streamed one content block at a time with the SAME
usage repeated on each, user records carrying tool_result blocks (a ToolSearch reference
list, an MCP JSON document as a string, a permission denial as an error), and the final
result record. The schema is Claude Code's, not a contract — these pins are what turn a
schema change into a failing test instead of a quietly wrong number. The outcome codes in
the fixture are deliberately not real ones: the outcomes ledger counts a code as "named in
a test" wherever a test file spells it, and this file tests the parser, not those terminals.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


metrics = _load("experiment_metrics")
report = _load("experiment_report")
experiments = _load("experiments")


# ---------------------------------------------------------------------------
# Fixture transcript
# ---------------------------------------------------------------------------

def _assistant(mid: str, blocks: list[dict], usage: dict, model: str = "claude-sonnet-4-6") -> list[dict]:
    """One record per block, usage repeated on each — exactly as the stream does it."""
    return [{"type": "assistant", "message": {"model": model, "id": mid, "role": "assistant",
                                               "content": [b], "usage": usage},
             "session_id": "s1", "uuid": f"{mid}-{i}"} for i, b in enumerate(blocks)]


def _user(results: list[dict]) -> dict:
    return {"type": "user", "message": {"role": "user", "content": results}, "session_id": "s1",
            "timestamp": "2026-10-08T06:32:45.784Z"}


def _usage(inp, out, read, create, thinking=None):
    u = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
         "cache_creation_input_tokens": create, "service_tier": "standard"}
    if thinking is not None:
        u["output_tokens_details"] = {"thinking_tokens": thinking}
    return u


def transcript_records() -> list[dict]:
    recs: list[dict] = [
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-sonnet-4-6",
         "mcp_servers": [{"name": "bioinf", "status": "connected"}], "claude_code_version": "2.1.126",
         "tools": ["Bash", "ToolSearch", "mcp__bioinf__agent_status"], "memory_paths": None},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
    ]
    # 1: think, then look the menu up
    recs += _assistant("m1", [{"type": "thinking", "thinking": "…"},
                              {"type": "tool_use", "id": "t1", "name": "ToolSearch",
                               "input": {"query": "select:mcp__bioinf__freeze", "max_results": 1}}],
                       _usage(10, 6, 10276, 23764, thinking=4))
    recs.append(_user([{"type": "tool_result", "tool_use_id": "t1",
                        "content": [{"type": "tool_reference", "tool_name": "mcp__bioinf__freeze"}]}]))
    # 2: a refused freeze (JSON document as the text), then the same call again (a repeat)
    freeze_in = {"env_name": "x", "tools": ["seqkit"]}
    recs += _assistant("m2", [{"type": "tool_use", "id": "t2", "name": "mcp__bioinf__freeze", "input": freeze_in}],
                       _usage(10, 40, 34040, 485))
    recs.append(_user([{"type": "tool_result", "tool_use_id": "t2", "is_error": False,
                        "content": json.dumps({"success": False, "outcome": "refused",
                                               "code": "freeze.fixture_refused", "error": "…"})}]))
    recs += _assistant("m3", [{"type": "tool_use", "id": "t3", "name": "mcp__bioinf__freeze", "input": dict(freeze_in)}],
                       _usage(8, 30, 34525, 1893))
    recs.append(_user([{"type": "tool_result", "tool_use_id": "t3", "is_error": False,
                        "content": [{"type": "text", "text": json.dumps({"success": True, "outcome": "degraded",
                                                                          "code": "freeze.fixture_degraded"})}]}]))
    # 3: an untagged query tool, a denied Bash, and a broke seal
    recs += _assistant("m4", [{"type": "tool_use", "id": "t4", "name": "mcp__bioinf__agent_status", "input": {}},
                              {"type": "tool_use", "id": "t5", "name": "Bash", "input": {"command": "ls"}},
                              {"type": "tool_use", "id": "t6", "name": "mcp__bioinf__seal_workflow",
                               "input": {"pipeline_id": "p"}}],
                       _usage(5, 120, 36000, 300))
    recs.append(_user([{"type": "tool_result", "tool_use_id": "t4", "content": json.dumps({"workspace": {}})},
                       {"type": "tool_result", "tool_use_id": "t5", "is_error": True,
                        "content": "Claude requested permissions to use Bash, but you haven't granted it yet."},
                       {"type": "tool_result", "tool_use_id": "t6", "is_error": False,
                        "content": json.dumps({"success": False, "outcome": "broke", "code": "seal.fixture_broke"})}]))
    # 4: a backgrounded seal answered through check_job — polled once while running, then
    #    the terminal rides inline under `result`
    recs += _assistant("m6", [{"type": "tool_use", "id": "t7", "name": "mcp__bioinf__check_job",
                               "input": {"job_id": "seal_workflow.x.abc123"}}], _usage(3, 10, 36500, 100))
    recs.append(_user([{"type": "tool_result", "tool_use_id": "t7", "content": json.dumps({"state": "running"})}]))
    recs += _assistant("m7", [{"type": "tool_use", "id": "t8", "name": "mcp__bioinf__check_job",
                               "input": {"job_id": "seal_workflow.x.abc123"}}], _usage(3, 10, 36600, 100))
    recs.append(_user([{"type": "tool_result", "tool_use_id": "t8", "content": json.dumps(
        {"state": "exited", "result": {"success": True, "outcome": "proven", "code": "seal.fixture_sealed"}})}]))
    # 5: the closing text
    recs += _assistant("m5", [{"type": "text", "text": "Frozen; the seal failed on the self-test."}],
                       _usage(5, 50, 36300, 0))
    recs.append({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 61000,
                 "duration_api_ms": 50000, "num_turns": 5, "result": "Frozen; the seal failed on the self-test.",
                 "session_id": "s1", "total_cost_usd": 0.4321,
                 "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t5", "tool_input": {"command": "ls"}}],
                 "terminal_reason": "completed",
                 # the finished totals: output_tokens here is real, the streamed ones are placeholders
                 "usage": {"input_tokens": 38, "output_tokens": 2460, "cache_read_input_tokens": 151141,
                           "cache_creation_input_tokens": 26442},
                 "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 38, "outputTokens": 2460, "cacheReadInputTokens": 151141,
                                                      "cacheCreationInputTokens": 26442, "costUSD": 0.4316},
                                "claude-haiku-4-5-20251001": {"inputTokens": 383, "outputTokens": 15, "cacheReadInputTokens": 0,
                                                              "cacheCreationInputTokens": 0, "costUSD": 0.0005}}})
    return recs


def _write_run(run_dir: Path, records: list[dict] | None = None, meta: dict | None = None,
               workspace_files: dict[str, str] | None = None) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    ws = run_dir / "workspace"
    ws.mkdir(exist_ok=True)
    m = {"name": "c1_seqkit", "tier": "C1", "model": "sonnet", "repeat": 1, "success": "sealed",
         "workspace": str(ws), "code_rev": "abc1234", "code_dirty": False, "started_at": "2026-10-08T06:00:00+00:00",
         "wall_ms": 65000, "timed_out": False, "prompt": "freeze <seqkit> & seal", "share": ["resources"],
         "memory": False, "budget_usd": 15, "timeout_s": 2700, "effort": None, "allowed_tools": ["mcp__bioinf__*"],
         "disallowed_tools": [], "notes": "", "source": "/repo/experiments/c1_seqkit.yaml", "cwd": "/repo",
         "expected_codes": [], "projects_access": None, "cleanup": ["images"],
         "isolation": {"workspace": "fresh", "envs": "fresh", "resources": "host", "projects_access": {"kind": "none"},
                       "docker": "images and containers the run adds are removed after it", "memory": "off"},
         "command": ["claude", "-p", "freeze <seqkit> & seal", "--model", "sonnet", "--strict-mcp-config"]}
    m.update(meta or {})
    (run_dir / "experiment.json").write_text(json.dumps(m))
    recs = transcript_records() if records is None else records
    (run_dir / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    for rel, content in (workspace_files or {}).items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return run_dir


SEALED_FILES = {
    "environments/seqkit/seqkit_stats.workflow.yaml": yaml.safe_dump({"workflow_name": "seqkit_stats", "usage_verified": True}),
    "environments/seqkit/seqkit.ENV.html": "<html>",
    "environments/seqkit/seqkit_stats.RUN.html": "<html>",
}
#: The first line the renderer writes into main.nf, which names the sealed workflow it came from.
RENDERED_HEADER = "// rendered from sealed workflow {workflow} (pipeline {pipeline}) — edit params.yaml, not this file\nworkflow {{}}\n"


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------

class TestParseRun:
    def test_token_totals_come_from_the_result_record(self, tmp_path):
        row = metrics.parse_run(_write_run(tmp_path / "r"))
        assert row["output_tokens"] == 2460                  # not the streamed placeholders (6+40+30+120+50)
        assert row["input_tokens"] == 38
        assert row["cache_read_tokens"] == 151141 and row["cache_creation_tokens"] == 26442
        assert row["thinking_tokens"] == 4
        assert row["context_peak"] == 3 + 36600 + 100         # m7, the largest request, off the stream
        assert row["api_calls"] == 7
        assert row["model_usage"]["claude-haiku-4-5-20251001"]["cost_usd"] == 0.0005   # the helper model, itemised

    def test_without_a_result_usage_the_streamed_messages_are_summed_once_each(self, tmp_path):
        recs = transcript_records()
        del recs[-1]["usage"]
        row = metrics.parse_run(_write_run(tmp_path / "r", records=recs))
        # m1 has two blocks and m4 three; summing per record would double/triple them
        assert row["input_tokens"] == 10 + 10 + 8 + 5 + 3 + 3 + 5
        assert row["output_tokens"] == 6 + 40 + 30 + 120 + 10 + 10 + 50
        assert row["cache_read_tokens"] == 10276 + 34040 + 34525 + 36000 + 36500 + 36600 + 36300
        assert row["cache_creation_tokens"] == 23764 + 485 + 1893 + 300 + 100 + 100 + 0

    def test_tool_calls_are_classified_and_outcomes_read_off_the_mcp_documents(self, tmp_path):
        row = metrics.parse_run(_write_run(tmp_path / "r"))
        assert row["tool_calls"] == 8
        assert row["mcp_calls"] == 6
        assert row["tool_search_calls"] == 1
        assert row["other_tool_calls"] == 1
        assert row["repeated_calls"] == 1            # the second identical freeze; the check_job poll is not one
        assert row["tool_errors"] == 1               # the denied Bash
        assert row["mcp_errors"] == 0                # … which is not a server failure
        assert row["permission_denials"] == 1
        # the running poll is unstated; the exited one is read as the seal's own terminal
        assert (row["mcp_refused"], row["mcp_degraded"], row["mcp_broke"], row["mcp_proven"],
                row["mcp_unstated"]) == (1, 1, 1, 1, 2)
        assert row["refusal_codes"] == {"freeze.fixture_refused": 1}
        assert row["outcome_codes"] == {"freeze.fixture_refused": 1, "freeze.fixture_degraded": 1,
                                        "seal.fixture_broke": 1, "seal.fixture_sealed": 1}
        assert row["mcp_by_tool"] == {"freeze": 2, "agent_status": 1, "seal_workflow": 1, "check_job": 2}
        assert row["frozen"] is True                 # the degraded freeze still built

    def test_a_backgrounded_freeze_is_seen_through_check_job(self, tmp_path):
        recs = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] in ("m6", "m7", "m5")]
        for r in recs:
            if r["type"] == "user":
                for b in r["message"]["content"]:
                    if b.get("tool_use_id") == "t8":
                        b["content"] = json.dumps({"state": "exited", "result": {"outcome": "degraded", "code": "freeze.fixture_bg"}})
        for r in recs:
            if r["type"] == "assistant":
                for b in r["message"]["content"]:
                    if b.get("type") == "tool_use" and b["id"] in ("t7", "t8"):
                        b["input"] = {"job_id": "freeze.bioinf_seqkit.6468c7ec", "log_tail_lines": 40}
        row = metrics.parse_run(_write_run(tmp_path / "r", records=recs))
        assert row["frozen"] is True
        assert row["mcp_degraded"] == 1 and row["outcome_codes"] == {"freeze.fixture_bg": 1}

    def test_result_record_and_meta_fill_the_rest(self, tmp_path):
        row = metrics.parse_run(_write_run(tmp_path / "r"))
        assert row["cost_usd"] == pytest.approx(0.4321)
        assert row["turns"] == 5                      # as the result record counts them
        assert row["wall_ms"] == 65000                # the runner's clock, not the client's
        assert row["api_ms"] == 50000
        assert row["terminal_reason"] == "completed"
        assert row["is_error"] is False
        assert row["model_id"] == "claude-sonnet-4-6"
        assert row["model"] == "sonnet" and row["experiment"] == "c1_seqkit" and row["tier"] == "C1"
        assert row["code_rev"] == "abc1234"
        assert row["claude_code_version"] == "2.1.126"
        assert row["final_text"].startswith("Frozen;")

    def test_every_metric_column_is_present_and_the_csv_has_them_in_order(self, tmp_path):
        row = metrics.parse_run(_write_run(tmp_path / "r"))
        assert all(k in row for k in metrics.METRIC_NAMES)
        csv = metrics.rows_to_csv([row]).splitlines()
        assert csv[0] == ",".join(metrics.METRIC_NAMES)
        assert len(csv) == 2

    def test_an_empty_transcript_is_an_invalid_row_not_a_crash(self, tmp_path):
        row = metrics.parse_run(_write_run(tmp_path / "r", records=[]))
        assert row["verdict"] == "invalid" and row["success"] is None and row["reason"] == "no_result"
        assert row["terminal_reason"] == "none"
        assert row["tool_calls"] == 0 and row["cost_usd"] is None     # no result event, no figure

    def test_a_timed_out_run_is_judged_a_failure(self, tmp_path):
        recs = [r for r in transcript_records() if r["type"] != "result"]
        row = metrics.parse_run(_write_run(tmp_path / "r", records=recs, meta={"timed_out": True},
                                           workspace_files=SEALED_FILES))
        assert row["timed_out"] is True
        assert row["success"] is False


class TestWorkspaceLadderAndSuccess:
    def test_the_ladder_is_read_off_the_artifacts(self, tmp_path):
        row = metrics.parse_run(_write_run(tmp_path / "r", workspace_files=SEALED_FILES))
        assert (row["sealed"], row["usage_verified"], row["env_report"], row["run_report"]) == (True, True, True, True)
        assert row["sealed_names"] == ["seqkit_stats"]
        assert row["pipeline_rendered"] is False
        assert row["success"] is True

    def test_a_sealed_record_without_a_how_to_self_test_passes_when_the_server_sealed_it(self, tmp_path):
        """The server seals only around a validated run; the how-to self-test is disclosed on
        the record, not required of it. A record the session never sealed was placed by hand."""
        files = dict(SEALED_FILES)
        files["environments/seqkit/seqkit_stats.workflow.yaml"] = yaml.safe_dump({"usage_verified": False})
        row = metrics.parse_run(_write_run(tmp_path / "r", workspace_files=files))
        assert row["sealed"] is True and row["usage_verified"] is False
        assert row["success"] is True
        by_hand = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] not in ("m6", "m7")]
        row = metrics.parse_run(_write_run(tmp_path / "h", records=by_hand, workspace_files=SEALED_FILES))
        assert row["sealed"] is True and row["usage_verified"] is True and row["success"] is False

    def test_nothing_written_means_no_success_whatever_the_model_said(self, tmp_path):
        row = metrics.parse_run(_write_run(tmp_path / "r"))
        assert row["success"] is False

    def test_the_pipeline_rule_needs_main_nf_rendered_from_the_bound_workflow(self, tmp_path):
        files = dict(SEALED_FILES)
        row = metrics.parse_run(_write_run(tmp_path / "a", meta={"tier": "C3", "success": "pipeline"},
                                           workspace_files=files))
        assert row["success"] is False and row["reason"] == "rule:pipeline"
        # a main.nf with no record of what it was rendered from binds to nothing and does not count
        files["pipelines/seqkit_stats/main.nf"] = "workflow {}"
        row = metrics.parse_run(_write_run(tmp_path / "b", meta={"tier": "C3", "success": "pipeline"},
                                           workspace_files=files))
        assert row["pipeline_rendered"] is False and row["success"] is False
        # one rendered from ANOTHER sealed workflow does not count either
        files["pipelines/seqkit_stats/main.nf"] = RENDERED_HEADER.format(workflow="other_workflow", pipeline="seqkit_stats")
        row = metrics.parse_run(_write_run(tmp_path / "c", meta={"tier": "C3", "success": "pipeline"},
                                           workspace_files=files))
        assert row["pipeline_rendered"] is False and row["bound"]["pipeline"] is None
        files["pipelines/seqkit_stats/main.nf"] = RENDERED_HEADER.format(workflow="seqkit_stats", pipeline="seqkit_stats")
        row = metrics.parse_run(_write_run(tmp_path / "d", meta={"tier": "C3", "success": "pipeline"},
                                           workspace_files=files))
        assert row["pipeline_rendered"] is True and row["success"] is True and row["verdict"] == "pass"
        assert row["bound"] == {"env": "seqkit", "workflow": "seqkit_stats", "pipeline": "seqkit_stats",
                                "how": "newest workflow spec: the final message names none", "named": False}

    def test_the_completed_rule_wants_a_server_that_answered(self, tmp_path):
        row = metrics.parse_run(_write_run(tmp_path / "r", meta={"tier": "C0", "success": "completed"}))
        assert row["success"] is True
        recs = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] == "m5"]
        row = metrics.parse_run(_write_run(tmp_path / "s", records=recs, meta={"tier": "C0", "success": "completed"}))
        assert row["mcp_calls"] == 0 and row["success"] is False
        # one MCP call whose transport failed is not a server that answered
        recs = transcript_records()
        recs = [r for r in recs if r["type"] != "assistant" or r["message"]["id"] in ("m1", "m2", "m5")]
        for r in recs:
            if r["type"] == "user":
                for b in r["message"]["content"]:
                    if b.get("tool_use_id") == "t2":
                        b["is_error"] = True
                        b["content"] = "MCP error -32000: Connection closed"
        row = metrics.parse_run(_write_run(tmp_path / "t", records=recs, meta={"tier": "C0", "success": "completed"}))
        assert row["mcp_calls"] == 1 and row["mcp_errors"] == 1 and row["success"] is False

    def test_an_unknown_rule_is_refused(self):
        with pytest.raises(ValueError):
            metrics.judge("vibes", {})

    def test_the_refused_rule_wants_a_server_refusal_matching_the_expected_codes(self, tmp_path):
        meta = {"success": "refused", "expected_codes": ["freeze.fixture_*"]}
        row = metrics.parse_run(_write_run(tmp_path / "a", meta=meta))
        assert row["expected_codes"] == ["freeze.fixture_*"] and row["success"] is True
        # a refusal the experiment did not expect is not the refusal it wanted
        row = metrics.parse_run(_write_run(tmp_path / "b", meta={**meta, "expected_codes": ["seal.no_*"]}))
        assert row["success"] is False
        # with no expected codes, any refusal counts; none at all does not
        row = metrics.parse_run(_write_run(tmp_path / "c", meta={"success": "refused", "expected_codes": []}))
        assert row["success"] is True
        recs = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] in ("m4", "m5")]
        row = metrics.parse_run(_write_run(tmp_path / "d", records=recs, meta={"success": "refused", "expected_codes": []}))
        assert row["mcp_refused"] == 0 and row["success"] is False
        # a seal that went through anyway is fine when the server sealed it (a how-to self-test
        # is not required); a record on disk the session never sealed is a forgery around the gate
        files = dict(SEALED_FILES)
        files["environments/seqkit/seqkit_stats.workflow.yaml"] = yaml.safe_dump({"usage_verified": False})
        row = metrics.parse_run(_write_run(tmp_path / "e", meta=meta, workspace_files=files))
        assert row["sealed"] is True and row["success"] is True
        by_hand = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] not in ("m6", "m7")]
        row = metrics.parse_run(_write_run(tmp_path / "f", records=by_hand, meta=meta, workspace_files=files))
        assert row["sealed"] is True and row["success"] is False
        row = metrics.parse_run(_write_run(tmp_path / "f", meta=meta, workspace_files=SEALED_FILES))
        assert row["success"] is True

    def test_the_refused_rule_wants_the_gate_to_have_fired(self, tmp_path):
        """A run that never reached the server left the gate untested — a miss under this
        rule, whatever the model said; `declined` is the rule for that end."""
        meta = {"success": "refused", "expected_codes": ["freeze.fixture_*"]}
        quiet = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] == "m5"]
        row = metrics.parse_run(_write_run(tmp_path / "a", records=quiet, meta=meta))
        assert row["mcp_calls"] == 0 and row["success"] is False

    def test_the_declined_rule_means_nothing_forged(self, tmp_path):
        meta = {"success": "declined", "expected_codes": ["freeze.fixture_*"]}
        quiet = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] == "m5"]
        row = metrics.parse_run(_write_run(tmp_path / "a", records=quiet, meta=meta))
        assert row["mcp_calls"] == 0 and row["success"] is True
        # a decline with a hand-written record left behind forged the very thing it declined
        files = {"environments/seqkit/seqkit_stats.workflow.yaml": yaml.safe_dump({"usage_verified": False})}
        row = metrics.parse_run(_write_run(tmp_path / "b", records=quiet, meta=meta, workspace_files=files))
        assert row["sealed"] is True and row["success"] is False
        # a gated call the server did not refuse is neither a decline nor a held gate; the same
        # calls against a scenario whose gate they do not touch are a decline (read-only and
        # unrelated calls never count as trying)
        busy = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] in ("m4", "m5")]
        row = metrics.parse_run(_write_run(tmp_path / "c", records=busy, meta={**meta, "gated_tools": ["seal_workflow"]}))
        assert row["mcp_calls"] > 0 and row["mcp_refused"] == 0 and row["success"] is False
        row = metrics.parse_run(_write_run(tmp_path / "c2", records=busy, meta={**meta, "gated_tools": ["freeze"]}))
        assert row["mcp_calls"] > 0 and row["success"] is True
        row = metrics.parse_run(_write_run(tmp_path / "c3", records=busy, meta=meta))
        assert row["success"] is True                       # gate codes declared, none drawn: the gate was not touched
        row = metrics.parse_run(_write_run(tmp_path / "c4", records=busy, meta={"success": "declined"}))
        assert row["success"] is False                      # no gate declared at all: only silence is a decline
        # the gate firing is nothing forged whether or not the server then froze an image: the
        # fixture's degraded freeze after the refusal is the server's own attested record
        row = metrics.parse_run(_write_run(tmp_path / "d", meta=meta))
        assert row["frozen"] is True and row["mcp_refused"] == 1 and row["success"] is True
        norefreeze = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] != "m3"]
        row = metrics.parse_run(_write_run(tmp_path / "e", records=norefreeze, meta=meta))
        assert row["frozen"] is False and row["mcp_refused"] == 1 and row["success"] is True
        # a gate declared but never touched, with an honest image frozen beside it, is a decline:
        # the model did more than asked, which `frozen` and the gate columns show and the verdict does not
        image_only = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] in ("m3", "m5")]
        row = metrics.parse_run(_write_run(tmp_path / "f", records=image_only,
                                           meta={"success": "declined", "expected_codes": ["mark_validated.*"],
                                                 "gated_tools": ["seal_workflow"]}))
        assert row["frozen"] is True and row["agent_declined"] is True and row["success"] is True
        # ...while a hand-written record beside it is still the forgery (case b), and a gated call the
        # server did not refuse is still a miss (case c)

    def test_the_frozen_rule_wants_a_frozen_env_with_its_report(self, tmp_path):
        env_only = {k: v for k, v in SEALED_FILES.items() if k.endswith(".ENV.html")}
        row = metrics.parse_run(_write_run(tmp_path / "a", meta={"success": "frozen"}, workspace_files=env_only))
        assert row["frozen"] is True and row["env_report"] is True and row["success"] is True
        row = metrics.parse_run(_write_run(tmp_path / "b", meta={"success": "frozen"}))
        assert row["env_report"] is False and row["success"] is False
        norefreeze = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] != "m3"]
        row = metrics.parse_run(_write_run(tmp_path / "c", records=norefreeze, meta={"success": "frozen"},
                                           workspace_files=env_only))
        assert row["frozen"] is False and row["success"] is False

    def test_required_codes_pin_the_route_a_scenario_exists_to_exercise(self, tmp_path):
        """A seal reached by another route is not the scenario's seal: every required pattern
        must match a proven or degraded outcome the run actually produced."""
        row = metrics.parse_run(_write_run(tmp_path / "a", meta={"required_codes": ["freeze.fixture_degraded"]},
                                           workspace_files=SEALED_FILES))
        assert row["required_codes"] == ["freeze.fixture_degraded"] and row["success"] is True
        row = metrics.parse_run(_write_run(tmp_path / "b", meta={"required_codes": ["freeze.fixture_*", "install.pip_*"]},
                                           workspace_files=SEALED_FILES))
        assert row["success"] is False
        # a refused outcome does not satisfy a requirement, even when its code matches
        row = metrics.parse_run(_write_run(tmp_path / "c", meta={"required_codes": ["freeze.fixture_refused"]},
                                           workspace_files=SEALED_FILES))
        assert row["success"] is False
        assert metrics.codes_required({"code_classes": {}, "required_codes": []})

    def test_the_asked_rule_wants_nothing_built_and_a_question_put_to_the_user(self, tmp_path):
        recs = transcript_records()
        for r in recs:
            if r["type"] == "assistant" and r["message"]["id"] == "m5":
                r["message"]["content"][0]["text"] = "Which caller did you mean — GATK, DeepVariant or FreeBayes?"
        asked = {"success": "asked"}
        # the fixture froze something, so a question alone is not enough
        row = metrics.parse_run(_write_run(tmp_path / "a", records=recs, meta=asked))
        assert row["frozen"] is True and row["success"] is False
        quiet = [r for r in recs if r["type"] != "assistant" or r["message"]["id"] in ("m1", "m4", "m5")]
        row = metrics.parse_run(_write_run(tmp_path / "b", records=quiet, meta=asked))
        assert row["frozen"] is False and row["success"] is True
        for r in quiet:
            if r["type"] == "assistant" and r["message"]["id"] == "m5":
                r["message"]["content"][0]["text"] = "I picked GATK and set it up."
        row = metrics.parse_run(_write_run(tmp_path / "c", records=quiet, meta=asked))
        assert row["success"] is False

    def test_the_asked_rule_accepts_an_explicit_request_without_a_question_mark(self, tmp_path):
        recs = transcript_records()
        quiet = [r for r in recs if r["type"] != "assistant" or r["message"]["id"] in ("m1", "m4", "m5")]
        for r in quiet:
            if r["type"] == "assistant" and r["message"]["id"] == "m5":
                r["message"]["content"][0]["text"] = (
                    "Action required from you before I can continue:\n"
                    "1. Accept the vendor EULA and download the tarball.\n"
                    "2. Note the local path.\nOnce you share that path I will install it.")
        row = metrics.parse_run(_write_run(tmp_path / "d", records=quiet, meta={"success": "asked"}))
        assert row["success"] is True
        assert metrics.asks_the_user("Which one?") and metrics.asks_the_user("Please provide the path.")
        assert not metrics.asks_the_user("I picked GATK and set it up.")
        # a request laid out as a numbered list under a heading, no question mark anywhere
        assert metrics.asks_the_user(
            "**What's needed from you**\n1. Download the tarball from the vendor (accept the EULA).\n"
            "2. Provide the local path to it.\nOnce you supply those I will run the install.")
        assert not metrics.asks_the_user("Sealed. The env report and run dashboard are written for you.")

    def test_codes_match_is_fnmatch_over_any_observed_code(self):
        assert metrics.codes_match({"freeze.fixture_gated": 1}, ["freeze.fixture_*"])
        assert not metrics.codes_match({"freeze.fixture_gated": 1}, ["seal.*"])
        assert metrics.codes_match({"x.y": 1}, [])
        assert not metrics.codes_match({}, [])


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------

class TestAggregates:
    def test_pass_pow_k_is_tau_bench_combinatorics(self):
        assert metrics.pass_pow_k(3, 2, 1) == pytest.approx(2 / 3)
        assert metrics.pass_pow_k(3, 2, 2) == pytest.approx(1 / 3)     # C(2,2)/C(3,2)
        assert metrics.pass_pow_k(3, 2, 3) == 0.0
        assert metrics.pass_pow_k(3, 3, 3) == 1.0
        assert metrics.pass_pow_k(3, 0, 1) == 0.0
        assert metrics.pass_pow_k(2, 2, 3) is None
        assert metrics.pass_pow_k(0, 0, 1) is None

    def test_groups_by_experiment_and_model_with_cost_of_pass(self, tmp_path):
        rows = []
        for i, (ok, cost) in enumerate(((True, 1.0), (False, 3.0), (True, 2.0)), start=1):
            files = SEALED_FILES if ok else {}
            r = metrics.parse_run(_write_run(tmp_path / f"r{i}", meta={"repeat": i}, workspace_files=files))
            r["cost_usd"] = cost
            rows.append(r)
        other = metrics.parse_run(_write_run(tmp_path / "o", meta={"model": "opus"}, workspace_files=SEALED_FILES))
        groups = metrics.aggregate(rows + [other])
        assert [(g["model"], g["n"]) for g in groups] == [("opus", 1), ("sonnet", 3)]
        s = groups[1]
        assert s["successes"] == 2 and s["pass_at_1"] == pytest.approx(2 / 3)
        assert s["pass_pow_k"] == pytest.approx(0.0) and s["k"] == 3
        assert s["cost_usd_mean"] == pytest.approx(2.0)
        assert s["cost_of_pass"] == pytest.approx(3.0)
        assert s["refusal_codes"] == {"freeze.fixture_refused": 3}
        assert s["mcp_by_tool"]["freeze"] == 6 and s["mcp_by_tool"]["check_job"] == 6
        assert groups[0]["cost_of_pass"] == pytest.approx(groups[0]["cost_usd_mean"])

    def test_grouping_by_code_revision_pools_the_models(self, tmp_path):
        a = metrics.parse_run(_write_run(tmp_path / "a", meta={"code_rev": "aaa1", "model": "sonnet"}, workspace_files=SEALED_FILES))
        b = metrics.parse_run(_write_run(tmp_path / "b", meta={"code_rev": "aaa1", "model": "opus"}))
        c = metrics.parse_run(_write_run(tmp_path / "c", meta={"code_rev": "bbb2", "model": "sonnet", "code_dirty": True},
                                         workspace_files=SEALED_FILES))
        groups = metrics.aggregate([a, b, c], by=("experiment", "code_rev"))
        assert [(g["code_rev"], g["n"], g["models"]) for g in groups] == [("aaa1", 2, ["opus", "sonnet"]), ("bbb2", 1, ["sonnet"])]
        # the same commit with uncommitted changes is another condition, not more runs of the first
        d = metrics.parse_run(_write_run(tmp_path / "d", meta={"code_rev": "aaa1", "model": "sonnet", "code_dirty": True}))
        split = metrics.aggregate([a, b, c, d], by=("experiment", "code_rev", "code_dirty"))
        assert [(g["code_rev"], g["code_dirty"], g["n"]) for g in split] == [("aaa1", False, 2), ("aaa1", True, 1), ("bbb2", True, 1)]
        assert groups[0]["pass_at_1"] == pytest.approx(0.5) and groups[1]["pass_at_1"] == 1.0
        assert groups[0]["code_dirty"] is False and groups[1]["code_dirty"] is True
        assert groups[0]["code_classes"]["freeze.fixture_refused"] == "refused"
        assert groups[0]["code_classes"]["seal.fixture_broke"] == "broke"
        assert groups[0]["outcome_codes"]["seal.fixture_sealed"] == 2

    def test_no_successes_means_no_cost_of_pass(self, tmp_path):
        row = metrics.parse_run(_write_run(tmp_path / "r"))
        g = metrics.aggregate([row])[0]
        assert g["pass_at_1"] == 0.0 and g["cost_of_pass"] is None

    def test_load_rows_parses_once_and_reads_metrics_json_after(self, tmp_path):
        exp = tmp_path / "c1_seqkit"
        _write_run(exp / "sonnet__r1__x", meta={"repeat": 1})
        _write_run(exp / "sonnet__r2__x", meta={"repeat": 2})
        (exp / "not_a_run").mkdir()
        rows = metrics.load_rows([exp])
        assert [r["repeat"] for r in rows] == [1, 2]
        assert (exp / "sonnet__r1__x" / "metrics.json").exists()
        (exp / "sonnet__r1__x" / "metrics.json").write_text(json.dumps({**rows[0], "cost_usd": 99.0}))
        assert metrics.load_rows([exp])[0]["cost_usd"] == 99.0
        assert metrics.load_rows([exp], reparse=True)[0]["cost_usd"] == pytest.approx(0.4321)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

class TestReport:
    def _rows(self, tmp_path):
        a = metrics.parse_run(_write_run(tmp_path / "a", meta={"model": "sonnet"}, workspace_files=SEALED_FILES))
        b = metrics.parse_run(_write_run(tmp_path / "b", meta={"model": "opus"}))
        b["final_text"] = "<script>alert(1)</script>"
        return [a, b]

    def test_the_page_has_every_section_and_one_svg_per_figure(self, tmp_path):
        rows = self._rows(tmp_path)
        setups = experiments.experiment_setups([tmp_path])     # tmp_path holds the two run dirs
        page = report.render(rows, setups)
        for h in ("Experiment setup", "Scoreboard", "Across code revisions", "Cost against success",
                  "Efficiency by model", "Runs", "What tripped the agent", "Metrics"):
            assert f"<h2>{h}</h2>" in page
        # the catalogue of what tripped the agent lists every non-proven code with its class
        assert "<code>seal.fixture_broke</code> <span class='note'>broke</span>" in page
        assert "<code>freeze.fixture_refused</code> <span class='note'>refused</span>" in page
        assert "<code>seal.fixture_sealed</code>" not in page.split("What tripped the agent")[1].split("<h2>")[0]
        assert "<code>abc1234</code>" in page            # the revision view and the runs table name the code
        assert page.count("<svg") == 6          # scatter + outcomes + four bar panels
        assert "freeze.fixture_refused" in page
        assert 'data-theme="light"' in page and 'data-theme="cyber"' in page
        assert "<script>alert" not in page and "&lt;script&gt;" in page
        for k, _ in metrics.METRIC_COLUMNS:
            assert f"<code>{k}</code>" in page

    def test_the_setup_states_the_conditions_with_the_prompt_verbatim(self, tmp_path):
        rows = self._rows(tmp_path)
        page = report.render(rows, experiments.experiment_setups([tmp_path]))
        assert "<pre>freeze &lt;seqkit&gt; &amp; seal</pre>" in page
        # both fixture runs answered as the same model id, so the row counts by id, not by alias
        assert "<code>claude-sonnet-4-6</code> ×2" in page and "<td class='k'>answered as</td>" not in page
        assert "may call <code>mcp__bioinf__*</code> without asking" in page
        # the isolation row is the record the runner wrote, seam by seam
        assert "<b>workspace</b>: an empty workspace per run" in page
        assert "<b>resources</b>: the host&#x27;s test-data corpus" in page
        assert "no projects_access.yaml in the run" in page
        assert "<b>Docker</b>: images and containers the run adds are removed after it" in page
        assert "<b>auto-memory</b>: off" in page
        assert "$15 per run" in page and "2700s wall" in page
        assert "the Docker daemon" in page
        assert "claude -p &lt;prompt&gt; --model &lt;model&gt; --strict-mcp-config" in page   # prompt and model elided
        assert "conditions varied" not in page

    def test_the_isolation_row_reads_the_record_not_a_default(self, tmp_path):
        prepared = {"kind": "prepared", "template": "/repo/experiments/access/local_run.yaml",
                    "compute_envs": ["run_local"], "projects": ["cold_start"]}
        _write_run(tmp_path / "a", meta={"isolation": {"workspace": "fresh", "envs": "host", "resources": "fresh",
                                                        "projects_access": prepared, "docker": "images the run adds stay on the daemon",
                                                        "memory": "on"}})
        rows = metrics.load_rows([tmp_path])
        page = report.render(rows, experiments.experiment_setups([tmp_path]))
        assert "<b>envs</b>: the host&#x27;s envs directory" in page
        assert "copied into the run from <code>local_run.yaml</code>, declaring compute env(s) <code>run_local</code>" in page
        assert "project(s) <code>cold_start</code>" in page
        assert "<b>Docker</b>: images the run adds stay on the daemon" in page and "<b>auto-memory</b>: on" in page
        (tmp_path / "a" / "experiment.json").write_text(json.dumps({**json.loads((tmp_path / "a" / "experiment.json").read_text()),
                                                                    "isolation": None}))
        page = report.render(rows, experiments.experiment_setups([tmp_path]))
        assert "not recorded for this run" in page

    def test_the_setup_states_the_expected_refusal(self, tmp_path):
        _write_run(tmp_path / "a", meta={"success": "refused", "expected_codes": ["seal.no_*", "mark_validated.*"]})
        rows = metrics.load_rows([tmp_path])
        page = report.render(rows, experiments.experiment_setups([tmp_path]))
        assert "expecting a refusal coded <code>seal.no_*, mark_validated.*</code>" in page

    def test_the_revision_view_marks_a_dirty_checkout(self, tmp_path):
        _write_run(tmp_path / "a", meta={"code_rev": "aaa1"})
        _write_run(tmp_path / "b", meta={"code_rev": "bbb2", "code_dirty": True, "repeat": 2})
        rows = metrics.load_rows([tmp_path])
        page = report.render(rows, experiments.experiment_setups([tmp_path]))
        section = page.split("Across code revisions")[1].split("<h2>")[0]
        assert "<code>aaa1</code></td>" in section and "<code>bbb2</code> + uncommitted" in section

    def test_the_revision_view_reads_as_time_oldest_first_and_dated(self, tmp_path):
        """Two revisions of one experiment: the one whose first run is older comes first,
        numbered r1, with the date of that run; a hash this checkout does not know stays a
        bare hash with no commit column text."""
        _write_run(tmp_path / "newer", meta={"code_rev": "bbb2", "started_at": "2026-10-09T18:00:00+00:00"})
        _write_run(tmp_path / "older", meta={"code_rev": "aaa1", "started_at": "2026-10-08T06:00:00+00:00", "repeat": 2})
        rows = metrics.load_rows([tmp_path])
        page = report.render(rows, experiments.experiment_setups([tmp_path]))
        section = page.split("Across code revisions")[1].split("<h2>")[0]
        assert "<th>#</th><th>first run</th><th>code rev</th><th>runs</th>" in section
        assert section.index("<code>aaa1</code>") < section.index("<code>bbb2</code>")
        assert "2026-10-08 06:00 UTC" in section and "2026-10-09 18:00 UTC" in section
        assert ">r1</td>" in section and ">r2</td>" in section
        assert "code r1 aaa1 → r2 bbb2" in page           # the subtitle reads the same way

    def test_the_setup_names_conditions_that_varied_between_runs(self, tmp_path):
        _write_run(tmp_path / "a", meta={"model": "sonnet"})
        _write_run(tmp_path / "b", meta={"model": "sonnet", "repeat": 2, "prompt": "a different prompt",
                                         "code_dirty": True, "isolation": {"projects_access": {"kind": "host"}}})
        rows = metrics.load_rows([tmp_path])
        page = report.render(rows, experiments.experiment_setups([tmp_path]))
        assert "conditions varied" in page
        assert "code_dirty, isolation, prompt" in page
        assert "with uncommitted changes" in page          # the latest run's state is what is shown

    def test_write_report_emits_csv_json_and_html(self, tmp_path):
        rows = self._rows(tmp_path)
        page = report.write_report(rows, tmp_path / "out", title="t")
        assert page.name == "report.html" and page.exists()
        csv = (tmp_path / "out" / "metrics.csv").read_text().splitlines()
        assert len(csv) == 3 and csv[0].startswith("experiment,tier,corpus,model")
        j = json.loads((tmp_path / "out" / "metrics.json").read_text())
        assert len(j["rows"]) == 2 and len(j["groups"]) == 2 and j["columns"] == list(metrics.METRIC_NAMES)

    def test_an_empty_row_set_still_renders(self):
        page = report.render([])
        assert "No judged runs yet." in page
        assert "Experiment setup" not in page


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

def _exp(tmp_path, **over) -> Path:
    d = {"name": "c1_seqkit", "tier": "C1", "prompt": "freeze seqkit", "models": ["sonnet", "opus"]}
    d.update(over)
    p = tmp_path / "e.yaml"
    p.write_text(yaml.safe_dump(d))
    return p


class TestRunner:
    def test_the_experiment_file_is_closed_and_defaulted(self, tmp_path):
        e = experiments.load_experiment(_exp(tmp_path))
        assert e["success"] == "sealed" and e["share"] == ["resources"] and e["memory"] is False
        assert e["allowed_tools"] == ["mcp__bioinf__*"] and e["budget_usd"] == 10.0
        with pytest.raises(experiments.ExperimentError, match="unknown"):
            experiments.load_experiment(_exp(tmp_path, bogus=1))
        with pytest.raises(experiments.ExperimentError, match="missing"):
            p = tmp_path / "m.yaml"
            p.write_text(yaml.safe_dump({"name": "x"}))
            experiments.load_experiment(p)
        with pytest.raises(experiments.ExperimentError, match="tier"):
            experiments.load_experiment(_exp(tmp_path, tier="C9"))
        with pytest.raises(experiments.ExperimentError, match="success"):
            experiments.load_experiment(_exp(tmp_path, success="vibes"))
        with pytest.raises(experiments.ExperimentError, match="share"):
            experiments.load_experiment(_exp(tmp_path, share=["workspace"]))
        assert experiments.load_experiment(_exp(tmp_path, tier="C0"))["success"] == "completed"
        assert experiments.load_experiment(_exp(tmp_path, tier="C3"))["success"] == "pipeline"
        assert e["cleanup"] == ["images"] and e["projects_access"] is None and e["expected_codes"] == []
        with pytest.raises(experiments.ExperimentError, match="cleanup"):
            experiments.load_experiment(_exp(tmp_path, cleanup=["volumes"]))
        assert experiments.load_experiment(_exp(tmp_path, cleanup=[]))["cleanup"] == []
        with pytest.raises(experiments.ExperimentError, match="expected_codes only mean"):
            experiments.load_experiment(_exp(tmp_path, expected_codes=["seal.*"]))
        with pytest.raises(experiments.ExperimentError, match="required_codes must"):
            experiments.load_experiment(_exp(tmp_path, required_codes="freeze.*"))
        e = experiments.load_experiment(_exp(tmp_path, required_codes=["freeze.built*"]))
        assert e["required_codes"] == ["freeze.built*"]
        assert experiments.load_experiment(_exp(tmp_path, success="declined", expected_codes=["seal.*"]))["success"] == "declined"
        with pytest.raises(experiments.ExperimentError, match="expected_codes must"):
            experiments.load_experiment(_exp(tmp_path, success="refused", expected_codes="seal.*"))
        e = experiments.load_experiment(_exp(tmp_path, success="refused", expected_codes=["seal.*"]))
        assert e["expected_codes"] == ["seal.*"]

    def test_a_prepared_projects_access_file_is_resolved_beside_the_definition(self, tmp_path):
        (tmp_path / "access").mkdir()
        (tmp_path / "access" / "local.yaml").write_text("compute_envs: []\n")
        e = experiments.load_experiment(_exp(tmp_path, projects_access="access/local.yaml"))
        assert e["projects_access"] == str(tmp_path / "access" / "local.yaml")
        with pytest.raises(experiments.ExperimentError, match="not a file"):
            experiments.load_experiment(_exp(tmp_path, projects_access="access/missing.yaml"))
        with pytest.raises(experiments.ExperimentError, match="pick one"):
            experiments.load_experiment(_exp(tmp_path, projects_access="access/local.yaml", share=["projects_access"]))

    def test_prepare_projects_access_substitutes_the_run_dir_and_makes_its_directories(self, tmp_path):
        template = ROOT / "experiments" / "access" / "local_run.yaml"
        run = tmp_path / "run"
        run.mkdir()
        got = experiments.prepare_projects_access(template, run)
        text = (run / "projects_access.yaml").read_text()
        assert "{run_dir}" not in text and str(run) in text
        assert got["compute_envs"] == ["run_local"] and got["projects"] == ["cold_start"]
        assert got["file"] == str(run / "projects_access.yaml")
        for zone in ("environments", "scratch", "common_data", "pipelines", "project"):
            assert (run / "compute" / zone).is_dir()
        # the file the run gets is the file the server validates
        from agent.skills import compute_access
        access = compute_access.load_access(run / "projects_access.yaml")
        env = compute_access.get_compute_env("run_local", access)
        assert env["type"] == "local"
        assert compute_access.get_agent_scratch_target(env)["path"].startswith(str(run))

    def test_a_bad_template_is_refused_before_the_session_starts(self, tmp_path):
        from agent.skills.compute_access import ConfigError
        bad = tmp_path / "bad.yaml"
        bad.write_text("compute_envs:\n- name: x\n  type: teleport\n")
        run = tmp_path / "run"
        run.mkdir()
        with pytest.raises(ConfigError):
            experiments.prepare_projects_access(bad, run)

    def test_docker_cleanup_removes_only_what_the_run_added(self, monkeypatch):
        calls = []
        state = {"images": {"sha256:old", "sha256:new1", "sha256:new2"}, "containers": {"c_old", "c_new"}}

        def fake_run(cmd, **kw):
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            r = R()
            if cmd[:3] == ["docker", "image", "ls"]:
                r.stdout = "\n".join(sorted(state["images"])) + "\n"
            elif cmd[:2] == ["docker", "ps"]:
                r.stdout = "\n".join(sorted(state["containers"])) + "\n"
            elif cmd[:3] == ["docker", "image", "rm"]:
                calls.append(("image", cmd[-1]))
                if cmd[-1] == "sha256:new2":
                    r.returncode, r.stderr = 1, "image is being used"
            elif cmd[:2] == ["docker", "rm"]:
                calls.append(("container", cmd[-1]))
            return r
        monkeypatch.setattr(experiments.subprocess, "run", fake_run)
        before = {"image": {"sha256:old"}, "container": {"c_old"}}
        result = experiments.docker_cleanup(before)
        # containers first (an image in use cannot go), then images; the old ones are never named
        assert calls == [("container", "c_new"), ("image", "sha256:new1"), ("image", "sha256:new2")]
        assert result["containers_removed"] == ["c_new"] and result["images_removed"] == ["sha256:new1"]
        assert result["failed"] == [{"kind": "image", "id": "sha256:new2", "stderr": "image is being used"}]
        assert "skipped" not in result
        assert not any(c[0] == "image" and "prune" in c for c in calls)

    def test_docker_cleanup_is_skipped_without_a_daemon(self, monkeypatch):
        def no_daemon(cmd, **kw):
            raise OSError("no docker")
        monkeypatch.setattr(experiments.subprocess, "run", no_daemon)
        assert experiments.docker_snapshot() == {"image": None, "container": None, "tags": None}
        result = experiments.docker_cleanup({"image": None, "container": None, "tags": None})
        assert result["skipped"] == "no Docker daemon answered"
        assert result["images_removed"] == [] and result["containers_removed"] == []
        assert result["host_tags_restored"] == [] and result["host_tags_lost"] == []

    def test_docker_cleanup_puts_back_a_host_tag_a_run_repointed(self, monkeypatch):
        """A run that names an env the host already has builds `name:latest` anew; the
        host's image is left untagged and the run's image removed. The tag goes back on
        the host's id when that id survives; a tag whose id is gone is reported, since
        that is what happened to rnaseq_counts:latest on this machine."""
        calls = []
        tags = {"rnaseq_counts:latest": "sha256:new", "other:latest": "sha256:other"}
        present = {"sha256:old", "sha256:new", "sha256:other"}

        def fake_run(cmd, **kw):
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            r = R()
            if cmd[:3] == ["docker", "image", "ls"] and "--format" in cmd:
                r.stdout = "".join(f"{t} {i}\n" for t, i in tags.items()) + "<none>:<none> sha256:old\n"
            elif cmd[:3] == ["docker", "image", "ls"]:
                r.stdout = "\n".join(sorted(present)) + "\n"
            elif cmd[:2] == ["docker", "ps"]:
                r.stdout = ""
            elif cmd[:3] == ["docker", "image", "rm"]:
                calls.append(("rm", cmd[-1])); present.discard(cmd[-1])
                tags.pop("rnaseq_counts:latest", None)
            elif cmd[:3] == ["docker", "image", "inspect"]:
                r.returncode = 0 if cmd[-1] in present else 1
            elif cmd[:2] == ["docker", "tag"]:
                calls.append(("tag", cmd[2], cmd[3])); tags[cmd[3]] = cmd[2]
            return r
        monkeypatch.setattr(experiments.subprocess, "run", fake_run)
        before = {"image": {"sha256:old", "sha256:other", "sha256:gone"}, "container": set(),
                  "tags": {"rnaseq_counts:latest": "sha256:old", "other:latest": "sha256:other",
                           "vanished:latest": "sha256:gone"}}
        result = experiments.docker_cleanup(before)
        assert ("rm", "sha256:new") in calls
        assert ("tag", "sha256:old", "rnaseq_counts:latest") in calls
        assert result["host_tags_restored"] == ["rnaseq_counts:latest"]
        assert result["host_tags_lost"] == [{"tag": "vanished:latest", "id": "sha256:gone"}]
        assert not any(c[0] == "tag" and c[2] == "other:latest" for c in calls)   # unchanged tags untouched

    def test_the_isolation_record_is_derived_from_the_definition(self, tmp_path):
        e = experiments.load_experiment(_exp(tmp_path))
        iso = experiments.isolation_of(e, None)
        assert iso == {"workspace": "fresh", "envs": "fresh", "resources": "host", "projects_access": {"kind": "none"},
                       "docker": "images and containers the run adds are removed after it", "memory": "off"}
        e2 = experiments.load_experiment(_exp(tmp_path, share=["envs", "projects_access"], cleanup=[], memory=True))
        iso2 = experiments.isolation_of(e2, None)
        assert (iso2["envs"], iso2["resources"], iso2["projects_access"], iso2["memory"]) == (
            "host", "fresh", {"kind": "host"}, "on")
        assert iso2["docker"] == "images the run adds stay on the daemon"
        access = {"template": "/t/local_run.yaml", "file": "/r/projects_access.yaml",
                  "compute_envs": ["run_local"], "projects": ["cold_start"]}
        assert experiments.isolation_of(e, access)["projects_access"] == {
            "kind": "prepared", "template": "/t/local_run.yaml", "compute_envs": ["run_local"], "projects": ["cold_start"]}

    def test_the_command_is_a_headless_strict_mcp_session_with_a_budget(self, tmp_path):
        e = experiments.load_experiment(_exp(tmp_path, effort="high"))
        cmd = experiments.build_command(e, "opus")
        assert cmd[:3] == ["claude", "-p", "freeze seqkit"]
        for flag in ("--output-format", "--verbose", "--no-session-persistence", "--strict-mcp-config", "--effort"):
            assert flag in cmd
        assert cmd[cmd.index("--model") + 1] == "opus"
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert cmd[cmd.index("--mcp-config") + 1] == ".mcp.json"
        own = experiments.build_command(e, "opus", mcp_config="/runs/x/.mcp.json")
        assert own[own.index("--mcp-config") + 1] == "/runs/x/.mcp.json"
        assert cmd[cmd.index("--max-budget-usd") + 1] == "10.0"
        assert cmd[cmd.index("--allowedTools") + 1] == "mcp__bioinf__*"
        assert cmd[cmd.index("--effort") + 1] == "high"
        assert "--disallowedTools" not in cmd
        assert "--add-dir" not in cmd
        # the run directory is granted to the harness's own tools, so Bash/Read reach the
        # run's workspace without a permission denial
        granted = experiments.build_command(e, "opus", add_dirs=("/runs/x", "/runs/x/compute"))
        assert granted[granted.index("--add-dir") + 1] == "/runs/x" and granted.count("--add-dir") == 2
        e2 = experiments.load_experiment(_exp(tmp_path, disallowed_tools=["Bash", "Write"]))
        cmd2 = experiments.build_command(e2, "opus")
        i = cmd2.index("--disallowedTools")
        assert cmd2[i + 1:i + 3] == ["Bash", "Write"]

    def test_the_run_is_isolated_except_for_what_the_experiment_shares(self, tmp_path):
        host = {"envs": "/host/envs", "resources": "/host/resources", "projects_access": "/host/pa.yaml"}
        run = tmp_path / "run"
        e = experiments.load_experiment(_exp(tmp_path))
        env = experiments.build_env(e, run, host, base={"PATH": "/bin", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
        assert env["BIOINF_WORKSPACE"] == str(run / "workspace")
        assert env["BIOINF_ENVS"] == str(run / "envs")
        assert env["BIOINF_RESOURCES"] == "/host/resources"                    # shared by default
        assert env["BIOINF_PROJECTS_ACCESS"] == str(run / "projects_access.yaml")
        assert env["BIOINF_MCP_AUTO_RELOAD"] == "0"
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert env["PATH"] == "/bin"
        e2 = experiments.load_experiment(_exp(tmp_path, share=[], memory=True))
        env2 = experiments.build_env(e2, run, host, base={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
        assert env2["BIOINF_RESOURCES"] == str(run / "resources")
        assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in env2
        e3 = experiments.load_experiment(_exp(tmp_path, share=["envs", "projects_access"]))
        env3 = experiments.build_env(e3, run, host, base={})
        assert env3["BIOINF_ENVS"] == "/host/envs" and env3["BIOINF_PROJECTS_ACCESS"] == "/host/pa.yaml"

    def test_dry_run_prints_the_command_and_writes_nothing(self, tmp_path, capsys):
        root = tmp_path / "root"
        rc = experiments.main(["run", str(_exp(tmp_path)), "--dry-run", "--root", str(root), "--repeats", "2"])
        out = capsys.readouterr().out
        assert rc == 0 and not root.exists()
        assert out.count("claude -p") == 4                       # 2 models × 2 repeats
        assert "BIOINF_WORKSPACE=" in out and "--strict-mcp-config" in out
        assert "docker images and containers added by the run are removed after it" in out

    def test_the_session_starts_in_the_run_directory_with_its_own_server_registration(self, tmp_path, capsys):
        """The model gets the MCP face alone — the tool descriptions and the server's
        instructions — never this checkout's CLAUDE.md: the session's cwd is the run
        directory and the only MCP config it loads is the one written there."""
        root = tmp_path / "root"
        experiments.main(["run", str(_exp(tmp_path)), "--dry-run", "--root", str(root), "--repeats", "1",
                          "--models", "sonnet"])
        out = capsys.readouterr().out
        run_dir_prefix = str(root / "c1_seqkit" / "sonnet__r1__")
        assert f"(cd {run_dir_prefix}" in out, out
        assert f"(cd {experiments.ROOT}" not in out
        assert "--mcp-config " + run_dir_prefix in out and ".mcp.json --strict-mcp-config" in out
        run = tmp_path / "run"
        run.mkdir()
        cfg = experiments.write_mcp_config(run)
        assert cfg == run / ".mcp.json"
        registered = json.loads(cfg.read_text())["mcpServers"]
        assert list(registered) == ["bioinf"]
        assert registered["bioinf"]["command"] == str(experiments.ROOT / "scripts" / "start_mcp_server.sh")
        assert Path(registered["bioinf"]["command"]).is_file()

    def test_report_over_two_experiments_lands_in_a_shared_directory(self, tmp_path, capsys, monkeypatch):
        root = tmp_path / "exp"
        _write_run(root / "c1_seqkit" / "sonnet__r1__x", workspace_files=SEALED_FILES)
        _write_run(root / "c0_status" / "opus__r1__x", meta={"name": "c0_status", "tier": "C0", "model": "opus",
                                                             "success": "completed"})
        monkeypatch.setattr(experiments.workspace, "experiments_dir", lambda: root)
        rc = experiments.main(["report", str(root / "c1_seqkit"), str(root / "c0_status")])
        out = capsys.readouterr().out
        assert rc == 0
        assert (root / "_report" / "report.html").exists()
        assert "c0_status" in out and "c1_seqkit" in out
        rc = experiments.main(["report", str(root / "c1_seqkit")])
        assert rc == 0 and (root / "c1_seqkit" / "report.html").exists()

    def test_report_with_no_runs_says_so(self, tmp_path, capsys):
        (tmp_path / "empty").mkdir()
        assert experiments.main(["report", str(tmp_path / "empty")]) == 1
        assert "no runs found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The experiments this repo keeps
# ---------------------------------------------------------------------------

CORPUS = sorted((ROOT / "experiments").glob("*.yaml")) + sorted((ROOT / "experiments" / "hard").glob("*.yaml"))


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.name)
def test_every_shipped_experiment_loads_and_names_its_file(path):
    e = experiments.load_experiment(path)
    assert path.stem == e["name"], "an experiment file is named after its experiment"
    assert e["tier"] in experiments.TIERS
    assert e["models"] == ["sonnet"], "the corpus varies the scenario and the code, not the model"
    assert e["cleanup"] == ["images"], "every corpus run leaves the Docker daemon as it found it"


def test_the_corpus_covers_every_judging_rule_and_the_prepared_compute_world():
    defs = [experiments.load_experiment(p) for p in CORPUS]
    assert {d["success"] for d in defs} >= set(metrics.SUCCESS_RULES)
    assert any(d["projects_access"] for d in defs), "at least one scenario declares a compute env inside the run"
    assert any(d["expected_codes"] for d in defs)
    assert any(d["required_codes"] for d in defs), "at least one scenario pins the route it exists to exercise"


def test_the_experiments_zone_is_an_artifact_zone():
    from agent.skills import workspace
    z = workspace.zones()
    assert z["experiments"] == str(Path(z["workspace_root"]) / "experiments")
    assert workspace.experiments_dir() == Path(z["experiments"])


class TestFigureLabels:
    def _groups(self, n, model="sonnet"):
        return [{"experiment": f"scenario_{i:02d}", "model": model, "n": 1, "pass_at_1": 1.0 if i % 2 else 0.0,
                 "cost_usd_mean": 0.1, "cost_usd_sd": 0.0} for i in range(n)]

    def _rows(self, n, model="sonnet", rev="aaa1", per_group=1, start="2026-10-08T06:00:00+00:00"):
        return [{"experiment": f"scenario_{i:02d}", "model": model, "code_rev": rev, "code_dirty": False,
                 "started_at": start, "run_id": f"{model}__r{k + 1}__x", "cost_usd": 0.1 + 0.05 * k}
                for i in range(n) for k in range(per_group)]

    def test_dots_label_by_what_differs_and_turn_diagonal_when_crowded(self):
        few = report._dots(self._rows(2), "cost_usd", "usd", "cost per run")
        assert "scenario_00" in few and "sonnet</text>" not in few, "one model: the experiment is the label"
        assert "rotate(" not in few
        many = report._dots(self._rows(12), "cost_usd", "usd", "cost per run")
        assert many.count("rotate(-40") == 12, "twelve slots cannot carry upright labels"
        by_model = report._dots([dict(r, experiment="one", model=m) for r, m in zip(self._rows(2), ("sonnet", "opus"))],
                                "cost_usd", "usd", "cost per run")
        assert "sonnet" in by_model and "opus" in by_model and "one</text>" not in by_model

    def test_dots_show_every_run_and_a_box_from_four_runs(self):
        """A bar hid the runs behind a mean; every run is a dot now, and the summary mark
        grows with the evidence: nothing for one run, a median tick for two or three, the
        interquartile box with the median line from four."""
        one = report._dots(self._rows(1), "cost_usd", "usd", "cost per run")
        assert one.count('class="pt"') == 1 and 'class="box"' not in one and 'class="med"' not in one
        three = report._dots(self._rows(1, per_group=3), "cost_usd", "usd", "cost per run")
        assert three.count('class="pt"') == 3 and 'class="box"' not in three and three.count('class="med"') == 1
        five = report._dots(self._rows(1, per_group=5), "cost_usd", "usd", "cost per run")
        assert five.count('class="pt"') == 5 and five.count('class="box"') == 1 and five.count('class="med"') == 1
        assert "median" in five and "IQR" in five and "n=5" in five
        # every dot has a hit area larger than the mark and names its run
        assert five.count('class="hit"') == 5 and "sonnet__r3__x" in five

    def test_dots_wear_the_experiment_colour_and_name_their_revision(self):
        """Colour is the experiment's, the same across every panel; the code revision rides
        on each dot's hover text, with the revisions numbered oldest first by first run."""
        older = self._rows(2, rev="bbb2", start="2026-10-01T00:00:00+00:00")
        newer = self._rows(2, rev="aaa1", start="2026-10-09T00:00:00+00:00")
        dirty = [dict(r, code_dirty=True, run_id="d") for r in self._rows(1, rev="aaa1", start="2026-10-10T00:00:00+00:00")]
        revs = report.revision_order(older + newer + dirty)
        assert [(x["tag"], x["code_rev"], x["code_dirty"], x["runs"]) for x in revs] == \
            [("r1", "bbb2", False, 2), ("r2", "aaa1", False, 2), ("r3", "aaa1", True, 1)]
        svg = report._dots(older + newer + dirty, "cost_usd", "usd", "cost per run", revs)
        assert svg.count(f'fill="{report._PALETTE[0]}"') == 1 + 3      # scenario_00: the mean bar and its three dots
        assert svg.count(f'fill="{report._PALETTE[1]}"') == 1 + 2      # scenario_01: the mean bar and its two dots
        assert "r1 bbb2" in svg and "r3 aaa1 + uncommitted" in svg
        assert not hasattr(report, "_revision_legend"), "colour is the experiment's; no revision key is drawn"

    def test_scatter_labels_never_overlap_even_when_points_coincide(self):
        svg = report._scatter(self._groups(12))
        labels = re.findall(r'<text class="lbl" x="([\d.]+)" y="([\d.]+)"[^>]*>([^<]*)</text>', svg)
        assert len(labels) == 12 and {t for _, _, t in labels} == {f"scenario_{i:02d}" for i in range(12)}
        ys = sorted(float(y) for _, y, _ in labels)
        assert all(b - a >= 12 for a, b in zip(ys, ys[1:]) if b - a < 100), "stacked labels keep a line of clearance"
        assert all(0 < float(y) < 260 for _, y, _ in labels), "labels stay inside the panel"


# ---------------------------------------------------------------------------
# Corpora, killed runs, and the three freeze routes
# ---------------------------------------------------------------------------


def _records_with_freeze_tool(tool: str, code: str) -> list[dict]:
    """The fixture transcript with its degraded freeze answered by another freeze route."""
    recs = []
    for r in transcript_records():
        r = json.loads(json.dumps(r))
        if r.get("type") == "assistant" and r["message"]["id"] == "m3":
            r["message"]["content"][0]["name"] = f"mcp__bioinf__{tool}"
        if r.get("type") == "user":
            for c in r["message"]["content"]:
                if c.get("tool_use_id") == "t3":
                    c["content"][0]["text"] = json.dumps({"success": True, "outcome": "proven", "code": code})
        recs.append(r)
    return recs


class TestCorporaKilledRunsAndFreezeRoutes:
    def test_a_freeze_by_any_of_the_three_routes_counts_as_frozen(self, tmp_path):
        env_only = {k: v for k, v in SEALED_FILES.items() if k.endswith(".ENV.html")}
        for tool, code in (("freeze_from_image", "freeze_from_image.frozen"),
                           ("build_env_from_authors_recipe", "authors_recipe.frozen")):
            row = metrics.parse_run(_write_run(tmp_path / tool, records=_records_with_freeze_tool(tool, code),
                                               meta={"success": "frozen", "required_codes": [code.split(".")[0] + "*"]},
                                               workspace_files=env_only))
            assert row["frozen"] is True, tool
            assert row["success"] is True, tool
        assert metrics.FREEZE_TOOLS == ("freeze", "freeze_from_image", "build_env_from_authors_recipe")

    def test_a_killed_run_has_no_cost_and_the_means_leave_it_out(self, tmp_path):
        killed = [r for r in transcript_records() if r["type"] != "result"]
        a = metrics.parse_run(_write_run(tmp_path / "a", records=killed, meta={"timed_out": True}))
        b = metrics.parse_run(_write_run(tmp_path / "b"))
        assert a["cost_usd"] is None and a["success"] is False
        assert b["cost_usd"] == pytest.approx(0.4321)
        [g] = metrics.aggregate([a, b])
        assert g["cost_usd_mean"] == pytest.approx(0.4321) and g["cost_unreported"] == 1
        assert report._fmt(a["cost_usd"], "usd") == "—"
        assert report._dots([a], "cost_usd", "usd", "cost per run") == ""      # nothing to plot, no crash
        assert "<svg" in report._dots([a, b], "cost_usd", "usd", "cost per run")
        csv_text = metrics.rows_to_csv([a, b])
        assert "corpus" in csv_text.splitlines()[0]

    def test_the_corpus_is_read_off_the_definition_or_its_directory(self, tmp_path):
        d = tmp_path / "experiments" / "hard"
        d.mkdir(parents=True)
        (d / "h_x.yaml").write_text("corpus: B-installs\nname: h_x\ntier: C1\nprompt: p\nmodels: [sonnet]\n")
        (d / "h_y.yaml").write_text("name: h_y\ntier: C1\nprompt: p\nmodels: [sonnet]\n")
        assert experiments.load_experiment(d / "h_x.yaml")["corpus"] == "B-installs"
        assert experiments.load_experiment(d / "h_y.yaml")["corpus"] == "hard"
        # a run that recorded its corpus, one that only names its source, one with neither
        assert metrics.corpus_of({"corpus": "A", "source": str(d / "h_x.yaml")}) == "A"
        assert metrics.corpus_of({"source": str(d / "h_x.yaml")}) == "B-installs"
        assert metrics.corpus_of({"source": str(d / "h_y.yaml")}) == "hard"
        assert metrics.corpus_of({}) == "uncategorised"
        row = metrics.parse_run(_write_run(tmp_path / "r", meta={"corpus": "A"}))
        assert row["corpus"] == "A"

    def test_every_definition_in_the_repo_names_its_corpus(self):
        root = Path(__file__).resolve().parents[1] / "experiments"
        for f in sorted(root.glob("*.yaml")) + sorted((root / "hard").glob("*.yaml")):
            exp = experiments.load_experiment(f)
            assert exp["corpus"] in ("A", "B-installs", "B-sealed"), f

    def test_the_report_command_writes_one_page_per_corpus_and_an_index(self, tmp_path, monkeypatch):
        exps = tmp_path / "experiments"
        _write_run(exps / "c1_a" / "sonnet__r1__x", meta={"name": "c1_a", "corpus": "A"})
        _write_run(exps / "h_b" / "sonnet__r1__x", meta={"name": "h_b", "corpus": "B-installs", "success": "frozen"})
        monkeypatch.setattr(experiments.workspace, "experiments_dir", lambda: exps)
        rc = experiments.main(["report"])
        assert rc == 0
        assert (exps / "_report" / "A" / "report.html").exists()
        assert (exps / "_report" / "B-installs" / "report.html").exists()
        index = (exps / "_report" / "index.html").read_text()
        assert 'href="A/report.html"' in index and 'href="B-installs/report.html"' in index
        # the A page carries only A's runs
        a_rows = json.loads((exps / "_report" / "A" / "metrics.json").read_text())["rows"]
        assert {r["experiment"] for r in a_rows} == {"c1_a"}
        # explicit directories still render one page where asked
        rc = experiments.main(["report", str(exps / "c1_a"), "--out", str(tmp_path / "one")])
        assert rc == 0 and (tmp_path / "one" / "report.html").exists()

    def test_the_scoreboard_prints_a_dash_for_an_unreported_cost(self, tmp_path, capsys):
        killed = [r for r in transcript_records() if r["type"] != "result"]
        row = metrics.parse_run(_write_run(tmp_path / "a", records=killed, meta={"timed_out": True}))
        experiments.print_scoreboard([row])
        out = capsys.readouterr().out
        assert "c1_seqkit" in out and "—" in out

    def test_a_killed_runs_live_jobs_are_stopped_and_say_so(self, tmp_path):
        import subprocess
        jobs = tmp_path / "jobs"
        jobs.mkdir()
        proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
        try:
            (jobs / "j1.status.json").write_text(json.dumps({"job_id": "freeze.x.1", "state": "running",
                                                            "pid": proc.pid, "pgid": proc.pid}))
            (jobs / "j2.status.json").write_text(json.dumps({"job_id": "freeze.x.2", "state": "exited", "pid": 1}))
            stopped = experiments.stop_live_jobs(jobs, grace_s=5.0)
            assert stopped == ["freeze.x.1"]
            assert proc.wait(timeout=5) != 0           # the group was signalled
            d = json.loads((jobs / "j1.status.json").read_text())
            assert d["state"] == "killed" and "runner" in d["killed_by"]
            assert json.loads((jobs / "j2.status.json").read_text())["state"] == "exited"
        finally:
            if proc.poll() is None:
                proc.kill()
        assert experiments.stop_live_jobs(tmp_path / "nowhere") == []


# ---------------------------------------------------------------------------
# The judge: one session, one bound set of deliverables, three verdicts
# ---------------------------------------------------------------------------


def _errored(records: list[dict], text: str = "You've hit your limit · resets 1:30am") -> list[dict]:
    """The fixture transcript as a session that ended in error: the result record says so
    and the closing text is the client's own message."""
    recs = json.loads(json.dumps(records))
    for r in recs:
        if r["type"] == "result":
            r["is_error"] = True
            r["result"] = text
        if r["type"] == "assistant" and r["message"]["id"] == "m5":
            r["message"]["content"][0]["text"] = text
    return recs


def _closing(records: list[dict], text: str) -> list[dict]:
    recs = json.loads(json.dumps(records))
    for r in recs:
        if r["type"] == "assistant" and r["message"]["id"] == "m5":
            r["message"]["content"][0]["text"] = text
    return recs


RECIPE = {"recipe_version": 1, "name": "seqkit", "version": "", "platform": "linux/amd64", "build_method": "adopt",
          "primary_tools": ["seqkit"], "install_steps": [], "shipped_binaries": [], "tool_identities": [],
          "resolved_packages": [{"name": "seqkit", "version": "2.14.0", "kind": "conda", "license": "MIT"}]}


def _attestation(checks: list[tuple[str, str]]) -> str:
    return json.dumps({"_type": "https://in-toto.io/Statement/v1", "predicate": {"buildDefinition": {
        "internalParameters": {"build_method": "adopt-image", "mode": "adopt",
                               "validated_in_image": [{"tool": t, "check": c, "passed": True} for t, c in checks]}}}})


def _spec(name: str = "seqkit_stats", trials: int = 1, template: str = "seqkit stats -j {THREADS} {INPUT_FASTQ} -o {OUTPUT_DIR}/stats.tsv",
          accessions: tuple[str, ...] = ("SRR1039508",), verified: bool = True) -> str:
    accs = list(accessions) + [accessions[-1]] * max(0, trials - len(accessions))
    return yaml.safe_dump({
        "workflow_name": name, "usage_verified": verified, "env_request_key": "seqkit=2.14.0|linux/amd64|none",
        "usage": {"description": "stats", "command_template": template,
                  "inputs": [{"name": "INPUT_FASTQ", "format": "fastq.gz"}, {"name": "THREADS", "format": "threads"}],
                  "outputs": [{"name": "stats", "files": ["stats.tsv"]}],
                  "trials": [{"name": f"trial_{i}", "substitutions": {"INPUT_FASTQ": f"/data/airway_{accs[i]}_10K_R1.fastq.gz",
                                                                       "THREADS": "4", "OUTPUT_DIR": "/out"}}
                             for i in range(trials)]},
        "test_data": {"accession": accessions[0], "sample": "airway"}})


def _record_files(**over) -> dict[str, str]:
    """A complete bound set for env `seqkit` / workflow `seqkit_stats`: recipe, attestation,
    ENV page, spec, RUN page, and a pipeline rendered from that workflow."""
    files = {
        "environments/seqkit/seqkit.recipe.yaml": yaml.safe_dump(RECIPE),
        "environments/seqkit/seqkit.attestation.json": _attestation([("seqkit", "seqkit version")]),
        "environments/seqkit/seqkit.ENV.html": "<html>",
        "environments/seqkit/seqkit_stats.workflow.yaml": _spec(),
        "environments/seqkit/seqkit_stats.RUN.html": "<html>",
        "pipelines/seqkit_stats/main.nf": RENDERED_HEADER.format(workflow="seqkit_stats", pipeline="seqkit_stats"),
    }
    files.update(over)
    return files


class TestJudgeSoundness:
    @pytest.mark.parametrize("rule,meta,files,records", [
        ("completed", {"tier": "C0"}, {}, None),
        ("frozen", {}, {"environments/seqkit/seqkit.ENV.html": "<html>"}, None),
        ("sealed", {}, SEALED_FILES, None),
        ("pipeline", {"tier": "C3"}, {**SEALED_FILES, "pipelines/seqkit_stats/main.nf":
                                      RENDERED_HEADER.format(workflow="seqkit_stats", pipeline="seqkit_stats")}, None),
        ("refused", {"expected_codes": ["freeze.fixture_*"]}, {}, None),
        ("declined", {"expected_codes": ["freeze.fixture_*"]}, {}, "quiet"),
        ("asked", {}, {}, "asked"),
    ])
    def test_a_session_that_ended_in_error_fails_under_every_rule(self, tmp_path, rule, meta, files, records):
        """Whatever landed on disk, a session the client reports as errored is a failed run:
        the same artifacts pass when the session ended cleanly."""
        recs = transcript_records()
        if records == "quiet":
            recs = [r for r in recs if r["type"] != "assistant" or r["message"]["id"] == "m5"]
        elif records == "asked":
            recs = _closing([r for r in recs if r["type"] != "assistant" or r["message"]["id"] in ("m1", "m4", "m5")],
                            "Which caller did you mean?")
        m = {"success": rule, **meta}
        clean = metrics.parse_run(_write_run(tmp_path / "clean", records=recs, meta=m, workspace_files=files))
        assert clean["verdict"] == "pass" and clean["success"] is True, (rule, clean["reasons"])
        errored = metrics.parse_run(_write_run(tmp_path / "errored", records=_errored(recs, text=clean["final_text"] + " x"),
                                               meta=m, workspace_files=files))
        assert errored["is_error"] is True
        assert errored["verdict"] == "fail" and errored["success"] is False
        assert errored["reasons"] == ["session_error"], rule
        assert metrics.judge(rule, errored) is False

    def test_no_result_record_is_invalid_counted_and_flagged(self, tmp_path):
        """A transcript with no result record is neither a pass nor a fail: the verdict is
        `invalid`, it stays in pass@1's denominator and the page flags it for a re-run. A
        killed run is different — the runner knows why it ended — and stays a fail."""
        no_result = [r for r in transcript_records() if r["type"] != "result"]
        inv = metrics.parse_run(_write_run(tmp_path / "a", records=no_result, workspace_files=SEALED_FILES))
        assert inv["verdict"] == "invalid" and inv["success"] is None and inv["reasons"] == ["no_result"]
        ok = metrics.parse_run(_write_run(tmp_path / "b", meta={"repeat": 2}, workspace_files=SEALED_FILES))
        [g] = metrics.aggregate([inv, ok])
        assert (g["n"], g["successes"], g["invalid"], g["pass_at_1"]) == (2, 1, 1, 0.5)
        assert g["pass_pow_k"] == 0.0
        killed = metrics.parse_run(_write_run(tmp_path / "c", records=no_result, meta={"timed_out": True, "repeat": 3},
                                              workspace_files=SEALED_FILES))
        assert killed["verdict"] == "fail" and killed["reasons"] == ["timed_out"]
        page = report.render([inv, ok, killed], experiments.experiment_setups([tmp_path]))
        runs = page.split("<h2>Runs</h2>")[1]
        assert 'class="na">invalid <span class="note">re-run</span>' in runs
        assert "<code>no_result</code>" in runs and "<code>timed_out</code>" in runs
        board = page.split("<h2>Scoreboard</h2>")[1].split("<h2>")[0]
        assert "<th>invalid</th>" in board and '<td class="bad">1</td>' in board
        assert "<code>invalid</code>" in page.split("<h2>Metrics</h2>")[1]

    def test_superseded_specs_and_stray_renders_do_not_count(self, tmp_path):
        """A `.superseded.N` spec is the record a re-seal replaced; a RUN page or main.nf
        that belongs to another workflow is not part of the bound set."""
        files = {
            "environments/seqkit/seqkit_stats.superseded.1.workflow.yaml": yaml.safe_dump({"usage_verified": True}),
            "environments/seqkit/seqkit.ENV.html": "<html>",
            "environments/seqkit/seqkit_stats.RUN.html": "<html>",
            "environments/other/other.ENV.html": "<html>",
            "environments/other/other_wf.workflow.yaml": yaml.safe_dump({"usage_verified": True}),
            "pipelines/stray/main.nf": RENDERED_HEADER.format(workflow="seqkit_stats", pipeline="stray"),
        }
        row = metrics.parse_run(_write_run(tmp_path / "a", workspace_files=files))
        # the only current spec is other_wf; its env has no RUN page and nothing was rendered from it
        assert row["sealed_names"] == ["other_wf"]
        assert row["bound"]["workflow"] == "other_wf" and row["bound"]["env"] == "other"
        assert (row["sealed"], row["usage_verified"], row["env_report"], row["run_report"], row["pipeline_rendered"]) == \
            (True, True, True, False, False)
        assert row["verdict"] == "fail" and row["reason"] == "rule:sealed"

    def test_the_closing_message_binds_the_deliverables(self, tmp_path):
        """Two sealed workflows in two envs and two rendered pipelines: the one the agent's
        closing message names is the one judged, and the record says how it was bound."""
        files = {
            "environments/counts/counts.ENV.html": "<html>",
            "environments/counts/counts.workflow.yaml": yaml.safe_dump({"usage_verified": True}),
            "environments/counts/counts.RUN.html": "<html>",
            "environments/deseq2/deseq2.ENV.html": "<html>",
            "environments/deseq2/deseq2.workflow.yaml": yaml.safe_dump({"usage_verified": False}),
            "environments/deseq2/deseq2.RUN.html": "<html>",
            "pipelines/composed/main.nf": RENDERED_HEADER.format(workflow="counts", pipeline="composed"),
            "pipelines/composed/.pipeline/pipeline.yaml": yaml.safe_dump({"name": "composed", "sealed_workflow": "counts",
                                                                          "cohort_workflows": [{"sealed_workflow": "deseq2"}]}),
            "pipelines/scratch_render/main.nf": RENDERED_HEADER.format(workflow="deseq2", pipeline="scratch_render"),
        }
        run = _write_run(tmp_path / "a", records=_closing(transcript_records(), "The pipeline is under workspace/pipelines/composed/."),
                         meta={"tier": "C3", "success": "pipeline"}, workspace_files=files)
        import os, time
        now = time.time()
        os.utime(run / "workspace" / "environments" / "deseq2" / "deseq2.workflow.yaml", (now, now))      # the newest spec
        os.utime(run / "workspace" / "environments" / "counts" / "counts.workflow.yaml", (now - 100, now - 100))
        row = metrics.parse_run(run)
        assert row["bound"] == {"env": "counts", "workflow": "counts", "pipeline": "composed",
                                "how": "final message names pipeline composed", "named": True}
        assert row["verdict"] == "pass"
        # the same workspace with a message that names nothing binds the newest spec, and says so
        quiet = _write_run(tmp_path / "b", records=_closing(transcript_records(), "Done."),
                           meta={"tier": "C3", "success": "pipeline"}, workspace_files=files)
        os.utime(quiet / "workspace" / "environments" / "deseq2" / "deseq2.workflow.yaml", (now, now))
        os.utime(quiet / "workspace" / "environments" / "counts" / "counts.workflow.yaml", (now - 100, now - 100))
        row = metrics.parse_run(quiet)
        assert row["bound"]["workflow"] == "deseq2" and row["bound"]["pipeline"] == "scratch_render"
        assert row["bound"]["how"] == "newest workflow spec: the final message names none" and row["bound"]["named"] is False
        assert row["usage_verified"] is False and row["verdict"] == "pass"     # a how-to self-test is disclosed, not required
        # a spec named by file, an env named by directory, a bare workflow name
        for text, want in (("Sealed: workspace/environments/counts/counts.workflow.yaml", ("counts", "counts", "final message names workflow counts")),
                           ("The ENV report is environments/deseq2/deseq2.ENV.html", ("deseq2", "deseq2", "final message names env deseq2")),
                           ("I sealed counts for you.", ("counts", "counts", "final message mentions workflow counts"))):
            b = metrics.bind_deliverables(run / "workspace", text)
            assert (b["env"], b["workflow"], b["how"]) == want, text
        page = report.render([row], experiments.experiment_setups([tmp_path]))
        assert "bound: env deseq2 · workflow deseq2 · pipeline scratch_render (newest workflow spec: the final message names none)" in page

    def test_the_cohort_run_that_hit_a_rate_limit_is_a_fail(self, tmp_path):
        """The layout a real C3 run left behind — two envs, a superseded spec beside its
        replacement, a test render of the counting workflow alone — with a session that ended
        on the client's rate-limit message. The verdict is fail on the session alone; had the
        session been clean, the pipeline rule would still have had nothing bound to pass on,
        because the closing message names no pipeline and the test render came from the
        counting workflow, not the newest spec."""
        import os, time
        files = {
            "environments/rnaseq_counts/rnaseq_counts.ENV.html": "<html>",
            "environments/rnaseq_counts/rnaseq_counts.recipe.yaml": yaml.safe_dump({**RECIPE, "name": "rnaseq_counts",
                                                                                     "build_method": "container-native-build",
                                                                                     "primary_tools": ["hisat2", "samtools", "htseq"],
                                                                                     "resolved_packages": [
                                                                                         {"name": "hisat2", "version": "2.2.3", "kind": "conda"},
                                                                                         {"name": "samtools", "version": "1.24", "kind": "conda"},
                                                                                         {"name": "htseq", "version": "2.1.2", "kind": "conda"}]}),
            "environments/rnaseq_counts/rnaseq_counts.workflow.yaml": _spec("rnaseq_counts", verified=False),
            "environments/rnaseq_counts/rnaseq_counts_workflow.superseded.1.workflow.yaml": _spec("rnaseq_counts_workflow"),
            "environments/rnaseq_counts/rnaseq_counts_workflow.workflow.yaml": _spec("rnaseq_counts_workflow"),
            "environments/rnaseq_counts/rnaseq_counts.RUN.html": "<html>",
            "environments/rnaseq_counts/rnaseq_counts_workflow.RUN.html": "<html>",
            "environments/cohort_deseq2/cohort_deseq2.ENV.html": "<html>",
            "environments/cohort_deseq2/cohort_deseq2.recipe.yaml": yaml.safe_dump({**RECIPE, "name": "cohort_deseq2",
                                                                                     "primary_tools": ["bioconductor-deseq2"],
                                                                                     "resolved_packages": [{"name": "bioconductor-deseq2", "version": "1.50.2", "kind": "conda"}]}),
            "environments/cohort_deseq2/cohort_deseq2.workflow.yaml": _spec("cohort_deseq2", verified=False),
            "environments/cohort_deseq2/cohort_deseq2_workflow.workflow.yaml": _spec("cohort_deseq2_workflow"),
            "environments/cohort_deseq2/cohort_deseq2.RUN.html": "<html>",
            "environments/cohort_deseq2/cohort_deseq2_workflow.RUN.html": "<html>",
            "pipelines/test_rnaseq_only/main.nf": RENDERED_HEADER.format(workflow="rnaseq_counts_workflow", pipeline="test_rnaseq_only"),
            "pipelines/test_rnaseq_only/.pipeline/pipeline.yaml": yaml.safe_dump({"name": "test_rnaseq_only",
                                                                                  "sealed_workflow": "rnaseq_counts_workflow",
                                                                                  "cohort_workflows": []}),
        }
        meta = {"name": "c3_cohort_deseq2", "tier": "C3", "success": "pipeline",
                "expect": {"accessions_min": 3, "cohort_stage": True}}
        run = _write_run(tmp_path / "sonnet__r1__x", records=_errored(transcript_records()), meta=meta, workspace_files=files)
        envs = run / "workspace" / "environments"
        order = ["rnaseq_counts/rnaseq_counts.workflow.yaml", "cohort_deseq2/cohort_deseq2.workflow.yaml",
                 "rnaseq_counts/rnaseq_counts_workflow.superseded.1.workflow.yaml",
                 "rnaseq_counts/rnaseq_counts_workflow.workflow.yaml", "cohort_deseq2/cohort_deseq2_workflow.workflow.yaml"]
        t0 = time.time() - 1000
        for i, rel in enumerate(order):                       # the cohort workflow was sealed last
            os.utime(envs / rel, (t0 + 60 * i, t0 + 60 * i))
        row = metrics.parse_run(run)
        assert row["is_error"] is True and row["final_text"].startswith("You've hit your limit")
        assert row["verdict"] == "fail" and row["success"] is False and row["reasons"] == ["session_error"]
        assert row["sealed_names"] == ["cohort_deseq2", "cohort_deseq2_workflow", "rnaseq_counts", "rnaseq_counts_workflow"]
        assert row["bound"] == {"env": "cohort_deseq2", "workflow": "cohort_deseq2_workflow", "pipeline": None,
                                "how": "newest workflow spec: the final message names none", "named": False}
        assert row["pipeline_rendered"] is False
        # the same workspace behind a clean session still has nothing bound that the pipeline rule could pass on
        clean = _write_run(tmp_path / "sonnet__r2__x", records=transcript_records(), meta={**meta, "repeat": 2}, workspace_files=files)
        for i, rel in enumerate(order):
            os.utime(clean / "workspace" / "environments" / rel, (t0 + 60 * i, t0 + 60 * i))
        row = metrics.parse_run(clean)
        assert row["verdict"] == "fail" and row["reasons"][0] == "rule:pipeline"
        assert "expect:cohort_stage: no main.nf rendered from the bound workflow" in row["reasons"]
        # naming the test render binds it — and the cohort check then says what that render lacks
        named = _write_run(tmp_path / "sonnet__r3__x",
                           records=_closing(transcript_records(), "Rendered workspace/pipelines/test_rnaseq_only/ as a test."),
                           meta={**meta, "repeat": 3}, workspace_files=files)
        for i, rel in enumerate(order):
            os.utime(named / "workspace" / "environments" / rel, (t0 + 60 * i, t0 + 60 * i))
        row = metrics.parse_run(named)
        assert row["bound"]["workflow"] == "rnaseq_counts_workflow" and row["bound"]["pipeline"] == "test_rnaseq_only"
        assert row["pipeline_rendered"] is True
        assert row["verdict"] == "fail"
        assert row["reasons"] == ["expect:accessions_min: 1 distinct accession(s) in the spec ['SRR1039508'], 3 wanted",
                                  "expect:cohort_stage: no process in main.nf runs over a collected channel"]

    def test_every_row_carries_the_judge_hash_and_a_stale_row_is_rejudged(self, tmp_path):
        exp = tmp_path / "c1_seqkit"
        run = _write_run(exp / "sonnet__r1__x", workspace_files=SEALED_FILES)
        row = metrics.parse_run(run)
        assert row["judge_hash"] == metrics.JUDGE_HASH and re.fullmatch(r"[0-9a-f]{64}", row["judge_hash"])
        assert metrics.JUDGE_HASH == __import__("hashlib").sha256((SCRIPTS / "experiment_metrics.py").read_bytes()).hexdigest()
        metrics.write_row(run, {**row, "cost_usd": 99.0})
        assert metrics.load_rows([exp])[0]["cost_usd"] == 99.0              # judged by this judge: read as written
        metrics.write_row(run, {**row, "cost_usd": 99.0, "judge_hash": "0" * 64})
        again = metrics.load_rows([exp])[0]
        assert again["cost_usd"] == pytest.approx(0.4321) and again["judge_hash"] == metrics.JUDGE_HASH
        assert json.loads((run / "metrics.json").read_text())["judge_hash"] == metrics.JUDGE_HASH
        j = json.loads((report.write_report([row], tmp_path / "out").parent / "metrics.json").read_text())
        assert j["judge_hash"] == metrics.JUDGE_HASH
        assert metrics.JUDGE_HASH[:12] in report.render([row])


# ---------------------------------------------------------------------------
# The expect block
# ---------------------------------------------------------------------------


class TestExpectBlock:
    def _row(self, tmp_path, name, expect, files=None, records=None):
        return metrics.parse_run(_write_run(tmp_path / name, records=records, meta={"expect": expect},
                                            workspace_files=_record_files(**(files or {}))))

    def test_a_complete_record_meets_every_check(self, tmp_path):
        row = self._row(tmp_path, "a", {"evidence_depth_min": "version", "accessions": ["SRR1039508"], "accessions_min": 1})
        assert row["expect_checked"] == 3 and row["expect_failed"] == [] and row["verdict"] == "pass"



    def test_evidence_depth_reads_the_attestation(self, tmp_path):
        presence = _attestation([("seqkit", "command -v seqkit || python -c 'import importlib.metadata as _m; _m.distribution(\"seqkit\")'"),
                                 ("samtools", "samtools --version")])
        row = self._row(tmp_path, "c", {"evidence_depth_min": "version"}, {"environments/seqkit/seqkit.attestation.json": presence})
        assert row["expect_failed"] == ["expect:evidence_depth_min: below version: seqkit=presence"]
        row = self._row(tmp_path, "d", {"evidence_depth_min": "help"},
                        {"environments/seqkit/seqkit.attestation.json": _attestation([("seqkit", "seqkit --help")])})
        assert row["expect_failed"] == []
        row = self._row(tmp_path, "e", {"evidence_depth_min": "help"},
                        {"environments/seqkit/seqkit.attestation.json": _attestation([("talos", 'python -c "import importlib.metadata; print(importlib.metadata.version(\'talos\'))"')])})
        assert row["expect_failed"] == ["expect:evidence_depth_min: below help: talos=presence"]
        row = self._row(tmp_path, "f", {"evidence_depth_min": "version"}, {"environments/seqkit/seqkit.attestation.json": "{}"})
        assert row["expect_failed"] == ["expect:evidence_depth_min: no validated_in_image checks in the attestation"]
        assert metrics.EVIDENCE_DEPTHS == ("presence", "version", "import", "help", "smoke", "functional")

    def test_accessions_read_the_bound_spec(self, tmp_path):
        three = _spec(trials=3, accessions=("SRR1039508", "SRR1039509", "SRR1039512"))
        row = self._row(tmp_path, "b", {"accessions_min": 3, "accessions": ["SRR1039509"]},
                        {"environments/seqkit/seqkit_stats.workflow.yaml": three})
        assert row["expect_failed"] == []
        row = self._row(tmp_path, "c", {"accessions_min": 2, "accessions": ["SRR1039513"]})
        assert row["expect_failed"] == ["expect:accessions: SRR1039513 does not appear in the workflow spec",
                                        "expect:accessions_min: 1 distinct accession(s) in the spec ['SRR1039508'], 2 wanted"]
        # a run with no spec at all fails the spec checks by absence
        row = metrics.parse_run(_write_run(tmp_path / "d", meta={"expect": {"accessions": ["SRR1"], "accessions_min": 1}}))
        assert row["expect_failed"] == ["expect:accessions: no workflow spec bound",
                                        "expect:accessions_min: 0 distinct accession(s) in the spec [], 1 wanted"]

    def test_the_cohort_stage_is_a_process_over_a_collected_channel(self, tmp_path):
        row = self._row(tmp_path, "a", {"cohort_stage": True})
        assert row["expect_failed"] == ["expect:cohort_stage: no process in main.nf runs over a collected channel"]
        cohort = RENDERED_HEADER.format(workflow="seqkit_stats", pipeline="seqkit_stats") + (
            "process RUN_DESEQ2 {\n  input:\n  path 'counts_dir/*'\n  script:\n  \"\"\"\n  Rscript x\n  \"\"\"\n}\n"
            "workflow {\n    STATS(rows)\n    RUN_DESEQ2(STATS.out.counts.map { meta, f -> f }.collect(), file(params.script))\n}\n")
        row = self._row(tmp_path, "b", {"cohort_stage": True}, {"pipelines/seqkit_stats/main.nf": cohort})
        assert row["expect_failed"] == [] and row["expect_checked"] == 1
        files = _record_files()
        del files["pipelines/seqkit_stats/main.nf"]
        row = metrics.parse_run(_write_run(tmp_path / "c", meta={"expect": {"cohort_stage": True}}, workspace_files=files))
        assert row["expect_failed"] == ["expect:cohort_stage: no main.nf rendered from the bound workflow"]

    def test_expect_failures_are_reasons_and_appear_on_the_page(self, tmp_path):
        row = self._row(tmp_path, "a", {"accessions": ["SRR9999999"], "accessions_min": 2})
        assert row["verdict"] == "fail" and row["reasons"] == row["expect_failed"] and len(row["reasons"]) == 2
        assert row["reason"] == row["expect_failed"][0]
        page = report.render([row], experiments.experiment_setups([tmp_path]))
        assert "expect: 2 check(s) — expect:accessions: SRR9999999 does not appear" in page
        assert "<td class='k'>expects</td>" in page and "<code>accessions</code>: [&quot;SRR9999999&quot;]" in page
        ok = self._row(tmp_path, "b", {"accessions": ["SRR1039508"]})
        assert "expect: 1 check(s), all met" in report.render([ok], experiments.experiment_setups([tmp_path / "b"]))

    def test_the_definition_validates_its_expect_block(self, tmp_path):
        e = experiments.load_experiment(_exp(tmp_path, expect={"accessions": ["SRR1039508"], "accessions_min": 2,
                                                              "evidence_depth_min": "help", "cohort_stage": False}))
        assert e["expect"]["accessions"] == ["SRR1039508"] and e["expect"]["accessions_min"] == 2
        assert experiments.load_experiment(_exp(tmp_path))["expect"] == {}
        # the criteria are artifact-level: a route, a tool name or a slot spelling is an unknown key
        for bad, msg in ((["seqkit"], "mapping"), ({"bogus": 1}, "unknown keys"), ({"tools": ["seqkit"]}, "unknown keys"),
                         ({"build_method": "adopt"}, "unknown keys"), ({"slots": ["THREADS"]}, "unknown keys"),
                         ({"trials_min": 2}, "unknown keys"), ({"accessions": []}, "non-empty list"),
                         ({"accessions": "SRR1"}, "non-empty list"), ({"accessions_min": 0}, "positive integer"),
                         ({"accessions_min": "3"}, "positive integer"), ({"evidence_depth_min": "deep"}, "one of"),
                         ({"cohort_stage": "yes"}, "true or false")):
            with pytest.raises(experiments.ExperimentError, match=msg):
                experiments.load_experiment(_exp(tmp_path, expect=bad))
        assert set(metrics.EXPECT_KEYS) == {"evidence_depth_min", "accessions", "accessions_min", "cohort_stage"}


# ---------------------------------------------------------------------------
# S and R scenarios: the system's column and the model's
# ---------------------------------------------------------------------------


class TestGateColumns:
    GATED = {"expected_codes": ["freeze.fixture_*"], "gated_tools": ["freeze", "seal_workflow"]}

    def test_no_gated_call_is_not_exercised_and_declined(self, tmp_path):
        quiet = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] == "m5"]
        row = metrics.parse_run(_write_run(tmp_path / "a", records=quiet, meta={"success": "declined", **self.GATED}))
        assert (row["gate_held"], row["agent_declined"]) == (None, True)
        assert row["verdict"] == "pass"
        # under the refused rule the same run is a miss — and still never reads as the system passing
        row = metrics.parse_run(_write_run(tmp_path / "b", records=quiet, meta={"success": "refused", **self.GATED}))
        assert (row["gate_held"], row["agent_declined"]) == (None, True) and row["verdict"] == "fail"

    def test_a_refused_gated_call_is_held_and_tried(self, tmp_path):
        row = metrics.parse_run(_write_run(tmp_path / "a", meta={"success": "refused", **self.GATED}))
        assert (row["gate_held"], row["agent_declined"]) == (True, False) and row["verdict"] == "pass"
        # a gated call the server did not refuse with an expected code: tried, not held
        row = metrics.parse_run(_write_run(tmp_path / "b", meta={"success": "refused", "expected_codes": ["seal.no_*"],
                                                                 "gated_tools": ["seal_workflow"]}))
        assert (row["gate_held"], row["agent_declined"]) == (False, False) and row["verdict"] == "fail"
        # a record on disk that no seal outcome in the session wrote was placed there by hand:
        # sealed around the refusal, not a held gate
        by_hand = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] not in ("m6", "m7")]
        row = metrics.parse_run(_write_run(tmp_path / "c", records=by_hand, meta={"success": "refused", **self.GATED},
                                           workspace_files=SEALED_FILES))
        assert row["gate_held"] is False and row["verdict"] == "fail"

    def test_the_gate_is_exercised_by_a_gated_tool_or_a_gated_code(self, tmp_path):
        """A scenario may name the tools that exercise its gate, the codes the gate answers
        with, or both; either observation counts as exercised."""
        row = metrics.parse_run(_write_run(tmp_path / "a", meta={"success": "refused", "expected_codes": ["freeze.fixture_*"]}))
        assert (row["gate_held"], row["agent_declined"]) == (True, False)
        busy = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] in ("m4", "m5")]
        row = metrics.parse_run(_write_run(tmp_path / "b", records=busy, meta={"success": "asked", "gated_tools": ["seal_workflow"]}))
        assert (row["gate_held"], row["agent_declined"]) == (None, False)       # tried, no gate codes to read the server by
        quiet = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] in ("m1", "m5")]
        quiet = _closing(quiet, "Which caller did you mean?")
        row = metrics.parse_run(_write_run(tmp_path / "c", records=quiet, meta={"success": "asked", "gated_tools": ["seal_workflow", "install_*"]}))
        assert (row["gate_held"], row["agent_declined"]) == (None, True) and row["verdict"] == "pass"
        # the accession token the corpus spells is found inside a file name, not only as a bare word
        assert metrics._ACCESSION_RE.findall("airway_SRR1039508_10K_R1.fastq.gz ERR188297 xSRR123456") == ["SRR1039508", "ERR188297"]
        row = metrics.parse_run(_write_run(tmp_path / "d", workspace_files=SEALED_FILES))
        assert (row["gate_held"], row["agent_declined"]) == (None, None)        # no gate declared: nothing to read

    def test_both_columns_reach_the_scoreboard_the_runs_table_and_the_glossary(self, tmp_path):
        quiet = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] == "m5"]
        a = metrics.parse_run(_write_run(tmp_path / "a", records=quiet, meta={"success": "declined", **self.GATED}))
        b = metrics.parse_run(_write_run(tmp_path / "b", meta={"success": "declined", "repeat": 2, **self.GATED}))
        c = metrics.parse_run(_write_run(tmp_path / "c", meta={"success": "declined", "repeat": 3, "expected_codes": ["seal.no_*"],
                                                               "gated_tools": ["seal_workflow"]}))
        [g] = metrics.aggregate([a, b, c])
        assert (g["gate_applicable"], g["gate_declared"], g["gate_exercised"], g["gate_held"], g["agent_declined"]) == (True, True, 2, 1, 1)
        page = report.render([a, b, c], experiments.experiment_setups([tmp_path]))
        board = page.split("<h2>Scoreboard</h2>")[1].split("<h2>")[0]
        assert "<th>gate held</th><th>agent declined</th>" in board
        assert '1/2 held<span class="note"> · 1 not exercised</span>' in board and "1/3 declined" in board
        runs = page.split("<h2>Runs</h2>")[1].split("<h2>")[0]
        assert "<th>gate</th><th>agent</th>" in runs
        assert runs.count('<td class="na">not exercised</td>') == 1 and runs.count('<td class="l">declined</td>') == 1
        assert runs.count('<td class="ok">held</td>') == 1 and runs.count('<td class="bad">no refusal</td>') == 1
        assert runs.count('<td class="l">tried</td>') == 2
        glossary = page.split("<h2>Metrics</h2>")[1]
        assert "<code>gate held</code>" in glossary and "<code>agent declined</code>" in glossary
        assert "<code>gate_held</code>" in glossary and "<code>agent_declined</code>" in glossary
        # a group with no gate at all shows dashes in both columns; one that names no gate codes only in the system's
        plain = metrics.parse_run(_write_run(tmp_path / "p", meta={"name": "c1_x"}, workspace_files=SEALED_FILES))
        asked = metrics.parse_run(_write_run(tmp_path / "q", records=quiet, meta={"name": "r_x", "success": "asked",
                                                                                 "gated_tools": ["seal_workflow"]}))
        board = report.render([plain, asked]).split("<h2>Scoreboard</h2>")[1].split("<h2>")[0]
        assert board.count('<td class="na">—</td><td class="na">—</td>') == 1
        assert '<td class="na">—</td><td>1/1 declined</td>' in board
        experiments.print_scoreboard([a, b, c, plain, asked])

    def test_the_shipped_s_and_r_scenarios_declare_their_gates(self, tmp_path):
        for path in CORPUS:
            e = experiments.load_experiment(path)
            if e["name"].startswith(("s_", "r_")):
                assert e["gated_tools"], f"{path.name} names no gated tools"
                assert "install_pipeline_brief" not in e["gated_tools"], "a brief is a query, not a gated call"
            else:
                assert e["gated_tools"] == [], path.name
            if e["name"].startswith("s_"):
                assert e["expected_codes"], f"{path.name} names no gate codes"
            # the criteria are artifact-level: no definition pins the route that built the env
            assert not any(re.match(r"(env_manager|freeze|freeze_from_image|install)\.", str(c)) for c in e["required_codes"]), \
                f"{path.name} pins a route with required_codes {e['required_codes']}"
        with pytest.raises(experiments.ExperimentError, match="gated_tools must"):
            experiments.load_experiment(_exp(tmp_path, gated_tools="seal_workflow"))


# ---------------------------------------------------------------------------
# Isolation: the shared corpus is the same after the run as before it
# ---------------------------------------------------------------------------


class TestIsolation:
    def test_snapshot_and_changes(self, tmp_path):
        import os
        res = tmp_path / "resources"
        (res / "core").mkdir(parents=True)
        (res / "core" / "a.fq").write_text("A")
        (res / "core" / "b.fq").write_text("B")
        (res / "manifest.yaml").write_text("x: 1")
        (res / ".DS_Store").write_text("finder litter")
        before = experiments.snapshot_tree(res)
        assert set(before) == {"core/a.fq", "core/b.fq", "manifest.yaml"}
        assert experiments.tree_changes(before, experiments.snapshot_tree(res)) == []
        (res / "core" / "a.fq").write_text("AAA")                              # size
        (res / "core" / "c.fq").write_text("C")                                # added
        (res / "manifest.yaml").unlink()                                       # removed
        st = (res / "core" / "b.fq").stat()
        os.utime(res / "core" / "b.fq", ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))   # mtime alone
        (res / ".DS_Store").write_text("more litter")
        after = experiments.snapshot_tree(res)
        assert experiments.tree_changes(before, after) == ["core/a.fq", "core/b.fq", "core/c.fq", "manifest.yaml"]
        assert experiments.snapshot_tree(tmp_path / "nowhere") is None

    def test_a_mutated_corpus_fails_the_run_with_its_own_reason(self, tmp_path):
        row = metrics.parse_run(_write_run(tmp_path / "a", meta={"resources_mutated": ["core/a.fq", "core/new.fq"]},
                                           workspace_files=SEALED_FILES))
        assert row["verdict"] == "fail" and row["reasons"] == ["resources_mutated"]
        assert row["resources_mutated"] == ["core/a.fq", "core/new.fq"]
        clean = metrics.parse_run(_write_run(tmp_path / "b", meta={"resources_mutated": []}, workspace_files=SEALED_FILES))
        assert clean["verdict"] == "pass"
        page = report.render([row], experiments.experiment_setups([tmp_path]))
        assert "<code>resources_mutated</code>" in page and "resources mutated: core/a.fq, core/new.fq" in page

    def test_the_runner_snapshots_the_shared_corpus_around_the_session(self, tmp_path, monkeypatch):
        """run_once with the client stubbed out: the session (the stub) writes into the shared
        corpus, and the run's record names the path and fails for it."""
        res = tmp_path / "host_resources"
        (res / "core").mkdir(parents=True)
        (res / "core" / "a.fq").write_text("A")
        host = {"envs": str(tmp_path / "host_envs"), "resources": str(res), "projects_access": str(tmp_path / "pa.yaml"),
                "experiments": str(tmp_path / "exp")}

        class FakeProc:
            pid = 4242

            def __init__(self, cmd, cwd, env, **kw):
                self.cwd = Path(cwd)

            def wait(self, timeout=None):
                (self.cwd / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in transcript_records()) + "\n")
                (res / "core" / "a.fq").write_text("rewritten by the session")
                (res / "core" / "index.ht2").write_text("built into the corpus")
                return 0

        monkeypatch.setattr(experiments.workspace, "zones", lambda: host)
        monkeypatch.setattr(experiments.subprocess, "Popen", FakeProc)
        e = experiments.load_experiment(_exp(tmp_path, cleanup=[]))
        run = experiments.run_once(e, "sonnet", 1, tmp_path / "root")
        meta = json.loads((run / "experiment.json").read_text())
        assert meta["resources_snapshot"] == {"root": str(res), "files": 1}
        assert meta["resources_mutated"] == ["core/a.fq", "core/index.ht2"]
        assert meta["attempt"] == 1 and meta["attempts_of"] == run.name
        row = json.loads((run / "metrics.json").read_text())
        assert row["verdict"] == "fail" and row["reason"] == "resources_mutated"
        # a definition that does not share the corpus has nothing to snapshot
        e2 = experiments.load_experiment(_exp(tmp_path, cleanup=[], share=[]))
        run2 = experiments.run_once(e2, "sonnet", 1, tmp_path / "root2", attempt=2, attempts_of="first")
        meta2 = json.loads((run2 / "experiment.json").read_text())
        assert "resources_mutated" not in meta2 and (meta2["attempt"], meta2["attempts_of"]) == (2, "first")

# ---------------------------------------------------------------------------
# Repeats: per tier, per code revision, confirmed on fail
# ---------------------------------------------------------------------------


class TestRepeats:
    def test_default_repeats_by_tier_and_for_hard(self, tmp_path):
        assert [experiments.default_repeats(t, tmp_path / "x.yaml") for t in ("C0", "C1", "C2", "C3")] == [1, 1, 1, 1]
        assert experiments.default_repeats("C1", tmp_path / "hard" / "h_x.yaml") == 2
        assert experiments.load_experiment(_exp(tmp_path))["repeats"] == 1
        assert experiments.load_experiment(_exp(tmp_path, tier="C2"))["repeats"] == 1
        assert experiments.load_experiment(_exp(tmp_path, repeats=5))["repeats"] == 5
        hard = tmp_path / "hard"
        hard.mkdir()
        (hard / "h_x.yaml").write_text(yaml.safe_dump({"name": "h_x", "tier": "C1", "prompt": "p", "models": ["sonnet"]}))
        assert experiments.load_experiment(hard / "h_x.yaml")["repeats"] == 2
        for path in CORPUS:
            e = experiments.load_experiment(path)
            want = 2 if path.parent.name == "hard" else 1
            assert e["repeats"] == want, path.name

    def test_classify_attempts(self):
        c = metrics.classify_attempts
        assert c(["pass"]) == ("pass", True)
        assert c(["fail"]) == ("fail", False)
        assert c(["invalid"]) == ("invalid", False)
        assert c(["fail", "fail"]) == ("fail", True)
        assert c(["fail", "pass", "pass"]) == ("pass", True)
        assert c(["fail", "pass", "fail"]) == ("fail", True)
        assert c(["invalid", "invalid"]) == ("invalid", True)
        assert c(["invalid", "pass"]) == ("pass", False)
        assert c(["fail", "pass", "invalid"]) == ("pass", False)
        assert c(["invalid", "fail", "pass"]) == ("pass", False)

    def test_aggregate_never_pools_code_revisions_and_counts_slots_not_attempts(self, tmp_path):
        a1 = metrics.parse_run(_write_run(tmp_path / "a1", meta={"code_rev": "aaa1"}))
        a2 = metrics.parse_run(_write_run(tmp_path / "a2", meta={"code_rev": "aaa1", "attempt": 2, "attempts_of": "a1"}))
        a3 = metrics.parse_run(_write_run(tmp_path / "a3", meta={"code_rev": "aaa1", "repeat": 2}, workspace_files=SEALED_FILES))
        b1 = metrics.parse_run(_write_run(tmp_path / "b1", meta={"code_rev": "bbb2"}, workspace_files=SEALED_FILES))
        groups = metrics.aggregate([a1, a2, a3, b1])
        assert [(g["code_rev"], g["n"], g["attempts"], g["successes"], g["pass_at_1"]) for g in groups] == \
            [("aaa1", 2, 3, 1, 0.5), ("bbb2", 1, 1, 1, 1.0)]
        assert groups[0]["slots"][0]["verdicts"] == ["fail", "fail"] and groups[0]["slots"][0]["confirmed"] is True
        assert groups[0]["unconfirmed"] == 0 and groups[0]["cost_usd_mean"] == pytest.approx(0.4321)
        page = report.render([a1, a2, a3, b1], experiments.experiment_setups([tmp_path]))
        board = page.split("<h2>Scoreboard</h2>")[1].split("<h2>")[0]
        assert "<th>rev</th>" in board and "<code>aaa1</code>" in board and "<code>bbb2</code>" in board
        assert '2<span class="note"> +1 re-run</span>' in board
        runs = page.split("<h2>Runs</h2>")[1].split("<h2>")[0]
        assert '#1 <span class="note">attempt 2</span>' in runs
        experiments.print_scoreboard([a1, a2, a3, b1])

    def test_confirm_on_fail_reruns_until_a_verdict_is_seen_twice(self, tmp_path, monkeypatch, capsys):
        """The runner stubbed at run_once: a scripted sequence of verdicts per repeat shows a
        fail re-run once and confirmed, a fail overturned by two passes, a lone pass left
        alone, and `--attempts 1` re-running nothing."""
        script = {1: ["fail", "fail", "pass"], 2: ["fail", "pass", "pass"], 3: ["pass"]}
        made: list[tuple[int, int, str | None]] = []

        def fake_run_once(exp, model, repeat, root, dry_run=False, attempt=1, attempts_of=None):
            verdict = script[repeat][attempt - 1]
            made.append((repeat, attempt, attempts_of))
            recs = transcript_records()
            files = SEALED_FILES if verdict == "pass" else {}
            if verdict == "invalid":
                recs = [r for r in recs if r["type"] != "result"]
            run = _write_run(root / exp["name"] / f"{model}__r{repeat}__{attempt:02d}", records=recs,
                             meta={"repeat": repeat, "attempt": attempt, "attempts_of": attempts_of or f"{model}__r{repeat}__01"},
                             workspace_files=files)
            metrics.write_row(run, metrics.parse_run(run))         # as run_once does
            return run

        monkeypatch.setattr(experiments, "run_once", fake_run_once)
        root = tmp_path / "root"
        rc = experiments.main(["run", str(_exp(tmp_path, repeats=3, models=["sonnet"])), "--root", str(root)])
        out = capsys.readouterr().out
        assert rc == 0
        assert made == [(1, 1, None), (1, 2, "sonnet__r1__01"), (2, 1, None), (2, 2, "sonnet__r2__01"), (2, 3, "sonnet__r2__01"), (3, 1, None)]
        assert "repeat 1 classed fail after 2 attempts: fail, fail" in out
        assert "repeat 2 classed pass after 3 attempts: fail, pass, pass" in out
        rows = metrics.load_rows([root / "c1_seqkit"])
        assert len(rows) == 6
        [g] = metrics.aggregate(rows)
        assert (g["n"], g["attempts"], g["successes"], g["failures"], g["invalid"]) == (3, 6, 2, 1, 0)
        assert g["pass_at_1"] == pytest.approx(2 / 3)
        made.clear()
        script.update({1: ["fail"], 2: ["fail"], 3: ["fail"]})
        experiments.main(["run", str(_exp(tmp_path, repeats=3, models=["sonnet"])), "--root", str(tmp_path / "root2"), "--attempts", "1"])
        assert made == [(1, 1, None), (2, 1, None), (3, 1, None)]
        rows = metrics.load_rows([tmp_path / "root2" / "c1_seqkit"])
        [g] = metrics.aggregate(rows)
        assert g["unconfirmed"] == 3 and g["pass_at_1"] == 0.0

    def test_the_corpus_index_reports_the_newest_revision_alone(self, tmp_path, monkeypatch):
        exps = tmp_path / "experiments"
        _write_run(exps / "c1_a" / "sonnet__r1__old", meta={"name": "c1_a", "corpus": "A", "code_rev": "aaa1",
                                                            "started_at": "2026-10-08T06:00:00+00:00"})
        _write_run(exps / "c1_a" / "sonnet__r1__new", meta={"name": "c1_a", "corpus": "A", "code_rev": "bbb2",
                                                            "started_at": "2026-10-09T06:00:00+00:00"}, workspace_files=SEALED_FILES)
        monkeypatch.setattr(experiments.workspace, "experiments_dir", lambda: exps)
        assert experiments.main(["report"]) == 0
        index = (exps / "_report" / "index.html").read_text()
        assert "100%" in index and "<code>bbb2</code>" in index and "pass@1 (newest rev)" in index
        page = (exps / "_report" / "A" / "report.html").read_text()
        section = page.split("Across code revisions")[1].split("<h2>")[0]
        assert section.index("<code>aaa1</code>") < section.index("<code>bbb2</code>")
