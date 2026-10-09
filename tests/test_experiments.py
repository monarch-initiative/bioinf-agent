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

    def test_an_empty_transcript_is_an_unjudged_row_not_a_crash(self, tmp_path):
        row = metrics.parse_run(_write_run(tmp_path / "r", records=[]))
        assert row["success"] is None
        assert row["terminal_reason"] == "none"
        assert row["tool_calls"] == 0 and row["cost_usd"] == 0.0

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

    def test_a_sealed_record_whose_self_test_failed_does_not_pass(self, tmp_path):
        files = dict(SEALED_FILES)
        files["environments/seqkit/seqkit_stats.workflow.yaml"] = yaml.safe_dump({"usage_verified": False})
        row = metrics.parse_run(_write_run(tmp_path / "r", workspace_files=files))
        assert row["sealed"] is True and row["usage_verified"] is False
        assert row["success"] is False

    def test_nothing_written_means_no_success_whatever_the_model_said(self, tmp_path):
        row = metrics.parse_run(_write_run(tmp_path / "r"))
        assert row["success"] is False

    def test_the_pipeline_rule_needs_main_nf(self, tmp_path):
        files = dict(SEALED_FILES)
        row = metrics.parse_run(_write_run(tmp_path / "a", meta={"tier": "C3", "success": "pipeline"},
                                           workspace_files=files))
        assert row["success"] is False
        files["pipelines/seqkit_stats/main.nf"] = "workflow {}"
        row = metrics.parse_run(_write_run(tmp_path / "b", meta={"tier": "C3", "success": "pipeline"},
                                           workspace_files=files))
        assert row["pipeline_rendered"] is True and row["success"] is True

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
        # a seal that went through anyway is fine only if its self-test passed
        files = dict(SEALED_FILES)
        files["environments/seqkit/seqkit_stats.workflow.yaml"] = yaml.safe_dump({"usage_verified": False})
        row = metrics.parse_run(_write_run(tmp_path / "e", meta=meta, workspace_files=files))
        assert row["sealed"] is True and row["success"] is False
        row = metrics.parse_run(_write_run(tmp_path / "f", meta=meta, workspace_files=SEALED_FILES))
        assert row["success"] is True

    def test_the_refused_rule_accepts_a_decline_that_never_called_the_server(self, tmp_path):
        meta = {"success": "refused", "expected_codes": ["seal.*"]}
        quiet = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] == "m5"]
        row = metrics.parse_run(_write_run(tmp_path / "a", records=quiet, meta=meta))
        assert row["mcp_calls"] == 0 and row["success"] is True
        # a decline with a hand-written record left behind forged the very thing it declined
        files = {"environments/seqkit/seqkit_stats.workflow.yaml": yaml.safe_dump({"usage_verified": False})}
        row = metrics.parse_run(_write_run(tmp_path / "b", records=quiet, meta=meta, workspace_files=files))
        assert row["sealed"] is True and row["success"] is False
        # a server call that was not a refusal is neither a decline nor a held gate
        busy = [r for r in transcript_records() if r["type"] != "assistant" or r["message"]["id"] in ("m4", "m5")]
        row = metrics.parse_run(_write_run(tmp_path / "c", records=busy, meta=meta))
        assert row["mcp_calls"] > 0 and row["mcp_refused"] == 0 and row["success"] is False

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
        assert len(csv) == 3 and csv[0].startswith("experiment,tier,model")
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
        assert experiments.docker_snapshot() == {"image": None, "container": None}
        result = experiments.docker_cleanup({"image": None, "container": None})
        assert result["skipped"] == "no Docker daemon answered"
        assert result["images_removed"] == [] and result["containers_removed"] == []

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

@pytest.mark.parametrize("path", sorted((ROOT / "experiments").glob("*.yaml")), ids=lambda p: p.name)
def test_every_shipped_experiment_loads_and_names_its_file(path):
    e = experiments.load_experiment(path)
    assert path.stem == e["name"], "an experiment file is named after its experiment"
    assert e["tier"] in experiments.TIERS
    assert e["models"] == ["sonnet"], "the corpus varies the scenario and the code, not the model"
    assert e["cleanup"] == ["images"], "every corpus run leaves the Docker daemon as it found it"


def test_the_corpus_covers_every_judging_rule_and_the_prepared_compute_world():
    defs = [experiments.load_experiment(p) for p in sorted((ROOT / "experiments").glob("*.yaml"))]
    assert {d["success"] for d in defs} >= set(metrics.SUCCESS_RULES)
    assert any(d["projects_access"] for d in defs), "at least one scenario declares a compute env inside the run"
    assert any(d["expected_codes"] for d in defs)


def test_the_experiments_zone_is_an_artifact_zone():
    from agent.skills import workspace
    z = workspace.zones()
    assert z["experiments"] == str(Path(z["workspace_root"]) / "experiments")
    assert workspace.experiments_dir() == Path(z["experiments"])


class TestFigureLabels:
    def _groups(self, n, model="sonnet"):
        return [{"experiment": f"scenario_{i:02d}", "model": model, "n": 1, "pass_at_1": 1.0 if i % 2 else 0.0,
                 "cost_usd_mean": 0.1, "cost_usd_sd": 0.0} for i in range(n)]

    def test_bars_label_by_what_differs_and_turn_diagonal_when_crowded(self):
        few = report._bars(self._groups(2), "cost_usd", "usd", "cost per run")
        assert "scenario_00" in few and "sonnet</text>" not in few, "one model: the experiment is the label"
        assert "rotate(" not in few
        many = report._bars(self._groups(12), "cost_usd", "usd", "cost per run")
        assert many.count("rotate(-40") == 12, "twelve bars cannot carry upright labels"
        by_model = report._bars([dict(g, experiment="one", model=m) for g, m in zip(self._groups(2), ("sonnet", "opus"))],
                                "cost_usd", "usd", "cost per run")
        assert "sonnet" in by_model and "opus" in by_model and "one</text>" not in by_model

    def test_scatter_labels_never_overlap_even_when_points_coincide(self):
        svg = report._scatter(self._groups(12))
        labels = re.findall(r'<text class="lbl" x="([\d.]+)" y="([\d.]+)"[^>]*>([^<]*)</text>', svg)
        assert len(labels) == 12 and {t for _, _, t in labels} == {f"scenario_{i:02d}" for i in range(12)}
        ys = sorted(float(y) for _, y, _ in labels)
        assert all(b - a >= 12 for a, b in zip(ys, ys[1:]) if b - a < 100), "stacked labels keep a line of clearance"
        assert all(0 < float(y) < 260 for _, y, _ in labels), "labels stay inside the panel"
