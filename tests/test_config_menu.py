"""The configuration menu cannot write a file the agent will refuse to load.

`scripts/configure.py` authors `projects_access.yaml` — the file that says which
directories on which machines the agent may list, upload to, download from, and
run jobs in. It has exactly one job beyond collecting keystrokes: never produce a
document the agent's own loader rejects. A menu that writes a key
`compute_access` does not allow, or offers a permission token that is not a
permission, fails at drive time instead of at authoring time, which is the worst
place for a configuration error to surface — the user has already left.

So the menu does not carry its own idea of validity. It projects every block
through a declared key tuple and hands the result to `load_access` before
writing. These tests pin the two halves that could silently drift apart: the
keys the menu can write, and the tokens it can offer.

The interactive loop is not tested here — it is input() over a terminal. What IS
tested is everything a save depends on, which is where a defect would cost
someone their configuration.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIGURE = ROOT / "scripts" / "configure.py"
CONFIG_SH = ROOT / "scripts" / "config.sh"

from agent.skills import compute_access  # noqa: E402


@pytest.fixture(scope="module")
def cfgmod():
    """Import configure.py by path — it lives in scripts/, not a package."""
    spec = importlib.util.spec_from_file_location("_configure", CONFIGURE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- the two drift seams ----------------------------------------------------

def test_every_env_key_the_menu_writes_is_one_the_loader_allows(cfgmod):
    """compute_envs[] is a CLOSED key set — the loader refuses unknown keys to
    catch typos (`data_transfr:` used to make the whole globus block invisible).
    A menu writing a key outside that set produces a file that refuses to load
    the moment the user tries to use it."""
    unknown = set(cfgmod.ENV_KEYS) - set(compute_access._ENV_ALLOWED_KEYS)
    assert not unknown, (
        f"scripts/configure.py can write compute_envs[] key(s) {sorted(unknown)} that "
        f"compute_access rejects. Either the key was renamed in the schema (update "
        f"ENV_KEYS) or the menu is inventing one.")


def test_every_zone_the_menu_offers_is_an_env_key_it_declares(cfgmod):
    """ZONES drives the prompts; ENV_KEYS decides what survives to the file. A
    zone missing from ENV_KEYS is a block the user fills in and the menu then
    silently discards."""
    zone_keys = {k for k, _, _ in cfgmod.ZONES}
    assert zone_keys <= set(cfgmod.ENV_KEYS), (
        f"zone(s) {sorted(zone_keys - set(cfgmod.ENV_KEYS))} are prompted for but not "
        f"projected into the saved env — the user's answer would be dropped on save")
    assert zone_keys == set(cfgmod.ZONE_LABELS), \
        "ZONE_LABELS must cover exactly the zones ZONES declares"


def test_the_menu_offers_exactly_the_permission_tokens_that_exist(cfgmod):
    """A token the menu cannot offer is a grant nobody can make through it; a
    token it offers that the loader does not know refuses the whole file."""
    assert set(cfgmod.PERMISSION_ORDER) == set(compute_access.PERMISSIONS)


def test_the_menu_offers_exactly_the_transfer_types_that_exist(cfgmod):
    assert set(cfgmod.TRANSFER_TYPES) == set(compute_access._DATA_TRANSFER_TYPES)


def test_the_menu_offers_the_job_managers_the_bridge_implements(cfgmod):
    """VALID_JOB_MANAGERS is the loader's enum; the menu appends a '(none)' escape
    hatch of its own, which must not reach the file as a value."""
    assert "(none)" not in compute_access.VALID_JOB_MANAGERS


# --- save behaviour ---------------------------------------------------------

def _env(name="cluster", scratch="/scratch/a/S/", common="/scratch/a/G/"):
    return {
        "name": name, "type": "ssh", "host": "h.example.edu", "user": "a",
        "agent_scratch_target": {"path": scratch, "permissions": ["upload", "download", "exec"]},
        "agent_common_data_target": {"path": common, "permissions": ["upload", "download", "exec"]},
    }


def test_a_document_the_menu_builds_loads_through_the_agents_own_loader(cfgmod, tmp_path):
    cfg = cfgmod.Config(tmp_path / "pa.yaml")
    cfg.envs.append(_env())
    cfg.projects.append({"name": "p1", "compute_envs": ["cluster"],
                         "directories": [{"env": "cluster", "path": "/scratch/a/p1",
                                          "permissions": ["file_name_only"]}]})
    assert cfg.validate() == "", cfg.validate()
    assert cfg.save() is True
    assert compute_access.load_access(tmp_path / "pa.yaml")


def test_save_refuses_an_invalid_document_and_writes_nothing(cfgmod, tmp_path):
    """The refusal must be TOTAL. A menu that writes a half-valid file has
    destroyed the working one the user had."""
    path = tmp_path / "pa.yaml"
    cfg = cfgmod.Config(path)
    # Nested zone paths — the loader requires disjoint subtrees.
    cfg.envs.append(_env(scratch="/scratch/a/", common="/scratch/a/G/"))
    assert cfg.validate() != ""
    assert cfg.save() is False
    assert not path.exists(), "an invalid configuration reached disk"


def test_save_never_overwrites_without_leaving_the_previous_version(cfgmod, tmp_path):
    """The menu rewrites the file, dropping hand-written comments. The backup is
    the only reason that is an acceptable trade."""
    path = tmp_path / "pa.yaml"
    cfg = cfgmod.Config(path)
    cfg.envs.append(_env())
    cfg.save()
    first = path.read_text()

    cfg.envs.append(_env(name="second", scratch="/scratch/b/S/", common="/scratch/b/G/"))
    cfg.save()
    backup = Path(str(path) + ".bak")
    assert backup.exists(), "no .bak — the previous configuration is unrecoverable"
    assert backup.read_text() == first


def test_saving_a_hand_written_file_unchanged_is_semantically_lossless(cfgmod, tmp_path):
    """Open, save, and the agent must see exactly what it saw before. This is the
    property that makes the menu safe to open on a config you already rely on —
    formatting and comments change, the grants do not."""
    path = tmp_path / "pa.yaml"
    path.write_text(yaml.safe_dump({
        "compute_envs": [_env(), {"name": "laptop", "type": "local"}],
        "projects": [{"name": "p1", "description": "x", "compute_envs": ["cluster", "laptop"],
                      "directories": [{"env": "laptop", "path": "/tmp/p1",
                                       "permissions": ["file_name_only", "upload"]}]}],
    }))
    before = compute_access.load_access(path)

    cfg = cfgmod.Config(path)
    assert cfg.save() is True
    assert compute_access.load_access(path) == before


def test_an_unparseable_file_opens_for_repair_instead_of_crashing(cfgmod, tmp_path):
    """This menu is how someone fixes a broken config, so refusing to open a
    broken one closes the wrong door. The state is stated, not swallowed."""
    path = tmp_path / "pa.yaml"
    path.write_text("compute_envs: [\n  unclosed")
    cfg = cfgmod.Config(path)
    assert "does not parse" in cfg.load_error
    assert cfg.envs == [] and cfg.projects == []


def test_top_level_keys_the_menu_does_not_manage_are_announced_not_dropped_silently(
        cfgmod, tmp_path):
    """The menu only knows compute_envs and projects. If a future schema grows a
    third section, a save would drop it — so the reader is told BEFORE they save,
    not after."""
    path = tmp_path / "pa.yaml"
    path.write_text(yaml.safe_dump({"compute_envs": [], "projects": [], "future_section": {"a": 1}}))
    cfg = cfgmod.Config(path)
    assert "future_section" in cfg.load_error and "DROP" in cfg.load_error


# --- the non-interactive surface -------------------------------------------

@pytest.mark.parametrize("flag,expect_rc", [("--validate", 0), ("--show", 0)])
def test_the_read_only_flags_work_without_a_terminal(tmp_path, flag, expect_rc):
    """--show / --validate are what a script (or the doctor) can call. They must
    not need a tty, and they must not hang waiting for one."""
    path = tmp_path / "pa.yaml"
    path.write_text(yaml.safe_dump({"compute_envs": [_env()], "projects": []}))
    p = subprocess.run([sys.executable, str(CONFIGURE), "--file", str(path), flag],
                       capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert p.returncode == expect_rc, p.stderr
    assert "valid" in p.stdout


def test_the_menu_refuses_to_run_interactively_without_a_terminal(tmp_path):
    """A prompt loop against a closed stdin is an infinite loop or a traceback.
    It must be neither — it must name the read-only flags."""
    path = tmp_path / "pa.yaml"
    path.write_text(yaml.safe_dump({"compute_envs": [], "projects": []}))
    p = subprocess.run([sys.executable, str(CONFIGURE), "--file", str(path)],
                       capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL, cwd=str(ROOT))
    assert p.returncode == 2
    assert "--show" in p.stderr and "--validate" in p.stderr


def test_validate_reports_a_nonzero_exit_for_an_invalid_file(tmp_path):
    path = tmp_path / "pa.yaml"
    path.write_text(yaml.safe_dump({
        "compute_envs": [_env()],
        "projects": [{"name": "p1", "compute_envs": ["no_such_env"], "directories": []}]}))
    p = subprocess.run([sys.executable, str(CONFIGURE), "--file", str(path), "--validate"],
                       capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert p.returncode == 1
    assert "invalid" in p.stdout


def test_the_wrapper_is_executable_and_delegates_to_the_shared_resolver():
    """config.sh must not grow its own idea of where the runtime interpreter is —
    the same rule tests/test_setup_surface_resolution.py enforces across scripts/."""
    import os
    assert os.access(CONFIG_SH, os.X_OK), "scripts/config.sh is not executable"
    text = CONFIG_SH.read_text()
    assert "_env.sh" in text and "BIOINF_RUNTIME_PY" in text


# --- absence is a state, not a pass (second cold-start drive) ----------------

def _run_configure(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CONFIGURE), *args],
                          cwd=ROOT, capture_output=True, text=True)


def test_validate_does_not_call_a_missing_file_valid(tmp_path):
    """`validate()` answers a question about the in-memory DOCUMENT, and an empty
    document is one the loader accepts. Every reporting surface called it
    directly, so `--validate` on a path holding no file printed `valid` and
    returned rc 0 — a green light for an unconfigured bridge."""
    missing = tmp_path / "nowhere" / "projects_access.yaml"
    res = _run_configure("--validate", "--file", str(missing))
    assert res.returncode == 2, (
        f"rc {res.returncode} for a nonexistent file; rc 0 claims the agent's "
        f"loader accepted something that is not there")
    # Match the verdict, not the stream — tmp_path itself contains "valid".
    verdict = res.stdout.split(":")[-1].strip()
    assert verdict.startswith("no configuration file"), res.stdout


def test_validate_accepts_a_real_file(tmp_path):
    cfg = tmp_path / "projects_access.yaml"
    cfg.write_text("compute_envs: []\nprojects: []\n")
    res = _run_configure("--validate", "--file", str(cfg))
    assert res.returncode == 0, res.stdout + res.stderr


def test_validate_rejects_a_broken_file(tmp_path):
    cfg = tmp_path / "projects_access.yaml"
    cfg.write_text("compute_envs:\n  - name: x\n    type: not_a_type\nprojects: []\n")
    res = _run_configure("--validate", "--file", str(cfg))
    assert res.returncode == 1, res.stdout + res.stderr
    assert "invalid" in res.stdout


def test_state_reports_three_values(cfgmod, tmp_path):
    """absent / valid / invalid — the third exists because a file that is not
    there is not a configuration the loader accepted, it is no configuration."""
    assert cfgmod.Config(tmp_path / "gone.yaml").state()[0] == "absent"

    good = tmp_path / "good.yaml"
    good.write_text("compute_envs: []\nprojects: []\n")
    assert cfgmod.Config(good).state()[0] == "valid"

    bad = tmp_path / "bad.yaml"
    bad.write_text("compute_envs:\n  - name: x\n    type: bogus\nprojects: []\n")
    assert cfgmod.Config(bad).state()[0] == "invalid"


def test_save_creates_its_parent_directory(cfgmod, tmp_path):
    """`save` wrote straight to the path. Pointed at a directory that does not
    exist yet, that raised FileNotFoundError out of the menu loop and took every
    environment staged in the session with it."""
    target = tmp_path / "does" / "not" / "exist" / "projects_access.yaml"
    cfg = cfgmod.Config(target)
    assert cfg.save() is True
    assert target.is_file()


def test_save_returns_false_instead_of_raising_when_unwritable(cfgmod, tmp_path):
    """An unwritable destination costs the user the save, never the work."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory\n")
    cfg = cfgmod.Config(blocker / "sub" / "projects_access.yaml")
    assert cfg.save() is False
