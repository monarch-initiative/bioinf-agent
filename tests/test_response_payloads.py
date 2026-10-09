"""What a tool hands back inline is for the NEXT call; the record stays whole on disk.

A corpus pass put 633 KB of tool results into the agent's context, most of it bulk
the agent never reads: evidence banners and checked coverage clauses in a freeze
result, a step's full stderr, nanopore fields that are null on every short-read
dataset. These pin the cuts and, more importantly, that the record is untouched.
"""
from __future__ import annotations

from pathlib import Path

import agent.mcp_server as ms
from agent.mcp_tools import freeze_tools, run_tools
from agent.skills import resources


def test_a_freeze_response_cuts_banners_and_checked_clauses_but_not_the_record():
    banner = "\n".join(f"line {i}" for i in range(40))
    record = {"verifications": [{"tool": "t", "check": "t --version", "rc": 0, "passed": True,
                                 "banner": banner, "out": "short"}],
              "contract_coverage": {"summary": "s", "fully_observed": True,
                                    "clauses": [{"clause": "BUILT", "status": "checked"},
                                                {"clause": "WELL_FORMED.shipped_binaries",
                                                 "status": "not_applicable"}]}}
    out = freeze_tools._compact_freeze_response({**record, "verifications": list(record["verifications"]),
                                                 "contract_coverage": dict(record["contract_coverage"])})
    v = out["verifications"][0]
    assert v["banner"].startswith("line 0\nline 1\nline 2") and "chars in the record" in v["banner"]
    assert v["out"] == "short" and v["check"] == "t --version"
    assert [c["clause"] for c in out["contract_coverage"]["clauses"]] == ["WELL_FORMED.shipped_binaries"]
    assert out["contract_coverage"]["summary"] == "s"
    # the record the response was built from is whole
    assert record["verifications"][0]["banner"] == banner
    assert len(record["contract_coverage"]["clauses"]) == 2


def test_a_short_banner_is_left_alone():
    v = freeze_tools._compact_verification({"banner": "Version: 1.4-r122", "out": ""})
    assert v["banner"] == "Version: 1.4-r122"


def test_a_missing_watch_dir_is_created_for_the_step(tmp_path):
    target = tmp_path / "out" / "deeper"
    assert run_tools._ensure_watch_dir(str(target)) is True
    assert target.is_dir()
    assert run_tools._ensure_watch_dir(str(target)) is False
    assert run_tools._ensure_watch_dir("") is False


def test_long_step_output_goes_to_the_step_log(tmp_path, monkeypatch):
    from agent.skills import workspace
    monkeypatch.setattr(workspace, "scratch_dir", lambda *parts: Path(tmp_path, *parts).mkdir(parents=True, exist_ok=True) or Path(tmp_path, *parts))
    out = ms._shrink_stdio_for_response({"stdout": "", "stderr": "x" * 9000, "command": "c", "returncode": 0},
                                        label="step.p1", log_subdir="step_logs")
    assert "TRUNCATED" in out["stderr"]
    assert Path(out["log_path"]).parent.name == "step_logs"
    assert Path(out["log_path"]).read_text().count("x") == 9000


def test_resource_rows_omit_nanopore_fields_that_are_null(tmp_path, monkeypatch):
    import yaml
    core = tmp_path / "core_test_data_hg38"; core.mkdir()
    (core / "manifest.yaml").write_text(yaml.safe_dump({
        "genome_build": "hg38",
        "sequencing_data": {"short_read": {"single_end": {"rnaseq": [
            {"sample": "s", "accession": "SRR1", "read_type": "short_read", "end_type": "single_end",
             "assay_type": "rnaseq", "platform": "illumina",
             "subsets": {"10K": {"r1": "r1.fq.gz", "num_reads": 10000, "available": True}}},
            {"sample": "n", "accession": "SRR2", "read_type": "long_read", "end_type": "",
             "assay_type": "wgs", "platform": "ont", "file_format": "pod5", "chemistry": "R10.4.1",
             "subsets": {"500": {"r1": "r.pod5", "num_reads": 500, "available": True}}}]}}}}))
    monkeypatch.setattr(resources.workspace, "resources_root", lambda: tmp_path)
    rows = resources.list_resources({"resource_type": "test_data"}, {})["test_data"]
    short, nano = rows
    assert "chemistry" not in short and "kit" not in short and short["r2"] is None
    assert nano["chemistry"] == "R10.4.1" and "flowcell" not in nano
