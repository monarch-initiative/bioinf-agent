"""
provenance — make the agent's contribution to a build AUDITABLE, MINIMAL, GROUNDED.

The synthesis tier (the long-tail overhaul) lets the agent compose install commands
for a tool with no generator/ecosystem home. That is the ONE place the agent could
fake things: emit a command, URL, or version from its TRAINING MEMORY instead of
from the tool's actual repository. Every OTHER fact in the artifact is runtime-
captured and the agent cannot touch it — commit SHA from `git rev-parse` after a
real clone, sha256 from hashing the fetched bytes, versions/lock/SBOM read from the
built env, pass/fail from re-running the tool in the shipped image. This module is
the programmatic firewall around the residual the agent must still contribute:

  1. Every captured install command carries a PROVENANCE tag — GENERATOR (our own
     code, trusted by construction), EXTRACTED (lifted VERBATIM from a fetched repo
     file, recorded with that file + its sha256), or AGENT_AUTHORED (the agent
     composed it from the repo's prose).
  2. An AGENT_AUTHORED command must be GROUNDED: every external reference it makes
     (download URL, git remote) must appear in the programmatically-fetched repo
     content. A URL the agent invented — not present in the tool's own files — is a
     grounding violation and the build is refused.

So the agent SELECTS and ORCHESTRATES over fetched ground truth; it is never the
SOURCE of a fact. Registry package names (pip/cargo/cran) are intentionally NOT
grounded here — those route through ecosystem backends (versions cross-checked
against the captured SBOM), not the synthesis tier. Pure (no I/O: the caller
fetches the repo and passes the corpus in); unit-testable.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

GENERATOR = "generator"
EXTRACTED = "extracted"
AGENT_AUTHORED = "agent_authored"
_KINDS = (GENERATOR, EXTRACTED, AGENT_AUTHORED)

# External references an authored command must justify against the repo's own
# files. A fetch URL is the dominant injection vector (pulling bytes from a host
# the tool never mentions); a git SSH remote is the other. Local build verbs
# (make, cd, ./configure) reference nothing external and need no grounding.
_URL_RE = re.compile(r"(?:https?|ftp|git)\+?://[^\s'\"()<>|;]+", re.IGNORECASE)
_GIT_SSH_RE = re.compile(r"git@[\w.\-]+:[\w.\-/]+", re.IGNORECASE)
_TRAIL = ".,);:'\"`"


def provenance_record(source: str, *, origin_file: Optional[str] = None,
                      origin_sha256: Optional[str] = None, span: Optional[list] = None,
                      selected_by: str = "") -> dict[str, Any]:
    """The provenance tag stored alongside a captured install command. `source` is
    one of GENERATOR / EXTRACTED / AGENT_AUTHORED. EXTRACTED records the file the
    command was lifted from + that file's sha256, so a verifier can re-confirm the
    command IS the repo's own instruction (not a paraphrase)."""
    if source not in _KINDS:
        raise ValueError(f"unknown provenance source {source!r}")
    rec: dict[str, Any] = {"source": source}
    if origin_file:
        rec["origin_file"] = origin_file
    if origin_sha256:
        rec["origin_sha256"] = origin_sha256
    if span:
        rec["span"] = list(span)
    if selected_by:
        rec["selected_by"] = selected_by
    return rec


def external_refs(command: str) -> list[str]:
    """The external references (download URLs, git SSH remotes) a command makes —
    the tokens that must trace to the repo's own files. Trailing shell/markdown
    punctuation is stripped so a URL matches its occurrence in prose. Order-stable,
    deduped."""
    refs: list[str] = []
    for rx in (_URL_RE, _GIT_SSH_RE):
        for m in rx.findall(command):
            refs.append(m.rstrip(_TRAIL))
    seen: set[str] = set()
    out: list[str] = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def ground(command: str, corpus: str) -> dict[str, Any]:
    """Is an authored `command` justified by the fetched repo `corpus` (the
    concatenated content of the tool's own files)? Grounded iff every external
    reference it makes appears verbatim in the corpus. A command with no external
    references (a pure build verb like `make`) is trivially grounded."""
    refs = external_refs(command)
    ungrounded = [r for r in refs if r not in corpus]
    return {"grounded": not ungrounded, "refs": refs, "ungrounded": ungrounded}


_RUN_RE = re.compile(r"^\s*RUN\s+(.*)$", re.IGNORECASE)


def extract_run_lines(dockerfile_text: str) -> list[str]:
    """Lift the shell commands from a Dockerfile's RUN directives VERBATIM — the
    EXTRACTED path. When a tool ships a Dockerfile, that file IS the authoritative
    install recipe, so we replay its RUN steps rather than let the agent paraphrase
    them (zero authoring → no grounding needed). Backslash line-continuations are
    preserved verbatim (a single logical command runs identically under `bash -c`);
    the JSON exec form `RUN ["a","b","cmd"]` returns its final element."""
    lines = dockerfile_text.splitlines()
    cmds: list[str] = []
    i = 0
    while i < len(lines):
        m = _RUN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        block = [m.group(1)]
        while block[-1].rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            block.append(lines[i])
        body = "\n".join(block).strip()
        if body.startswith("[") and body.endswith("]"):
            try:
                import json
                parts = json.loads(body)
                body = str(parts[-1]) if parts else ""
            except Exception:
                pass
        if body:
            cmds.append(body)
        i += 1
    return cmds
