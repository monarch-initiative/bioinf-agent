"""Where things live: the SYSTEM (the repo) and the ARTIFACTS (outside it).

The paradigm: tools/system in one place, the data and artifacts they produce
in another. Concretely:

``code_root()`` — THE SYSTEM
    The git checkout, self-contained: code, the per-clone conda + runtime
    (``.miniforge`` / ``.conda_runtime``), the tool environments it bootstraps
    (``envs/``) and the core/test data it validates with (``resources/``) —
    the last two generated, untracked, and rebuilt by setup. Clone it and it
    works; delete it and the *system* is gone, but never your artifacts.

``workspace_root()`` — THE ARTIFACTS
    Everything the agent PRODUCES: ENV/RUN reports, build recipes, sealed
    specs (``reports/``), staged container tarballs (``containers/``), job
    state and drafts (``scratch/``). Default ``~/bioinf_workspace`` —
    deliberately the same place the config menu offers as the local compute
    env's zone defaults, so one folder holds everything the agent makes.
    A NEW VERSION OF THE SYSTEM PLUGS INTO THE ARTIFACTS THAT ALREADY EXIST:
    the record is digest-addressed yaml, coupled to schemas, never to clones.

External to both, and owned by the user: ``~/.bioinf_agent/projects_access.yaml``
(see ``projects_access_path``) and the user's real data, wherever it lives.

RESOLUTION IS DETERMINISTIC AND NEVER RAISES. ``$BIOINF_WORKSPACE`` overrides
the artifact root (the test suite's sandbox seam, and the relocation hatch);
otherwise it is ``~/bioinf_workspace``, unconditionally — resolution consults
no filesystem state, so it cannot silently answer differently tomorrow.
Nothing here raises, deliberately: ``mcp_server`` builds artifact paths at
IMPORT, so a resolver that refuses costs the agent its entire tool surface.
Unusable roots are diagnosed by ``scripts/doctor.py``, where a human can act.
"""
from __future__ import annotations

import os
from pathlib import Path

#: The artifact root's directory name under $HOME. A default CONVENTION, not a
#: requirement — the config menu offers the same name as the local compute
#: env's zone defaults, and $BIOINF_WORKSPACE relocates it wholesale.
# Deliberately NOT "bioinf_agent": colliding with the repo's own directory
# name makes the split read as a leak rather than a design.
DEFAULT_WORKSPACE_NAME = "bioinf_workspace"


def code_root() -> Path:
    """The git checkout — this file is ``<root>/agent/skills/workspace.py``."""
    return Path(__file__).resolve().parents[2]


def workspace_source() -> str:
    """WHICH of the two answers resolution used: ``env`` · ``default``.
    Reported by the doctor and by ``agent_status``, so a surprising path can
    be traced to the thing that chose it without reading this code."""
    return "env" if os.environ.get("BIOINF_WORKSPACE", "").strip() else "default"


def workspace_root() -> Path:
    """The artifact root. Never raises; see the module docstring.

    An override holding something that is not a path (an embedded NUL, a
    partial value) falls back rather than propagating: `Path.resolve` raises
    on those, and this function is on the import path of every tool the agent
    has.
    """
    raw = os.environ.get("BIOINF_WORKSPACE", "").strip()
    if raw:
        try:
            return Path(raw).expanduser().resolve()
        except (OSError, ValueError):
            pass
    return Path.home() / DEFAULT_WORKSPACE_NAME


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------
# Five, split across the two roots, and each answers "may I delete this?"
# differently — which is what makes the taxonomy teachable rather than
# arbitrary:
#
#   SYSTEM (in the checkout, untracked — setup rebuilds them):
#     envs         delete, rebuild from the recipe
#     resources    expensive to refetch, but refetchable — setup pulls them
#
#   ARTIFACTS (under workspace_root() — they outlive any clone):
#     scratch      delete freely             never share
#     containers   delete, rebuild from the frozen env    share as .sif
#     reports      NEVER delete — the record    it IS the deliverable
#     pipelines    delete, re-render from the sealed workflow   hand over as a directory
#     common_data  delete, refetch from the source URL the record names
#
# Local zones auto-create on demand. The no-auto-mkdir rule is a CLUSTER rule
# about the user's territory; here the agent owns these directories, and a
# resolver that raised at import would leave the server with no tools at all.


def _zone(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _envs_path() -> Path:
    raw = os.environ.get("BIOINF_ENVS", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return code_root() / "envs"


def conda_envs_dir() -> Path:
    """Host conda tool envs — pre-freeze iteration. Part of the SYSTEM: lives
    in the checkout (untracked), rebuilt by the install primitives, dies with
    the clone. ``$BIOINF_ENVS`` overrides — the test suite's sandbox seam.
    """
    return _zone(_envs_path())


def images_dir() -> Path:
    """Container tarballs staged for Apptainer conversion. An ARTIFACT —
    ``<workspace>/containers``, the same name the config menu offers as the
    local compute env's container zone. Rebuildable from the frozen env."""
    return _zone(workspace_root() / "containers")


def reports_dir() -> Path:
    """ENV reports, attestations, recipes, sealed WorkflowSpecs, RUN dashboards.

    The product. Small, textual, portable — what an auditor or a paper's
    supplement receives. A peer of the other zones, not a child of any.
    """
    return _zone(workspace_root() / "reports")


def common_data_dir() -> Path:
    """Reference data the agent downloads for a LOCAL run — genomes, annotations,
    public databases — under ``<workspace>/common_data``, the same folder the config
    menu offers as the local compute env's common-data zone, so a local download
    lands where a cluster download lands on its env. Refetchable from its source URL;
    the sealed record pins it by sha256, not by location."""
    return _zone(workspace_root() / "common_data")


def pipelines_dir() -> Path:
    """Rendered pipelines — one directory per pipeline (the typed record, the
    stage scripts, main.nf/config when Nextflow was requested, the samplesheet
    template, the explain page). An ARTIFACT: rendered from a sealed workflow,
    handed over as a directory, run without the agent."""
    return _zone(workspace_root() / "pipelines")


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
    return code_root() / "resources"


def resources_root() -> Path:
    """Reference genomes and test datasets. Part of the SYSTEM: setup pulls
    the whole core corpus into the checkout (untracked), so a fresh clone
    bootstraps itself and validates tools without asking where the data is.

    Relocatable via ``$BIOINF_RESOURCES`` for the shared-mount case (one
    institutional corpus feeding N clones) — and the test suite's sandbox seam.
    """
    return _zone(_resources_path())


def projects_access_path() -> Path:
    """The operator's command-and-control file.

    A FIXED machine-level home — ``~/.bioinf_agent/projects_access.yaml`` —
    the ``~/.ssh``/``~/.aws`` pattern: the file
    describes a compute world (clusters, accounts, directory grants) that
    belongs to the MACHINE, not to any clone and not to wherever the
    working-directory default happens to point, and it holds real hostnames
    and usernames, so a checkout is the wrong container twice over.
    DELIBERATELY decoupled from ``workspace_root()``: relocating the products
    must never relocate the config out from under the agent.

    ``$BIOINF_PROJECTS_ACCESS`` overrides with an explicit FILE path — the
    test suite's isolation seam, and the escape hatch for cloud machines
    whose ``$HOME`` is ephemeral.
    """
    env = os.environ.get("BIOINF_PROJECTS_ACCESS", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".bioinf_agent" / "projects_access.yaml"


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
        # SYSTEM zones — in the checkout, untracked, rebuilt by setup.
        "envs":             str(_envs_path()),
        "resources":        str(_resources_path()),
        # ARTIFACT zones — under workspace_root(), they outlive any clone.
        "containers":       str(root / "containers"),
        "reports":          str(root / "reports"),
        "scratch":          str(root / "scratch"),
        "pipelines":        str(root / "pipelines"),
        "common_data":      str(root / "common_data"),
        # Through the resolver, never re-derived: the config home is DECOUPLED
        # from the workspace (fixed ~/.bioinf_agent), and a second spelling
        # here is exactly how the doctor once validated a file the agent
        # could not see.
        "projects_access":  str(projects_access_path()),
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
