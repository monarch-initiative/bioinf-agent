"""A reference database cannot be recorded wrong, and a built one has a producer.

The record is typed at its write seam: `ReferenceDatabase` takes exactly the fields its
producers write, `local_path` is required, and the draft funnel enforces it — a patch
with a stray key or no path is refused with the field named, where it used to land and
anchor nothing. The draft's list merges by name, so a patch cannot erase the records
the download and acquire primitives wrote. A reference the agent BUILT here is
registered through `download_reference_database(url="", local_path=…)`. At render, an
authored artifact staged with `generated_by` is data (a path input pinned by sha256),
not a script to carry; and the orphan-input remedy names the primitive by what the
input IS, data first.
"""
from __future__ import annotations

import hashlib

import pytest
from pipeline_fixtures import GTF, sealed_rnaseq_spec

from agent.models.core_data import ReferenceDatabase, WorkflowSpec
from agent.skills import pipeline_record as pr
from agent.skills import spec_writer as sw
from agent.skills import typed_nouns
from agent.skills.pipeline_state import PipelineState


# ── the model and the funnel ─────────────────────────────────────────────────

def test_the_record_takes_its_producers_fields_and_nothing_else():
    ReferenceDatabase.model_validate({"name": "idx", "version": "1", "local_path": "/d/idx",
                                      "locus": "cluster", "compute_env": "c", "recipe": "r.yaml",
                                      "recipe_sha256": "a" * 64, "files": [], "sha256": None})
    with pytest.raises(Exception, match="path"):
        ReferenceDatabase.model_validate({"name": "idx", "version": "1", "path": "/d/idx"})
    with pytest.raises(Exception, match="local_path"):
        ReferenceDatabase.model_validate({"name": "idx", "version": "1"})


def test_the_funnel_enforces_the_record_and_says_the_list_is_patchable():
    tn = typed_nouns.REGISTRY["reference_databases"]
    assert tn.mode == typed_nouns.ENFORCED and tn.patchable
    with pytest.raises(typed_nouns.TypedNounViolation, match="patchable: re-send the whole list") as e:
        typed_nouns.check_draft({"reference_databases": [{"name": "idx", "version": "1", "path": "/d"}]},
                                source="test:refdb")
    assert "reference_databases[0]" in str(e.value) and "path" in str(e.value)


# ── the draft: patch merges by name and refuses a malformed record ───────────

def _draft():
    ps = PipelineState({})
    pid = ps.start("demo", "d")["pipeline_id"]
    return ps, pid


def test_a_patch_cannot_erase_the_records_the_primitives_wrote():
    ps, pid = _draft()
    ps.patch(pid, {"reference_databases": [{"name": "gtf", "version": "47", "local_path": "/d/a.gtf",
                                            "source_url": "https://x/a.gtf"}]})
    ps.patch(pid, {"reference_databases": [{"name": "idx", "version": "1", "local_path": "/d/idx"}]})
    ps.patch(pid, {"reference_databases": [{"name": "gtf", "version": "47", "local_path": "/d/a.gtf",
                                            "description": "amended"}]})
    recs = {r["name"]: r for r in ps.get_draft(pid)["reference_databases"]}
    assert set(recs) == {"gtf", "idx"}
    assert recs["gtf"]["source_url"] == "https://x/a.gtf" and recs["gtf"]["description"] == "amended"


def test_a_record_with_the_wrong_key_is_refused_at_the_write_and_nothing_lands():
    ps, pid = _draft()
    ps.patch(pid, {"reference_databases": [{"name": "gtf", "version": "47", "local_path": "/d/a.gtf"}]})
    r = ps.patch(pid, {"reference_databases": [{"name": "idx", "version": "1", "path": "/d/idx"}]})
    assert r["outcome"] == "refused" and r["code"] == "pipeline_state.record_malformed"
    assert "path" in r["error"] and "patchable" in r["error"] and r["patched_keys"] == ["reference_databases"]
    assert [x["name"] for x in ps.get_draft(pid)["reference_databases"]] == ["gtf"]


def test_an_unknown_pipeline_id_lists_the_open_drafts():
    ps, pid = _draft()
    r = ps.patch("nope", {"description": "x"})
    assert r["code"] == "pipeline_state.unknown_pipeline" and r["open_drafts"] == [pid] and pid in r["error"]


# ── the producer for "I built this reference here" ───────────────────────────

@pytest.fixture
def tools(monkeypatch):
    from agent import mcp_server as ms
    from agent.mcp_tools import data_tools
    ps, pid = _draft()
    monkeypatch.setattr(ms, "_pipeline_state", ps)
    return data_tools, ps, pid


def test_a_built_file_is_registered_hashed_and_recorded(tools, tmp_path):
    data_tools, ps, pid = tools
    f = tmp_path / "chr22.gtf"
    f.write_text("chr22\tHAVANA\tgene\n")
    r = data_tools.download_reference_database(name="chr22_gtf", url="", local_path=str(f),
                                               version="47", pipeline_id=pid)
    assert (r["outcome"], r["code"], r["kind"]) == ("proven", "data.refdb_registered", "file")
    digest = hashlib.sha256(f.read_bytes()).hexdigest()
    assert r["sha256"] == digest and r["size_bytes"] == f.stat().st_size
    assert (tmp_path / "chr22.gtf.source.sha256").read_text().split()[0] == digest
    [rec] = ps.get_draft(pid)["reference_databases"]
    assert rec["local_path"] == str(f) and rec["available"] is True and rec["sha256"] == digest
    assert "source_url" not in rec                       # no download origin is recorded as none


def test_a_built_directory_is_registered_by_path(tools, tmp_path):
    data_tools, ps, pid = tools
    d = tmp_path / "hisat2_index"
    d.mkdir(); (d / "chr22.1.ht2").write_bytes(b"x")
    r = data_tools.download_reference_database(name="idx", url="", local_path=str(d), pipeline_id=pid)
    assert r["code"] == "data.refdb_registered" and r["kind"] == "directory" and r["sha256"] is None
    assert ps.get_draft(pid)["reference_databases"][0]["local_path"] == str(d)


def test_registering_what_is_not_there_is_refused(tools, tmp_path):
    data_tools, _, pid = tools
    r = data_tools.download_reference_database(name="idx", url="", local_path=str(tmp_path / "no"), pipeline_id=pid)
    assert r["outcome"] == "refused" and r["code"] == "data.register_db_missing" and "pass `url`" in r["error"]
    r = data_tools.download_reference_database(name="idx", url="", pipeline_id=pid)
    assert r["code"] == "data.download_db_missing_args" and "local_path" in r["error"]
    r = data_tools.download_reference_database(name="idx", url="", local_path="rel/idx")
    assert r["code"] == "data.register_db_path_relative"
    assert data_tools.download_reference_database(name="", url="")["code"] == "data.download_db_missing_args"


# ── the render: an authored data file is a path input, an authored script is carried ──

def _authored(path, generated_by=True, size=10):
    a = {"path": path, "role": "staged_input", "description": "d", "sha256": "a" * 64,
         "size_bytes": size, "created_at": "2026-10-06T00:00:00+00:00", "language": ""}
    if generated_by:
        a["generated_by"] = "hisat2-build …"
        a["content_excerpt"] = "<binary; first 64 bytes hex: 00>"
    else:
        a["content_excerpt"] = "x" * size
    return a


def _spec_with(**extra) -> WorkflowSpec:
    return WorkflowSpec.model_validate({**sealed_rnaseq_spec().model_dump(), **extra})


def test_a_generated_by_data_file_stays_a_path_input_pinned_by_its_record():
    rec = pr.derive_pipeline_record(_spec_with(authored_artifacts=[_authored(GTF)]), name="rnaseq_counts")
    gtf = rec.param("GTF")
    assert gtf.default == GTF and gtf.source == "authored_artifact:chr22.gtf" and rec.scripts == []
    assert "data file" in gtf.reason


def test_a_declared_reference_database_wins_over_an_authored_record_at_the_same_path():
    rec = pr.derive_pipeline_record(_spec_with(
        reference_databases=[{"name": "gencode", "version": "47", "local_path": GTF, "available": True}],
        authored_artifacts=[_authored(GTF)]), name="rnaseq_counts")
    assert rec.param("GTF").source == "reference_database:gencode" and rec.scripts == []


def test_the_script_suffix_vocabulary_is_one_reading_shared_by_seal_and_render():
    """The seal's orphan walk exempts a script path and the render refuses to leave an
    authored script uncarried (tests/test_pipeline_cohort.py); both read one vocabulary."""
    from agent.models import core_data
    assert core_data.is_script_path("/x/run.R") and core_data.is_script_path("/x/a.sh")
    assert core_data.is_script_path("/x/main.nf") and not core_data.is_script_path(GTF)
    assert not core_data.is_script_path("/x/chr22.1.ht2") and not core_data.is_script_path(None)


# ── the remedy orders the declaring primitive by what the input is ───────────

def test_the_orphan_remedy_names_data_first_and_the_script_primitive_last():
    step = {"step": 1, "tool": "t", "command": "c", "returncode": 0,
            "inputs": [{"path": "/ext/orphan.gtf"}], "detected_outputs": ["/out/a.tsv"],
            "validation": {"/out/a.tsv": {"passed": True}}, "validation_locus": "local",
            "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 1.0, "max_cpu_percent": 1.0}}
    [v] = [v for v in sw.check_workflow_invariants({"pipeline_steps": [step]})
           if v["invariant"] == "I8.composition_coherence"]
    remedy = v["remedy"]
    assert remedy.index("download_reference_database") < remedy.index("select_test_data") \
        < remedy.index("stage_authored_artifact")
    assert "local_path=" in remedy and "discard_pipeline_draft" in remedy


def test_the_brief_and_the_artifact_docstring_route_data_to_the_reference_primitive():
    from agent.mcp_tools import data_tools, workflow_tools
    i8 = data_tools._BRIEF_HINTS["I8"]
    assert i8.index("download_reference_database") < i8.index("stage_authored_artifact")
    doc = workflow_tools.stage_authored_artifact.__doc__
    assert "download_reference_database" in doc and "indexed" not in doc.lower()
