#!/usr/bin/env python3
"""configure.py — the configuration menu for projects_access.yaml.

`projects_access.yaml` is the agent's command-and-control file: which compute
environments exist, which projects may use them, and exactly which directories
the agent is allowed to list, upload to, download from, or run jobs in. Nothing
in the HPC bridge works without it, and every path in it is a permission grant.

Run via `./scripts/config.sh` (which resolves the runtime interpreter). This
module drives the menu; it does NOT define what a valid configuration is —
every save is checked by `agent.skills.compute_access.load_access`, the same
loader the agent itself uses, so the menu cannot bless a file the agent will
later refuse. The annotated schema reference is
`agent/skills/projects_access.yaml.example`.

The file is rewritten on save, which drops hand-written comments; the previous
version is copied to `projects_access.yaml.bak` first and the menu says so.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402  (runtime env only — config.sh guarantees it)

from agent.skills.compute_access import (  # noqa: E402
    PERMISSIONS,
    VALID_JOB_MANAGERS,
    ConfigError,
    default_access_path,
    load_access,
)

# Ordered for display; PERMISSIONS is the authority on membership, and the
# assertion below fails the menu loudly if a token is ever added without being
# offered here (an unofferable permission is a grant nobody can make).
PERMISSION_ORDER = ["file_name_only", "upload", "download", "exec", "none"]
assert set(PERMISSION_ORDER) == set(PERMISSIONS), (
    f"permission tokens changed: {sorted(set(PERMISSIONS) ^ set(PERMISSION_ORDER))} — "
    f"add them to PERMISSION_ORDER so the menu can offer them")

TRANSFER_TYPES = ["scp_head_node", "globus"]

#: The three env-level zones, in the order the agent uses them. `key` is the
#: schema key, `default_perms` what the bridge needs to use the zone at all.
ZONES = [
    ("agent_scratch_target", "agent sandbox — job working dirs, logs, per-run staging",
     ["file_name_only", "upload", "download", "exec"]),
    ("agent_common_data_target", "shared reference data — genomes, public databases",
     ["file_name_only", "upload", "download", "exec"]),
    ("container_upload_target", "where .sif container images are staged",
     ["file_name_only", "upload"]),
]

ZONE_LABELS = {
    "agent_scratch_target": "scratch",
    "agent_common_data_target": "common_data",
    "container_upload_target": "containers",
}

#: Every compute_envs[] key the menu can write, in the order it writes them.
#: Load-bearing — `edit_env` projects its result through this tuple, so a key not
#: listed here never reaches the file. Checked against the loader's allowed-key
#: set by tests/test_config_menu.py: a key the menu writes and the validator
#: rejects would make the menu's own output unloadable.
ENV_KEYS = (
    "name", "type", "host", "user", "job_manager", "email",
    "apptainer_module", "nextflow_module",
    "agent_scratch_target", "agent_common_data_target", "container_upload_target",
    "data_transfer", "slurm",
)

HEADER = """\
# projects_access.yaml — the agent's command-and-control file.
#
# Maintained by ./scripts/config.sh. Hand-editing is fine: the menu re-reads
# this file every run and validates it before writing. A menu save rewrites the
# file, which drops hand-written comments — the previous version is kept as
# projects_access.yaml.bak.
#
# Annotated schema reference: agent/skills/projects_access.yaml.example
"""


# ---------------------------------------------------------------------------
# terminal helpers
# ---------------------------------------------------------------------------

def c(code: str, s: str) -> str:
    return s if os.environ.get("NO_COLOR") else f"\033[{code}m{s}\033[0m"


BOLD = lambda s: c("1", s)      # noqa: E731
DIM = lambda s: c("2", s)       # noqa: E731
GREEN = lambda s: c("32", s)    # noqa: E731
RED = lambda s: c("31", s)      # noqa: E731
YELLOW = lambda s: c("33", s)   # noqa: E731


def dump(data: dict) -> str:
    """Serialize the document. One spelling, because the text this produces is
    what the user reads and hand-edits afterwards: `allow_unicode` keeps em-dashes
    from becoming \\u2014 escapes, and `default_flow_style=None` renders leaf lists
    inline (`permissions: [upload, exec]`), matching the annotated example."""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                          default_flow_style=None, width=100)


class Abort(Exception):
    """The user backed out of a sub-flow. Nothing is committed."""


def ask(prompt: str, default: str = "", allow_empty: bool = False) -> str:
    """One line of input. Blank accepts `default`. `-` aborts the sub-flow."""
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input(f"  {prompt}{suffix}: ").strip()
        except EOFError:
            raise Abort()
        if raw == "-":
            raise Abort()
        value = raw or default
        if value or allow_empty:
            return value
        print(RED("  a value is required (enter '-' to go back)"))


def ask_optional(prompt: str, default: str = "") -> str:
    """Same, but blank-and-no-default means 'leave this out'."""
    return ask(f"{prompt} (blank to omit)", default, allow_empty=True)


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"  {prompt} [{d}]: ").strip().lower()
        except EOFError:
            raise Abort()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


def ask_choice(prompt: str, options: list[str], default: Optional[str] = None) -> str:
    print(f"  {prompt}")
    for i, opt in enumerate(options, 1):
        mark = DIM("  (default)") if opt == default else ""
        print(f"    {i}) {opt}{mark}")
    while True:
        raw = ask("choose", default or "", allow_empty=False)
        if raw in options:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print(RED(f"  pick 1-{len(options)} or type the value"))


def ask_permissions(default: list[str]) -> list[str]:
    """Permissions are DISCRETE, not a lattice — `upload` does not imply
    `download`. So this asks for the exact set rather than a level."""
    print(f"  permissions — space-separated subset of {' '.join(PERMISSION_ORDER)}")
    print(DIM("    file_name_only: list this dir (one level)   upload: write new files"))
    print(DIM("    download: fetch files back                  exec: a job may write here"))
    while True:
        raw = ask("permissions", " ".join(default))
        tokens = raw.split()
        bad = [t for t in tokens if t not in PERMISSIONS]
        if bad:
            print(RED(f"  not permission tokens: {bad}"))
            continue
        if not tokens:
            print(RED("  at least one token is required"))
            continue
        return tokens


def ask_multi(prompt: str, options: list[str], default: list[str]) -> list[str]:
    if not options:
        raise Abort()
    print(f"  {prompt}")
    for i, opt in enumerate(options, 1):
        print(f"    {i}) {opt}")
    while True:
        raw = ask("choose (space-separated)", " ".join(default))
        picked: list[str] = []
        for tok in raw.split():
            if tok in options:
                picked.append(tok)
            elif tok.isdigit() and 1 <= int(tok) <= len(options):
                picked.append(options[int(tok) - 1])
            else:
                picked = []
                print(RED(f"  no such option: {tok}"))
                break
        if picked:
            return list(dict.fromkeys(picked))


def ask_abs_path(prompt: str, default: str) -> str:
    """Absolute paths only — the bridge's permission match is a path-prefix
    match, and a relative path has no stable meaning on a remote env."""
    while True:
        p = ask(prompt, default)
        if p.startswith("/"):
            return p
        print(RED("  must be an absolute path (start with /)"))


def rule(title: str = "") -> None:
    print()
    print(BOLD(f"── {title} " + "─" * max(0, 60 - len(title))) if title else "─" * 64)


# ---------------------------------------------------------------------------
# the config document
# ---------------------------------------------------------------------------

class Config:
    """The in-memory document plus its dirty flag. Nothing reaches disk until
    `save`, and `save` refuses a document the agent's own loader rejects."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"compute_envs": [], "projects": []}
        self.dirty = False
        self.load_error = ""
        self.reload()

    def reload(self) -> None:
        self.dirty = False
        self.load_error = ""
        if not self.path.exists():
            self.data = {"compute_envs": [], "projects": []}
            return
        try:
            raw = yaml.safe_load(self.path.read_text()) or {}
        except yaml.YAMLError as e:
            # Load the file even when it is broken — this menu is how someone
            # fixes it, so refusing to open it would be the wrong door to close.
            self.data = {"compute_envs": [], "projects": []}
            self.load_error = f"{self.path.name} does not parse as YAML: {e}"
            return
        if not isinstance(raw, dict):
            self.data = {"compute_envs": [], "projects": []}
            self.load_error = f"{self.path.name}: top level must be a mapping"
            return
        self.data = {
            "compute_envs": raw.get("compute_envs") or [],
            "projects": raw.get("projects") or [],
        }
        extra = set(raw) - {"compute_envs", "projects"}
        if extra:
            self.load_error = (f"{self.path.name} has top-level keys this menu does not "
                               f"manage and will DROP on save: {sorted(extra)}")

    @property
    def envs(self) -> list[dict]:
        return self.data["compute_envs"]

    @property
    def projects(self) -> list[dict]:
        return self.data["projects"]

    def env_names(self) -> list[str]:
        return [e.get("name", "?") for e in self.envs]

    def validate(self) -> str:
        """Run the agent's own loader over the current document. Returns "" when
        it passes, else the loader's message.

        Validating a temp copy rather than re-implementing the rules is the whole
        point: a second validator here would eventually disagree with the one the
        agent enforces, and the menu would bless a file that refuses at drive time.
        """
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(dump(self.data))
            tmp = Path(fh.name)
        try:
            load_access(tmp)
            return ""
        except (ConfigError, FileNotFoundError) as e:
            # The loader names the temp path; show the real filename instead.
            return str(e).replace(str(tmp), self.path.name)
        finally:
            tmp.unlink(missing_ok=True)

    def save(self) -> bool:
        err = self.validate()
        if err:
            print(RED("  NOT saved — the agent's loader rejects this configuration:"))
            print(f"    {err}")
            print(DIM("  fix it in the menu, or quit without saving to keep the file on disk."))
            return False
        backup = ""
        if self.path.exists():
            backup = str(self.path) + ".bak"
            shutil.copy2(self.path, backup)
        self.path.write_text(HEADER + "\n" + dump(self.data))
        self.dirty = False
        print(GREEN(f"  saved {self.path}"))
        if backup:
            print(DIM(f"  previous version: {backup}"))
        return True


# ---------------------------------------------------------------------------
# defaults — the placeholders that make a first configuration a few keystrokes
# ---------------------------------------------------------------------------

def ssh_defaults(user: str) -> dict[str, str]:
    """Conventional cluster layout. Every zone is a disjoint subtree, which the
    validator requires — a breach of one zone must not reach another."""
    base = f"/scratch/{user}"
    return {
        "agent_scratch_target": f"{base}/CLAUDE_SCRATCH/",
        "agent_common_data_target": f"{base}/CLAUDE_GENOMES/",
        "container_upload_target": f"{base}/CLAUDE_CONTAINERS/",
    }


def local_defaults() -> dict[str, str]:
    """A local env is at zone-parity with a cluster — same three zones, local
    paths — which is what lets a production run be the same kind of thing on
    either locus. Kept under the repo so nothing is written outside it."""
    base = ROOT / "data" / "local_env"
    return {
        "agent_scratch_target": f"{base}/scratch/",
        "agent_common_data_target": f"{base}/common_data/",
        "container_upload_target": f"{base}/containers/",
    }


# ---------------------------------------------------------------------------
# compute environments
# ---------------------------------------------------------------------------

def ssh_target(env: dict) -> str:
    """`user@host`, or bare host when no user is declared — an ssh_config alias
    carries its own user, and printing `None@host` would name a login nobody set."""
    user, host = env.get("user"), env.get("host", "?")
    return f"{user}@{host}" if user else host


def describe_env(env: dict) -> str:
    bits = [env.get("type", "?")]
    if env.get("type") == "ssh":
        bits.append(ssh_target(env))
    zones = [ZONE_LABELS[k] for k, _, _ in ZONES if env.get(k)]
    bits.append("zones: " + (", ".join(zones) if zones else DIM("none")))
    dt = (env.get("data_transfer") or {}).get("type")
    if dt:
        bits.append(dt)
    return "  ".join(bits)


def edit_zone(env: dict, key: str, purpose: str, default_perms: list[str],
              path_default: str) -> None:
    current = env.get(key)
    rule(key)
    print(DIM(f"  {purpose}"))
    if current:
        print(f"  current: {current.get('path')}  {current.get('permissions')}")
        if not ask_yes_no("keep this zone?", True):
            env.pop(key, None)
            print(DIM(f"  {key} removed"))
            return
    elif not ask_yes_no(f"declare {key}?", True):
        env.pop(key, None)
        return
    path = ask_abs_path("path", (current or {}).get("path") or path_default)
    perms = ask_permissions((current or {}).get("permissions") or default_perms)
    desc = ask_optional("description", (current or {}).get("description") or purpose)
    block = {"path": path, "permissions": perms}
    if desc:
        block["description"] = desc
    env[key] = block


def edit_slurm(env: dict) -> None:
    rule("slurm policy (optional)")
    print(DIM("  Scheduler POLICY only — per-job time/mem/cpus are passed to the run"))
    print(DIM("  primitives, not declared here. Omit the block entirely if your cluster"))
    print(DIM("  needs no account or partition."))
    current = env.get("slurm") or {}
    if not ask_yes_no("declare a slurm block?", bool(current)):
        env.pop("slurm", None)
        return
    blk: dict[str, Any] = {}
    account = ask_optional("account (--account)", current.get("account", ""))
    if account:
        blk["account"] = account
    partition = ask_optional("default CPU partition", current.get("partition", ""))
    if partition:
        blk["partition"] = partition
    gpu = current.get("gpu") or {}
    if ask_yes_no("declare the GPU convention (partition + qos)?", bool(gpu)):
        blk["gpu"] = {
            "partition": ask("gpu partition", gpu.get("partition", "")),
            "qos": ask("gpu qos", gpu.get("qos", "")),
        }
    env["slurm"] = blk if blk else {}
    if not blk:
        env.pop("slurm")


def edit_transfer(env: dict) -> None:
    rule("data transfer (optional)")
    print(DIM("  How bytes move. scp_head_node is the default and needs no setup;"))
    print(DIM("  globus is off-head-node and checksummed end-to-end, and needs both"))
    print(DIM("  endpoint UUIDs (globus endpoint search '<display name>')."))
    current = env.get("data_transfer") or {}
    if not ask_yes_no("declare a data_transfer block?", bool(current)):
        env.pop("data_transfer", None)
        return
    ttype = ask_choice("protocol", TRANSFER_TYPES,
                       current.get("type") or "scp_head_node")
    blk: dict[str, Any] = {"type": ttype}
    if ttype == "globus":
        g = current.get("globus") or {}
        blk["globus"] = {
            "local_endpoint_id": ask("local endpoint UUID", g.get("local_endpoint_id", "")),
            "local_endpoint_name": ask("local endpoint display name",
                                       g.get("local_endpoint_name", "")),
            "remote_endpoint_id": ask("remote endpoint UUID", g.get("remote_endpoint_id", "")),
            "remote_endpoint_name": ask("remote endpoint display name",
                                        g.get("remote_endpoint_name", "")),
        }
    env["data_transfer"] = blk


def edit_env(cfg: Config, env: Optional[dict]) -> None:
    """Add (env=None) or edit one compute environment. The whole block is built
    here and only merged into the document at the end, so aborting midway leaves
    the configuration exactly as it was."""
    creating = env is None
    src = dict(env or {})
    rule("new compute environment" if creating else f"edit {src.get('name')}")
    print(DIM("  Enter '-' at any prompt to go back without changing anything."))

    etype = ask_choice("type", ["ssh", "local"], src.get("type") or "ssh")
    name_default = src.get("name") or ("cluster" if etype == "ssh" else "laptop")
    name = ask("name (your label; projects reference it)", name_default)
    if creating and name in cfg.env_names():
        print(RED(f"  a compute env named {name!r} already exists"))
        raise Abort()

    new: dict[str, Any] = {"name": name, "type": etype}

    if etype == "ssh":
        new["host"] = ask("ssh host (or an ssh_config alias)", src.get("host", ""))
        # Optional: an ssh_config alias carries its own User, and declaring a
        # second one here only creates a way for the two to disagree.
        user = ask_optional("ssh user", src.get("user") or os.environ.get("USER", ""))
        if user:
            new["user"] = user
        jm = ask_choice("job manager", list(VALID_JOB_MANAGERS) + ["(none)"],
                        src.get("job_manager") or "slurm")
        if jm != "(none)":
            new["job_manager"] = jm
        email = ask_optional("notification email (--mail-user on every job)",
                             src.get("email", ""))
        if email:
            new["email"] = email
        print(DIM("  Lmod modules a cluster production run loads on the compute node."))
        print(DIM("  `cluster_module_avail` finds the exact names once this env is reachable."))
        for key, placeholder in (("apptainer_module", "apptainer/1.5.0"),
                                 ("nextflow_module", "nextflow/25.04.7")):
            val = ask_optional(key, src.get(key) or placeholder)
            if val:
                new[key] = val
        defaults = ssh_defaults(user or "USER")
    else:
        defaults = local_defaults()

    for key, purpose, perms in ZONES:
        new[key] = src.get(key)              # carry current so edit_zone sees it
        edit_zone(new, key, purpose, perms, defaults[key])

    new["data_transfer"] = src.get("data_transfer")
    edit_transfer(new)
    if etype == "ssh":
        new["slurm"] = src.get("slurm")
        edit_slurm(new)

    # Project through ENV_KEYS: fixes key order, and drops the `None` a declined
    # zone leaves behind. An absent key and an explicit `null` both disable a
    # zone, and carrying two spellings of one state is how they come to mean
    # different things later.
    new = {k: new[k] for k in ENV_KEYS if new.get(k) is not None}

    if creating:
        cfg.envs.append(new)
    else:
        env.clear()
        env.update(new)
    cfg.dirty = True
    print(GREEN(f"  {name} staged — choose 'save' to write it to disk"))


def rename_env_everywhere(cfg: Config, old: str, new: str) -> None:
    for proj in cfg.projects:
        proj["compute_envs"] = [new if e == old else e
                                for e in (proj.get("compute_envs") or [])]
        for d in proj.get("directories") or []:
            if d.get("env") == old:
                d["env"] = new


def menu_envs(cfg: Config) -> None:
    while True:
        rule("compute environments")
        if not cfg.envs:
            print(DIM("  none declared yet"))
        for i, env in enumerate(cfg.envs, 1):
            print(f"  {i}) {BOLD(env.get('name', '?'))}   {describe_env(env)}")
        print()
        print("  a) add    e) edit    r) remove    b) back")
        choice = ask("action", "b")
        try:
            if choice == "b":
                return
            if choice == "a":
                edit_env(cfg, None)
            elif choice == "e" and cfg.envs:
                env = pick(cfg.envs, "edit which?")
                before = env.get("name")
                edit_env(cfg, env)
                if env.get("name") != before:
                    rename_env_everywhere(cfg, before, env["name"])
                    print(DIM(f"  projects referencing {before!r} now point at {env['name']!r}"))
            elif choice == "r" and cfg.envs:
                env = pick(cfg.envs, "remove which?")
                users = [p.get("name") for p in cfg.projects
                         if env.get("name") in (p.get("compute_envs") or [])]
                if users:
                    print(RED(f"  {env.get('name')} is used by project(s): {users}"))
                    print(DIM("  remove it from those projects first — an unknown env "
                              "reference makes the whole file invalid."))
                    continue
                if ask_yes_no(f"remove {env.get('name')}?", False):
                    cfg.envs.remove(env)
                    cfg.dirty = True
        except Abort:
            print(DIM("  (cancelled)"))


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------

def pick(items: list[dict], prompt: str) -> dict:
    for i, it in enumerate(items, 1):
        print(f"    {i}) {it.get('name', '?')}")
    while True:
        raw = ask(prompt, "1")
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1]
        for it in items:
            if it.get("name") == raw:
                return it
        print(RED("  no such entry"))


def edit_directories(cfg: Config, proj: dict) -> None:
    """The explicit grants — the project's OWN data, typically protected. The
    env-level zones are inherited from `compute_envs` and must not be re-declared
    here."""
    dirs = proj.setdefault("directories", [])
    allowed = proj.get("compute_envs") or []
    while True:
        rule(f"{proj.get('name')} — directories")
        print(DIM("  Project-specific paths only. Scratch / common_data / container"))
        print(DIM("  zones come with the env and do not belong here."))
        if not dirs:
            print(DIM("  none declared"))
        for i, d in enumerate(dirs, 1):
            print(f"  {i}) [{d.get('env')}] {d.get('path')}   {d.get('permissions')}")
        print()
        print("  a) add    r) remove    b) back")
        choice = ask("action", "b")
        if choice == "b":
            return
        try:
            if choice == "a":
                env_name = (allowed[0] if len(allowed) == 1
                            else ask_choice("which env does this directory live on?",
                                            allowed, allowed[0] if allowed else None))
                path = ask_abs_path("absolute path", "")
                perms = ask_permissions(["file_name_only"])
                desc = ask_optional("description", "")
                block = {"env": env_name, "path": path, "permissions": perms}
                if desc:
                    block["description"] = desc
                dirs.append(block)
                cfg.dirty = True
            elif choice == "r" and dirs:
                idx = ask("remove which number?", "1")
                if idx.isdigit() and 1 <= int(idx) <= len(dirs):
                    dirs.pop(int(idx) - 1)
                    cfg.dirty = True
        except Abort:
            print(DIM("  (cancelled)"))


def edit_project(cfg: Config, proj: Optional[dict]) -> None:
    creating = proj is None
    src = dict(proj or {})
    rule("new project" if creating else f"edit {src.get('name')}")
    if not cfg.envs:
        print(RED("  declare a compute environment first — a project must name one."))
        raise Abort()

    name = ask("name (also the auto-prefix in scratch; letters, digits, _ and - only)",
               src.get("name", ""))
    if creating and name in [p.get("name") for p in cfg.projects]:
        print(RED(f"  a project named {name!r} already exists"))
        raise Abort()
    desc = ask_optional("description", src.get("description", ""))
    envs = ask_multi("which compute environments may this project use?",
                     cfg.env_names(),
                     src.get("compute_envs") or cfg.env_names()[:1])

    target = proj if not creating else {}
    target["name"] = name
    if desc:
        target["description"] = desc
    else:
        target.pop("description", None)
    target["compute_envs"] = envs
    # A directory on an env the project no longer lists is an unreachable grant,
    # and the validator refuses it — drop them here rather than at save time,
    # where the message would be about a line the user did not just touch.
    kept = [d for d in (target.get("directories") or []) if d.get("env") in envs]
    dropped = len(target.get("directories") or []) - len(kept)
    target["directories"] = kept
    if dropped:
        print(YELLOW(f"  dropped {dropped} directory grant(s) on env(s) no longer listed"))
    if creating:
        cfg.projects.append(target)
    cfg.dirty = True
    edit_directories(cfg, target)


def menu_projects(cfg: Config) -> None:
    while True:
        rule("projects")
        if not cfg.projects:
            print(DIM("  none declared yet"))
        for i, p in enumerate(cfg.projects, 1):
            ndirs = len(p.get("directories") or [])
            print(f"  {i}) {BOLD(p.get('name', '?'))}   envs: "
                  f"{', '.join(p.get('compute_envs') or []) or DIM('none')}   "
                  f"{ndirs} director{'y' if ndirs == 1 else 'ies'}")
        print()
        print("  a) add    e) edit    d) directories    r) remove    b) back")
        choice = ask("action", "b")
        try:
            if choice == "b":
                return
            if choice == "a":
                edit_project(cfg, None)
            elif choice == "e" and cfg.projects:
                edit_project(cfg, pick(cfg.projects, "edit which?"))
            elif choice == "d" and cfg.projects:
                edit_directories(cfg, pick(cfg.projects, "directories for which?"))
            elif choice == "r" and cfg.projects:
                p = pick(cfg.projects, "remove which?")
                if ask_yes_no(f"remove {p.get('name')}?", False):
                    cfg.projects.remove(p)
                    cfg.dirty = True
        except Abort:
            print(DIM("  (cancelled)"))


# ---------------------------------------------------------------------------
# reachability
# ---------------------------------------------------------------------------

def test_reachability(cfg: Config) -> None:
    """ssh only when the user asks for it — this is a menu item they chose, and
    it runs the same BatchMode probe the bridge does, so a PASS here means the
    bridge's ssh will work rather than merely that the host resolves."""
    rule("ssh reachability")
    ssh_envs = [e for e in cfg.envs if e.get("type") == "ssh"]
    if not ssh_envs:
        print(DIM("  no ssh environments declared — nothing to probe"))
        return
    for env in ssh_envs:
        target = ssh_target(env)
        print(f"  {env.get('name')} ({target}) … ", end="", flush=True)
        try:
            p = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, "true"],
                capture_output=True, text=True, timeout=25)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(RED(f"FAIL ({e.__class__.__name__})"))
            continue
        if p.returncode == 0:
            print(GREEN("OK"))
        else:
            print(RED("FAIL"))
            detail = (p.stderr or "").strip().splitlines()
            if detail:
                print(f"      {detail[-1]}")
            print(DIM("      The bridge uses BatchMode, so it never prompts for a password."))
            print(DIM("      fix: open `ssh " + str(env.get('host')) + "` in a separate terminal"))
            print(DIM("      and leave it open — every bridge call rides that ControlMaster socket."))


# ---------------------------------------------------------------------------
# show / main
# ---------------------------------------------------------------------------

def show(cfg: Config) -> None:
    rule(f"{cfg.path.name}")
    if not cfg.path.exists():
        print(YELLOW("  not created yet — the HPC bridge is disabled until it exists"))
        print(DIM("  (everything local — install, freeze, seal — works without it)"))
    if cfg.load_error:
        print(YELLOW(f"  {cfg.load_error}"))
    for env in cfg.envs:
        print(f"\n  {BOLD(env.get('name', '?'))}  {describe_env(env)}")
        for key, _, _ in ZONES:
            blk = env.get(key)
            if blk:
                print(f"      {key:<26} {blk.get('path')}  {blk.get('permissions')}")
        if env.get("slurm"):
            print(f"      {'slurm':<26} {env['slurm']}")
    for p in cfg.projects:
        print(f"\n  {BOLD(p.get('name', '?'))}  envs: {', '.join(p.get('compute_envs') or [])}")
        for d in p.get("directories") or []:
            print(f"      [{d.get('env')}] {d.get('path')}  {d.get('permissions')}")
    err = cfg.validate()
    print()
    print(GREEN("  valid — the agent's loader accepts this configuration") if not err
          else RED(f"  invalid: {err}"))


def status_line(cfg: Config) -> str:
    err = cfg.validate()
    state = GREEN("valid") if not err else RED("invalid")
    mark = YELLOW("  * unsaved changes") if cfg.dirty else ""
    return (f"  {cfg.path.name}: {state}   "
            f"{len(cfg.envs)} compute env(s), {len(cfg.projects)} project(s){mark}")


def offer_template(cfg: Config) -> None:
    rule("no configuration yet")
    print("  The HPC bridge needs projects_access.yaml: which clusters exist, which")
    print("  projects may use them, and exactly which directories the agent may touch.")
    print(DIM("  Nothing local depends on it — install, freeze and seal work without it."))
    print()
    if ask_yes_no("build one now?", True):
        try:
            edit_env(cfg, None)
        except Abort:
            print(DIM("  (cancelled)"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--show", action="store_true", help="print the configuration and exit")
    ap.add_argument("--validate", action="store_true",
                    help="validate and exit; rc 0 when the agent's loader accepts it")
    ap.add_argument("--file", default=None, help="configuration file (default: ./projects_access.yaml)")
    args = ap.parse_args()

    path = Path(args.file) if args.file else default_access_path()
    cfg = Config(path)

    if args.validate:
        err = cfg.validate()
        print(f"{path}: " + ("valid" if not err else f"invalid\n  {err}"))
        return 0 if not err else 1
    if args.show:
        show(cfg)
        return 0

    if not sys.stdin.isatty():
        print("configure.py is an interactive menu and stdin is not a terminal.", file=sys.stderr)
        print("Use --show or --validate for a non-interactive read.", file=sys.stderr)
        return 2

    print(BOLD("\nbioinf-agent — configuration"))
    print(DIM(f"  {path}"))
    if not path.exists():
        try:
            offer_template(cfg)
        except Abort:
            print(DIM("  (cancelled)"))

    while True:
        rule()
        print(status_line(cfg))
        if cfg.load_error:
            print(YELLOW(f"  {cfg.load_error}"))
        print()
        print("  1) compute environments   add / edit / remove")
        print("  2) projects               add / edit / remove")
        print("  3) show                   the full configuration")
        print("  4) test ssh reachability")
        print("  5) save                   6) reload from disk      q) quit")
        try:
            choice = ask("choose", "1")
        except Abort:
            choice = "q"
        actions: dict[str, Callable[[], Any]] = {
            "1": lambda: menu_envs(cfg),
            "2": lambda: menu_projects(cfg),
            "3": lambda: show(cfg),
            "4": lambda: test_reachability(cfg),
            "5": cfg.save,
            "6": cfg.reload,
        }
        if choice == "q":
            if cfg.dirty and not ask_yes_no("unsaved changes — quit anyway?", False):
                continue
            print()
            return 0
        action = actions.get(choice)
        if action:
            try:
                action()
            except Abort:
                print(DIM("  (cancelled)"))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n(interrupted — nothing was written)")
        sys.exit(130)
    except Abort:
        # End of input at a prompt. Nothing is written — every mutation is staged
        # in memory until an explicit save.
        print("\n(end of input — nothing was written)")
        sys.exit(130)
