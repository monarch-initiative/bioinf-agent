"""Where things live: the code checkout, and the workspace the agent writes into.

Two roots, and the whole point is that they are NOT the same directory.

``code_root()``
    The git checkout. Holds code, ``config/``, ``scripts/``, and the per-clone
    runtime env (``.conda_runtime`` / ``.miniforge``). Everything here either is
    tracked or dies with the clone.

``workspace_root()``
    Everything the agent produces: conda envs, container tarballs, ENV/RUN
    reports, sealed specs, job state, transfer receipts. These outlive any
    checkout — they are the product — so they must not sit inside one.

``resources_root()``
    Reference genomes and test datasets. A peer of the workspace zones rather
    than a child, because it is the one zone likely to already exist as a shared
    institutional mount; it defaults inside the umbrella so a first-time user
    faces one location question, not two.

THE RULE IS ABOUT LIFETIME, NOT LOCATION. "No generated file under the
checkout" is the slogan, but it is false as stated: ``.conda_runtime/`` is
generated and must stay, because ``.mcp.json`` names ``./.conda_runtime/bin/python``.
The real rule is that the checkout may hold what dies with it and never what
outlives it. ``tests/test_workspace_resolution.py`` carries the allowlist for the
first category, and its purpose is to make ADDING to that list expensive.

RESOLUTION IS DETERMINISTIC AND NEVER RAISES.

Order: ``$BIOINF_WORKSPACE``, else the single-line pointer file
``.bioinf_workspace`` in the checkout (written by ``scripts/setup.sh``), else
``~/bioinf_agent``.

The fallback is ONE path, unconditionally — not "``~/Desktop/bioinf_agent`` when
a Desktop exists". Setup's *prompt* prefers Desktop, because a human sees and
confirms that choice; resolution must not, or a machine that grows a Desktop
folder later silently answers the question differently than it did yesterday.
That is the shape of the defect this module exists to remove: the config menu
wrote to one path while the doctor read another, and three surfaces reported
"valid" for a file the agent could not see.

Nothing here raises, and that is deliberate: ``mcp_server`` builds artifact paths
at IMPORT, so a resolver that refuses an unusual workspace costs the agent its
entire tool surface rather than costing it one directory. Unusable workspaces are
diagnosed loudly by ``scripts/doctor.py`` and at setup time, where a human can act
on the message. See ``home_containment_error``.
"""
from __future__ import annotations

import os
from pathlib import Path

#: The pointer file, relative to the checkout. One line: the workspace path.
#: Gitignored — it is per-machine, like the runtime env it sits beside.
POINTER_FILENAME = ".bioinf_workspace"

#: Resolution fallback when neither the env var nor the pointer file answers.
#: Deliberately not environment-sensitive; see the module docstring.
DEFAULT_WORKSPACE_NAME = "bioinf_agent"


def code_root() -> Path:
    """The git checkout — this file is ``<root>/agent/skills/workspace.py``."""
    return Path(__file__).resolve().parents[2]


def _pointer_path() -> Path:
    return code_root() / POINTER_FILENAME


def _read_pointer() -> str:
    """The workspace path recorded in the pointer file, or "".

    Tolerant on purpose: a human may edit this file, so blank lines and a
    leading ``#`` comment are skipped rather than treated as a path.
    """
    p = _pointer_path()
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        pass
    return ""


def workspace_source() -> str:
    """WHICH of the three answers resolution used: ``env`` · ``pointer`` ·
    ``default``. Reported by the doctor and by ``agent_status``, so a surprising
    path can be traced to the thing that chose it without reading this code."""
    if os.environ.get("BIOINF_WORKSPACE", "").strip():
        return "env"
    return "pointer" if _read_pointer() else "default"


def workspace_root() -> Path:
    """The artifact umbrella. Never raises; see the module docstring.

    A pointer holding something that is not a path (an embedded NUL, a partial
    write) falls back rather than propagating: `Path.resolve` raises on those,
    and this function is on the import path of every tool the agent has.
    """
    raw = os.environ.get("BIOINF_WORKSPACE", "").strip() or _read_pointer()
    if raw:
        try:
            return Path(raw).expanduser().resolve()
        except (OSError, ValueError):
            pass
    return Path.home() / DEFAULT_WORKSPACE_NAME


def write_pointer(path: Path | str) -> Path:
    """Record the chosen workspace in the checkout. Returns the pointer path.

    Called by setup after the user answers. Writing the resolved absolute path
    rather than what was typed means a later ``cd`` cannot change where the
    agent looks.
    """
    resolved = Path(path).expanduser().resolve()
    pointer = _pointer_path()
    pointer.write_text(
        "# The bioinf-agent workspace: where every generated artifact lives.\n"
        "# Written by scripts/setup.sh. Override with $BIOINF_WORKSPACE.\n"
        f"{resolved}\n"
    )
    return pointer


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------
# Four, and each answers "may I delete this?" differently — which is what makes
# the taxonomy teachable rather than arbitrary:
#
#   scratch      delete freely            never share
#   environments delete, rebuild from the recipe   share as registry image + .sif
#   resources    expensive to refetch     share aggressively
#   reports      NEVER delete — the record   it IS the deliverable
#
# Local zones auto-create on demand. The no-auto-mkdir rule is a CLUSTER rule
# about the user's territory; here the agent owns these directories, and a
# resolver that raised at import would leave the server with no tools at all.


def _zone(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def environments_dir() -> Path:
    """Parent of the two build-artifact zones. Rarely wanted directly."""
    return _zone(workspace_root() / "environments")


def conda_envs_dir() -> Path:
    """Host conda envs — pre-freeze iteration. Rebuildable from the recipe."""
    return _zone(workspace_root() / "environments" / "conda")


def images_dir() -> Path:
    """Container tarballs staged for Apptainer conversion. Rebuildable."""
    return _zone(workspace_root() / "environments" / "images")


def reports_dir() -> Path:
    """ENV reports, attestations, recipes, sealed WorkflowSpecs, RUN dashboards.

    The product. Small, textual, portable — what an auditor or a paper's
    supplement receives. A peer of the other zones, not a child of any.
    """
    return _zone(workspace_root() / "reports")


def scratch_dir(*parts: str) -> Path:
    """Transient state: job status, pipeline drafts, render staging, receipts.

    ``scratch_dir("jobs")`` rather than ``scratch_dir() / "jobs"`` so the
    subdirectory is created too — every caller wanted that and half of them
    remembered.
    """
    return _zone(workspace_root().joinpath("scratch", *parts))


def _resources_path() -> Path:
    raw = os.environ.get("BIOINF_RESOURCES", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return workspace_root() / "resources"


def resources_root() -> Path:
    """Reference genomes and test datasets.

    Relocatable via ``$BIOINF_RESOURCES`` for the shared-mount case; defaults
    inside the umbrella so first-time setup asks one question.
    """
    return _zone(_resources_path())


def projects_access_path() -> Path:
    """The operator's command-and-control file.

    At the workspace root, not in the checkout: it describes a compute world
    (clusters, accounts, directories) that outlives any clone, and it holds real
    hostnames and usernames, so a checkout is the wrong container for it in two
    independent ways.
    """
    return workspace_root() / "projects_access.yaml"


def zones() -> dict[str, str]:
    """Every resolved location, for the doctor and ``agent_status``.

    A layout change invalidates every "look in env_reports/" habit an agent or a
    user has, so the resolved paths have to be readable from inside the running
    system rather than inferred from this file.

    DESCRIBES, never creates. The zone accessors above mkdir on demand because
    their caller is about to write; a report that conjured the directories it is
    reporting on could never say one was missing.
    """
    root = workspace_root()
    return {
        "code_root":        str(code_root()),
        "workspace_root":   str(root),
        "workspace_source": workspace_source(),
        "environments":     str(root / "environments"),
        "conda_envs":       str(root / "environments" / "conda"),
        "images":           str(root / "environments" / "images"),
        "reports":          str(root / "reports"),
        "scratch":          str(root / "scratch"),
        "resources":        str(_resources_path()),
        "projects_access":  str(root / "projects_access.yaml"),
    }


# ---------------------------------------------------------------------------
# Guards — reported, never enforced at import
# ---------------------------------------------------------------------------

def home_containment_error(path: Path | str | None = None) -> str:
    """Why Docker will refuse to bind-mount this workspace, or "" if it won't.

    Docker Desktop shares a fixed set of host prefixes with the VM; a path
    outside them mounts as an empty directory rather than failing loudly, so a
    freeze validating "inside the image" would validate against nothing. Keeping
    the workspace under ``$HOME`` sidesteps the whole question.

    One implementation, two callers (setup and the doctor) — a containment rule
    re-typed at the second call site is how the two come to disagree about which
    machines are usable.
    """
    target = Path(path) if path is not None else workspace_root()
    try:
        target.resolve().relative_to(Path.home().resolve())
    except ValueError:
        return (f"{target} is outside $HOME ({Path.home()}). Docker bind-mounts "
                f"resolve against the Docker VM's shared prefixes, so an env "
                f"frozen here would validate against an empty directory. "
                f"Choose a workspace under $HOME.")
    return ""
