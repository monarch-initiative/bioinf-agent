"""Tests for acquire_data — cluster reference-data acquisition + locus-aware seal.

Covers:
  - the simple SLURM download-script renderer (resumable, sidecar, extract, header)
  - the globus-accessible staging-dir guard
  - acquire_to_cluster refusals (bad name / no url / non-ssh / no common_data /
    remote_dir outside common_data) + happy path (mocked transfer + sbatch)
  - locus-aware I5 (check_cluster_reference_db) verdicts over a mocked ssh probe
  - refresh_cluster_reference_db provenance enrichment
  - the seal-side integration points: spec_writer I5 branch + workflow_tools
    _refresh_reference_databases branch route to the cluster path for locus=cluster
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent.skills import acquire_data, compute_access, workflow_render


_FAKE_ACCESS = {
    "compute_envs": [{
        "name": "hpc", "type": "ssh", "host": "hpc-test", "email": "a@b.org",
        "agent_common_data_target": {
            "path": "/work/common",
            "permissions": ["file_name_only", "upload", "download", "exec"],
        },
    }],
    "projects": [],
}


def _slurm_v():
    return workflow_render._check_slurm(dict(acquire_data._DEFAULT_DL_SLURM))


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

class TestRenderDownloadScript:
    def test_header_and_resumable_fetch_and_sidecar(self):
        s = acquire_data.render_download_script(
            name="gnomad", url="https://x.org/g.vcf.bgz",
            dest_filename="g.vcf.bgz", extract=False, slurm_v=_slurm_v(), email="")
        assert s.startswith("#!/usr/bin/env bash\n")
        assert "#SBATCH --job-name=acquire_gnomad" in s
        assert "#SBATCH --output=%x-%j.out" in s
        # resumable: both wget -c and curl -C - present (fallback chain)
        assert "wget -c" in s
        assert "curl -L --fail -C -" in s
        # sha256 sidecar anchor
        assert "g.vcf.bgz.source.sha256" in s
        # runs on a compute node via SLURM_SUBMIT_DIR
        assert 'cd "${SLURM_SUBMIT_DIR:-' in s
        assert "set -euo pipefail" in s

    def test_no_email_no_mail_lines(self):
        s = acquire_data.render_download_script(
            name="x", url="https://x.org/f.dat", dest_filename="f.dat",
            extract=False, slurm_v=_slurm_v(), email="")
        assert "--mail-user" not in s

    def test_extract_block_per_archive_type(self):
        for fn, tool in (("d.zip", "unzip -o"), ("d.tar.gz", "tar -xzf"),
                         ("d.tgz", "tar -xzf"), ("d.tar", "tar -xf")):
            s = acquire_data.render_download_script(
                name="x", url=f"https://x.org/{fn}", dest_filename=fn,
                extract=True, slurm_v=_slurm_v(), email="")
            assert tool in s, f"{fn} should render {tool}"

    def test_no_extract_block_for_plain_file(self):
        s = acquire_data.render_download_script(
            name="x", url="https://x.org/f.vcf.gz", dest_filename="f.vcf.gz",
            extract=True, slurm_v=_slurm_v(), email="")
        # a .vcf.gz is not an archive we unpack; no tar/unzip
        assert "tar -x" not in s and "unzip" not in s


class TestStagingLocation:
    def test_staging_dir_is_repo_local_not_system_temp(self):
        stage = acquire_data._DL_STAGE_DIR.resolve()
        sys_tmp = Path(tempfile.gettempdir()).resolve()
        assert sys_tmp not in stage.parents and stage != sys_tmp
        repo_root = Path(acquire_data.__file__).resolve().parents[2]
        assert str(stage).startswith(str(repo_root))


# ---------------------------------------------------------------------------
# Orchestrator refusals + happy path
# ---------------------------------------------------------------------------

class TestAcquireToClusterRefusals:
    def test_bad_name(self):
        r = acquire_data.acquire_to_cluster(
            name="bad name!", url="https://x.org/f", compute_env="hpc",
            _pipeline_state=object())
        assert r["outcome"] == "refused" and r["code"] == "acquire.bad_name"

    def test_no_url(self, monkeypatch):
        r = acquire_data.acquire_to_cluster(
            name="ok", url="", compute_env="hpc", _pipeline_state=object())
        assert r["code"] == "acquire.no_url"

    def test_non_ssh_env(self, monkeypatch):
        acc = {"compute_envs": [{"name": "lap", "type": "local"}], "projects": []}
        monkeypatch.setattr(compute_access, "load_access", lambda *_a, **_k: acc)
        r = acquire_data.acquire_to_cluster(
            name="ok", url="https://x.org/f", compute_env="lap",
            _pipeline_state=object())
        assert r["code"] == "acquire.not_ssh_env"

    def test_no_common_data(self, monkeypatch):
        acc = {"compute_envs": [{"name": "hpc", "type": "ssh", "host": "h"}],
               "projects": []}
        monkeypatch.setattr(compute_access, "load_access", lambda *_a, **_k: acc)
        r = acquire_data.acquire_to_cluster(
            name="ok", url="https://x.org/f", compute_env="hpc",
            _pipeline_state=object())
        assert r["code"] == "acquire.no_common_data"

    def test_remote_dir_outside_common_data(self, monkeypatch):
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        r = acquire_data.acquire_to_cluster(
            name="ok", url="https://x.org/f", compute_env="hpc",
            remote_dir="/somewhere/else", _pipeline_state=object())
        assert r["code"] == "acquire.remote_dir_outside_common_data"


class TestAcquireToClusterHappyPath:
    def test_submits_and_records_cluster_entry(self, monkeypatch):
        from agent.skills import transfer, submit_workflow

        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        captured = {}

        def fake_upload(**kw):
            captured["remote_abs_path"] = kw["remote_abs_path"]
            return {"success": True, "remote_abs_path": kw["remote_abs_path"]}

        def fake_sbatch(env, workflow_dir, **kw):
            captured["workflow_dir"] = workflow_dir
            return {"job_id": "999001", "launcher": f"{workflow_dir}/launcher.sh"}

        monkeypatch.setattr(transfer, "upload", fake_upload)
        monkeypatch.setattr(submit_workflow, "sbatch_via_ssh", fake_sbatch)
        monkeypatch.setattr(submit_workflow, "_write_submission_manifest",
                            lambda **kw: "/tmp/manifest.json")

        # fake pipeline state that records the patch
        class FakeState:
            def __init__(self): self.patched = None; self.draft = {}
            def get_draft(self, pid): return self.draft
            def patch(self, pid, d): self.patched = d
        st = FakeState()

        r = acquire_data.acquire_to_cluster(
            name="exomiser_hg38", url="https://x.org/exomiser_2402.zip",
            compute_env="hpc", pipeline_id="p1", _pipeline_state=st)

        assert r["outcome"] == "proven" and r["success"] is True
        assert r["job_id"] == "999001"
        # landed under common_data/<name>
        assert captured["workflow_dir"] == "/work/common/exomiser_hg38"
        assert captured["remote_abs_path"] == "/work/common/exomiser_hg38/launcher.sh"
        # archive → db_path is the extracted DIR
        assert r["db_path"] == "/work/common/exomiser_hg38"
        # recorded a cluster-locus reference_databases entry
        rdbs = st.patched["reference_databases"]
        assert len(rdbs) == 1
        e = rdbs[0]
        assert e["locus"] == "cluster" and e["compute_env"] == "hpc"
        assert e["local_path"] == "/work/common/exomiser_hg38"
        assert e["available"] is False


# ---------------------------------------------------------------------------
# Locus-aware I5 + refresh
# ---------------------------------------------------------------------------

def _cluster_rdb(**over):
    base = {"name": "gnomad", "compute_env": "hpc",
            "local_path": "/work/common/gnomad/g.vcf.bgz", "locus": "cluster"}
    base.update(over)
    return base


class TestCheckClusterReferenceDb:
    def test_missing_compute_env_unverifiable(self):
        v = acquire_data.check_cluster_reference_db(_cluster_rdb(compute_env=None))
        assert len(v) == 1 and v[0]["invariant"] == "I5.reference_database_unverifiable"

    def test_present_nonempty_passes(self, monkeypatch):
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        monkeypatch.setattr(acquire_data, "_probe_cluster_path",
                            lambda *a, **k: {"exists": True, "size_bytes": 12345,
                                             "sha256": "a" * 64})
        assert acquire_data.check_cluster_reference_db(_cluster_rdb()) == []

    def test_missing_on_cluster(self, monkeypatch):
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        monkeypatch.setattr(acquire_data, "_probe_cluster_path",
                            lambda *a, **k: {"exists": False, "size_bytes": None,
                                             "sha256": None})
        v = acquire_data.check_cluster_reference_db(_cluster_rdb())
        assert v[0]["invariant"] == "I5.reference_database_missing"

    def test_empty_on_cluster(self, monkeypatch):
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        monkeypatch.setattr(acquire_data, "_probe_cluster_path",
                            lambda *a, **k: {"exists": True, "size_bytes": 0,
                                             "sha256": None})
        v = acquire_data.check_cluster_reference_db(_cluster_rdb())
        assert v[0]["invariant"] == "I5.reference_database_empty"

    def test_ssh_failure_unverifiable(self, monkeypatch):
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        monkeypatch.setattr(acquire_data, "_probe_cluster_path",
                            lambda *a, **k: {"error": "ssh down", "hint": "run ssh"})
        v = acquire_data.check_cluster_reference_db(_cluster_rdb())
        assert v[0]["invariant"] == "I5.reference_database_unverifiable"
        assert "hint" in v[0]

    # -- the content anchor: added 2026-07-31, absent for the life of the clause -----
    #
    # I5's statement, CLAUDE.md's I5 row and this function's own docstring all promised
    # "existence + non-empty + sidecar hash" for a cluster DB. The code stopped at
    # non-empty and never read probe["sha256"], which _probe_cluster_path was already
    # returning on the same hop. Every cluster DB in the corpus sealed on existence alone.

    def test_sidecar_mismatch_is_refused(self, monkeypatch):
        """The bytes moved: the recorded anchor and the cluster sidecar disagree."""
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        monkeypatch.setattr(acquire_data, "_probe_cluster_path",
                            lambda *a, **k: {"exists": True, "size_bytes": 10,
                                             "sha256": "b" * 64})
        v = acquire_data.check_cluster_reference_db(_cluster_rdb(sha256="a" * 64))
        assert len(v) == 1 and v[0]["invariant"] == "I5.reference_database_mutated"
        assert v[0]["recorded_sha256"] == "a" * 64
        assert v[0]["observed_sha256"] == "b" * 64

    def test_sidecar_match_passes(self, monkeypatch):
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        monkeypatch.setattr(acquire_data, "_probe_cluster_path",
                            lambda *a, **k: {"exists": True, "size_bytes": 10,
                                             "sha256": "A" * 64})   # case-insensitive
        assert acquire_data.check_cluster_reference_db(_cluster_rdb(sha256="a" * 64)) == []

    @pytest.mark.parametrize("recorded,observed", [
        (None,      "b" * 64),   # nothing was pinned at record time
        ("a" * 64,  None),       # no sidecar on the cluster to compare against
        (None,      None),
    ])
    def test_existence_only_when_there_is_no_anchor_to_compare(
            self, monkeypatch, recorded, observed):
        """Half an anchor is not a mismatch. With nothing recorded, or no sidecar on the
        cluster, there is no comparison to make and the clause must not invent one — the
        record's own `sha256: null` is the disclosure, and `data_pins` is the one place
        that reports it. A violation here would be a second answer to one question."""
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        monkeypatch.setattr(acquire_data, "_probe_cluster_path",
                            lambda *a, **k: {"exists": True, "size_bytes": 10,
                                             "sha256": observed})
        assert acquire_data.check_cluster_reference_db(_cluster_rdb(sha256=recorded)) == []


class TestRefreshClusterReferenceDb:
    def test_enriches_from_probe(self, monkeypatch):
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        monkeypatch.setattr(acquire_data, "_probe_cluster_path",
                            lambda *a, **k: {"exists": True, "size_bytes": 999,
                                             "sha256": "b" * 64})
        out = acquire_data.refresh_cluster_reference_db(_cluster_rdb())
        assert out["available"] is True and out["size_bytes"] == 999
        assert out["sha256"] == "b" * 64

    def test_ssh_failure_leaves_recorded_values(self, monkeypatch):
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        monkeypatch.setattr(acquire_data, "_probe_cluster_path",
                            lambda *a, **k: {"error": "ssh down"})
        out = acquire_data.refresh_cluster_reference_db(
            _cluster_rdb(available=False))
        assert out["available"] is False   # unchanged

    def test_refresh_never_overwrites_a_recorded_anchor(self, monkeypatch):
        """THE LAUNDERING FIX. Seal calls this refresh to build the artifact's
        reference_databases and then immediately re-validates that artifact. While this
        overwrote `sha256` from the probe, the check that followed compared the observed
        value against itself — so a DB whose bytes had genuinely changed produced perfect
        agreement, and the mismatch clause above could never have fired on the artifact
        pass even once it existed. An anchor is a claim about what the bytes WERE; an
        observation is what they are now. Replacing the first with the second manufactures
        the agreement it is supposed to test for.

        `available` and `size_bytes` ARE observations and must still refresh."""
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        monkeypatch.setattr(acquire_data, "_probe_cluster_path",
                            lambda *a, **k: {"exists": True, "size_bytes": 999,
                                             "sha256": "b" * 64})
        out = acquire_data.refresh_cluster_reference_db(_cluster_rdb(sha256="a" * 64))
        assert out["sha256"] == "a" * 64, "the recorded anchor was overwritten by the probe"
        assert out["available"] is True and out["size_bytes"] == 999

    def test_refresh_still_fills_an_absent_anchor(self, monkeypatch):
        """Fill-only, not read-only: a DB recorded before the sidecar existed still gets
        pinned. Only an EXISTING claim is protected."""
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        monkeypatch.setattr(acquire_data, "_probe_cluster_path",
                            lambda *a, **k: {"exists": True, "size_bytes": 999,
                                             "sha256": "b" * 64})
        assert acquire_data.refresh_cluster_reference_db(
            _cluster_rdb())["sha256"] == "b" * 64

    def test_seal_refresh_then_check_can_still_catch_a_moved_reference(self, monkeypatch):
        """THE TWO DEFECTS HID EACH OTHER — pinned end to end.

        Reproduces seal's real order (refresh the record, then re-validate the artifact)
        and asserts a moved cluster reference survives the refresh and is caught. Before
        this pair of fixes the same sequence returned zero violations twice over: once
        because nothing compared, and once because there was nothing left to compare."""
        from agent.mcp_tools import workflow_tools
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        monkeypatch.setattr(acquire_data, "_probe_cluster_path",
                            lambda *a, **k: {"exists": True, "size_bytes": 10,
                                             "sha256": "b" * 64})
        refreshed = workflow_tools._refresh_reference_databases(
            [_cluster_rdb(sha256="a" * 64)])
        assert refreshed[0]["sha256"] == "a" * 64, "refresh laundered the anchor"
        v = acquire_data.check_cluster_reference_db(refreshed[0])
        assert [x["invariant"] for x in v] == ["I5.reference_database_mutated"]


# ---------------------------------------------------------------------------
# Seal-side integration points
# ---------------------------------------------------------------------------

class TestSealIntegration:
    def test_spec_writer_i5_routes_cluster_entry(self, monkeypatch):
        from agent.skills import spec_writer
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        # cluster DB is missing on the cluster → I5 must flag it
        monkeypatch.setattr(acquire_data, "_probe_cluster_path",
                            lambda *a, **k: {"exists": False, "size_bytes": None,
                                             "sha256": None})
        spec = {"reference_databases": [_cluster_rdb()]}
        v = spec_writer._check_reference_database_availability(spec)
        assert any(x["invariant"] == "I5.reference_database_missing" for x in v)

    def test_refresh_reference_databases_routes_cluster(self, monkeypatch):
        from agent.mcp_tools import workflow_tools
        monkeypatch.setattr(compute_access, "load_access",
                            lambda *_a, **_k: _FAKE_ACCESS)
        monkeypatch.setattr(acquire_data, "_probe_cluster_path",
                            lambda *a, **k: {"exists": True, "size_bytes": 555,
                                             "sha256": "c" * 64})
        # a local Path check would mark available=False (cluster path not local);
        # the cluster branch must ssh-derive available=True instead.
        out = workflow_tools._refresh_reference_databases([_cluster_rdb()])
        assert out[0]["available"] is True and out[0]["size_bytes"] == 555

    def test_local_refresh_fills_an_absent_anchor_but_never_overwrites_one(self, tmp_path):
        """The LOCAL branch launders the same way the cluster one did, just less totally.

        Local I5 hashes the FILE and compares it to the recorded value, so re-deriving that
        value from today's sidecar means a reference swapped together with its sidecar
        sails through. Same rule, same reason: `available`/`size_bytes` are observations
        and refresh; `sha256` is an anchor and is filled only when absent.

        (Found by a GUARD, not by design: reverting this line broke no test, because the
        local branch had none. A production change nothing can fail on is not covered.)"""
        from agent.mcp_tools import workflow_tools
        db = tmp_path / "ref.fa"
        db.write_text(">chr1\nACGT\n")
        Path(f"{db}.source.sha256").write_text("b" * 64 + "  ref.fa\n")

        pinned = workflow_tools._refresh_reference_databases(
            [{"name": "ref", "local_path": str(db), "sha256": "a" * 64}])[0]
        assert pinned["sha256"] == "a" * 64, "the recorded anchor was overwritten"
        assert pinned["available"] is True and pinned["size_bytes"] == db.stat().st_size

        filled = workflow_tools._refresh_reference_databases(
            [{"name": "ref", "local_path": str(db)}])[0]
        assert filled["sha256"] == "b" * 64, "an absent anchor must still be filled"
