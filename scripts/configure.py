#!/usr/bin/env python3
"""configure.py — the configuration menu for projects_access.yaml.

`projects_access.yaml` is the agent's command-and-control file: which compute
environments exist, which projects may use them, and exactly which directories
the agent is allowed to list, upload to, download from, or run jobs in. Nothing
in the HPC bridge works without it, and every path in it is a permission grant.

Run via `./scripts/config.sh` (which resolves the runtime interpreter); pass
`--web` for the browser rendering of the same menu (scripts/config_web.py —
both renderers read the ONE field spec declared in this module: ZONES,
PERMISSION_GLOSSES, TRANSFER_TYPES, MODULE_PLACEHOLDERS, the zone-path
defaults, and the loader's PROJECT_NAME_RE). This module drives the menu; it
does NOT define what a valid configuration is — every save is checked by
`agent.skills.compute_access.load_access`, the same loader the agent itself
uses, so the menu cannot bless a file the agent will later refuse. The
annotated schema reference is `agent/skills/projects_access.yaml.example`; an
agent authoring the file non-interactively writes that YAML directly and
checks it with `--validate`.

The file is rewritten on save, which drops hand-written comments; the previous
version is copied to `projects_access.yaml.bak` first and the menu says so.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402  (runtime env only — config.sh guarantees it)

from agent.skills import workspace  # noqa: E402
from agent.skills.compute_access import (  # noqa: E402
    PERMISSIONS,
    PROJECT_NAME_RE,
    VALID_JOB_MANAGERS,
    ConfigError,
    _UUID_RE,
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

#: What each token GRANTS, one axis each — `upload` is about putting bytes,
#: `exec` about running jobs, and the glosses must keep them distinguishable.
#: Every renderer (terminal prompt, web page) reads THIS dict; a second
#: spelling of a permission's meaning is how the two drift.
PERMISSION_GLOSSES = {
    "file_name_only": "list the file and dir names under this path — never "
                      "file contents; a deep listing is capped per call and "
                      "says so when truncated",
    "upload":         "put new files into this dir or anywhere under it (never overwrites)",
    "download":       "fetch files from this dir or anywhere under it back to this machine",
    "exec":           "run jobs that use this dir as their working directory — "
                      "outputs land in place; this alone does not let the agent "
                      "list, push or fetch",
    "none":           "no access (placeholder — a dir with only this is unreachable)",
}
assert set(PERMISSION_GLOSSES) == set(PERMISSIONS), "every token needs a gloss"

TRANSFER_TYPES = ["scp_head_node", "globus"]

#: Placeholder Lmod module names shown while a cluster's real ones are unknown
#: (`cluster_module_avail` discovers the real ones once the env is reachable).
#: One spelling for both renderers.
MODULE_PLACEHOLDERS = {"apptainer_module": "apptainer/1.5.0",
                       "nextflow_module": "nextflow/25.04.7"}

#: Least-privilege default for a freshly granted project directory.
DIR_DEFAULT_PERMS = ["file_name_only"]

#: The env-level zones, in the order the agent uses them. `key` is the schema
#: key, `default_perms` the tokens the bridge needs to use the zone as intended
#: (the menu's "defaults" reset writes exactly these), `required` whether the
#: menu insists the zone be declared. ALL FOUR are required (menu review,
#: 2026-09-18): scratch + common_data are what the run primitives refuse
#: without, containers is where every staged .sif lands, and reports is where
#: the record mirrors — an env missing any of them fails far from where it was
#: typed. Same four zones the local workspace has — full parity, so a
#: production run is the same kind of thing on either locus.
ZONES = [
    ("agent_scratch_target", "agent sandbox — job working dirs, logs, per-run staging",
     ["file_name_only", "upload", "download", "exec"], True),
    ("agent_common_data_target", "shared reference data — genomes, public databases",
     ["file_name_only", "upload", "download", "exec"], True),
    # `download` has no consumer in the staging flow (verification is a remote
    # checksum) but is granted by default so a staged .sif can be pulled back
    # when ever needed (user call, menu review 2026-09-18).
    ("container_upload_target", "where .sif container images are staged",
     ["file_name_only", "upload", "download"], True),
    # No `exec`: reports are read, never run.
    ("agent_reports_target", "the record — ENV/RUN reports mirrored next to the .sif",
     ["file_name_only", "upload", "download"], True),
]

ZONE_LABELS = {
    "agent_scratch_target": "scratch",
    "agent_common_data_target": "common_data",
    "container_upload_target": "containers",
    "agent_reports_target": "reports",
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
    "agent_reports_target", "data_transfer", "slurm",
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


def path_source(path: Path) -> str:
    """WHERE the menu's file path came from, in words — so "is this the right
    file?" is answerable from either renderer instead of from source code.
    The default is the FIXED machine-level home, ~/.bioinf_agent/ — the
    ~/.ssh-config pattern: per machine, hidden, never in a checkout (the file
    carries real hostnames and outlives any clone)."""
    if path != default_access_path():
        return "an explicit --file override — NOT the agent's default path"
    if os.environ.get("BIOINF_PROJECTS_ACCESS", "").strip():
        return ("chosen by $BIOINF_PROJECTS_ACCESS — the same path the agent "
                "reads, so this menu and the agent read one file")
    return ("the agent's fixed config home (~/.bioinf_agent/, like "
            "~/.ssh/config) — the same path the agent reads, so this menu "
            "and the agent read one file")


def dump(data: dict) -> str:
    """Serialize the document. One spelling, because the text this produces is
    what the user reads and hand-edits afterwards: `allow_unicode` keeps em-dashes
    from becoming \\u2014 escapes, and `default_flow_style=None` renders leaf lists
    inline (`permissions: [upload, exec]`), matching the annotated example."""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                          default_flow_style=None, width=100)


class Abort(Exception):
    """The user backed out of a sub-flow. Nothing is committed."""


def _print_notice(kind: str, text: str) -> None:
    """Default `Config.notify` sink — the terminal rendering of save messages."""
    color = {"ok": GREEN, "error": RED}.get(kind, DIM)
    print(color(f"  {text}"))


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
    print(DIM("    independent grants, not a ladder — upload does not imply download"))
    for tok in PERMISSION_ORDER:
        if tok != "none":
            print(DIM(f"    {tok:<15} {PERMISSION_GLOSSES[tok]}"))
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
    `save`, and `save` refuses a document the agent's own loader rejects.

    `notify` receives every message `save` produces, as (kind, text) with kind in
    {ok, error, note}. The default renders to the terminal; the web app passes a
    collector so the SAME save path (validate → .bak → write) serves both
    renderers instead of the page growing its own."""

    def __init__(self, path: Path, notify: Optional[Callable[[str, str], None]] = None):
        self.path = path
        self.data: dict[str, Any] = {"compute_envs": [], "projects": []}
        self.dirty = False
        self.load_error = ""
        self._notify = notify or _print_notice
        self.reload()

    def reload(self) -> None:
        self.dirty = False
        self.load_error = ""
        if not self.path.exists():
            self.data = {"compute_envs": [], "projects": []}
            # The config home moved to ~/.bioinf_agent/ (2026-09-18). A file
            # still sitting at the old workspace-root location would otherwise
            # read as "nothing configured" — absence with a findable cause is
            # stated, with the one-command fix.
            legacy = workspace.workspace_root() / "projects_access.yaml"
            if self.path == default_access_path() and legacy.exists():
                self.load_error = (
                    f"found a configuration at the LEGACY location {legacy} — "
                    f"this menu and the agent now read {self.path}; adopt it "
                    f"with: mv {legacy} {self.path}")
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

    def state(self) -> tuple[str, str]:
        """The configuration's state, in THREE values — `absent`, `valid`,
        `invalid` — and the message that goes with it.

        Every surface that REPORTS on the configuration reads this, not
        `validate`. `validate` asks whether the in-memory document would be
        accepted, and an empty document is accepted; a path holding no file must
        not answer that question with `valid`. Staged-but-unsaved work is graded
        on the document, because the useful question there is whether `save`
        will take it.
        """
        if not self.path.exists() and not self.dirty:
            return "absent", f"no configuration file at {self.path}"
        err = self.validate()
        return ("valid", "") if not err else ("invalid", err)

    def save(self) -> bool:
        err = self.validate()
        if err:
            self._notify("error", "NOT saved — the agent's loader rejects this "
                                  f"configuration: {err}")
            self._notify("note", "fix it in the menu, or quit without saving to "
                                 "keep the file on disk.")
            return False
        backup = ""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                backup = str(self.path) + ".bak"
                shutil.copy2(self.path, backup)
            self.path.write_text(HEADER + "\n" + dump(self.data))
        except OSError as e:
            # Returning False keeps the menu open with the document intact. An
            # unwritable destination must cost the user the save, not the work:
            # raising here unwinds out of the menu loop and everything staged in
            # this session is gone.
            self._notify("error", f"NOT saved — could not write {self.path}: {e}")
            self._notify("note", "the configuration is still loaded here; fix the "
                                 "path or permissions and save again.")
            return False
        self.dirty = False
        self._notify("ok", f"saved {self.path}")
        if backup:
            self._notify("note", f"previous version: {backup}")
        return True


# ---------------------------------------------------------------------------
# defaults — the placeholders that make a first configuration a few keystrokes
# ---------------------------------------------------------------------------

def ssh_defaults(user: str) -> dict[str, str]:
    """Conventional cluster layout. Every zone is a disjoint subtree, which the
    validator requires — a breach of one zone must not reach another."""
    base = f"/scratch/{user}"
    return {
        "agent_reports_target": f"{base}/CLAUDE_REPORTS/",
        "agent_scratch_target": f"{base}/CLAUDE_SCRATCH/",
        "agent_common_data_target": f"{base}/CLAUDE_GENOMES/",
        "container_upload_target": f"{base}/CLAUDE_CONTAINERS/",
    }


def local_defaults() -> dict[str, str]:
    """Conventional local layout — the four zones FLAT under
    ~/bioinf_workspace, mirroring how `ssh_defaults` is a convention
    (/scratch/{user}/CLAUDE_*) rather than a resolver lookup. A menu default
    only, machine-independent on purpose: routing it through workspace_root()
    made the offered paths follow this machine's pointer into whatever dir an
    older setup recorded, which read as broken. The zone names ARE the
    directory names, so the structure explains itself."""
    base = Path.home() / workspace.DEFAULT_WORKSPACE_NAME
    return {
        "agent_scratch_target": f"{base}/scratch/",
        "agent_common_data_target": f"{base}/common_data/",
        "container_upload_target": f"{base}/containers/",
        "agent_reports_target": f"{base}/reports/",
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
    zones = [ZONE_LABELS[k] for k, _, _, _ in ZONES if env.get(k)]
    bits.append("zones: " + (", ".join(zones) if zones else DIM("none")))
    dt = (env.get("data_transfer") or {}).get("type")
    if dt:
        bits.append(dt)
    return "  ".join(bits)


def note_if_missing_locally(env: dict, path: str) -> None:
    """CS60: on a `type: local` env the path is on THIS filesystem, so checking
    costs nothing. A note, never a refusal and never a mkdir — the standing rule
    is that the agent does not create directories on the user's behalf."""
    if env.get("type") == "local" and not Path(path).is_dir():
        print(YELLOW(f"  note: {path} does not exist yet (accepted — nothing "
                     f"creates it for you)"))


def edit_zone(env: dict, key: str, purpose: str, default_perms: list[str],
              path_default: str, required: bool) -> None:
    current = env.get(key)
    rule(key)
    print(DIM(f"  {purpose}"))
    if required:
        # A required zone is not offered a decline — declining here just moves
        # the failure to drive time, where the message is about a job instead
        # of a config line.
        if current:
            print(f"  current: {current.get('path')}  {current.get('permissions')}")
    elif current:
        print(f"  current: {current.get('path')}  {current.get('permissions')}")
        if not ask_yes_no("keep this zone?", True):
            env.pop(key, None)
            print(DIM(f"  {key} removed"))
            return
    elif not ask_yes_no(f"declare {key}?", True):
        env.pop(key, None)
        return
    path = ask_abs_path("path", (current or {}).get("path") or path_default)
    note_if_missing_locally(env, path)
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


def _globus_cli() -> str:
    """The globus CLI, if installed. Resolved at use, never at import — the menu
    must work on a machine that will never touch Globus. PATH first, then the
    runtime env's bin/ — setup installs globus-cli THERE, and this menu may be
    the first thing that runs after setup, on a shell whose PATH never saw it."""
    found = shutil.which("globus")
    if found:
        return found
    runtime_copy = Path(sys.executable).parent / "globus"
    return str(runtime_copy) if os.access(runtime_copy, os.X_OK) else ""


_GLOBUS_INSTALL_HINT = ("globus CLI not found — re-run ./scripts/setup.sh (it installs "
                        "globus-cli into the runtime env), then `globus login` — or "
                        "type the UUIDs by hand below")


def _globus_search(query: str) -> tuple[list[dict], str]:
    """`globus endpoint search` → ([{id, display_name, owner_string}], "") or
    ([], why-not). Any CLI failure degrades to manual entry, never blocks."""
    exe = _globus_cli()
    if not exe:
        return [], _GLOBUS_INSTALL_HINT
    if query.startswith("-"):
        return [], "search text may not start with '-' (it would read as a CLI option)"
    try:
        p = subprocess.run([exe, "endpoint", "search", query, "-F", "json",
                            "--limit", "10"],
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return [], "globus endpoint search timed out after 30s"
    if p.returncode != 0:
        tail = ((p.stderr or p.stdout).strip().splitlines() or ["?"])[-1]
        return [], f"globus endpoint search failed: {tail}"
    try:
        rows = json.loads(p.stdout).get("DATA") or []
    except (json.JSONDecodeError, AttributeError):
        return [], "globus endpoint search returned unparseable output"
    return [r for r in rows if isinstance(r, dict) and r.get("id")], ""


def _globus_local_id() -> str:
    """This machine's Globus Connect Personal endpoint, or "". The one UUID that
    never needs a search — the CLI knows it outright."""
    exe = _globus_cli()
    if not exe:
        return ""
    try:
        p = subprocess.run([exe, "endpoint", "local-id"],
                           capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return ""
    out = p.stdout.strip()
    return out if p.returncode == 0 and _UUID_RE.match(out) else ""


def _pick_globus_endpoint(which: str, cur_id: str, cur_name: str) -> tuple[str, str]:
    """Search-and-pick one endpoint, so the UUID is READ off Globus rather than
    typed. Falls back to manual UUID entry on decline or any CLI failure."""
    if ask_yes_no(f"search Globus for the {which} endpoint by name?", not cur_id):
        rows, why = _globus_search(ask("search text (the endpoint's display name)",
                                       cur_name))
        if why:
            print(YELLOW(f"  {why}"))
        elif not rows:
            print(YELLOW("  no endpoints matched — check the spelling, or enter "
                         "the UUID by hand"))
        else:
            for i, r in enumerate(rows, 1):
                print(f"    {i}) {r.get('display_name') or '?'}  "
                      f"{DIM(r['id'])}  {DIM(r.get('owner_string') or '')}")
            raw = ask("choose (number; blank to enter the UUID by hand)", "",
                      allow_empty=True)
            if raw.isdigit() and 1 <= int(raw) <= len(rows):
                r = rows[int(raw) - 1]
                return r["id"], r.get("display_name") or cur_name or "?"
    return (ask(f"{which} endpoint UUID", cur_id),
            ask(f"{which} endpoint display name", cur_name))


def edit_transfer(env: dict) -> None:
    rule("data transfer (optional)")
    print(DIM("  How bytes move to/from this env. scp_head_node is the default and"))
    print(DIM("  needs no setup; globus is off-head-node and checksummed end-to-end."))
    current = env.get("data_transfer") or {}
    if not ask_yes_no("declare a data_transfer block?", bool(current)):
        env.pop("data_transfer", None)
        return
    ttype = ask_choice("protocol", TRANSFER_TYPES,
                       current.get("type") or "scp_head_node")
    blk: dict[str, Any] = {"type": ttype}
    if ttype == "globus":
        g = current.get("globus") or {}
        lid, lname = g.get("local_endpoint_id", ""), g.get("local_endpoint_name", "")
        detected = "" if lid else _globus_local_id()
        if detected:
            print(DIM(f"  local endpoint detected (`globus endpoint local-id`): {detected}"))
        lid = ask("local endpoint UUID (this machine's Globus Connect Personal)",
                  lid or detected)
        lname = ask("local endpoint display name", lname)
        rid, rname = _pick_globus_endpoint("remote", g.get("remote_endpoint_id", ""),
                                           g.get("remote_endpoint_name", ""))
        blk["globus"] = {
            "local_endpoint_id": lid,
            "local_endpoint_name": lname,
            "remote_endpoint_id": rid,
            "remote_endpoint_name": rname,
        }
        print(DIM("  note: a read-only `globus ls` succeeding does NOT prove transfers"))
        print(DIM("  will work — verify with one small test transfer before anything big."))
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
        for key, placeholder in MODULE_PLACEHOLDERS.items():
            val = ask_optional(key, src.get(key) or placeholder)
            if val:
                new[key] = val
        defaults = ssh_defaults(user or "USER")
    else:
        defaults = local_defaults()

    for key, purpose, perms, required in ZONES:
        new[key] = src.get(key)              # carry current so edit_zone sees it
        edit_zone(new, key, purpose, perms, defaults[key], required)

    # CS59: data_transfer and slurm are NETWORK/scheduler questions. A local env
    # is asked neither — but hand-written blocks are carried through untouched
    # rather than silently dropped on the next edit (the loader accepts both on
    # any env type, and this menu must not be lossier than the loader).
    new["data_transfer"] = src.get("data_transfer")
    new["slurm"] = src.get("slurm")
    if etype == "ssh":
        edit_transfer(new)
        edit_slurm(new)
    else:
        kept = [k for k in ("data_transfer", "slurm") if new.get(k)]
        if kept:
            print(DIM(f"  {' + '.join(kept)}: kept as-is (declared in the file; "
                      f"a local env does not use them)"))

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
                env = next((e for e in cfg.envs if e.get("name") == env_name), {})
                path = ask_abs_path("absolute path (e.g. /work/mylab/rnaseq_2026)", "")
                note_if_missing_locally(env, path)
                perms = ask_permissions(list(DIR_DEFAULT_PERMS))
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

    while True:
        name = ask("name (also the auto-prefix in scratch; letters, digits, . _ and - only)",
                   src.get("name", ""))
        if PROJECT_NAME_RE.match(name):
            break
        bad = sorted({ch for ch in name if not re.match(r"[A-Za-z0-9._-]", ch)})
        # The name becomes a path component — enforce the rule the prompt states,
        # here where the fix is one keystroke away rather than in a job launcher.
        print(RED(f"  {name!r} breaks the stated rule"
                  + (f" (illegal: {' '.join(map(repr, bad))})" if bad else "")
                  + " — letters, digits, . _ and - only"))
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


def projects_unlocked(cfg: Config) -> bool:
    """D10, revised in menu review (2026-09-18): a project names the compute
    environments its work runs on — and, on remote machines, the directory
    grants into the user's territory. With no env declared there is nothing a
    project could name, so the section unlocks once ANY compute env exists,
    local or ssh — or when the file already holds projects, which must stay
    editable regardless of how they got there."""
    return bool(cfg.projects) or bool(cfg.envs)


#: One spelling of the D10 lock explanation, read by both renderers.
PROJECTS_LOCKED_NOTE = (
    "A project names which compute environments its work runs on — and, for "
    "remote machines, exactly which of YOUR directories the agent may touch. "
    "It unlocks once a compute environment (local or ssh) is declared, because "
    "a project must name at least one env to compute on.")


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
        for key, _, _, _ in ZONES:
            blk = env.get(key)
            if blk:
                print(f"      {key:<26} {blk.get('path')}  {blk.get('permissions')}")
        if env.get("slurm"):
            print(f"      {'slurm':<26} {env['slurm']}")
    for p in cfg.projects:
        print(f"\n  {BOLD(p.get('name', '?'))}  envs: {', '.join(p.get('compute_envs') or [])}")
        for d in p.get("directories") or []:
            print(f"      [{d.get('env')}] {d.get('path')}  {d.get('permissions')}")
    state, msg = cfg.state()
    print()
    if state == "absent":
        print(YELLOW(f"  {msg}"))
        print(DIM("  nothing is configured — the HPC bridge is unavailable. Local "
                  "install, freeze and seal do not need this file."))
    elif state == "valid":
        print(GREEN("  valid — the agent's loader accepts this configuration"))
    else:
        print(RED(f"  invalid: {msg}"))


def status_line(cfg: Config) -> str:
    state, _ = cfg.state()
    shown = {"valid": GREEN("valid"), "invalid": RED("invalid"),
             "absent": YELLOW("not created yet")}[state]
    mark = YELLOW("  * unsaved changes") if cfg.dirty else ""
    return (f"  {cfg.path.name}: {shown}   "
            f"{len(cfg.envs)} compute env(s), {len(cfg.projects)} project(s){mark}")


def offer_template(cfg: Config) -> None:
    rule("no configuration yet")
    print("  projects_access.yaml declares where the agent may RUN and TOUCH things:")
    print("  compute environments (this machine, an HPC cluster, or both) and — for")
    print("  remote machines — exactly which of your directories it may reach.")
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
                    help="validate and exit; rc 0 accepted, 1 rejected, 2 no file to check")
    ap.add_argument("--web", action="store_true",
                    help="open the browser menu instead (127.0.0.1 only; the page's "
                         "close buttons end it, as does Ctrl-C)")
    ap.add_argument("--port", type=int, default=0,
                    help="port for --web (default: an ephemeral free port)")
    ap.add_argument("--file", default=None,
                    help="configuration file (default: <workspace>/projects_access.yaml)")
    args = ap.parse_args()

    path = Path(args.file) if args.file else default_access_path()
    cfg = Config(path)

    if args.web:
        sys.path.insert(0, str(ROOT / "scripts"))
        import config_web  # noqa: E402  (needs starlette/uvicorn — runtime env only)
        return config_web.serve(path, sys.modules[__name__], port=args.port)

    if args.validate:
        state, msg = cfg.state()
        if state == "absent":
            print(f"{path}: no configuration file — nothing to validate")
            return 2
        print(f"{path}: " + ("valid" if state == "valid" else f"invalid\n  {msg}"))
        return 0 if state == "valid" else 1
    if args.show:
        show(cfg)
        return 0

    if not sys.stdin.isatty():
        print("configure.py is an interactive menu and stdin is not a terminal.", file=sys.stderr)
        print("Use --show or --validate for a non-interactive read, or --web for the "
              "browser menu.", file=sys.stderr)
        print("(An agent authoring this file should write the YAML directly — schema: "
              "agent/skills/projects_access.yaml.example — then check it with --validate.)",
              file=sys.stderr)
        return 2

    print(BOLD("\nbioinf-agent — configuration"))
    print(DIM(f"  {path}"))
    print(DIM(f"  ({path_source(path)})"))
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
        unlocked = projects_unlocked(cfg)
        print("  1) compute environments   add / edit / remove")
        if unlocked:
            print("  2) projects               add / edit / remove")
        else:
            print(DIM("  2) projects               (locked — needs a compute env; "
                      "choose it to see why)"))
        print("  3) show                   the full configuration")
        print("  4) test ssh reachability")
        print("  5) save                   6) reload from disk      q) quit")
        try:
            choice = ask("choose", "1")
        except Abort:
            choice = "q"

        def explain_lock() -> None:
            print(DIM(f"  {PROJECTS_LOCKED_NOTE}"))

        actions: dict[str, Callable[[], Any]] = {
            "1": lambda: menu_envs(cfg),
            "2": (lambda: menu_projects(cfg)) if unlocked else explain_lock,
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
