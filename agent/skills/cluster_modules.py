"""
cluster_module_avail — discover what HPC modules (Lmod/tmod) are
installable on the cluster, so the agent can choose the right
`module load X/Y.Z` line to put in the launcher (Step 9's sbatch
wrapper that runs nextflow on a compute node).

Why this earns its place:
  - Phase 2 launchers ship `module load apptainer/<v>` + `module load
    nextflow/<v>` lines. The version strings can't be hardcoded; they
    drift as the cluster's module tree evolves.
  - Those versions are chosen per-run (the apptainer_module /
    nextflow_module args to the submit primitives), so discoverability
    matters: a fresh project asking "what nextflow versions are
    available?" should get an answer without the user having to ssh in
    and grep.
  - cluster_module_avail is read-only, ssh-only, doesn't submit jobs.
    Same trust posture as snapshot_project.

The shell surface
-----------------
`module avail` is a shell function (provided by Lmod or environment-
modules). It's not a binary on PATH. Most HPCs surface it via
`/etc/profile.d/lmod.sh` or similar. Running `ssh host 'module avail'`
in non-interactive mode often FAILS because the function isn't loaded.

Two paths:
  - `ssh host bash -lc 'module avail'`  — forces a login shell so the
    profile.d setup runs. Works on most clusters.
  - `ssh host bash -c 'source /etc/profile.d/lmod.sh && module avail'`
    — explicit source; brittle.

We use the login-shell approach. The remote command shape is pinned by
a test.

Output parsing
--------------
`module avail` (and `module avail <pattern>`) writes to STDERR on most
systems (both Lmod and tmod, surprisingly). The output is a multi-column
list grouped by section:

    ------- /nas/hpc_cluster/apps-modulefiles -------
       apptainer/1.0.0  apptainer/1.4.1  nextflow/22.04.5  nextflow/25.04.7  ...

We capture stderr, drop section headers (lines starting with `---`),
collapse whitespace, and emit a flat list. Optional `pattern` is
forwarded to `module avail <pattern>` and ALSO filtered client-side
(defense in depth — some Lmod versions don't filter as advertised).
"""
from __future__ import annotations

import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.skills import compute_access
from agent.skills.outcomes import refused, broke
from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint


# A `module avail` pattern token is a boring subset: alnum + `_+.-/`.
# Refuses shell metacharacters that would break out of `module avail X`.
_SAFE_PATTERN_CHARS_RE = re.compile(r"^[A-Za-z0-9_+./-]+$")


def _validate_pattern(pattern: Optional[str]) -> Optional[str]:
    """Pattern is optional. When given, it must be a safe token — refusing
    shell metacharacters so a smuggled `nextflow; rm -rf /` can't escape
    the `module avail` shell line."""
    if pattern is None:
        return None
    if not isinstance(pattern, str):
        raise ValueError(
            f"pattern must be a string or None, got {type(pattern).__name__}")
    if not pattern:
        return None  # treat empty as absent
    if len(pattern) > 128:
        raise ValueError(f"pattern length {len(pattern)} exceeds 128")
    if not _SAFE_PATTERN_CHARS_RE.match(pattern):
        raise ValueError(
            f"pattern {pattern!r} contains forbidden characters — "
            f"only alnum + '_+.-/' allowed")
    return pattern


def _build_module_avail_cmd(pattern: Optional[str]) -> str:
    """The remote shell command. We force a login shell with `bash -lc`
    so the profile.d/lmod.sh setup loads. `module avail` writes to
    STDERR; we redirect to STDOUT so subprocess captures it cleanly.
    Pinned by a test."""
    inner = "module avail"
    if pattern:
        inner = f"{inner} {shlex.quote(pattern)}"
    # 2>&1 because module avail writes to stderr — capturing only stdout
    # would lose everything. The trailing `|| true` keeps the rc=0 even
    # when `module avail` exits non-zero (some Lmod versions return 1
    # when the pattern matches nothing; that's not an error for us).
    return f"bash -lc {shlex.quote(inner + ' 2>&1')} || true"


def _parse_module_avail_output(text: str) -> list[str]:
    """Parse multi-column `module avail` output into a flat list of
    module strings. Drops section headers (`---` separators) and STOPS
    on Lmod/tmod footer markers (everything past `Where:` or `Use "module
    spider"` is the legend, not modules).

    Defense: we tolerate trailing-version annotations (Lmod marks the
    default version with `(D)` and loaded modules with `(L)`); strip
    them so the agent sees clean `<name>/<version>` strings."""
    out: list[str] = []
    # Footer markers — once we see one, STOP. The legend that follows
    # would contaminate the output (e.g. `D:  Default Module` would split
    # into `Default`, `Module` which both match the safe-token regex).
    _FOOTERS = ("where:", "use \"module", "use 'module",
                "key:", "see http", "to find ", "matched any")
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        # Footer marker → stop here; the rest is legend.
        lower = s.lower()
        if any(lower.startswith(fm) for fm in _FOOTERS):
            break
        # Section headers: typically wrapped in `--- ... ---`.
        if s.startswith("-") or s.startswith("="):
            continue
        # Trailing-colon section labels (e.g. "Subdir:") — drop the line.
        if s.endswith(":"):
            continue
        # Split on whitespace; each token is one module entry.
        for tok in s.split():
            # Strip Lmod annotations like `(D)`, `(L)`, `(default)`.
            tok = re.sub(r"\([A-Za-z:]+\)$", "", tok).strip()
            if not tok:
                continue
            # A valid module entry looks like `<name>` or `<name>/<version>`.
            # Skip anything containing characters that don't fit (paths,
            # comments, etc.).
            if not _SAFE_PATTERN_CHARS_RE.match(tok):
                continue
            out.append(tok)
    return out


def cluster_module_avail(project_name: str,
                        compute_env_name: str,
                        pattern: Optional[str] = None,
                        *,
                        access_path: Optional[str] = None,
                        timeout: int = 60) -> dict:
    """List the modules available on `compute_env_name` (filtered by
    `pattern` if given).

    Pure-read: runs ONE ssh invocation of `bash -lc 'module avail …'`,
    parses the output, returns a flat list. Does not load any module;
    does not submit any job; never writes anything.

    Authorization: requires that `project` has a `compute_env_access`
    entry for `compute_env_name`. No per-directory permission needed
    (we're not touching the filesystem). The check is: project knows
    this env — the same shape as `check_env_target_capability`'s
    "project on env" leg, but without the target-capability requirement.

    Returns:
      {
        "compute_env":  "<env_name>",
        "pattern":      "<pattern>" | None,
        "modules":      ["nextflow/25.04.7", "apptainer/1.4.1", …],
        "module_count": <int>,
        "captured_at":  "<iso utc>",
      }
    Returns {"error": "...", "hint": "..."} on any failure.
    """
    try:
        norm_pattern = _validate_pattern(pattern)

        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        # Project must have access to the env. We re-use the shape of
        # check_env_target_capability (the "project on env" half) by
        # walking compute_env_access[].
        has_access = any(
            isinstance(b, dict) and b.get("compute_env") == compute_env_name
            for b in (project.get("compute_env_access") or []))
        if not has_access:
            return refused("modules.no_env_access", error=
                f"PermissionDenied: project {project_name!r} has no "
                f"compute_env_access entry for compute_env "
                f"{compute_env_name!r}")

        env_type = env.get("type")
        if env_type != "ssh":
            return refused("modules.not_ssh_env", error=
                f"cluster_module_avail only supports ssh compute envs; "
                f"got type={env_type!r} on env {compute_env_name!r}")

        remote_cmd = _build_module_avail_cmd(norm_pattern)
        argv = _ssh_argv(env, remote_cmd)
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
        # rc captures ssh-layer failures (BatchMode, no session, etc.);
        # `module avail`'s own non-zero exit is hidden by the `|| true`
        # tail in our remote_cmd.
        if res.returncode != 0:
            hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
            return broke("modules.ssh_failed", error=
                f"ssh invocation failed (rc={res.returncode}): "
                f"{(res.stderr or '').strip()[:500]}",
                **({"hint": hint} if hint else {}))

        modules = _parse_module_avail_output(res.stdout)
        # Client-side pattern re-filter — defense in depth.
        if norm_pattern:
            modules = [m for m in modules if norm_pattern in m]
        return {
            "compute_env":  compute_env_name,
            "pattern":      norm_pattern,
            "modules":      modules,
            "module_count": len(modules),
            "captured_at":  datetime.now(timezone.utc).isoformat(),
        }

    except (ValueError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return refused("modules.bad_arg", error=f"{type(e).__name__}: {e}")
    except subprocess.TimeoutExpired as e:
        return broke("modules.timeout", error=f"module avail timed out after {e.timeout}s")
