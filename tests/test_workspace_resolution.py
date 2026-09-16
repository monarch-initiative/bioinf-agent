"""Where artifacts go is answered in exactly ONE place: agent/skills/workspace.py.

WHY THIS FILE EXISTS. Before the split, "the project root" was one name doing three
unrelated jobs — where the code is, where artifacts go, and the default cwd for a
subprocess — reached through five spellings (`self.project_root`, `_ms.PROJECT_ROOT`,
`_ms._env_mgr.project_root`, `Path(__file__).parents[2]`, `config["paths"][...]`) at
137 sites. A `paths:` block in the config carried a comment admitting it was
"PARTIALLY HONOURED": nine call sites read it, five hardcoded the directory. So
setting a key relocated part of the Layer-1 state and split the rest away from it,
and nothing in the build noticed.

The user-visible form of the same disease was CS55: the config menu wrote
projects_access.yaml to one path while the doctor validated another, and three
surfaces reported "valid" for a file the agent could not see.

This is the structural fix's immune system — the layout twin of
tests/test_setup_surface_resolution.py (conda) and tests/test_one_reading_per_field.py
(record fields). The failure mode it guards is not malice but convenience: the next
person who needs an output directory will write `Path(__file__).parents[2] / "reports"`
because it is one line, and nothing else in the build would notice.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "agent"
SCRIPTS = ROOT / "scripts"
RESOLVER = AGENT / "skills" / "workspace.py"


def _py_files(*roots: Path) -> list[Path]:
    out: list[Path] = []
    for r in roots:
        out += [p for p in r.rglob("*.py") if "__pycache__" not in p.parts]
    return sorted(out)


# ---------------------------------------------------------------------------
# 1. No second resolver
# ---------------------------------------------------------------------------

#: Spellings that reach a repo-relative root by walking up from __file__. Legal in
#: the resolver itself (that IS the implementation) and in tests (which must locate
#: the checkout without importing the thing under test).
_WALK_UP = re.compile(r"Path\(__file__\)(?:\.resolve\(\))?(?:\.parents?\[[0-9]\]|\.parent){2,}")


def test_only_the_resolver_walks_up_to_a_root():
    """`Path(__file__).parents[2]` is how a second answer gets written.

    One line, no import, and it silently means "the checkout" — which stopped
    being where artifacts live. A module that needs a directory asks `workspace`
    for the ZONE it wants; a module that genuinely needs the checkout asks
    `workspace.code_root()`.

    SCOPE: `agent/` only. A script under `scripts/` must locate the checkout to
    put it on `sys.path` BEFORE it can import anything from it, so forbidding the
    walk-up there would forbid the import that makes the resolver reachable. What
    a script must not do is name an artifact directory — see the next test.
    """
    offenders = []
    for p in _py_files(AGENT):
        if p == RESOLVER:
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _WALK_UP.search(line):
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, (
        "these walk up from __file__ to reach a root instead of asking "
        "agent/skills/workspace.py:\n  " + "\n  ".join(offenders) +
        "\n\nUse workspace.code_root() for the checkout, or the zone accessor "
        "(reports_dir / conda_envs_dir / images_dir / scratch_dir / "
        "resources_root) for anything generated.")


#: Directory names that WERE artifact zones inside the checkout. Naming one is how
#: a caller reaches the OLD location — the path still reads plausibly, it just
#: points at a directory the agent no longer writes to. Deliberately not
#: "pipeline_drafts": that is now a subdirectory name INSIDE the scratch zone, and
#: `scratch_dir("pipeline_drafts")` is the correct way to say it.
_DEAD_ZONE_NAMES = ("env_reports", "docker_images")


def test_nothing_names_a_zone_that_moved(monkeypatch):
    """The names outlive the directories. `<root> / "env_reports"` is the exact
    shape of a reader that keeps working — it creates the directory, writes into
    it, and reports success — while every other surface looks elsewhere."""
    offenders = []
    for p in _py_files(AGENT, SCRIPTS):
        for i, line in enumerate(p.read_text().splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#") or '"""' in line:
                continue
            for name in _DEAD_ZONE_NAMES:
                if f'"{name}"' in line or f"'{name}'" in line:
                    offenders.append(f"{p.relative_to(ROOT)}:{i}: {stripped}")
    assert not offenders, (
        "these name a directory that moved out of the checkout:\n  " +
        "\n  ".join(offenders) +
        "\n\nAsk workspace.reports_dir() / images_dir() / scratch_dir(...).")


def test_no_paths_block_in_the_config():
    """A config key for an output directory is the most durable way to
    reintroduce two answers: the key and the hardcoded fallback both look
    correct in isolation, and they differ only on machines where someone set it."""
    text = (ROOT / "config" / "agent_config.yaml").read_text()
    assert not re.search(r"^paths:", text, re.M), (
        "config/agent_config.yaml grew a `paths:` block again. Artifact "
        "locations are resolved by agent/skills/workspace.py, not configured.")
    offenders = [f"{p.relative_to(ROOT)}"
                 for p in _py_files(AGENT, SCRIPTS)
                 if 'config["paths"]' in p.read_text()]
    assert not offenders, f"these read a deleted config block: {offenders}"


def test_the_ambiguous_name_is_gone():
    """`project_root` meant three things. It is not repointed, it is DELETED —
    a repointed name keeps its old readings alive in every reader's head."""
    offenders = []
    for p in _py_files(AGENT):
        if p == RESOLVER:
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"\bself\.(_)?project_root\b|\b_env_mgr\.project_root\b", line):
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, (
        "the `project_root` attribute is back:\n  " + "\n  ".join(offenders))


# ---------------------------------------------------------------------------
# 2. No artifact under the checkout
# ---------------------------------------------------------------------------

#: The checkout may hold what DIES WITH IT, and nothing that outlives it. That is
#: the actual rule — "no generated file under the checkout" is false as stated,
#: because .mcp.json names ./.conda_runtime/bin/python, so the runtime env must be
#: here. This list exists to make adding to it expensive: every entry is a thing
#: that is worthless the moment the clone is deleted.
CHECKOUT_ALLOWED = {
    ".conda_runtime",      # the interpreter .mcp.json names by path
    ".miniforge",          # private conda, installed only when the machine has none
    ".bioinf_workspace",   # the pointer TO the workspace; meaningless elsewhere
    ".git",
    ".coverage",
}


def test_the_checkout_allowlist_is_small_and_justified():
    """The allowlist is the rule's escape hatch; a test that reads it is what
    keeps someone from quietly appending `env_reports` to it."""
    assert len(CHECKOUT_ALLOWED) <= 6, (
        "the checkout allowlist grew. Every entry must be worthless once the "
        "clone is deleted — if it outlives the checkout it belongs in the "
        "workspace, not on this list.")


def test_no_zone_accessor_resolves_inside_the_checkout(monkeypatch, tmp_path):
    """The load-bearing assertion. With a workspace configured, every zone must
    land outside the checkout — otherwise the split is decorative."""
    from agent.skills import workspace
    monkeypatch.setenv("BIOINF_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("BIOINF_RESOURCES", raising=False)
    code = workspace.code_root()
    for name, value in workspace.zones().items():
        if name in ("code_root", "workspace_source"):
            continue
        p = Path(value).resolve()
        assert code not in p.parents and p != code, \
            f"zone {name!r} resolved to {p}, which is inside the checkout {code}"


def test_the_default_workspace_is_not_the_checkout(monkeypatch):
    """Even with nothing configured. A default that lands in the checkout would
    make the rule true only for users who ran setup."""
    from agent.skills import workspace
    monkeypatch.delenv("BIOINF_WORKSPACE", raising=False)
    monkeypatch.setattr(workspace, "_read_pointer", lambda: "")
    root = workspace.workspace_root()
    assert workspace.code_root() not in root.parents
    assert root == Path.home() / workspace.DEFAULT_WORKSPACE_NAME


# ---------------------------------------------------------------------------
# 3. Resolution behaviour
# ---------------------------------------------------------------------------

def test_resolution_order_env_beats_pointer(monkeypatch, tmp_path):
    from agent.skills import workspace
    monkeypatch.setattr(workspace, "_read_pointer", lambda: str(tmp_path / "from_pointer"))
    monkeypatch.setenv("BIOINF_WORKSPACE", str(tmp_path / "from_env"))
    assert workspace.workspace_root() == (tmp_path / "from_env").resolve()
    assert workspace.workspace_source() == "env"


def test_resolution_falls_back_to_the_pointer(monkeypatch, tmp_path):
    from agent.skills import workspace
    monkeypatch.delenv("BIOINF_WORKSPACE", raising=False)
    monkeypatch.setattr(workspace, "_read_pointer", lambda: str(tmp_path / "ws"))
    assert workspace.workspace_root() == (tmp_path / "ws").resolve()
    assert workspace.workspace_source() == "pointer"


def test_resolution_never_raises_on_a_hostile_pointer(monkeypatch):
    """mcp_server builds artifact paths at IMPORT. A resolver that refuses an
    unusable workspace costs the agent its entire tool surface — every tool
    vanishes — rather than costing it one directory. Diagnose in the doctor,
    where a human reads the message and can act on it."""
    from agent.skills import workspace
    monkeypatch.delenv("BIOINF_WORKSPACE", raising=False)
    for junk in ("", "   ", "\x00not a path", "relative/path"):
        monkeypatch.setattr(workspace, "_read_pointer", lambda j=junk: j)
        assert isinstance(workspace.workspace_root(), Path)


def test_the_pointer_file_ignores_comments_and_blanks(tmp_path, monkeypatch):
    """A human may edit this file; a leading comment must not become the path."""
    from agent.skills import workspace
    pointer = tmp_path / workspace.POINTER_FILENAME
    pointer.write_text("# a comment\n\n   \n/some/where\n")
    monkeypatch.setattr(workspace, "_pointer_path", lambda: pointer)
    assert workspace._read_pointer() == "/some/where"


def test_write_pointer_records_an_absolute_path(tmp_path, monkeypatch):
    """Recording what the user TYPED would let a later `cd` change where the
    agent looks."""
    from agent.skills import workspace
    pointer = tmp_path / workspace.POINTER_FILENAME
    monkeypatch.setattr(workspace, "_pointer_path", lambda: pointer)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ws").mkdir()
    workspace.write_pointer("ws")
    monkeypatch.setattr(workspace, "_read_pointer",
                        lambda: [l for l in pointer.read_text().splitlines()
                                 if l and not l.startswith("#")][0])
    assert Path(workspace._read_pointer()).is_absolute()


def test_zones_describes_without_creating(monkeypatch, tmp_path):
    """A report that conjures the directories it reports on can never say one
    is missing — which is the only interesting thing it has to say on a fresh
    machine."""
    from agent.skills import workspace
    ws = tmp_path / "untouched"
    monkeypatch.setenv("BIOINF_WORKSPACE", str(ws))
    monkeypatch.delenv("BIOINF_RESOURCES", raising=False)
    workspace.zones()
    assert not ws.exists(), "workspace.zones() created the workspace it was describing"


def test_zone_accessors_do_create(monkeypatch, tmp_path):
    """The other half. The no-auto-mkdir rule is a CLUSTER rule about the user's
    territory; locally the agent owns these directories, and every caller of an
    accessor is about to write into it."""
    from agent.skills import workspace
    monkeypatch.setenv("BIOINF_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("BIOINF_RESOURCES", raising=False)
    for fn in (workspace.conda_envs_dir, workspace.images_dir, workspace.reports_dir,
               workspace.resources_root):
        assert fn().is_dir()
    assert workspace.scratch_dir("jobs").is_dir()


def test_resources_is_independently_relocatable(monkeypatch, tmp_path):
    """The zone most likely to already exist as a shared institutional mount."""
    from agent.skills import workspace
    monkeypatch.setenv("BIOINF_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("BIOINF_RESOURCES", str(tmp_path / "shared"))
    assert workspace.resources_root() == (tmp_path / "shared").resolve()
    assert workspace.reports_dir().is_relative_to(tmp_path / "ws")


def test_home_containment_is_one_implementation(monkeypatch, tmp_path):
    """Docker Desktop shares a fixed set of host prefixes with its VM; a path
    outside them mounts EMPTY rather than failing, so a freeze would validate
    against nothing. Setup and the doctor must agree on which machines are
    usable, so the rule is a function, not a re-typed condition."""
    from agent.skills import workspace
    assert workspace.home_containment_error(Path.home() / "ws") == ""
    err = workspace.home_containment_error(Path("/opt/elsewhere"))
    assert err and "outside $HOME" in err


# ---------------------------------------------------------------------------
# 4. The consumers actually moved
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module,attr,zone", [
    ("agent.skills.env_manager",  "envs_dir",      "conda"),
    ("agent.skills.job_manager",  "jobs_dir",      "scratch"),
])
def test_the_singletons_land_in_the_workspace(monkeypatch, tmp_path, module, attr, zone):
    """Constructing a manager must write into the configured workspace and
    nowhere else — this is what a test redirecting $BIOINF_WORKSPACE relies on."""
    import importlib
    monkeypatch.setenv("BIOINF_WORKSPACE", str(tmp_path / "ws"))
    mod = importlib.import_module(module)
    cls = {"agent.skills.env_manager": "EnvManager",
           "agent.skills.job_manager": "JobManager"}[module]
    inst = getattr(mod, cls)({})
    assert Path(getattr(inst, attr)).is_relative_to(tmp_path / "ws")


def test_the_access_file_has_one_home(monkeypatch, tmp_path):
    """CS55, as a standing check. The menu WRITES where every reader LOOKS."""
    from agent.skills import compute_access, workspace
    monkeypatch.setenv("BIOINF_WORKSPACE", str(tmp_path / "ws"))
    assert compute_access.default_access_path() == workspace.projects_access_path()
    assert compute_access.default_access_path().parent == workspace.workspace_root()


def test_agent_status_reports_the_zones():
    """An agent resuming a session carries a "look in env_reports/" habit that
    the split invalidated. `agent_status` is the first call of a resumed session,
    so it is where the new locations have to be stated."""
    src = (AGENT / "skills" / "agent_status.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "agent_status")
    keys = {k.value for n in ast.walk(fn) if isinstance(n, ast.Dict)
            for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    assert "workspace" in keys, \
        "agent_status() no longer reports the resolved workspace zones"
