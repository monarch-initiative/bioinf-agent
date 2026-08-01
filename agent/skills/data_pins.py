"""
data_pins — is the data this PRODUCTION run binds the data the workflow was
sealed against?

THE HOLE THIS CLOSES. `run_production_pipeline` re-anchors exactly one pin: the
ENV, by digest, via `EnvCache.lookup_verified`. The DATA was entirely unpinned.
A workflow validated against gencode.v44 could be production-run against v39 —
or against a path that does not exist at all — and the command rendered,
launched, and reported identically. The env half of the promise was enforced by
a content digest; the data half was not enforced at all.

That is the exact data analog of the requested-vs-installed TOOL divergence the
env report already flags (`env_report_helpers.version_divergences`), and this
module is deliberately shaped like it, including its conservatism:

    "a report crying a false mismatch is itself a lie"

so a divergence is reported ONLY when a recorded anchor and a real observation
both exist and differ. Everything else is `unverified` — which is a THIRD state,
not a quiet pass, and the caller must render it as such.

TWO THINGS THIS GETS RIGHT THAT THE OBVIOUS IMPLEMENTATION GETS WRONG. Both
were found by checking the naive version against the repo's own sealed
artifacts, and both would have shipped a check that cries wolf on real work:

1. THE DIRECTION IS BOUND-INPUT → SEALED-ANCHOR, never the reverse. Asking "is
   every sealed reference still bound?" false-positives immediately: in
   `rnaseq_deseq2_chr22.workflow.yaml` the single `reference_databases` pin
   (gencode.v44.basic.annotation.gtf.gz) is consumed by NO step — step 4 used a
   different, chr22-subset GTF. A sealed pin that this run does not use is not a
   divergence, it is a pin this run does not use.

2. THE UNIVERSE IS EVERY EXTERNAL SOURCE, not `reference_databases`. On that
   same workflow all 16 content anchors live in `authored_artifacts`, and
   `reference_databases` holds the one pin nothing consumed. Seal already knows
   this — its composition walk unions test_data, reference_databases,
   runtime_configs and authored_artifacts — so scoping a data check to
   references alone would miss almost every real pin. We take the same union.

LOCUS. A cluster production run binds CLUSTER paths; a local `stat` reports
every one of them missing. Observation is therefore locus-aware, and on the
cluster it is deliberately CHEAP: existence via one batched ssh probe, content
only from a `<path>.source.sha256` sidecar when one exists. We do not
`sha256sum` a multi-hundred-GB reference on a head node to satisfy a preflight —
that is exactly the rudeness the project forbids elsewhere, and the honest
alternative is to say `unverified` out loud.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping, Optional

from agent.models import core_data as _core_data

#: Above this we do not read a whole file to hash it. Same value as
#: `spec_writer._check_reference_database_availability`'s cap and
#: `core_data.ANCHOR_HASH_CAP_BYTES` — an artifact size-anchored by one of them must be
#: size-anchored by all of them, or two checks disagree merely because one gave up
#: sooner. Pinned by test_test_data_is_pinned.test_the_hash_cap_is_the_same_number_everywhere.
HASH_CAP_BYTES = 2 * 1024 ** 3

MATCH = "match"
DIVERGED = "diverged"
UNANCHORED = "unanchored"
UNVERIFIED = "unverified"

VERIFIED = "verified"
NOT_ATTEMPTED = "not_attempted"


def _sha256_file(path: Path) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def sealed_anchors(spec: Mapping) -> dict[str, dict]:
    """Every externally-sourced artifact a sealed WorkflowSpec pins, keyed by
    absolute path → {source, name, sha256, size_bytes, locus}.

    The union is the same one seal's composition walk uses. `sha256`/`size_bytes`
    are whatever the artifact actually recorded — frequently nothing, which is
    why the comparison must treat an absent anchor as uncomparable rather than
    as a mismatch."""
    out: dict[str, dict] = {}

    def _add(path, *, source, name="", sha256="", size_bytes=None, locus=""):
        if not path or not isinstance(path, str):
            return
        out[path] = {"source": source, "name": name or Path(path).name,
                     "sha256": sha256 or "", "size_bytes": size_bytes,
                     "locus": locus or ""}

    # test_data. Read through `core_data.test_data_paths` + `resolve_data_path` — this
    # loop used to take any value STARTING WITH "/", and `select_test_data` builds its
    # paths from the manifest's `core_dir`, which is relative in the shipped config. On
    # both sealed specs that carry test_data it therefore matched nothing: the check
    # reported 17 anchors over a workflow whose actual sequencing inputs it could not
    # see. The anchors written at selection are what makes these comparable at all.
    td = spec.get("test_data")
    anchors = _core_data.test_data_anchors(td)
    for key, raw in _core_data.test_data_paths(td).items():
        rec = anchors.get(key) or {}
        _add(str(_core_data.resolve_data_path(raw)), source="test_data", name=str(key),
             sha256=rec.get("sha256") or "", size_bytes=rec.get("size_bytes"))

    for rdb in spec.get("reference_databases") or []:
        if isinstance(rdb, Mapping):
            _add(rdb.get("local_path"), source="reference_databases",
                 name=rdb.get("name") or "", sha256=rdb.get("sha256") or "",
                 size_bytes=rdb.get("size_bytes"), locus=rdb.get("locus") or "")

    for cfg in spec.get("runtime_configs") or []:
        if isinstance(cfg, Mapping):
            _add(cfg.get("path"), source="runtime_configs",
                 name=cfg.get("name") or "", sha256=cfg.get("sha256") or "")

    for art in spec.get("authored_artifacts") or []:
        if isinstance(art, Mapping):
            _add(art.get("path"), source="authored_artifacts",
                 name=art.get("role") or "", sha256=art.get("sha256") or "")

    return out


def _anchor_locus(anchor: Mapping) -> str:
    """`local` unless the artifact says otherwise. Only reference_databases carry
    a locus today; everything else is a path on the machine that recorded it."""
    return (anchor.get("locus") or "local").strip().lower() or "local"


def _same_namespace(anchor: Mapping, observing: str) -> bool:
    """An absolute path only means something at the locus that recorded it.

    This guard is not hypothetical: three of the five sealed workflows on disk
    pin `locus: cluster` references, and without it a local observer stats a
    cluster path, finds nothing, and reports DIVERGED on four artifacts that are
    sitting exactly where they were sealed. A check that cries wolf on real work
    gets switched off, which costs more than never having written it."""
    a = _anchor_locus(anchor)
    o = (observing or "local").strip().lower()
    o = "local" if o in ("", "host", "container") else o
    return a == o


def _classify_local(bound_path: str, anchor: Optional[dict]) -> dict:
    p = Path(bound_path)
    exists = p.is_file() or p.is_dir()
    row = {"path": bound_path, "exists": exists,
           "sealed_as": (anchor or {}).get("name") or None,
           "sealed_source": (anchor or {}).get("source") or None,
           "sealed_sha256": (anchor or {}).get("sha256") or None,
           "observed_sha256": None, "verdict": UNVERIFIED, "reason": ""}

    if anchor is None:
        row["verdict"] = UNANCHORED
        row["reason"] = ("this path is not among the artifacts the sealed workflow "
                         "recorded — the run may be correct, but nothing pins it")
        return row
    if not _same_namespace(anchor, "local"):
        row["exists"] = None
        row["reason"] = (f"the sealed workflow recorded this artifact at the "
                         f"{_anchor_locus(anchor)} locus; a local check is looking in "
                         f"a different namespace and cannot speak to it")
        return row
    if not exists:
        row["verdict"] = DIVERGED
        row["reason"] = "the sealed workflow pins this path, but it is not on disk"
        return row

    recorded = anchor.get("sha256") or ""
    if not recorded:
        row["reason"] = ("the sealed workflow recorded no content hash for this "
                         "artifact, so its bytes cannot be compared")
        return row
    if p.is_dir():
        row["reason"] = "a directory has no single content hash to compare"
        return row
    try:
        size = p.stat().st_size
    except OSError:
        size = None
    if size is not None and size > HASH_CAP_BYTES:
        row["reason"] = (f"{size} bytes is above the {HASH_CAP_BYTES}-byte hash cap; "
                         f"seal size-anchored this artifact too")
        recorded_size = anchor.get("size_bytes")
        if isinstance(recorded_size, int) and recorded_size > 0 and size != recorded_size:
            row["verdict"] = DIVERGED
            row["reason"] = (f"size changed since seal: recorded {recorded_size} bytes, "
                             f"on disk {size}")
        return row

    observed = _sha256_file(p)
    row["observed_sha256"] = observed
    if not observed:
        row["reason"] = "the file could not be read to hash it"
        return row
    if observed == recorded:
        row["verdict"] = MATCH
        row["reason"] = "content matches the sealed pin"
    else:
        row["verdict"] = DIVERGED
        row["reason"] = ("content differs from what the workflow was sealed against "
                         "— this is a different artifact under the same path")
    return row


def _classify_cluster(bound_path: str, anchor: Optional[dict],
                      present: Optional[bool],
                      observed_sha: str = "") -> dict:
    """Cluster paths live in a different namespace and cannot be stat-ed locally.

    Content is NEVER hashed by us here: running `sha256sum` over a hundreds-of-GB
    reference on a head node is precisely the rudeness this project forbids. What
    we can compare for free is the `<path>.source.sha256` SIDECAR, which the
    cluster download path writes and which `_probe_cluster_path` reads with a
    `head -n1`. That turns the cluster check from existence-only — which the
    review called near-vacuous, correctly — into a real content comparison, at
    the cost of one cheap read.

    Its limit is stated rather than hidden: the sidecar is an assertion made when
    the artifact was fetched, not a hash of the bytes as they are now. It catches
    the artifact being REPLACED by a process that maintained it; it cannot catch
    bytes rotting underneath a stale sidecar. Absent sidecar → `unverified`."""
    row = {"path": bound_path, "exists": present,
           "sealed_as": (anchor or {}).get("name") or None,
           "sealed_source": (anchor or {}).get("source") or None,
           "sealed_sha256": (anchor or {}).get("sha256") or None,
           "observed_sha256": None, "verdict": UNVERIFIED, "reason": ""}
    if anchor is None:
        row["verdict"] = UNANCHORED
        row["reason"] = ("this path is not among the artifacts the sealed workflow "
                         "recorded — the run may be correct, but nothing pins it")
        return row
    if not _same_namespace(anchor, "cluster"):
        row["exists"] = None
        row["reason"] = (f"the sealed workflow recorded this artifact at the "
                         f"{_anchor_locus(anchor)} locus; a cluster check is looking in "
                         f"a different namespace and cannot speak to it")
        return row
    if present is False:
        row["verdict"] = DIVERGED
        row["reason"] = "the sealed workflow pins this path, but it is not on the cluster"
        return row

    recorded = anchor.get("sha256") or ""
    row["observed_sha256"] = observed_sha or None
    if not recorded:
        row["reason"] = ("the sealed workflow recorded no content hash for this "
                         "artifact, so its bytes cannot be compared")
    elif not observed_sha:
        row["reason"] = ("no <path>.source.sha256 sidecar on the cluster to compare "
                         "against, and hashing the artifact on the head node is not "
                         "an acceptable preflight cost")
    elif observed_sha == recorded:
        row["verdict"] = MATCH
        row["reason"] = "the cluster sidecar hash matches the sealed pin"
    else:
        row["verdict"] = DIVERGED
        row["reason"] = ("the cluster sidecar hash differs from what the workflow was "
                         "sealed against — the artifact at this path was replaced")
    return row


def check_bound_inputs(spec: Mapping, inputs: Mapping[str, str], *,
                       locus: str = "local",
                       remote_presence: Optional[Mapping[str, bool]] = None,
                       remote_sha256: Optional[Mapping[str, str]] = None) -> dict:
    """Compare what this run BINDS against what the workflow was SEALED against.

    Returns {status, findings[], counts}. `status` is `verified` only when every
    bound input matched a sealed anchor by content — anything else is reported
    for what it is, never rounded up."""
    anchors = sealed_anchors(spec)
    findings = []
    for slot, path in (inputs or {}).items():
        anchor = anchors.get(path)
        if locus == "cluster":
            row = _classify_cluster(path, anchor,
                                    (remote_presence or {}).get(path),
                                    (remote_sha256 or {}).get(path, ""))
        else:
            row = _classify_local(path, anchor)
        row["slot"] = slot
        findings.append(row)

    counts = {v: sum(1 for f in findings if f["verdict"] == v)
              for v in (MATCH, DIVERGED, UNANCHORED, UNVERIFIED)}
    if counts[DIVERGED]:
        status = DIVERGED
    elif not findings:
        status = NOT_ATTEMPTED
    elif counts[MATCH] == len(findings):
        status = VERIFIED
    else:
        status = UNVERIFIED
    return {"status": status, "findings": findings, "counts": counts,
            "sealed_anchor_count": len(anchors), "locus": locus}


def summarize(check: Mapping) -> str:
    """One line a human can act on."""
    c = check.get("counts") or {}
    status = check.get("status")
    if status == NOT_ATTEMPTED:
        return "no sealed workflow was named, so nothing pins this run's data"
    parts = [f"{c.get(MATCH, 0)} matched"]
    if c.get(DIVERGED):
        parts.append(f"{c[DIVERGED]} DIVERGED")
    if c.get(UNANCHORED):
        parts.append(f"{c[UNANCHORED]} not pinned by the sealed workflow")
    if c.get(UNVERIFIED):
        parts.append(f"{c[UNVERIFIED]} could not be compared")
    return ", ".join(parts)
