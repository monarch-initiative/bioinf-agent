"""
BioContainers / mulled adoption — the conda tier's path to a pre-built,
content-addressed container without building one ourselves.

Every bioconda package is auto-built into a container at quay.io/biocontainers,
and multi-tool combos get a deterministic "mulled-v2" image whose name is a
SHA1 over the sorted package names (the repo) and the sorted versions (the tag).
So freeze() can ADOPT an existing image BY DIGEST instead of building one — free
reproducibility, ecosystem interop, and the image digest becomes the lock.

Adoption succeeds only when the exact version set was pre-built upstream: single
tools almost always have a biocontainer; arbitrary multi-tool combos exist only
if previously requested in the BioContainers multi-package-containers repo.
When there's no match, the caller builds its own image (the fallback path).

`mulled_v2_name` reimplements galaxy.tool_util.deps.mulled.util.v2_image_name
(SHA1-based), validated against canonical oracle vectors in tests — so there is
no galaxy runtime dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from typing import Any, Optional

QUAY_NS = "biocontainers"
_BUILD_NUM_RE = re.compile(r"[-_](\d+)$")


def mulled_v2_name(packages: list[tuple[str, Optional[str]]], image_build: Optional[str] = None) -> str:
    """BioContainers mulled-v2 image name for a set of (name, version) packages.

    Single package → ``name:version`` (or just ``name`` if versionless).
    Multiple       → ``mulled-v2-{sha1(names)}:{sha1(versions)}[-build]``.

    Names are lowercased (conda/quay convention) and sorted before hashing, so
    the result is order-independent. Exact reimplementation of galaxy's
    v2_image_name (SHA1); pinned against oracle vectors in the test-suite.
    """
    targets = sorted(((n.lower(), v) for n, v in packages if n), key=lambda t: t[0])
    if not targets:
        return ""
    if len(targets) == 1:
        name, version = targets[0]
        return f"{name}:{version}" if version else name

    package_hash = hashlib.sha1("\n".join(t[0] for t in targets).encode()).hexdigest()
    versions = [t[1] for t in targets]
    if any(versions):
        version_hash = hashlib.sha1("\n".join(v or "null" for v in versions).encode()).hexdigest()
    else:
        version_hash = ""

    if not image_build:
        build_suffix = ""
    elif version_hash:
        build_suffix = f"-{image_build}"
    else:
        build_suffix = image_build
    suffix = f":{version_hash}{build_suffix}" if (version_hash or build_suffix) else ""
    return f"mulled-v2-{package_hash}{suffix}"


def _build_number(tag_name: str) -> int:
    """Trailing build number used to rank competing builds of the same version:
    'samtools 1.21--h96c455f_1' → 1, '<vhash>-2' → 2. Unknown → -1."""
    m = _BUILD_NUM_RE.search(tag_name or "")
    return int(m.group(1)) if m else -1


def _version_key(tag_name: str) -> tuple:
    """Sort key for biocontainer tags: (version_tuple, build_number).

    Single-package tag shape is '{version}--{recipe-hash}_{build_number}':
      busco 6.0.0--pyhdfd78af_3  → ((6, 0, 0), 3)
      busco 3.0.2--py_13         → ((3, 0, 2), 13)
      samtools 1.21--h96c455f_1  → ((1, 21), 1)

    Mulled-v2 tag shape is 'mulled-v2-<hash>:<vhash>-<build_number>' — the
    version is the hash (opaque), so we fall through to build_number alone:
      mulled-v2-abc-3            → ((), 3)

    The PREVIOUS ranking was build_number alone — that picked busco 3.0.2
    (build 13) over busco 6.0.0 (build 3) when both matched a versionless
    lookup. The BUSCO-stress wrong-version trust violation. Ranking by
    version_tuple FIRST gives "latest version wins"; build_number remains
    the tiebreaker within a version.

    A tag with no parseable version segment sorts BELOW any versioned tag
    (`()` < `(0,)` < `(1,)` …) — safe for mulled-v2 (all candidates share
    that shape, so build_number is what differs)."""
    name = (tag_name or "").strip()
    if "--" in name:
        ver_part, _, _build_part = name.partition("--")
    else:
        # mulled-v2 has no `--` separator; version_hash + build_num are joined by `-`
        ver_part = ""
    nums = re.findall(r"\d+", ver_part)
    ver_tuple = tuple(int(n) for n in nums)
    return (ver_tuple, _build_number(name))


def _quay_tags(repo: str, like: Optional[str] = None, limit: int = 50, timeout: int = 30) -> list[dict]:
    """Active tags for quay.io/biocontainers/{repo}, optionally filtered by a
    `like` substring. Network failures return [] (caller treats as 'no match',
    falling back to build-our-own) rather than raising."""
    url = f"https://quay.io/api/v1/repository/{QUAY_NS}/{repo}/tag/?onlyActiveTags=true&limit={limit}"
    if like:
        url += f"&filter_tag_name=like:{like}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r).get("tags", []) or []
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return []


def resolve_biocontainer(packages: list[tuple[str, Optional[str]]], timeout: int = 30) -> dict[str, Any]:
    """Look up an adoptable BioContainers image for `packages` (list of
    (name, version)) and return its immutable digest reference.

    Returns, on a hit:
        {found: True, repo, tag, image, digest, image_by_digest, candidates}
    where `image_by_digest` (quay.io/biocontainers/{repo}@sha256:…) is the
    adopt-by-digest reference freeze() records as the lock.

    On a miss (no pre-built image for this exact version set):
        {found: False, repo, image_name, reason}
    so the caller knows to build its own image instead.
    """
    pkgs = [(n.lower(), v) for n, v in packages if n]
    if not pkgs:
        return {"found": False, "reason": "no packages"}

    if len(pkgs) == 1:
        name, version = pkgs[0]
        repo = name
        like = version or None
        tag_prefix = f"{version}--" if version else ""
    else:
        full = mulled_v2_name(pkgs)                 # repo[:version_hash]
        repo = full.split(":", 1)[0]                # mulled-v2-<package_hash>
        version_hash = full.split(":", 1)[1] if ":" in full else ""
        like = version_hash or None
        tag_prefix = f"{version_hash}-" if version_hash else ""

    tags = _quay_tags(repo, like=like, timeout=timeout)
    matches = [t for t in tags if t.get("name", "").startswith(tag_prefix)] if tag_prefix else tags
    if not matches:
        return {
            "found": False,
            "repo": repo,
            "image_name": f"{repo}:{like}" if like else repo,
            "reason": "no pre-built biocontainer for this exact version set — build our own",
        }

    best = max(matches, key=lambda t: _version_key(t.get("name", "")))
    tag = best.get("name")
    digest = best.get("manifest_digest")
    return {
        "found": True,
        "repo": repo,
        "tag": tag,
        "image": f"quay.io/{QUAY_NS}/{repo}:{tag}",
        "digest": digest,
        "image_by_digest": f"quay.io/{QUAY_NS}/{repo}@{digest}" if digest else None,
        "candidates": [t.get("name") for t in matches],
    }
