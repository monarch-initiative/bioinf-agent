"""
Environment freezing — content-addressing + the solve-once cache.

Two notions of identity (this is the "scale" unlock):

  request_key    — what was ASKED for: (tools+versions, platform, accel). Used
                   for cache LOOKUP: "have we already solved samtools=1.21 on
                   linux-64?" Channels drift, so the same request can resolve
                   differently over time — hence it is only a lookup handle.

  content_digest — what was actually GOT: a sha256 over the resolved lock +
                   source commit_shas + binary/artifact sha256s + platform +
                   accel. Identical bytes → identical digest. This is the proof
                   of identity; an adopted/built image digest is the shipping
                   handle on top of it.

The EnvCache maps request_key → {content_digest, image, image_digest, …} in a
JSON file so freeze() can hand back a proven artifact by hash instead of
re-solving — the wall between install-hell and the biology layer that consumes
the env. Everything here is pure / filesystem-only (no network), so it is fully
deterministic and unit-testable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional


def request_key(tools: list[tuple[str, str]], platform: str, accel: str = "none") -> str:
    """Canonical lookup handle for 'what was asked for'. Order-independent."""
    spec = ",".join(f"{n}={v}" if v else n for n, v in sorted(tools))
    return f"{spec}|{platform}|{accel or 'none'}"


def compute_content_digest(parts: dict) -> str:
    """sha256 over a canonicalized identity dict → 'sha256:…'. Stable across
    key order and process runs (json sort_keys)."""
    canon = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode()).hexdigest()


def content_digest_parts(spec: dict) -> dict:
    """Extract the identity-determining parts of a spec/draft. Kept separate
    from the hash so tests (and debugging) can see exactly what feeds the
    digest. Every component is something the runtime captured, not an
    agent assertion: lock_sha256 (conda list --explicit), source commit_shas
    (I11), binary sha256s (I14), authored-artifact sha256s (I9)."""
    pkgs = [p for p in (spec.get("packages") or []) if isinstance(p, dict)]

    def _im(p):
        im = p.get("install_method")
        return im if isinstance(im, dict) else {}

    sources = sorted(
        [[p.get("name", ""), _im(p).get("commit_sha", "")]
         for p in pkgs if _im(p).get("type") == "source"]
    )
    binaries = sorted(
        [[p.get("name", ""), _im(p).get("sha256", "")]
         for p in pkgs if _im(p).get("type") == "binary"]
    )
    artifacts = sorted(
        a.get("sha256", "") for a in (spec.get("authored_artifacts") or [])
        if isinstance(a, dict)
    )
    docker = spec.get("docker") if isinstance(spec.get("docker"), dict) else {}
    accel = spec.get("accelerator") if isinstance(spec.get("accelerator"), dict) else {}
    return {
        "lock":      spec.get("lock_sha256") or "",
        "sources":   sources,
        "binaries":  binaries,
        "artifacts": artifacts,
        "platform":  (docker or {}).get("platform") or "",
        "accel":     (accel or {}).get("type") or "none",
    }


def content_digest_from_spec(spec: dict) -> str:
    """Content digest of an env from its (draft or finalized) spec dict."""
    return compute_content_digest(content_digest_parts(spec))


class EnvCache:
    """Persisted request_key → artifact-record map. The store that makes
    'solve once, pull by digest' real: a cache hit returns the content_digest +
    image without re-solving the env."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def lookup(self, key: str) -> Optional[dict]:
        return self._load().get(key)

    def register(self, key: str, record: dict) -> dict:
        data = self._load()
        data[key] = record
        self._save(data)
        return record

    def all(self) -> dict:
        return self._load()
