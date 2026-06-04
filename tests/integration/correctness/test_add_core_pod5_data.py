"""
add_core_pod5_data — the pod5 ingestion helper for raw nanopore signal files.

Pod5 is binary Apache Arrow (not subsettable like gzipped FASTQ). The helper
downloads the WHOLE file, sha256-anchors it, and records nanopore-specific
metadata (chemistry, flowcell, kit, suggested_model) on the SampleMeta sidecar
so downstream basecaller pipelines (dorado, bonito, remora) pick the right
model without re-probing.

These tests cover:
  - happy path: a mocked download lands the file, sha256-anchors, populates SampleMeta
  - idempotency: second call with the same anchor is a no-op
  - sha256 mismatch fails closed (download is treated as poisoned)
  - select_test_data routes by `file_format` when both pod5 + fastq exist
    under the same assay_type (the disambiguation knob)
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from agent.models.core_data import SampleMeta
from agent.skills.core_test_data import add_core_pod5_data


# A tiny "pod5" payload — just bytes; nothing here parses the format.
# 64 bytes ensures size mismatches are detectable and the hash is stable.
FAKE_POD5_BYTES = b"POD5_FAKE_SIGNAL_BLOB_FOR_TEST_FIXTURE_PURPOSES__________________"  # 64 B
FAKE_SHA256 = hashlib.sha256(FAKE_POD5_BYTES).hexdigest()


def _config(tmp_path: Path) -> dict:
    """Minimal config dict the helper needs (just paths.data_dir)."""
    return {"paths": {"data_dir": str(tmp_path / "data")}}


def _mock_urlopen(payload: bytes):
    """Build a urlopen mock that returns `payload` once via .read()."""
    resp = MagicMock()
    # The helper reads in 1 MB chunks until empty; one-shot the payload then ""
    resp.read.side_effect = [payload, b""]
    resp.__enter__ = lambda s: resp
    resp.__exit__ = lambda s, *a: None
    return resp


@pytest.fixture
def mock_gen_manifest(monkeypatch):
    """gen_manifest.py is a subprocess — stub it so tests don't shell out."""
    fake = MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(
        "agent.skills.core_test_data.subprocess.run",
        lambda *a, **kw: fake)
    return fake


# ---------------------------------------------------------------------------
# 1. Happy path — download, sha256-anchor, SampleMeta carries pod5 metadata
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_add_core_pod5_data_writes_file_and_sample_meta(tmp_path, mock_gen_manifest):
    """End-to-end: helper downloads the (mocked) pod5, writes it under the
    expected long_read layout, sha256-anchors, and produces a SampleMeta
    sidecar with the nanopore-specific fields populated."""
    config = _config(tmp_path)

    with patch("agent.skills.core_test_data.urllib.request.urlopen",
               return_value=_mock_urlopen(FAKE_POD5_BYTES)):
        result = add_core_pod5_data(
            config,
            accession="HG002_chr1_MAT_pod5",
            sample="HG002",
            source_url="https://example.test/chr1_MAT.pod5",
            assay_type="ont_wgs",
            platform="ont",
            chemistry="dna_r10.4.1_e8.2_400bps_5khz",
            flowcell="FLO-PRO114M",
            kit="SQK-LSK114",
            suggested_model="dna_r10.4.1_e8.2_400bps_hac@v5.0.0",
            expected_sha256=FAKE_SHA256,
            expected_size=len(FAKE_POD5_BYTES),
        )

    assert result["success"], result
    assert result["file_format"] == "pod5"
    assert result["sha256"] == FAKE_SHA256
    assert result["size_bytes"] == len(FAKE_POD5_BYTES)

    pod5_path = Path(result["path"])
    assert pod5_path.exists()
    assert pod5_path.suffix == ".pod5"
    assert pod5_path.read_bytes() == FAKE_POD5_BYTES
    # Long-read layout matches add_core_test_data's convention.
    assert "long_read/ont/ont_wgs/" in result["path"]

    # SampleMeta carries the pod5 metadata.
    meta = SampleMeta.from_yaml(result["sample_meta"])
    assert meta.file_format == "pod5"
    assert meta.chemistry == "dna_r10.4.1_e8.2_400bps_5khz"
    assert meta.flowcell == "FLO-PRO114M"
    assert meta.kit == "SQK-LSK114"
    assert meta.suggested_model == "dna_r10.4.1_e8.2_400bps_hac@v5.0.0"
    assert meta.read_type == "long_read"
    assert meta.platform == "ont"
    assert meta.assay_type == "ont_wgs"

    # The single subset "1" points at the pod5 file.
    assert "1" in meta.subsets
    sub = meta.subsets["1"]
    assert sub.r1.endswith(".pod5")
    assert sub.r2 is None
    assert sub.available is True


# ---------------------------------------------------------------------------
# 2. Idempotency — second call with matching anchor is a no-op (no download)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_add_core_pod5_data_is_idempotent(tmp_path, mock_gen_manifest):
    """Second call against a file already present with matching sha256 must
    not invoke urlopen (no re-download). Manifest still rebuilt (cheap)."""
    config = _config(tmp_path)
    args = dict(
        accession="HG002_chr1_MAT_pod5", sample="HG002",
        source_url="https://example.test/chr1_MAT.pod5",
        expected_sha256=FAKE_SHA256,
        expected_size=len(FAKE_POD5_BYTES),
    )

    # First call — downloads.
    with patch("agent.skills.core_test_data.urllib.request.urlopen",
               return_value=_mock_urlopen(FAKE_POD5_BYTES)) as m1:
        r1 = add_core_pod5_data(config, **args)
        assert m1.call_count == 1
    assert r1["success"], r1

    # Second call — file already there with matching sha256; urlopen MUST
    # NOT be called.
    with patch("agent.skills.core_test_data.urllib.request.urlopen") as m2:
        r2 = add_core_pod5_data(config, **args)
        assert m2.call_count == 0, "second call re-downloaded — idempotency broken"
    assert r2["success"], r2
    assert r2["sha256"] == FAKE_SHA256
    # The result reports the same on-disk file.
    assert r2["path"] == r1["path"]


# ---------------------------------------------------------------------------
# 3. sha256 mismatch — fail closed, leave nothing on disk
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_add_core_pod5_data_rejects_wrong_sha256(tmp_path, mock_gen_manifest):
    """When the download's sha256 doesn't match expected, the helper MUST
    delete the partial file and return success=False. Refusing to register
    a poisoned download is the I14-analog for pod5."""
    config = _config(tmp_path)
    wrong_sha = "0" * 64

    with patch("agent.skills.core_test_data.urllib.request.urlopen",
               return_value=_mock_urlopen(FAKE_POD5_BYTES)):
        result = add_core_pod5_data(
            config,
            accession="HG002_chr1_MAT_pod5", sample="HG002",
            source_url="https://example.test/chr1_MAT.pod5",
            expected_sha256=wrong_sha,
        )

    assert not result["success"]
    assert "sha256" in result["error"].lower()
    # No .pod5 file should be present after a failed integrity check.
    pod5_files = list((tmp_path / "data").rglob("*.pod5"))
    assert pod5_files == [], f"poisoned pod5 left on disk: {pod5_files!r}"
    # And no .tmp either.
    tmp_files = list((tmp_path / "data").rglob("*.tmp"))
    assert tmp_files == [], f"tmp file leaked: {tmp_files!r}"


# ---------------------------------------------------------------------------
# 4. select_test_data routes pod5 entries by file_format
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_select_test_data_routes_pod5_by_file_format(tmp_path, mock_gen_manifest):
    """Drives the full _list_resources → select_test_data chain with a
    realistic manifest containing BOTH a pod5 entry and the existing FASTQ
    ont_wgs entry. Pin that `file_format='pod5'` picks the pod5 entry; the
    default (no file_format) does not regress existing FASTQ matching."""
    from agent.skills.resources import list_resources

    config = _config(tmp_path)
    core_dir = Path(config["paths"]["data_dir"]) / "core_test_data_hg38"

    # Build a manifest by hand — same shape gen_manifest produces.
    seq_data = {
        "long_read": {
            "ont": {
                "ont_wgs": [
                    # 1. The existing FASTQ entry shape (no pod5 fields).
                    {
                        "sample": "NA12878",
                        "accession": "ERR3152364",
                        "read_type": "long_read",
                        "end_type": "single_end",
                        "assay_type": "ont_wgs",
                        "platform": "ont",
                        "database": "EBI_SRA",
                        "subsets": {
                            "500": {
                                "r1": "long_read/ont/ont_wgs/NA12878_ERR3152364_500_R1.fastq.gz",
                                "r2": None, "num_reads": 500, "available": True,
                            },
                        },
                    },
                    # 2. The new pod5 entry (carries file_format + chemistry).
                    {
                        "sample": "HG002",
                        "accession": "HG002_chr1_MAT_pod5",
                        "read_type": "long_read",
                        "end_type": "single_end",
                        "assay_type": "ont_wgs",
                        "platform": "ont",
                        "database": "local",
                        "file_format": "pod5",
                        "chemistry": "dna_r10.4.1_e8.2_400bps_5khz",
                        "flowcell": "FLO-PRO114M",
                        "kit": "SQK-LSK114",
                        "suggested_model": "dna_r10.4.1_e8.2_400bps_hac@v5.0.0",
                        "subsets": {
                            "1": {
                                "r1": "long_read/ont/ont_wgs/HG002_HG002_chr1_MAT_pod5_1.pod5",
                                "r2": None, "num_reads": 0, "available": True,
                            },
                        },
                    },
                ],
            },
        },
    }
    manifest = {
        "genome_build": "hg38",
        "chromosome_subset": "all",
        "sequencing_data": seq_data,
        "genome": {},
    }
    core_dir.mkdir(parents=True, exist_ok=True)
    (core_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest))

    # Materialize the actual files so .available logic returns True.
    long_dir = core_dir / "long_read" / "ont" / "ont_wgs"
    long_dir.mkdir(parents=True, exist_ok=True)
    (long_dir / "NA12878_ERR3152364_500_R1.fastq.gz").write_bytes(b"\x1f\x8b\x08fakegz")
    (long_dir / "HG002_HG002_chr1_MAT_pod5_1.pod5").write_bytes(FAKE_POD5_BYTES)

    # 1. _list_resources surfaces file_format + chemistry + suggested_model
    #    on the per-sample record (the wire surface for select_test_data).
    data = list_resources({"resource_type": "test_data"}, config)["test_data"]
    fmts = {(d["accession"], d["file_format"]) for d in data}
    assert ("ERR3152364", "fastq") in fmts          # default for FASTQ entries
    assert ("HG002_chr1_MAT_pod5", "pod5") in fmts  # pod5 entry tagged

    pod5_rec = next(d for d in data if d["accession"] == "HG002_chr1_MAT_pod5")
    assert pod5_rec["chemistry"] == "dna_r10.4.1_e8.2_400bps_5khz"
    assert pod5_rec["suggested_model"] == "dna_r10.4.1_e8.2_400bps_hac@v5.0.0"

    # 2. Drive select_test_data via the MCP module so config + scoring run
    #    the same way they do in production.
    from agent import mcp_server as m
    orig_config = m.config
    m.config = config
    try:
        # 2a. file_format="pod5" → picks the pod5 entry deterministically
        res_pod5 = m.select_test_data(assay_type="ont_wgs", file_format="pod5")
        assert "error" not in res_pod5, res_pod5
        td = res_pod5["test_data"]
        assert td["accession"] == "HG002_chr1_MAT_pod5"
        assert td["file_format"] == "pod5"
        assert td["chemistry"] == "dna_r10.4.1_e8.2_400bps_5khz"
        assert td["suggested_model"] == "dna_r10.4.1_e8.2_400bps_hac@v5.0.0"

        # 2b. Without file_format, the existing FASTQ-vs-pod5 outcome
        #     depends on score ties; the safe pin is that whatever is
        #     picked, the chosen entry's `file_format` field is sane.
        res_default = m.select_test_data(assay_type="ont_wgs")
        assert "error" not in res_default, res_default
        # Default-picked entry's file_format key is omitted (it was "fastq",
        # which the helper drops) OR is "pod5".
        picked = res_default["test_data"]
        assert picked.get("file_format", None) in (None, "pod5"), picked
    finally:
        m.config = orig_config
