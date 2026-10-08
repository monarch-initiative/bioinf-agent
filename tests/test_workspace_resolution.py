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
        "(environments_dir / env_dir / conda_envs_dir / scratch_dir / "
        "resources_root) for anything generated.")


#: Directory names that WERE artifact zones inside the checkout. Naming one is how
#: a caller reaches the OLD location — the path still reads plausibly, it just
#: points at a directory the agent no longer writes to. Deliberately not
#: "pipeline_drafts": that is now a subdirectory name INSIDE the scratch zone, and
#: `scratch_dir("pipeline_drafts")` is the correct way to say it.
_DEAD_ZONE_NAMES = ("env_reports", "docker_images", "containers")


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
        "\n\nAsk workspace.environments_dir() / env_dir(name) / scratch_dir(...).")


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
# 2. The system/artifacts split (settled 2026-09-21)
# ---------------------------------------------------------------------------

#: The checkout holds the SYSTEM and nothing that outlives it: everything on
#: this list is rebuilt by ./scripts/setup.sh from a fresh clone, so deleting
#: the clone costs nothing but a re-download. What the agent PRODUCES
#: (environments, pipelines, scratch) must resolve OUTSIDE — a new version of the
#: system plugs into the artifacts previous versions made. This list exists to
#: make adding to it expensive: every entry must be worthless-once-deleted.
CHECKOUT_ALLOWED = {
    ".conda_runtime",      # the interpreter .mcp.json names by path
    ".miniforge",          # the repo-private conda setup always installs
    "envs",                # host tool envs — rebuilt by the install primitives
    "resources",           # core/test data corpus — re-downloaded by setup
    ".git",
    ".coverage",
}

#: The zones() keys that are SYSTEM (must default into the checkout) vs
#: ARTIFACT (must never resolve into it). A new zone has to pick a side here,
#: which is the point.
SYSTEM_ZONES = {"envs", "resources"}
ARTIFACT_ZONES = {"environments", "scratch", "pipelines", "common_data", "experiments",
                  "projects_access"}


def test_the_checkout_allowlist_is_small_and_justified():
    """The allowlist is the rule's escape hatch; a test that reads it is what
    keeps someone from quietly appending `env_reports` to it."""
    assert len(CHECKOUT_ALLOWED) <= 6, (
        "the checkout allowlist grew. Every entry must be rebuildable by "
        "setup.sh from a fresh clone — if it outlives the checkout it belongs "
        "in the workspace, not on this list.")


def test_every_zone_is_classified():
    """A zone nobody placed is a zone that lands wherever its author guessed."""
    from agent.skills import workspace
    keys = set(workspace.zones()) - {"code_root", "workspace_root", "workspace_source"}
    assert keys == SYSTEM_ZONES | ARTIFACT_ZONES, (
        f"zones() changed ({sorted(keys)}) — classify the new/renamed zone as "
        f"SYSTEM or ARTIFACT above, and add gitignore/setup coverage if SYSTEM")


def test_no_artifact_zone_resolves_inside_the_checkout(monkeypatch, tmp_path):
    """The load-bearing assertion. Whatever the agent produces must land
    outside the checkout — otherwise the split is decorative."""
    from agent.skills import workspace
    monkeypatch.setenv("BIOINF_WORKSPACE", str(tmp_path / "ws"))
    code = workspace.code_root()
    z = workspace.zones()
    for name in ARTIFACT_ZONES:
        p = Path(z[name]).resolve()
        assert code not in p.parents and p != code, \
            f"artifact zone {name!r} resolved to {p}, inside the checkout {code}"


def test_the_system_zones_default_into_the_checkout(monkeypatch):
    """The other half: with no overrides, envs/ and resources/ are the
    checkout's own untracked dirs — a fresh clone is self-contained."""
    from agent.skills import workspace
    for var in ("BIOINF_ENVS", "BIOINF_RESOURCES"):
        monkeypatch.delenv(var, raising=False)
    z = workspace.zones()
    assert Path(z["envs"]) == workspace.code_root() / "envs"
    assert Path(z["resources"]) == workspace.code_root() / "resources"


def test_the_system_zones_are_gitignored():
    """In the checkout but never in git — the clone stays small; the corpus is
    downloaded, not tracked."""
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    for name in ("/envs/", "/resources/"):
        assert name in ignore, f"{name} missing from .gitignore — a bootstrap " \
            f"would offer multi-GB of downloads to `git add -A`"


def test_the_default_workspace_is_not_the_checkout(monkeypatch):
    """Even with nothing configured. A default that lands in the checkout would
    make the rule true only for users who set the override."""
    from agent.skills import workspace
    monkeypatch.delenv("BIOINF_WORKSPACE", raising=False)
    root = workspace.workspace_root()
    assert workspace.code_root() not in root.parents
    assert root == Path.home() / workspace.DEFAULT_WORKSPACE_NAME


# ---------------------------------------------------------------------------
# 3. Resolution behaviour
# ---------------------------------------------------------------------------

def test_resolution_is_env_or_default_and_consults_no_state(monkeypatch, tmp_path):
    """$BIOINF_WORKSPACE wins; otherwise ~/bioinf_workspace, unconditionally.
    No pointer file, no config key, no filesystem probe — so resolution cannot
    silently answer differently tomorrow."""
    from agent.skills import workspace
    monkeypatch.setenv("BIOINF_WORKSPACE", str(tmp_path / "from_env"))
    assert workspace.workspace_root() == (tmp_path / "from_env").resolve()
    assert workspace.workspace_source() == "env"

    monkeypatch.delenv("BIOINF_WORKSPACE", raising=False)
    assert workspace.workspace_root() == Path.home() / workspace.DEFAULT_WORKSPACE_NAME
    assert workspace.workspace_source() == "default"


def test_resolution_never_raises_on_a_hostile_override(monkeypatch):
    """mcp_server builds artifact paths at IMPORT. A resolver that refuses an
    unusable value costs the agent its entire tool surface — every tool
    vanishes — rather than costing it one directory. Diagnose in the doctor,
    where a human reads the message and can act on it. (A NUL byte cannot
    even enter os.environ on POSIX, so blank/whitespace is the whole
    reachable junk space — those must yield the DEFAULT, not a cwd-relative
    surprise.)"""
    from agent.skills import workspace
    for junk in ("", "   "):
        monkeypatch.setenv("BIOINF_WORKSPACE", junk)
        assert workspace.workspace_root() == Path.home() / workspace.DEFAULT_WORKSPACE_NAME


def test_zones_describes_without_creating(monkeypatch, tmp_path):
    """A report that conjures the directories it reports on can never say one
    is missing — which is the only interesting thing it has to say on a fresh
    machine."""
    from agent.skills import workspace
    ws = tmp_path / "untouched"
    monkeypatch.setenv("BIOINF_WORKSPACE", str(ws))
    monkeypatch.setenv("BIOINF_RESOURCES", str(tmp_path / "untouched_res"))
    monkeypatch.setenv("BIOINF_ENVS", str(tmp_path / "untouched_envs"))
    workspace.zones()
    for p in (ws, tmp_path / "untouched_res", tmp_path / "untouched_envs"):
        assert not p.exists(), f"workspace.zones() created {p} while describing it"


def test_zone_accessors_do_create(monkeypatch, tmp_path):
    """The other half. The no-auto-mkdir rule is a CLUSTER rule about the user's
    territory; locally the agent owns these directories, and every caller of an
    accessor is about to write into it."""
    from agent.skills import workspace
    monkeypatch.setenv("BIOINF_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("BIOINF_RESOURCES", str(tmp_path / "res"))
    monkeypatch.setenv("BIOINF_ENVS", str(tmp_path / "envs"))
    for fn in (workspace.conda_envs_dir, workspace.environments_dir, workspace.resources_root):
        assert fn().is_dir()
    assert workspace.env_dir("e").is_dir()
    assert workspace.scratch_dir("jobs").is_dir()


def test_the_system_zones_are_independently_relocatable(monkeypatch, tmp_path):
    """$BIOINF_RESOURCES: the shared institutional mount (one corpus feeding N
    clones). $BIOINF_ENVS: the suite's own sandbox seam. Both relocate their
    zone without moving anything else."""
    from agent.skills import workspace
    monkeypatch.setenv("BIOINF_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("BIOINF_RESOURCES", str(tmp_path / "shared"))
    monkeypatch.setenv("BIOINF_ENVS", str(tmp_path / "elsewhere"))
    assert workspace.resources_root() == (tmp_path / "shared").resolve()
    assert workspace.conda_envs_dir() == (tmp_path / "elsewhere").resolve()
    assert workspace.environments_dir().is_relative_to(tmp_path / "ws")


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

@pytest.mark.parametrize("module,cls,attr,var,sub", [
    ("agent.skills.env_manager", "EnvManager", "envs_dir", "BIOINF_ENVS",      ""),
    ("agent.skills.job_manager", "JobManager", "jobs_dir", "BIOINF_WORKSPACE", "ws"),
])
def test_the_singletons_land_where_the_resolver_points(monkeypatch, tmp_path,
                                                       module, cls, attr, var, sub):
    """Constructing a manager must write into the redirected zone and nowhere
    else — this is what the suite's sandbox seams rely on. EnvManager follows
    the envs override (a SYSTEM zone); JobManager follows the workspace."""
    import importlib
    target = tmp_path / (sub or "envs")
    monkeypatch.setenv(var, str(target))
    mod = importlib.import_module(module)
    inst = getattr(mod, cls)({})
    assert Path(getattr(inst, attr)).is_relative_to(target)


def test_the_access_file_has_one_home(monkeypatch, tmp_path):
    """CS55, as a standing check. The menu WRITES where every reader LOOKS —
    and the home is DECOUPLED from the workspace (menu review 2026-09-18):
    the fixed machine-level dotdir, ~/.bioinf_agent, like ~/.ssh. Relocating
    the products must never relocate the config out from under the agent."""
    from agent.skills import compute_access, workspace
    monkeypatch.setenv("BIOINF_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("BIOINF_PROJECTS_ACCESS", raising=False)
    assert compute_access.default_access_path() == workspace.projects_access_path()
    assert (compute_access.default_access_path()
            == Path.home() / ".bioinf_agent" / "projects_access.yaml"), \
        "the config home must not follow the workspace"

    # The override is a FILE path, honored verbatim — the suite's isolation
    # seam and the cloud escape hatch.
    monkeypatch.setenv("BIOINF_PROJECTS_ACCESS", str(tmp_path / "cfg" / "pa.yaml"))
    assert compute_access.default_access_path() == tmp_path / "cfg" / "pa.yaml"


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



def test_the_runtime_env_is_the_checkouts_conda_runtime():
    """setup.sh builds it there and scripts/activate.sh puts it on PATH; the pipeline
    tool records both for the page's local step."""
    from agent.skills import workspace
    assert workspace.runtime_env_dir() == workspace.code_root() / ".conda_runtime"
    assert (workspace.code_root() / "scripts" / "activate.sh").is_file()
