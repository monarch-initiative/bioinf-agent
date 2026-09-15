"""
Drift-lint for REMEDY PROSE — the strings a refusal hands the agent as its next
move ("call stage_apptainer_image(... freeze_request_key=...) first", "pass
binary_in_archive=<path>", "discard_pipeline_draft and redrive").

"The gate is the guide" only holds while the guide's directions are real.
Nothing type-checks a name inside a string: rename a tool or drop a parameter
and the code compiles, the suite stays green, and the refusal starts pointing
at a door that no longer exists — the exact drift class the invariant-registry
and tool-surface lints close for prose TABLES, here closed for prose STRINGS.
(scripts/extract_outcomes.py deliberately harvests codes only, never message
text, so this surface had no lint at all until now.)

Two assertions, both fully mechanical — no hand-maintained allowlist:

  1. Every snake_case name a remedy drops must exist somewhere in agent/ as a
     name the code actually uses (a def, a parameter, a kwarg, an attribute, a
     module, or an exact-string dict key / enum value). A registered tool that
     gets renamed disappears from that vocabulary, so its stale mention fails.
  2. Every `param=` a remedy instructs the caller to pass must be a real
     parameter of the tool the string names (else of the enclosing function or
     a registered tool defined in the same file). Display formatting
     (f"rc={rc}") is distinguished from instruction STRUCTURALLY: an
     interpolated value directly after the `=` marks it as display.

The vocabulary is deliberately TIGHT (no local-variable names): measured at
introduction, all five retired tool names (finalize_pipeline,
save_pipeline_spec, build_docker_image, ...) fall outside it while the live
corpus of 300+ strings has zero residue — falsifiable and green, not vacuous.
test_the_vocabulary_stays_falsifiable keeps it that way.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from agent.skills.tool_surface import REGISTRY

ROOT = Path(__file__).resolve().parents[1]
SWEEP_DIRS = [ROOT / "agent" / "mcp_tools", ROOT / "agent" / "skills",
              ROOT / "agent" / "validators"]
SKIP = {"__init__.py", "outcomes.py"}

HELPERS = {"proven", "refused", "broke", "vanished", "degraded", "loop"}
PROSE_KWARGS = {"error", "hint", "remedy"}
PROSE_DICT_KEYS = {"remedy"}

#: Stands in for any interpolated / non-constant expression inside a harvested
#: string. `param=\x00` is display formatting, never an instruction to check.
MARK = "\x00"

#: snake_case with at least one underscore — the shape of a tool / param /
#: field reference. Single words ("evidence", "type") are handled only by the
#: param= check, where structure disambiguates them.
TOKEN = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

#: Path / filename spans ("large_files/gather_files.sh", "projects_access.yaml")
#: are file references, not code names — stripped before token extraction.
PATHY = re.compile(
    r"[\w.]*/[\w./]*|\b[a-z0-9_]+\.(?:ya?ml|json|sh|py|md|nf|sif|tsv|csv|txt|gz|html)\b")

PARAM_MENTION = re.compile(r"\b([a-z][a-z0-9_]*)=(?!=)")


def _text(node) -> str:
    """Constant content of a string expression; every non-constant part
    (f-string interpolation, variable concat) becomes MARK."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_text(v) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _text(node.left) + _text(node.right)
    return MARK


def _harvest_prose() -> list[dict]:
    """Every remedy-bearing string constant: error=/hint=/remedy= kwargs on
    outcome-helper calls plus "remedy": dict values, with the enclosing
    function's name and parameters (candidates for the param= check)."""
    out: list[dict] = []
    for d in SWEEP_DIRS:
        for path in sorted(d.glob("*.py")):
            if path.name in SKIP:
                continue
            rel = str(path.relative_to(ROOT))
            tree = ast.parse(path.read_text(), filename=str(path))
            stack: list = []

            def enclosing() -> tuple[str, set[str]]:
                for n in reversed(stack):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        a = n.args
                        return n.name, {x.arg for x in
                                        a.posonlyargs + a.args + a.kwonlyargs}
                return "<module>", set()

            def emit(value_node, kind: str):
                text = _text(value_node)
                if not text.replace(MARK, "").strip():
                    return
                fname, fargs = enclosing()
                out.append({"where": f"{rel}:{value_node.lineno}", "file": rel,
                            "kind": kind, "text": text,
                            "func": fname, "func_args": fargs})

            def visit(node):
                stack.append(node)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id in HELPERS:
                    for kw in node.keywords:
                        if kw.arg in PROSE_KWARGS:
                            emit(kw.value, f"{kw.arg}=")
                elif isinstance(node, ast.Dict):
                    for k, v in zip(node.keys, node.values):
                        if isinstance(k, ast.Constant) and k.value in PROSE_DICT_KEYS:
                            emit(v, f'"{k.value}":')
                for child in ast.iter_child_nodes(node):
                    visit(child)
                stack.pop()

            visit(tree)
    return out


def _used_names() -> set[str]:
    """Names the code actually uses, agent/-wide: module stems, def/class
    names, every function's parameters, call-site kwargs, attribute names, and
    string constants that ARE a bare token (dict keys, enum values, patchable-
    key rosters). Deliberately NOT local-variable names (ast.Name) — wide
    enough to swallow stale tool names, and the detector goes vacuous."""
    names: set[str] = set()
    for path in (ROOT / "agent").rglob("*.py"):
        names.add(path.stem)
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
                if hasattr(node, "args"):
                    a = node.args
                    names.update(x.arg for x in a.posonlyargs + a.args + a.kwonlyargs)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if TOKEN.fullmatch(node.value):
                    names.add(node.value)
            elif isinstance(node, ast.keyword) and node.arg:
                names.add(node.arg)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
    return names


def _tool_params() -> dict[str, set[str]]:
    """Signature parameters of every registered tool, harvested from any
    function DEF bearing a registered name (the MCP wrapper and, where one
    exists, its skills twin — e.g. env_manager.install_git_repo)."""
    params: dict[str, set[str]] = {}
    for d in SWEEP_DIRS:
        for path in sorted(d.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.name in REGISTRY:
                    a = node.args
                    params.setdefault(node.name, set()).update(
                        x.arg for x in a.posonlyargs + a.args + a.kwonlyargs)
    return params


def _registry_tools_in_file(rel: str) -> set[str]:
    tree = ast.parse((ROOT / rel).read_text())
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name in REGISTRY}


PROSE = _harvest_prose()
USED = _used_names()
TOOL_PARAMS = _tool_params()
ALL_TOOL_PARAMS = set().union(*TOOL_PARAMS.values()) if TOOL_PARAMS else set()


@pytest.mark.integration
def test_every_name_a_remedy_drops_exists():
    """A snake_case name in a remedy must exist in the code it points at.
    When this fires the remedy is directing the agent at a renamed or removed
    tool/param/field — fix the STRING (or, if the name is legitimately new
    vocabulary, introduce it in code first; prose never leads)."""
    offenders = []
    for p in PROSE:
        for tok in TOKEN.findall(PATHY.sub(" ", p["text"])):
            if tok not in REGISTRY and tok not in USED:
                offenders.append(f'{p["where"]} ({p["kind"]}) names {tok!r} '
                                 "which exists nowhere in agent/")
    assert not offenders, "stale names in remedy prose:\n  " + "\n  ".join(offenders)


@pytest.mark.integration
def test_param_mentions_bind_to_a_real_signature():
    """A `param=` a remedy tells the caller to pass must be a real parameter
    of the tool the string names — else of the enclosing function or of a
    registered tool defined in the same file. `param=<interpolated>` is
    display formatting (f"rc={rc}"), not an instruction, and is exempt; so is
    a bare word that is a parameter of no registered tool ("type=", "local=")."""
    offenders = []
    for p in PROSE:
        text = p["text"]
        named_tools = [t for t in TOKEN.findall(text) if t in REGISTRY]
        for m in PARAM_MENTION.finditer(text):
            param = m.group(1)
            if text[m.end():m.end() + 1] == MARK:
                continue          # display formatting, not an instruction
            if "_" not in param and param not in ALL_TOOL_PARAMS:
                continue          # bare display word (rc=, type=, remote=)
            candidates: set[str] = set(p["func_args"])
            for t in named_tools or _registry_tools_in_file(p["file"]):
                candidates |= TOOL_PARAMS.get(t, set())
            if param not in candidates:
                scope = (f"the named tool(s) {named_tools}" if named_tools
                         else f'{p["func"]}() or any registered tool in {p["file"]}')
                offenders.append(f'{p["where"]} instructs `{param}=` '
                                 f"but {scope} has no such parameter")
    assert not offenders, ("remedy prose instructs a parameter that does not "
                           "exist:\n  " + "\n  ".join(offenders))


@pytest.mark.integration
def test_the_vocabulary_stays_falsifiable():
    """Guards the detector itself. (1) The harvest found a real corpus — an
    AST change that silently empties it would make both lints vacuously green.
    (2) Names of RETIRED tools stay OUTSIDE the vocabulary — if a widening
    (say, adding local-variable names) swallows them, the lint can no longer
    catch the drift class it exists for. If one of these names is ever
    legitimately re-introduced in code, remove it from RETIRED here."""
    assert len(PROSE) >= 50, (
        f"only {len(PROSE)} remedy strings harvested — the harvest itself "
        "has regressed (kwarg shapes changed, or SWEEP_DIRS moved)")
    assert any(t in REGISTRY for p in PROSE for t in TOKEN.findall(p["text"])), \
        "no remedy names any registered tool — the tool-mention check is vacuous"
    RETIRED = ["finalize_pipeline", "save_pipeline_spec", "build_docker_image",
               "this_tool_never_existed"]
    swallowed = [n for n in RETIRED if n in USED or n in REGISTRY]
    assert not swallowed, (
        f"vocabulary now contains retired/nonsense names {swallowed} — it has "
        "grown too wide to detect a stale tool mention")
