#!/usr/bin/env python3
"""
derive_pod5_seed.py — build (or verify) the committed nanopore pod5 seed.

The core corpus needs one small raw-signal file so basecaller pipelines (dorado,
bonito, remora) have a real input. Public pod5 comes almost entirely from Oxford
Nanopore's own releases, which carry a non-commercial licence, so the seed is
instead DERIVED from a public-domain source and committed to the repo:

    source   HPRC bucket (CC0 1.0), UCSC HG002 R10.4.1 sheared run PAM69552
             s3://human-pangenomics/submissions/3b9be4f8-…--UCSC_HG002_R1041_nanopore/
             HG002_Sheared_R1041/10_4_22_R1041_HG002_1B_StandardSpeed.fast5.tar
    member   one multi-read fast5 (4000 reads) fetched by HTTP byte range — the
             tar is uncompressed, so a single member is addressable without the
             500 GB archive
    derive   pod5 convert fast5  →  pod5 filter to 5 read ids (the five lowest
             UUIDs among reads with 20k–60k signal samples)
    seed     config/seed_data/pod5/HG002_PAM69552_R1041_5reads.pod5  (+ sidecar)

Usage:
    python scripts/derive_pod5_seed.py --check       # verify the committed seed against
                                                     # its sidecar and config (no network)
    python scripts/derive_pod5_seed.py --rederive    # rebuild from source (~320 MB fetch;
                                                     # needs `pip install pod5`)

A pod5 file embeds a fresh file identifier on every write, so two derivations are
never byte-identical. Identity is therefore pinned two ways: the committed BYTES by
sha256 (what setup verifies), and the CONTENT by a digest over (read_id, signal) that
any re-derivation must reproduce. `--rederive` refuses to overwrite the seed when the
content digest changes.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import date
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
SEED_DIR = REPO / "config" / "seed_data" / "pod5"
SEED = SEED_DIR / "HG002_PAM69552_R1041_5reads.pod5"
READ_IDS = SEED_DIR / "HG002_PAM69552_R1041_5reads.read_ids.txt"
SIDECAR = SEED_DIR / "HG002_PAM69552_R1041_5reads.provenance.yaml"
DATASETS = REPO / "config" / "core_datasets.yaml"

SOURCE = {
    "bucket": "human-pangenomics",
    "bucket_license": "CC0-1.0",
    "bucket_registry": "https://registry.opendata.aws/hpgp-data/",
    "url": ("https://s3-us-west-2.amazonaws.com/human-pangenomics/submissions/"
            "3b9be4f8-a269-4f89-ab9c-a0a0b0a10af6--UCSC_HG002_R1041_nanopore/"
            "HG002_Sheared_R1041/10_4_22_R1041_HG002_1B_StandardSpeed.fast5.tar"),
    "member": "PAM69552_009474b2_72.fast5",
    "member_size": 316321328,
    "member_sha256": "f8c21b3e3c620f5e5677a39c91f576dc82dc0f34577c6180a8a95ab4070fe256",
    "sample": "HG002 (NA24385, GIAB Ashkenazim son; Personal Genome Project consent)",
    "run": {
        "flow_cell_id": "PAM69552",
        "flow_cell_product_code": "FLO-PRO114M",
        "sequencing_kit": "SQK-LSK114",
        "sample_rate_hz": 4000,
        "translocation_speed_bps": 400,
        "protocol_run_id": "b2e111a3-c323-4f42-9e1b-55c589f20302",
        "acquisition_id": "009474b226a8c0821dfedd708a320779a88dc0aa",
        "acquisition_start_time": "2022-10-04T19:21:18+00:00",
        "device": "PromethION (PRO-PRC048)",
    },
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_digest(pod5_path: Path) -> tuple[str, list[dict]]:
    """sha256 over (read_id, raw signal bytes) in read_id order — stable across
    re-derivations, unlike the file bytes. Returns the digest and a per-read summary."""
    import pod5  # only needed here and in --rederive

    h = hashlib.sha256()
    reads = []
    with pod5.Reader(pod5_path) as r:
        for rd in sorted(r.reads(), key=lambda x: str(x.read_id)):
            h.update(str(rd.read_id).encode())
            h.update(rd.signal.tobytes())
            reads.append({"read_id": str(rd.read_id),
                          "num_samples": int(rd.num_samples),
                          "channel": int(rd.pore.channel)})
    return h.hexdigest(), reads


def _range(url: str, start: int, size: int):
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{start + size - 1}"})
    return urllib.request.urlopen(req, timeout=120)


def locate_member(url: str, member: str) -> tuple[int, int]:
    """Walk tar headers over HTTP until `member` is found. Returns (data_offset, size).
    Handles GNU LongLink ('L') headers, which this archive uses for its deep paths."""
    off = 0
    pending: str | None = None
    while True:
        with _range(url, off, 512) as resp:
            hdr = resp.read()
        name = hdr[0:100].split(b"\0")[0].decode()
        if not name:
            raise SystemExit(f"member {member!r} not found in {url}")
        size = int(hdr[124:136].split(b"\0")[0].strip() or b"0", 8)
        typ = hdr[156:157]
        data_off = off + 512
        if typ == b"L":
            with _range(url, data_off, size) as resp:
                pending = resp.read().split(b"\0")[0].decode()
        else:
            full = pending or name
            pending = None
            if typ == b"0" and full.rsplit("/", 1)[-1] == member:
                return data_off, size
        off = data_off + ((size + 511) // 512) * 512


def fetch_member(url: str, member: str, expect_size: int, expect_sha: str, dst: Path) -> None:
    data_off, size = locate_member(url, member)
    if size != expect_size:
        raise SystemExit(f"{member}: size {size} != expected {expect_size}")
    h = hashlib.sha256()
    with _range(url, data_off, size) as resp, dst.open("wb") as out:
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            out.write(chunk)
            h.update(chunk)
    if h.hexdigest() != expect_sha:
        raise SystemExit(f"{member}: sha256 {h.hexdigest()} != expected {expect_sha}")


def need_pod5_cli() -> str:
    exe = shutil.which("pod5")
    if not exe:
        raise SystemExit("the `pod5` CLI is required to re-derive: pip install pod5")
    return exe


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------
def rederive(write: bool) -> int:
    pod5_cli = need_pod5_cli()
    read_ids = [l.strip() for l in READ_IDS.read_text().splitlines() if l.strip()]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        fast5 = tmp / SOURCE["member"]
        print(f"fetching {SOURCE['member']} ({SOURCE['member_size'] / 1e6:.0f} MB) by byte range …")
        fetch_member(SOURCE["url"], SOURCE["member"], SOURCE["member_size"],
                     SOURCE["member_sha256"], fast5)
        full = tmp / "full.pod5"
        subprocess.run([pod5_cli, "convert", "fast5", str(fast5), "-o", str(full)],
                       check=True, capture_output=True)
        out = tmp / SEED.name
        subprocess.run([pod5_cli, "filter", str(full), "--ids", str(READ_IDS), "--output", str(out)],
                       check=True, capture_output=True)
        digest, reads = content_digest(out)
        got_ids = [r["read_id"] for r in reads]
        if got_ids != sorted(read_ids):
            raise SystemExit(f"read ids differ from {READ_IDS.name}: {got_ids}")

        if SIDECAR.exists():
            prior = yaml.safe_load(SIDECAR.read_text())["derived"]["content_sha256"]
            if prior != digest:
                raise SystemExit(f"content digest changed: {prior} → {digest}; refusing to overwrite")
            print(f"content digest reproduced: {digest}")

        if not write:
            print("dry run — seed not written (pass --write to replace it)")
            return 0
        shutil.copyfile(out, SEED)
        versions = subprocess.run([pod5_cli, "--version"], capture_output=True, text=True).stdout.strip()
        SIDECAR.write_text(yaml.safe_dump({
            "seed": SEED.name,
            "derived_on": date.today().isoformat(),
            "derived_by": "scripts/derive_pod5_seed.py --rederive --write",
            "tool": versions,
            "source": SOURCE,
            "selection": {
                "rule": "five lowest read_id UUIDs among reads with 20000–60000 signal samples",
                "read_ids_file": READ_IDS.name,
            },
            "derived": {
                "size_bytes": SEED.stat().st_size,
                "sha256": sha256_path(SEED),
                "content_sha256": digest,
                "reads": reads,
            },
        }, sort_keys=False))
        print(f"wrote {SEED.relative_to(REPO)} and {SIDECAR.name}")
        return 0


def check() -> int:
    problems: list[str] = []
    side = yaml.safe_load(SIDECAR.read_text())["derived"]
    if SEED.stat().st_size != side["size_bytes"]:
        problems.append(f"size {SEED.stat().st_size} != sidecar {side['size_bytes']}")
    actual = sha256_path(SEED)
    if actual != side["sha256"]:
        problems.append(f"sha256 {actual} != sidecar {side['sha256']}")
    cfg = yaml.safe_load(DATASETS.read_text())
    entry = next((e for e in cfg.get("pod5", [])
                  if e.get("source_path") == str(SEED.relative_to(REPO))), None)
    if entry is None:
        problems.append(f"no pod5 entry in {DATASETS.name} names source_path {SEED.relative_to(REPO)}")
    else:
        if entry.get("expected_sha256") != actual:
            problems.append(f"config expected_sha256 {entry.get('expected_sha256')} != file {actual}")
        if entry.get("expected_size") != SEED.stat().st_size:
            problems.append(f"config expected_size {entry.get('expected_size')} != file {SEED.stat().st_size}")
    try:
        digest, reads = content_digest(SEED)
    except ImportError:
        print("pod5 package not installed — bytes verified, content digest skipped")
    else:
        if digest != side["content_sha256"]:
            problems.append(f"content digest {digest} != sidecar {side['content_sha256']}")
        want = sorted(l.strip() for l in READ_IDS.read_text().splitlines() if l.strip())
        if [r["read_id"] for r in reads] != want:
            problems.append("read ids in seed differ from the read_ids file")
    for p in problems:
        print("FAIL:", p)
    if not problems:
        print(f"OK: {SEED.relative_to(REPO)} matches sidecar and config ({actual[:12]}…)")
    return 1 if problems else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify the committed seed")
    mode.add_argument("--rederive", action="store_true", help="rebuild from the CC0 source")
    ap.add_argument("--write", action="store_true", help="with --rederive: replace the seed + sidecar")
    args = ap.parse_args()
    sys.exit(check() if args.check else rederive(write=args.write))


if __name__ == "__main__":
    main()
