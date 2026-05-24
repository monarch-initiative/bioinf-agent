"""
synthesis — the long-tail install tier where the AGENT is the generator and the
CONTRACT is the safety net.

For a tool with no generator/ecosystem home, we do NOT enumerate an install shape.
Instead the flow is two-call, and the runtime is the sole source of facts:

  1. synth_fetch (I/O, in the caller) clones the repo at a ref, captures the
     CONCRETE commit via `git rev-parse` (the agent never supplies it), reads the
     build-relevant files + their sha256s, and returns them + a grounding corpus.
  2. The agent READS that fetched content, SELECTS the authoritative build file,
     and submits commands tagged EXTRACTED (lifted verbatim) or AGENT_AUTHORED
     (composed from the repo's prose).
  3. validate_submission (here, pure) RE-VERIFIES every submission against the
     fetch the runtime did — an EXTRACTED command must actually occur in the file
     it names (checked against the runtime's own fetched bytes, not the agent's
     word), an AGENT_AUTHORED command must be GROUNDED (its URLs/remotes present in
     the repo). Only then does the build run, and the honesty contract still gates
     the result (the tool must run in the shipped image).

So no install shape is pre-known; the agent reads the tool's OWN build instructions,
and provenance + grounding + the contract make open-ended generation safe. This is
the low-level mirror of seal_workflow: compose freely, gate with a finite contract.

Pure core (file ranking, extracted-verification, submission validation); the clone
+ file read live in the caller so this stays unit-testable.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from agent.skills import provenance as _prov

# A shell line-continuation (backslash + newline) is glue, not part of the command;
# fold it so a maintainer's multi-line Dockerfile RUN matches the agent's one-line
# reflow of the same step.
_CONT = re.compile(r"\\\s*\n")

# Authoritative build sources, best first. The earlier the category, the more the
# file IS the repo's own canonical recipe: a Dockerfile is the literal build; an
# install/build script is the maintainer's own command sequence; CI shows exactly
# how upstream builds it; a Makefile/CMakeLists implies the conventional invocation;
# prose (README) is the last resort and the only one that forces authoring.
_BUILD_SOURCES: list[tuple[str, Callable[[str], bool]]] = [
    ("dockerfile",     lambda n: n == "dockerfile" or n.endswith(".dockerfile") or n.endswith("/dockerfile")),
    ("install_script", lambda n: n.rsplit("/", 1)[-1] in
                                 ("install.sh", "build.sh", "setup.sh", "compile.sh", "make.sh", "bootstrap.sh")),
    ("ci_workflow",    lambda n: ".github/workflows/" in n and n.endswith((".yml", ".yaml"))),
    ("make",           lambda n: n.rsplit("/", 1)[-1] in ("makefile", "gnumakefile", "cmakelists.txt")),
    ("python_build",   lambda n: n.rsplit("/", 1)[-1] in ("setup.py", "pyproject.toml")),
    ("readme",         lambda n: n.rsplit("/", 1)[-1].startswith("readme")),
]


def is_build_relevant(path: str) -> bool:
    """Should synth_fetch pull this file? The build sources above PLUS the files
    where install URLs/instructions live (so the grounding corpus is complete).
    Keeps the fetch to the build surface — not the whole tree."""
    n = path.lower()
    return any(pred(n) for _, pred in _BUILD_SOURCES)


def rank_build_sources(paths: list[str]) -> list[dict[str, str]]:
    """Rank the fetched files by how authoritative a build recipe each is, best
    first. The agent uses this to choose what to EXTRACT from (Dockerfile beats
    README). Returns [{category, path}] in priority order, stable within a tier."""
    out: list[dict[str, str]] = []
    for category, pred in _BUILD_SOURCES:
        for p in paths:
            if pred(p.lower()):
                out.append({"category": category, "path": p})
    return out


def build_corpus(files: list[dict]) -> str:
    """The grounding corpus: the concatenated text of every fetched file. An
    AGENT_AUTHORED command's external references must appear somewhere in here."""
    return "\n".join(f.get("text", "") for f in files if isinstance(f, dict))


# Non-git distribution: a tool shipped as a raw release/vendor ARCHIVE rather than a
# clone-able repo (the "no GitHub" case). Detected by file suffix; the agent can
# force the kind via mode=.
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
                     ".tar", ".zip")


def source_kind(url: str, mode: str = "auto") -> str:
    """How synth_fetch should acquire `url`: a git repo ('git' → clone, anchored by
    commit) or a release/vendor ARCHIVE ('archive' → download+extract, anchored by
    sha256). auto = an archive file suffix → 'archive', else 'git' (most sources are
    clone-able repos — and a plain git clone already covers ANY git host, not just
    GitHub). Pass mode='archive' to force a raw tarball/zip whose URL has no
    recognizable suffix. Honors an explicit mode verbatim."""
    if mode in ("git", "archive"):
        return mode
    u = url.lower().split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return "archive" if u.endswith(_ARCHIVE_SUFFIXES) else "git"


def _norm(s: str) -> str:
    """Whitespace-normalize for verbatim comparison — collapses backslash-newline
    continuations and indentation so a copied Dockerfile RUN body matches the file
    regardless of how the agent reflowed it. Reformatting is tolerated; paraphrase
    (any token change) is not."""
    return " ".join(_CONT.sub(" ", s).split())


def verify_extracted(command: str, origin_text: str) -> dict[str, Any]:
    """Does `command` actually occur in `origin_text` (the runtime's fetched bytes
    of the file the agent named)? Verbatim modulo whitespace. Also matches against
    the file's Dockerfile RUN bodies, so 'extracted from Dockerfile' is honored
    when the agent lifts a RUN step. This is the anti-fake check: it rejects a
    command the agent CLAIMS came from the repo but didn't."""
    nc = _norm(command)
    if not nc:
        return {"matched": False, "reason": "empty command"}
    if nc in _norm(origin_text):
        return {"matched": True}
    for run_line in _prov.extract_run_lines(origin_text):
        if nc in _norm(run_line):
            return {"matched": True}
    return {"matched": False,
            "reason": "command does not occur verbatim in the named origin file — "
                      "it is a paraphrase, not an extraction"}


def validate_submission(fetch: dict, commands: list[dict]) -> dict[str, Any]:
    """The gate between the agent's submission and the build. `fetch` is what the
    runtime captured in synth_fetch ({commit, files:[{path,sha256,text}], corpus});
    `commands` is the agent's proposed install sequence, each
    {command, source, origin_file?, evidence?, engine_coupled?, purpose?}.

    Re-verifies every command against the runtime's OWN fetched content:
      EXTRACTED  → must occur verbatim in the named origin_file; provenance stamped
                   with the runtime's sha256 of that file (not the agent's).
      AGENT_AUTHORED → must be grounded against the corpus.
      anything else → rejected (a synthesis command is one or the other).

    Returns {ok, violations, records}; `records` are provenance-tagged longtail
    entries ready for ContainerBuild.run. ok == False ⇒ no build."""
    files = {f["path"]: f for f in fetch.get("files", []) if isinstance(f, dict) and f.get("path")}
    corpus = fetch.get("corpus") or build_corpus(fetch.get("files", []))
    violations: list[dict] = []
    records: list[dict] = []

    for c in commands:
        cmd = c.get("command", "")
        src = c.get("source")
        if src == _prov.EXTRACTED:
            of = c.get("origin_file")
            f = files.get(of)
            if not f:
                violations.append({"command": cmd,
                                   "reason": f"origin_file {of!r} was not in the fetched repo"})
                continue
            ve = verify_extracted(cmd, f.get("text", ""))
            if not ve["matched"]:
                violations.append({"command": cmd, "origin_file": of, "reason": ve["reason"]})
                continue
            prov = _prov.provenance_record(_prov.EXTRACTED, origin_file=of,
                                           origin_sha256=f.get("sha256"), selected_by="agent")
        elif src == _prov.AGENT_AUTHORED:
            g = _prov.ground(cmd, corpus)
            if not g["grounded"]:
                violations.append({"command": cmd, "ungrounded": g["ungrounded"],
                                   "reason": "agent-authored install references a URL/remote not "
                                             "present in the fetched repo — possible hallucination"})
                continue
            prov = _prov.provenance_record(_prov.AGENT_AUTHORED, selected_by="agent")
        else:
            violations.append({"command": cmd,
                               "reason": f"synthesis command source must be {_prov.EXTRACTED!r} or "
                                         f"{_prov.AGENT_AUTHORED!r}, got {src!r}"})
            continue
        rec: dict[str, Any] = {"command": cmd, "provenance": prov}
        for k in ("evidence", "purpose"):
            if c.get(k):
                rec[k] = c[k]
        if c.get("engine_coupled"):
            rec["engine_coupled"] = True
        records.append(rec)

    return {"ok": not violations, "violations": violations, "records": records}
