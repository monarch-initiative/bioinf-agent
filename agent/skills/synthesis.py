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
from typing import Any, Callable, NamedTuple

from agent.skills import provenance as _prov

# A shell line-continuation (backslash + newline) is glue, not part of the command;
# fold it so a maintainer's multi-line Dockerfile RUN matches the agent's one-line
# reflow of the same step.
_CONT = re.compile(r"\\\s*\n")


def _base(n: str) -> str:
    return n.rsplit("/", 1)[-1]


def _ext(n: str) -> str:
    """The final extension of the basename, "" for an extensionless file (INSTALL)."""
    b = _base(n)
    i = b.rfind(".")
    return b[i:] if i > 0 else ""


#: Extensions a human-readable instruction file plausibly has. The gate that keeps
#: `install_notes` from swallowing SOURCE code that merely has "install" in its name
#: (`src/installer.cpp`) while still catching the extensionless `INSTALL`.
_INSTRUCTION_EXTS = ("", ".txt", ".md", ".rst", ".adoc", ".org", ".text", ".in",
                     ".sh", ".bash", ".commands", ".cmd")

#: Declarative dependency lists — not a recipe, but the authoritative statement of WHAT
#: has to be installed, which is most of the job for a repo whose "build" is a conda env.
_DEP_MANIFESTS = ("environment.yml", "environment.yaml", "env.yml", "env.yaml",
                  "conda.yml", "conda.yaml", "conda_env.yml", "conda_env.yaml",
                  "dependencies.txt", "deps.txt", "description")


class BuildSource(NamedTuple):
    """One category of file `synth_fetch` pulls into the grounding corpus.

    `is_what` is the served, human-facing account of the category — carried HERE, beside
    the predicate, so the tool description that lists the corpus is GENERATED from the
    same object that decides what gets fetched. See `catalog` / `catalog_sentence`."""
    category: str
    matches:  Callable[[str], bool]
    is_what:  str


# Authoritative build sources, best first. The earlier the category, the more the
# file IS the repo's own canonical recipe: a Dockerfile is the literal build; an
# install/build script is the maintainer's own command sequence; an INSTALL-style notes
# file is that same sequence written down rather than made executable; CI shows exactly
# how upstream builds it; a Makefile/CMakeLists implies the conventional invocation; a
# dependency manifest states what must be present without saying how; prose (README) is
# the last resort and the only one that forces authoring.
#
# `install_notes` and `dep_manifest` were MISSING until 2026-08-07, and their absence was
# a reachability hole with a docstring over it: this function's own contract promised
# "the build sources PLUS the files where install URLs/instructions live", and there was
# no PLUS. Measured on a real academic repo (S4a): the README says *"install the relevant
# packages provided in the packageInstallCommands.txt file"*, that file holds every conda
# line the tool needs, and the corpus synth_fetch returned was ONE file — the README.
# `synth_build` then refuses the real install commands, correctly and unfixably: an
# `extracted` command must be anchored to a file in `files[]`, and the only file that
# holds them was never fetched. Fails safe, but the whole long tail this tier exists for
# — academic repos whose install steps live in a plain text file — was unreachable.
BUILD_SOURCES: list[BuildSource] = [
    BuildSource("dockerfile",
                lambda n: n == "dockerfile" or n.endswith((".dockerfile", "/dockerfile")),
                "the literal build, best of all"),
    BuildSource("install_script",
                lambda n: _base(n) in ("install.sh", "build.sh", "setup.sh",
                                       "compile.sh", "make.sh", "bootstrap.sh"),
                "the maintainer's own executable command sequence"),
    BuildSource("install_notes",
                lambda n: ("install" in _base(n) and "uninstall" not in _base(n)
                           and _ext(n) in _INSTRUCTION_EXTS),
                "an INSTALL / packageInstallCommands-style file: the same commands "
                "written down rather than made executable"),
    BuildSource("ci_workflow",
                lambda n: ".github/workflows/" in n and n.endswith((".yml", ".yaml")),
                "how upstream really builds it"),
    BuildSource("make",
                lambda n: _base(n) in ("makefile", "gnumakefile", "cmakelists.txt"),
                "the conventional invocation"),
    BuildSource("python_build",
                lambda n: _base(n) in ("setup.py", "pyproject.toml"),
                "setup.py / pyproject"),
    BuildSource("dep_manifest",
                lambda n: (_base(n) in _DEP_MANIFESTS
                           or (_base(n).startswith("requirements") and _base(n).endswith(".txt"))),
                "requirements.txt / environment.yml / DESCRIPTION — WHAT to install, "
                "not how"),
    BuildSource("readme",
                lambda n: _base(n).startswith("readme"),
                "prose, the last resort and the only one that forces authoring"),
]


def catalog() -> list[dict[str, str]]:
    """`[{category, is_what}]` in ranking order — THE account of what the grounding
    corpus reaches for.

    Exists so no second one has to. Every doc that wants to name the categories reads
    this instead of re-typing them, because the hand-typed account is exactly what
    drifted: `is_build_relevant`'s docstring described a category (install instructions)
    that the predicate list did not contain, on the function whose entire job is corpus
    completeness."""
    return [{"category": s.category, "is_what": s.is_what} for s in BUILD_SOURCES]


def catalog_sentence() -> str:
    """The catalog as one served line, best first — substituted into `synth_fetch`'s
    tool description at registration so the CONTRACT the model reads cannot claim a
    category the code lacks (nor omit one it gained)."""
    return " › ".join(f"{s.category} ({s.is_what})" for s in BUILD_SOURCES)


def describes_build_sources(fn):
    """Fill `{BUILD_SOURCES}` in `fn`'s docstring from `catalog_sentence()`.

    Decorate BELOW `@mcp.tool()` so the substitution happens before FastMCP snapshots
    the docstring into the served description. The same move `tool_surface.apply_to`
    makes for routing guardrails, and for the same reason: a generated sentence has no
    second copy on disk to go stale."""
    if fn.__doc__ and "{BUILD_SOURCES}" in fn.__doc__:
        fn.__doc__ = fn.__doc__.replace("{BUILD_SOURCES}", catalog_sentence())
    return fn


def is_build_relevant(path: str) -> bool:
    """Should synth_fetch pull this file? True for every category in `BUILD_SOURCES` —
    call `catalog()` for that list rather than restating it here.

    Keeps the fetch to the build surface, not the whole tree."""
    n = path.lower()
    return any(s.matches(n) for s in BUILD_SOURCES)


def rank_build_sources(paths: list[str]) -> list[dict[str, str]]:
    """Rank the fetched files by how authoritative a build recipe each is, best
    first. The agent uses this to choose what to EXTRACT from (Dockerfile beats
    README). Returns [{category, path}] in priority order, stable within a tier.

    A path is listed ONCE, at its best category — `install.sh` is an install_script and
    would also match install_notes by name, and a file appearing twice in a ranking
    reads as two different files to anyone scanning it."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for s in BUILD_SOURCES:
        for p in paths:
            if p not in seen and s.matches(p.lower()):
                seen.add(p)
                out.append({"category": s.category, "path": p})
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
            if not isinstance(of, str):
                # REFUSE, DO NOT RAISE — and this is the shape our OWN tool hands back.
                # `synth_fetch` returns ranked_sources as [{category, path}], and both synth
                # docstrings say to "pass its path as origin_file". An agent that passes the
                # ELEMENT rather than its `path` used to get `TypeError: unhashable type:
                # 'dict'` out of the line below, which reads as a bug in the runtime rather
                # than a mistake it can correct. A refusal that names the fix is the whole
                # difference.
                violations.append({
                    "command": cmd,
                    "reason": (f"origin_file must be the file's PATH (a string); got "
                               f"{type(of).__name__}. synth_fetch's ranked_sources entries "
                               "are {category, path} dicts — pass the `path` value, not the "
                               "entry."),
                })
                continue
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
