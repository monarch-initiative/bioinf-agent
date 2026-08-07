"""
test_data is an anchored input source, like every other one.

THE HOLE, reproduced against the live checker before any of this was written:

    spec["test_data"] = {"genome_build": "hg38", "r1": "/nope/ghost.fastq.gz"}
    check_workflow_invariants(spec)  ->  []          # sealed green

and the same for a file whose bytes were rewritten between two seal calls. Every
OTHER external source was pinned — reference_databases by I5, authored_artifacts by
I8's re-hash, runtime_configs by their sha256 — so the one source step 3 of the
documented protocol tells the agent to produce was the one nothing checked. Its paths
were read only as strings, to make I8's universe of traceable inputs bigger.

Underneath that sat a two-readings defect with a measurable consequence. "Which
test_data values are paths" was spelled twice and the spellings disagreed on both
axes: `spec_writer` matched a fixed key tuple and resolved relative paths against the
project root, while `data_pins` matched any key but only values starting with "/".
`select_test_data` builds its paths from the manifest's `core_dir`, which is relative
in the shipped config — so on both sealed specs that carry test_data, the production
data-pin check matched ZERO of it while reporting 17 anchors from elsewhere.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest
import yaml

from agent.models import core_data as cd
from agent.skills import data_pins, spec_writer as sw
from agent.skills.spec_writer import (TEST_DATA_DIVERGED, TEST_DATA_NOT_ATTEMPTED,
                                      TEST_DATA_UNANCHORED, TEST_DATA_VERIFIED,
                                      check_workflow_invariants, verify_test_data)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _spec(tmp_path: Path, test_data: dict) -> dict:
    """A workflow whose single step consumes test_data's r1 and passes every other
    run-side invariant, so anything that fires is about the test data."""
    out = tmp_path / "out.txt"
    out.write_text("result\n")
    inputs = [{"path": p} for p in cd.test_data_paths(test_data).values()]
    return {"workflow_name": "wf", "pipeline_steps": [{
        "step": 1, "tool": "t", "command": "c", "returncode": 0,
        "inputs": inputs, "detected_outputs": [str(out)],
        "validation": {"out.txt": {"passed": True, "file": str(out)}},
        "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 10.0,
                           "max_cpu_percent": 5.0},
    }], "test_data": test_data}


def _reads(tmp_path: Path, content: str = "@r1\nACGT\n+\nIIII\n") -> str:
    p = tmp_path / "reads.fastq"
    p.write_text(content)
    return str(p)


def _ids(spec) -> list[str]:
    return [v["invariant"] for v in check_workflow_invariants(spec)]


# ---------------------------------------------------------------------------
# THE one reading of "which values are paths"
# ---------------------------------------------------------------------------

def test_the_leaf_finds_what_each_old_reader_missed():
    """One case per old spelling, so this fails if either half is reintroduced."""
    got = cd.test_data_paths({
        "genome_build": "hg38",
        "r1": "data/core_test_data_hg38/x_R1.fastq.gz",   # data_pins missed: relative
        "pod5_dir": "/abs/signal",                        # spec_writer missed: not in its tuple
    })
    assert got == {"r1": "data/core_test_data_hg38/x_R1.fastq.gz",
                   "pod5_dir": "/abs/signal"}


@pytest.mark.parametrize("key,val", [
    ("source_url", "https://ftp.example.org/reads_1.fastq.gz"),
    ("r1", "{READS_R1}"),
    ("r2", "$READS"),
    ("platform", "illumina/nextseq"),
    ("suggested_model", "dna_r10.4.1_e8.2_400bps_hac@v5.0.0"),
    ("num_reads", 10000),
])
def test_a_value_that_is_not_a_path_is_not_promoted_to_one(key, val):
    """Deliberately key-driven rather than "contains a slash". This set feeds I8's
    external-source universe, where a wrong extra entry WIDENS what counts as a
    traceable input — the direction that fails silently. `platform` is the live
    example: `illumina/nextseq` has a slash and is not a file."""
    assert cd.test_data_paths({key: val}) == {}


def test_a_relative_path_resolves_against_the_project_root_not_the_cwd(monkeypatch,
                                                                      tmp_path):
    """`select_test_data` records paths built from the manifest's `core_dir`, which the
    shipped config leaves relative. Resolving those against the CWD — which for the MCP
    server is not guaranteed to be anything — silently points the check at nothing."""
    monkeypatch.chdir(tmp_path)
    p = cd.resolve_data_path("data/core_test_data_hg38/x.fastq.gz")
    assert p.is_absolute() and str(p).startswith(str(cd._PROJECT_ROOT))
    assert tmp_path not in p.parents


def test_both_readers_now_go_through_the_leaf():
    """The lint in test_one_reading_per_field.py owns named FIELDS; this owns the KEY
    SET, which is not a field name and so cannot be caught there. Asserting on behaviour
    rather than source text.

    One shape per old reader's blind spot, because a fixture that avoids the defect is
    how the defect survives: `pod5_dir` was outside spec_writer's key tuple, and the
    RELATIVE `r1` was outside data_pins' "starts with /" test. An earlier draft of this
    test used an absolute pod5_dir for both halves — which the old data_pins reader also
    matched, so reverting it left the suite green."""
    td = {"genome_build": "hg38",
          "pod5_dir": "/abs/signal",                            # spec_writer missed
          "r1": "data/core_test_data_hg38/x_R1.fastq.gz"}       # data_pins missed
    assert {"pod5_dir", "r1"} <= set(cd.test_data_paths(td))

    named = {a["name"] for a in data_pins.sealed_anchors({"test_data": td}).values()}
    assert {"pod5_dir", "r1"} <= named, \
        f"data_pins is not reading test_data through the leaf; saw {named}"
    assert verify_test_data({"test_data": td})["checked"] == 2, \
        "spec_writer is not reading test_data through the leaf"


def test_a_relative_test_data_path_becomes_a_comparable_pin():
    """The shape BOTH sealed specs on disk carry, and the one that made the production
    data check blind. `select_test_data` builds paths from the manifest's `core_dir`,
    relative in the shipped config; `sealed_anchors` must key on the resolved absolute
    path, because that is what a production run binds and compares against."""
    td = {"genome_build": "hg38",
          "r1": "data/core_test_data_hg38/short_read/paired_end/rnaseq/x_R1.fastq.gz"}
    anchors = data_pins.sealed_anchors({"test_data": td})
    assert len(anchors) == 1, f"the relative test_data path was not seen at all: {anchors}"
    key = next(iter(anchors))
    assert key.startswith("/") and key.endswith("x_R1.fastq.gz"), key
    assert str(cd._PROJECT_ROOT) in key, \
        "a relative test_data path must resolve against the project root"


# ---------------------------------------------------------------------------
# THE REPRODUCTION — what used to seal green
# ---------------------------------------------------------------------------

def test_seal_refuses_a_test_data_path_that_is_not_on_disk(tmp_path):
    """Verbatim the shape that sealed green before this clause existed."""
    spec = _spec(tmp_path, {"genome_build": "hg38", "r1": "/nope/ghost.fastq.gz"})
    assert "I8.test_data_missing" in _ids(spec)


def test_seal_refuses_an_empty_test_data_input(tmp_path):
    """An interrupted copy leaves a zero-byte file, which is as unusable as no file —
    and unlike a missing one it satisfies every existence check."""
    empty = tmp_path / "empty.fastq"
    empty.write_text("")
    assert "I8.test_data_empty" in _ids(_spec(tmp_path, {"genome_build": "hg38",
                                                         "r1": str(empty)}))


def test_seal_refuses_an_empty_test_data_directory(tmp_path):
    d = tmp_path / "core"
    d.mkdir()
    assert "I8.test_data_empty" in _ids(_spec(tmp_path, {"genome_build": "hg38",
                                                         "core_data_dir": str(d)}))


def test_seal_refuses_bytes_that_changed_since_selection(tmp_path):
    """A SAME-LENGTH rewrite, so only the hash can catch it. This is the case a
    size-only anchor waves through."""
    reads = _reads(tmp_path)
    anchors = {"r1": cd.anchor_for_path(reads).model_dump()}
    Path(reads).write_text("@r1\nTTTT\n+\nIIII\n")           # same length, other bases
    spec = _spec(tmp_path, {"genome_build": "hg38", "r1": reads,
                            "content_anchors": anchors})
    assert "I8.test_data_mutated" in _ids(spec)
    assert verify_test_data(spec)["status"] == TEST_DATA_DIVERGED


def test_seal_refuses_a_truncated_input(tmp_path):
    reads = _reads(tmp_path)
    anchors = {"r1": cd.anchor_for_path(reads).model_dump()}
    Path(reads).write_text("@r1\n")
    assert "I8.test_data_size_mismatch" in _ids(
        _spec(tmp_path, {"genome_build": "hg38", "r1": reads,
                         "content_anchors": anchors}))


def test_an_unchanged_anchored_input_seals_and_says_verified(tmp_path):
    reads = _reads(tmp_path)
    spec = _spec(tmp_path, {"genome_build": "hg38", "r1": reads,
                            "content_anchors": {"r1": cd.anchor_for_path(reads).model_dump()}})
    assert _ids(spec) == []
    assert verify_test_data(spec)["status"] == TEST_DATA_VERIFIED


# ---------------------------------------------------------------------------
# the three states — silence is not a verdict
# ---------------------------------------------------------------------------

def test_an_unanchored_block_is_unanchored_not_verified(tmp_path):
    """The legacy shape (and anything that reached a draft outside the primitive). It
    does not refuse the seal — existence still passed — but it must not read as proof.
    Both sealed specs on disk that carry test_data are exactly this."""
    spec = _spec(tmp_path, {"genome_build": "hg38", "r1": _reads(tmp_path)})
    assert _ids(spec) == []
    r = verify_test_data(spec)
    assert (r["status"], r["checked"], r["anchored"]) == (TEST_DATA_UNANCHORED, 1, 0)


def test_a_partly_anchored_block_does_not_round_up(tmp_path):
    r1, r2 = _reads(tmp_path), str(tmp_path / "r2.fastq")
    Path(r2).write_text("@r2\nGGGG\n")
    spec = _spec(tmp_path, {"genome_build": "hg38", "r1": r1, "r2": r2,
                            "content_anchors": {"r1": cd.anchor_for_path(r1).model_dump()}})
    r = verify_test_data(spec)
    assert (r["status"], r["checked"], r["anchored"]) == (TEST_DATA_UNANCHORED, 2, 1)


def test_no_test_data_is_not_attempted_rather_than_verified():
    """A workflow with no test data must not borrow the word `verified` from a check
    that examined nothing — the vacuous-pass shape this codebase keeps paying for."""
    assert verify_test_data({})["status"] == TEST_DATA_NOT_ATTEMPTED
    assert verify_test_data({"test_data": {"genome_build": "hg38"}})["status"] \
        == TEST_DATA_NOT_ATTEMPTED


def test_the_sealed_artifact_states_which_of_the_three_it_is():
    """`test_data_integrity` is on WorkflowSpec, so a renderer never has to infer the
    verdict from an absent violation."""
    from agent.models.core_data import WorkflowSpec
    assert "test_data_integrity" in WorkflowSpec.model_fields


# ---------------------------------------------------------------------------
# the anti-laundering rule
# ---------------------------------------------------------------------------

def test_seal_never_writes_an_anchor_it_is_about_to_check(tmp_path):
    """THE I5 bug, pre-empted. There, the seal-time refresh overwrote the recorded
    sha256 with the current observation and then re-validated moments later — comparing
    a value with itself and manufacturing perfect agreement for a reference that had
    moved. An anchor is a claim about the PAST; only the producer may write one."""
    td = {"genome_build": "hg38", "r1": _reads(tmp_path)}
    spec = _spec(tmp_path, td)
    before = copy.deepcopy(spec)
    verify_test_data(spec)
    check_workflow_invariants(spec)
    assert spec == before, "seal mutated the record it was checking"
    assert "content_anchors" not in spec["test_data"], \
        "seal filled in the anchor it then compares against — that proves nothing"


def test_an_anchor_records_what_the_bytes_were_not_what_they_are(tmp_path):
    """The anchor must survive the file changing; if it tracked the file it would
    always agree with it."""
    reads = _reads(tmp_path)
    anchor = cd.anchor_for_path(reads)
    Path(reads).write_text("different\n")
    assert cd.anchor_for_path(reads).sha256 != anchor.sha256


# ---------------------------------------------------------------------------
# the anchor record itself — the typed-seam rules
# ---------------------------------------------------------------------------

def test_a_file_anchor_carries_the_real_digest(tmp_path):
    reads = _reads(tmp_path)
    a = cd.anchor_for_path(reads)
    assert a.kind == "file"
    assert a.sha256 == hashlib.sha256(Path(reads).read_bytes()).hexdigest()
    assert a.size_bytes == Path(reads).stat().st_size


def test_a_directory_states_that_it_has_no_hash(tmp_path):
    """`sha256=None` STATED, not an empty string a reader could take for "hashed, and
    it came out empty"."""
    a = cd.anchor_for_path(tmp_path)
    assert (a.kind, a.sha256, a.size_bytes) == ("directory", None, None)


def test_a_missing_path_gets_no_anchor_rather_than_a_hollow_one():
    """The producer must not record `{sha256: None}` for a file that was not there —
    that is a pin asserting nothing, and the seal-side existence check is the honest
    complaint."""
    assert cd.anchor_for_path("/nope/ghost.fastq.gz") is None


def test_the_anchor_model_forbids_extras_and_defaults_nothing():
    """The `shipped_binaries` lesson: a permissive model with defaults does not catch
    drift, it authors it. Every field required, `None` a value a producer must state."""
    from agent.models.core_data import ContentAnchor
    with pytest.raises(Exception):
        ContentAnchor(kind="file", sha256="ab", size_bytes=1, algorithm="sha256")
    with pytest.raises(Exception):
        ContentAnchor(kind="file", sha256="ab")           # size_bytes not defaulted
    assert not any(f.is_required() is False
                   for f in ContentAnchor.model_fields.values()), \
        "a defaulted field lets a producer stay silent and look complete"


def test_the_hash_cap_is_the_same_number_everywhere(tmp_path):
    """A file size-anchored by one check must be size-anchored by all of them, or two
    checks disagree merely because one gave up sooner."""
    assert cd.ANCHOR_HASH_CAP_BYTES == data_pins.HASH_CAP_BYTES
    src = Path(sw.__file__).read_text()
    assert "HASH_CAP_BYTES = 2 * 1024 * 1024 * 1024" in src, \
        "spec_writer's reference-DB cap moved away from core_data.ANCHOR_HASH_CAP_BYTES"


def test_an_oversized_file_is_size_anchored_rather_than_hashed(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "ANCHOR_HASH_CAP_BYTES", 4)
    reads = _reads(tmp_path)
    a = cd.anchor_for_path(reads)
    assert a.kind == "file" and a.sha256 is None and a.size_bytes > 4


def test_an_oversized_input_is_not_reported_as_mutated(tmp_path, monkeypatch):
    """`sha256=None` above the cap must not compare as a mismatch against a recorded
    digest — a report crying a false mutation is itself a lie."""
    reads = _reads(tmp_path)
    anchors = {"r1": cd.anchor_for_path(reads).model_dump()}
    monkeypatch.setattr(cd, "ANCHOR_HASH_CAP_BYTES", 4)
    spec = _spec(tmp_path, {"genome_build": "hg38", "r1": reads,
                            "content_anchors": anchors})
    assert "I8.test_data_mutated" not in _ids(spec)


# ---------------------------------------------------------------------------
# the producer
# ---------------------------------------------------------------------------

def _manifest_config(tmp_path: Path) -> tuple[dict, Path]:
    """A miniature core-test-data tree with REAL files, so select_test_data runs the
    production path (list_resources -> score -> anchor) with nothing stubbed."""
    core = tmp_path / "data" / "core_test_data_hg38"
    reads = core / "short_read" / "paired_end" / "rnaseq"
    reads.mkdir(parents=True)
    (reads / "S_R1.fastq.gz").write_text("@a\nACGT\n+\nIIII\n")
    (reads / "S_R2.fastq.gz").write_text("@a\nTGCA\n+\nIIII\n")
    (core / "manifest.yaml").write_text(yaml.safe_dump({
        "genome_build": "hg38", "species": "homo_sapiens",
        "sequencing_data": {"short_read": {"paired_end": {"rnaseq": [{
            "sample": "S", "accession": "SRR1", "read_type": "short_read",
            "end_type": "paired_end", "assay_type": "rnaseq", "platform": "illumina",
            "subsets": {"10K": {
                "r1": "short_read/paired_end/rnaseq/S_R1.fastq.gz",
                "r2": "short_read/paired_end/rnaseq/S_R2.fastq.gz",
                "num_reads": 10000, "available": True}}}]}}},
    }))
    return {"paths": {"data_dir": str(tmp_path / "data")}}, core


def test_select_test_data_anchors_every_path_it_emits(tmp_path, monkeypatch):
    """THE producer↔leaf contract, driven end to end. If a producer starts emitting a
    new path key that `TEST_DATA_PATH_KEYS` does not cover, the key gets no anchor and
    this fails — which is the mechanism, not a paragraph asking future readers to
    remember to update the tuple."""
    from agent import mcp_server as m
    config, core = _manifest_config(tmp_path)
    monkeypatch.setattr(m, "config", config)

    res = m.select_test_data(genome_build="hg38", assay_type="rnaseq")
    td = res["test_data"]
    paths = cd.test_data_paths(td)
    assert set(paths) >= {"r1", "r2"}, td

    anchors = td.get("content_anchors") or {}
    for key, raw in paths.items():
        p = cd.resolve_data_path(raw)
        if not p.exists():
            continue
        assert key in anchors, (
            f"select_test_data emitted the path {key}={raw} with no content anchor — "
            f"either anchor it or it is an input nothing pins")

    for key in ("r1", "r2"):
        assert anchors[key]["sha256"] == hashlib.sha256(
            Path(td[key]).read_bytes()).hexdigest()
        assert anchors[key]["kind"] == "file"
    assert anchors.get("core_data_dir", {}).get("kind") == "directory"


def test_a_selected_dataset_then_seals_verified(tmp_path, monkeypatch):
    """The documented happy path, end to end: select, then check. This is the claim the
    whole change exists to make true."""
    from agent import mcp_server as m
    config, _ = _manifest_config(tmp_path)
    monkeypatch.setattr(m, "config", config)
    td = m.select_test_data(genome_build="hg38", assay_type="rnaseq")["test_data"]
    spec = _spec(tmp_path, td)
    assert _ids(spec) == []
    assert verify_test_data(spec)["status"] == TEST_DATA_VERIFIED


def test_a_selected_dataset_that_is_then_swapped_fails_to_seal(tmp_path, monkeypatch):
    from agent import mcp_server as m
    config, _ = _manifest_config(tmp_path)
    monkeypatch.setattr(m, "config", config)
    td = m.select_test_data(genome_build="hg38", assay_type="rnaseq")["test_data"]
    Path(td["r1"]).write_text("@b\nGGGG\n+\nIIII\n")          # same length, other reads
    assert "I8.test_data_mutated" in _ids(_spec(tmp_path, td))


def test_select_test_data_does_not_fabricate_an_anchor_for_a_missing_file(tmp_path,
                                                                          monkeypatch):
    from agent import mcp_server as m
    config, core = _manifest_config(tmp_path)
    monkeypatch.setattr(m, "config", config)
    (core / "short_read" / "paired_end" / "rnaseq" / "S_R2.fastq.gz").unlink()
    td = m.select_test_data(genome_build="hg38", assay_type="rnaseq")["test_data"]
    assert "r2" not in (td.get("content_anchors") or {})
    assert "I8.test_data_missing" in _ids(_spec(tmp_path, td))


# ---------------------------------------------------------------------------
# data_pins — the production-run half
# ---------------------------------------------------------------------------

def test_a_production_run_can_now_see_the_workflows_test_data(tmp_path, monkeypatch):
    """Before the leaf, `sealed_anchors` matched only values starting with "/", so a
    spec sealed from `select_test_data` contributed nothing and a production run bound
    its inputs against a pin that was not there."""
    from agent import mcp_server as m
    config, _ = _manifest_config(tmp_path)
    monkeypatch.setattr(m, "config", config)
    td = m.select_test_data(genome_build="hg38", assay_type="rnaseq")["test_data"]

    anchors = data_pins.sealed_anchors({"test_data": td})
    from_td = {v["name"] for v in anchors.values() if v["source"] == "test_data"}
    assert {"r1", "r2"} <= from_td
    assert all(str(k).startswith("/") for k in anchors), \
        "sealed_anchors must key on resolved absolute paths"

    check = data_pins.check_bound_inputs({"test_data": td}, {"READS": td["r1"]})
    assert check["status"] == data_pins.VERIFIED, check


def test_a_production_run_binding_swapped_data_reports_diverged(tmp_path, monkeypatch):
    from agent import mcp_server as m
    config, _ = _manifest_config(tmp_path)
    monkeypatch.setattr(m, "config", config)
    td = m.select_test_data(genome_build="hg38", assay_type="rnaseq")["test_data"]
    Path(td["r1"]).write_text("@b\nGGGG\n+\nIIII\n")
    check = data_pins.check_bound_inputs({"test_data": td}, {"READS": td["r1"]})
    assert check["status"] == data_pins.DIVERGED, check


def test_the_real_sealed_specs_are_read_as_unanchored_not_as_verified():
    """The two sealed artifacts on disk that carry test_data predate anchors. They must
    read as `unanchored` — the honest third state — and must not refuse, since their
    inputs are still where they were left. Guards against the tempting shortcut of
    treating "no anchor" as "nothing to complain about, call it verified"."""
    repo = Path(__file__).resolve().parents[1]
    seen = 0
    for f in sorted((repo / "env_reports").glob("*.workflow.yaml")):
        spec = yaml.safe_load(f.read_text())
        if not cd.test_data_paths(spec.get("test_data")):
            continue
        seen += 1
        r = verify_test_data(spec)
        assert r["status"] in (TEST_DATA_UNANCHORED, TEST_DATA_VERIFIED), (f.name, r)
        assert r["checked"] > 0
    if seen == 0:
        pytest.skip("no sealed spec in env_reports/ carries test_data paths "
                    "(env_reports/ is gitignored, so this is vacuous in a clean clone)")


# ---------------------------------------------------------------------------
# the RUN dashboard — an unpinned input must not read as a pinned one
# ---------------------------------------------------------------------------

def _panel(spec: dict) -> str:
    from agent.skills.run_dashboard_html import _render_inputs
    return _render_inputs(spec)


def test_the_dashboard_says_when_test_data_was_never_anchored(tmp_path):
    """The panel used to print a bare path beside reference DBs and authored artifacts
    that show their sha256, so data nothing pinned rendered exactly like data something
    did. Reports never substitute a request for an observation."""
    html = _panel({"test_data": {"genome_build": "hg38", "r1": _reads(tmp_path)},
                   "test_data_integrity": {"status": TEST_DATA_UNANCHORED}})
    assert "not anchored" in html
    assert "NOT content-anchored" in html


def test_the_dashboard_shows_the_digest_when_there_is_one(tmp_path):
    reads = _reads(tmp_path)
    anchor = cd.anchor_for_path(reads)
    html = _panel({"test_data": {"genome_build": "hg38", "r1": reads,
                                 "content_anchors": {"r1": anchor.model_dump()}},
                   "test_data_integrity": {"status": TEST_DATA_VERIFIED}})
    assert anchor.sha256[:19] in html
    assert "not anchored" not in html
    assert "re-verified at seal" in html


def test_the_dashboard_reads_paths_through_the_leaf(tmp_path):
    """It carried a FOURTH hand-spelling of the key set; a slot outside that tuple was
    simply invisible on the page."""
    html = _panel({"test_data": {"genome_build": "hg38", "pod5_dir": str(tmp_path)}})
    assert "pod5_dir" in html


# ---------------------------------------------------------------------------
# the inventory — the row a user reads before trusting a sealed workflow
# ---------------------------------------------------------------------------

def _inventory(tmp_path: Path, spec: dict, **kw):
    from agent.skills.resources import list_pipelines
    d = tmp_path / "specs"
    d.mkdir(exist_ok=True)
    (d / f"{spec['workflow_name']}.workflow.yaml").write_text(yaml.safe_dump(spec))
    cfg = {"paths": {"pipelines_dir": str(d), "envs_dir": str(tmp_path / "envs")}}
    return list_pipelines(cfg, **kw)["workflows"][0]


def _sealed(name: str, td: dict | None, integrity: dict | None) -> dict:
    s = {"workflow_name": name, "env_request_key": "x|linux/amd64|none",
         "pipeline_steps": []}
    if td is not None:
        s["test_data"] = td
    if integrity is not None:
        s["test_data_integrity"] = integrity
    return s


def test_the_compact_row_warns_when_the_inputs_were_never_anchored(tmp_path):
    row = _inventory(tmp_path, _sealed("legacy", {"r1": "/data/x.fq"}, None))
    assert row["test_data_status"] == TEST_DATA_UNANCHORED


def test_the_compact_row_stays_quiet_when_the_inputs_are_pinned(tmp_path):
    """Compact means warnings survive and confirmations do not — the same rule the
    how-to status follows in the row beside it."""
    row = _inventory(tmp_path, _sealed("good", {"r1": "/data/x.fq"},
                                       {"status": TEST_DATA_VERIFIED}))
    assert "test_data_status" not in row


def test_the_compact_row_stays_quiet_when_there_is_no_test_data(tmp_path):
    assert "test_data_status" not in _inventory(tmp_path, _sealed("none", None, None))


@pytest.mark.parametrize("td,integrity,expect", [
    (None, None, TEST_DATA_NOT_ATTEMPTED),
    ({"r1": "/data/x.fq"}, None, TEST_DATA_UNANCHORED),
    ({"r1": "/data/x.fq"}, {"status": TEST_DATA_VERIFIED}, TEST_DATA_VERIFIED),
])
def test_both_inventory_forms_agree_on_the_verdict(tmp_path, td, integrity, expect):
    """The compact and detail rows had two derivations of this within minutes of each
    other and they already disagreed — the compact one keyed off the stored field alone,
    so both legacy specs on disk showed no warning at all."""
    spec = _sealed("w", td, integrity)
    compact = _inventory(tmp_path, spec)
    detail = _inventory(tmp_path, spec, detail=True)
    assert detail["test_data_status"] == expect
    assert compact.get("test_data_status", expect) == expect


def test_the_inventory_does_not_touch_the_filesystem_to_answer(tmp_path):
    """An inventory listing must stay a cheap read: `verify_test_data` stats and hashes
    every declared input, and this row names paths that may be on a cluster. Disclosure
    of a stored claim, not a re-derivation — same posture as the how-to status."""
    row = _inventory(tmp_path, _sealed("ghost", {"r1": "/nope/ghost.fastq.gz"},
                                       {"status": TEST_DATA_VERIFIED}), detail=True)
    assert row["test_data_status"] == TEST_DATA_VERIFIED


# ---------------------------------------------------------------------------
# WHAT KIND of data is a requirement; WHICH INSTANCE is a preference
# ---------------------------------------------------------------------------
#
# Every criterion was additive with no requirement, and the only refusal was `score == 0`
# — which `genome_build`, defaulting to "hg38" and worth 32 points, made unreachable.
# Measured 2026-08-07 against the real core data:
#
#     select_test_data(assay_type="nonexistent_zzz")  ->  exome / HG00096
#     select_test_data(assay_type="chipseq")          ->  exome / HG00096
#
# The second is the one that matters. chipseq is a REAL assay that is simply not on this
# disk, and the answer was unrelated exome reads with nothing saying anything had been
# substituted. This function is the SOLE producer of `test_data.content_anchors`, so the
# wrong dataset gets sha256-anchored, I8 re-verifies those anchors happily at seal, and
# the spec records a green ChIP-seq run performed on exome data. Every gate downstream is
# satisfied, because each one is true of the data that was actually used.

def test_a_requested_assay_that_is_not_on_disk_refuses_instead_of_substituting(tmp_path, monkeypatch):
    from agent import mcp_server as m
    config, _ = _manifest_config(tmp_path)      # this tree holds rnaseq and nothing else
    monkeypatch.setattr(m, "config", config)

    res = m.select_test_data(genome_build="hg38", assay_type="chipseq")
    assert res["outcome"] == "refused"
    assert res["code"] == "data.no_test_data_match"
    assert res.get("test_data") is None, "a refusal must not hand back a substitute"
    assert "assay_type" in res["unmet"]
    # EARN THE REFUSAL: say what IS here, so the caller can re-ask or fetch.
    assert any("rnaseq" in x for x in res["on_disk"])
    assert "add_core_test_data" in res["error"]


def test_a_requested_genome_build_that_is_not_on_disk_refuses(tmp_path, monkeypatch):
    from agent import mcp_server as m
    config, _ = _manifest_config(tmp_path)
    monkeypatch.setattr(m, "config", config)
    res = m.select_test_data(genome_build="mm10", assay_type="rnaseq")
    assert res["outcome"] == "refused"
    assert "genome_build" in res["unmet"]


def test_a_requested_file_format_that_is_not_on_disk_refuses(tmp_path, monkeypatch):
    """A basecaller asking for pod5 must never be handed FASTQ — it cannot run on it."""
    from agent import mcp_server as m
    config, _ = _manifest_config(tmp_path)
    monkeypatch.setattr(m, "config", config)
    res = m.select_test_data(genome_build="hg38", assay_type="rnaseq", file_format="pod5")
    assert res["outcome"] == "refused"
    assert "file_format" in res["unmet"]


def test_an_unmet_PREFERENCE_still_returns_the_best_available_match(tmp_path, monkeypatch):
    """The other half, and it is why the split exists rather than a blanket exact-match.
    `end_type`, `sample` and `subset` name WHICH of several equivalent datasets to prefer;
    substituting there is the useful best-effort behaviour this function was built for."""
    from agent import mcp_server as m
    config, _ = _manifest_config(tmp_path)
    monkeypatch.setattr(m, "config", config)
    res = m.select_test_data(genome_build="hg38", assay_type="rnaseq",
                             sample="SOMEONE_ELSE", subset="999K")
    assert res.get("outcome") != "refused", "an unmet preference is not a miss"
    assert res["test_data"]["sample"] == "S"
    assert res["test_data"]["assay_type"] == "rnaseq"


def test_asking_for_nothing_in_particular_still_works(tmp_path, monkeypatch):
    """The default call must keep behaving — genome_build defaults to hg38 and the tree
    has hg38 data, so the requirement is met rather than merely scored."""
    from agent import mcp_server as m
    config, _ = _manifest_config(tmp_path)
    monkeypatch.setattr(m, "config", config)
    res = m.select_test_data()
    assert res.get("outcome") != "refused"
    assert res["test_data"]["assay_type"] == "rnaseq"
