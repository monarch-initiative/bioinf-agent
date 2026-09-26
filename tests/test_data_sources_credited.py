"""
Every dataset the bootstrap fetches must carry its terms of use and its credit,
and must have a row in docs/data_sources.md.

The repo redistributes none of the corpus — each user downloads it at setup — but
several sources (ENCODE, 1000 Genomes, GIAB, HPRC, the CC BY nanopore set) ask to
be cited or acknowledged, and a user publishing from a sealed artifact needs to
know which. A dataset added to config/core_datasets.yaml without a `license` and
`citation`, or without a row in the human table, fails here rather than shipping
uncredited.

The pod5 seed is the one dataset committed to the repo, so its bytes are pinned
here too: the file, its provenance sidecar and the config entry must agree on
size and sha256.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
DATASETS = REPO / "config" / "core_datasets.yaml"
DOC = REPO / "docs" / "data_sources.md"

KNOWN_LICENSES = {
    "INSDC-unrestricted", "CC0-1.0", "CC-BY-4.0", "BSD-3-Clause",
    "public-domain", "ENCODE-open",
}


def _entries() -> list[tuple[str, dict]]:
    cfg = yaml.safe_load(DATASETS.read_text())
    out = []
    for group in ("short_read", "long_read", "pod5", "phenopackets"):
        for e in cfg.get(group, []) or []:
            key = e.get("accession") or Path(e["source_url"]).stem
            out.append((f"{group}:{key}", e))
    return out


ENTRIES = _entries()


@pytest.mark.parametrize("label,entry", ENTRIES, ids=[l for l, _ in ENTRIES])
def test_every_dataset_states_license_and_citation(label, entry):
    assert entry.get("license") in KNOWN_LICENSES, (
        f"{label}: `license` missing or not one of {sorted(KNOWN_LICENSES)}; "
        f"add the vocabulary term to config/core_datasets.yaml and this test together")
    assert str(entry.get("citation", "")).strip(), f"{label}: `citation` missing"


@pytest.mark.parametrize("label,entry", ENTRIES, ids=[l for l, _ in ENTRIES])
def test_every_dataset_has_a_row_in_the_data_sources_doc(label, entry):
    doc = DOC.read_text()
    key = entry.get("accession") or Path(entry["source_url"]).stem
    assert key in doc, f"{label}: no row for {key!r} in docs/data_sources.md"


def test_pod5_seed_bytes_match_sidecar_and_config():
    seeds = [e for _, e in ENTRIES if e.get("source_path")]
    assert seeds, "no pod5 entry declares a repo-relative source_path"
    for e in seeds:
        path = REPO / e["source_path"]
        assert path.is_file(), f"seed missing: {e['source_path']}"
        assert not e.get("source_url"), "a seed entry carries source_path, not source_url"
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        assert sha == e["expected_sha256"], f"{path.name}: config expected_sha256 is stale"
        assert path.stat().st_size == e["expected_size"], f"{path.name}: config expected_size is stale"
        sidecar = path.with_suffix("").with_suffix(".provenance.yaml")
        assert sidecar.is_file(), f"provenance sidecar missing next to {path.name}"
        derived = yaml.safe_load(sidecar.read_text())["derived"]
        assert derived["sha256"] == sha, f"{sidecar.name}: sha256 does not match the seed"
        assert derived["size_bytes"] == path.stat().st_size
        assert len(derived["reads"]) >= 1
        ids_file = path.with_suffix("").with_suffix(".read_ids.txt")
        want = sorted(l.strip() for l in ids_file.read_text().splitlines() if l.strip())
        assert sorted(r["read_id"] for r in derived["reads"]) == want


def test_pod5_seed_is_small_enough_to_live_in_git():
    for _, e in ENTRIES:
        if e.get("source_path"):
            assert (REPO / e["source_path"]).stat().st_size < 2_000_000, (
                "a committed seed must stay well under 2 MB; derive fewer reads")
