"""
cluster_partitions — discover the SLURM partitions on a compute env, so a
GPU convention can be SUGGESTED from the cluster rather than typed by hand.

Why this earns its place
------------------------
`workflow_render` refuses a `gpus > 0` job unless the env declares a
`slurm.gpu: {partition, qos}` convention — correctly, because a GPU job that
lands on a CPU partition never sees a device and the tool silently falls back
(or dies in `dlopen`). But nothing could DISCOVER those values: the bridge
could list Lmod modules and nothing else, so the one remaining step in the
whole chain that required a human to look something up and type it was the
GPU convention.

`cluster_module_avail` is the sibling for the same reason — launcher module
versions drift with the cluster's module tree, so they are discovered, not
hardcoded. Partitions drift the same way.

Both halves of the convention, from two probes
----------------------------------------------
`sinfo` answers the hardware half (which partitions carry GPUs, of what type,
with what limits). It cannot answer which QoS a job must request — and this
module's first version therefore declared the QoS half unobservable and
shipped `qos_observable: False` as a permanent property.

That was wrong, and measuring it is what showed it: `scontrol show partition`
carries `AllowQos=`, and the real cluster answered
`AllowQos=gpu_access,gpu_access_plus` on every GPU partition. The limit was
`sinfo`'s, not SLURM's. Both probes now run in ONE ssh round trip and the
result carries `gpu_convention_candidates` — the `{partition, qos}` pairs the
cluster would actually accept.

CANDIDATES, not a choice: which GPU to ask for is a sizing judgement (an A100
and a consumer card are both "has a GPU" and are not interchangeable), so the
pick stays with the caller. A partition whose QoS was not observed yields no
candidate at all, because half a convention renders a GPU header that lands
the job on the wrong queue — the precise failure the convention prevents.

The shell surface
-----------------
`sinfo` is a real binary (unlike `module`, which is a shell function), but we
still use `bash -lc` for the same reason the sibling does: on many clusters
SLURM's bin dir is added to PATH by profile.d, and a non-login shell may not
have it.

One row per partition+state group is what `sinfo` emits — a partition with
nodes in `idle` and `alloc` produces two rows. We aggregate by partition name
and keep the states as a set, so the caller sees one record per partition.

Output parsing
--------------
`-h` drops the header; `-o` pins the field order so parsing never depends on
the site's default format. Fields are pipe-delimited because a partition name
cannot contain `|` and the alternative (whitespace) collides with `%G` values
and with `(null)`.

`%P` marks the default partition with a trailing `*`, which is a FACT worth
keeping (it is what a job gets when it names no partition) — recorded as
`is_default` with the marker stripped from the name.
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


# Same boring subset the sibling uses for `module avail` patterns.
_SAFE_PATTERN_CHARS_RE = re.compile(r"^[A-Za-z0-9_+./-]+$")

# The pinned field order. Changing this changes the parser — both are pinned
# by a test so they cannot drift apart.
#   %P partition (default marked `*`)   %a avail        %l time limit
#   %c cpus/node                        %m memory (MB)  %G gres
#   %D node count                       %T state
_SINFO_FORMAT = "%P|%a|%l|%c|%m|%G|%D|%T"
_SINFO_FIELDS = ("partition", "avail", "time_limit", "cpus_per_node",
                 "memory_mb", "gres", "nodes", "state")

# SLURM writes a literal "(null)" for an unset gres, not an empty field.
_GRES_ABSENT = ("(null)", "null", "", "n/a")

# Separates the two probes in one ssh round trip. Deliberately not a string
# any SLURM output could contain.
#
# MUST NOT START WITH `#`. It did — `###BIOINF_SCONTROL###` — and bash begins
# a comment at any word starting with `#`, so `echo ###X###; scontrol …`
# commented out the REST OF THE LINE, silently including the second probe.
# Every unit test passed (none runs a real shell) and the live cluster call
# returned `qos_observable: False` with six GPU partitions marked unobserved.
# _sentinel_is_shell_safe pins this.
_SCONTROL_SENTINEL = "__BIOINF_SCONTROL__"

# A gres entry looks like `gpu:a100:8` / `gpu:8` / `gpu:a100:8(S:0-1)`. We want
# the TYPE when the site records one, because "this partition has GPUs" and
# "this partition has A100s" are different answers to a sizing question.
_GRES_GPU_RE = re.compile(r"\bgpu:(?:([A-Za-z0-9_.-]+):)?(\d+)")


def _validate_pattern(pattern: Optional[str]) -> Optional[str]:
    """Pattern is optional and filtered CLIENT-side only — `sinfo` has no
    substring filter, and `-p <name>` requires an exact partition name, which
    is the opposite of what a discovery call wants. Still validated as a safe
    token: it costs nothing and keeps one rule for both cluster probes."""
    if pattern is None:
        return None
    if not isinstance(pattern, str):
        raise ValueError(
            f"pattern must be a string or None, got {type(pattern).__name__}")
    if not pattern:
        return None                       # treat empty as absent
    if len(pattern) > 128:
        raise ValueError(f"pattern length {len(pattern)} exceeds 128")
    if not _SAFE_PATTERN_CHARS_RE.match(pattern):
        raise ValueError(
            f"pattern {pattern!r} contains forbidden characters — "
            f"only alnum + '_+.-/' allowed")
    return pattern


def _build_sinfo_cmd() -> str:
    """The remote shell command — TWO probes in ONE ssh round trip.

    `sinfo` answers hardware (gres, limits, node counts). It does NOT answer
    which QoS a job must request, which is the other half of the GPU
    convention — the first version of this module stated that as a hard
    limit. It is a limit of `sinfo`, not of SLURM: `scontrol show partition`
    carries `AllowQos=`, and on a real cluster the GPU partitions answered
    `AllowQos=gpu_access,gpu_access_plus`. So the convention IS fully
    discoverable and shipping half of it would have been a self-inflicted gap.

    `-o` (oneline) makes scontrol one record per line, which is why it is
    parsed by key=value rather than by the multi-line block format.

    Failure semantics differ ON PURPOSE:
      - `sinfo` is load-bearing → `|| exit $?` so its rc propagates. Without
        it the trailing command's rc would win and a cluster with no SLURM
        would report as a cluster with no partitions — a false "no GPUs here".
      - `scontrol` is enrichment → `|| true`, because a site that restricts
        it should degrade to "qos not observed", never fail the whole call.
    Pinned by tests."""
    inner = (f"sinfo -h -o {shlex.quote(_SINFO_FORMAT)} || exit $?; "
             f"echo {shlex.quote(_SCONTROL_SENTINEL)}; "
             f"scontrol show partition -o 2>/dev/null || true")
    return f"bash -lc {shlex.quote(inner)}"


def _split_probe_output(text: str) -> tuple[str, str]:
    """Split the combined output on the sentinel into (sinfo, scontrol).
    A missing sentinel means scontrol never ran — the scontrol half is then
    empty, which reads downstream as `qos_observable: False`."""
    if _SCONTROL_SENTINEL not in text:
        return text, ""
    head, _, tail = text.partition(_SCONTROL_SENTINEL)
    return head, tail


def _parse_scontrol_partitions(text: str) -> dict[str, dict]:
    """Parse `scontrol show partition -o` into {name: {allowed_qos, qos}}.

    Each line is space-separated `Key=Value`. We read only what the GPU
    convention needs; everything else is already covered by sinfo.

    `AllowQos=ALL` means the partition constrains nothing — recorded as an
    empty list with `qos_unrestricted: True`, NOT as "no qos found". Those
    are different facts: one says any qos works, the other says we did not
    learn which. `QoS=N/A` is SLURM's way of writing absent."""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("PartitionName="):
            continue
        fields: dict[str, str] = {}
        for tok in s.split():
            k, sep, v = tok.partition("=")
            if sep:
                fields[k] = v
        name = fields.get("PartitionName", "").rstrip("*")
        if not name:
            continue
        allow = (fields.get("AllowQos") or "").strip()
        unrestricted = allow.upper() == "ALL"
        qos_list: list[str] = []
        if allow and not unrestricted and allow.upper() not in ("N/A", "NONE"):
            qos_list = [q for q in allow.split(",") if q]
        part_qos = (fields.get("QoS") or "").strip()
        out[name] = {
            "allowed_qos":      qos_list,
            "qos_unrestricted": unrestricted,
            "partition_qos":    ("" if part_qos.upper() in ("N/A", "NONE", "")
                                 else part_qos),
        }
    return out


def _parse_gres_gpus(gres: str) -> list[dict]:
    """Pull GPU entries out of a gres string. Returns [] when the partition
    declares none — which is the common case and is not an error."""
    if not gres or gres.strip().lower() in _GRES_ABSENT:
        return []
    out: list[dict] = []
    for m in _GRES_GPU_RE.finditer(gres):
        gpu_type, count = m.group(1), m.group(2)
        try:
            n = int(count)
        except ValueError:
            continue
        out.append({"type": gpu_type or "", "count_per_node": n})
    return out


def _parse_sinfo_output(text: str) -> list[dict]:
    """Parse the pinned `sinfo -h -o` output into one record per PARTITION.

    `sinfo` emits one row per partition+state group, so a partition with both
    idle and allocated nodes appears twice. We fold those into a single record
    and keep every state seen, because "this partition exists" is the question
    being asked and a per-state view would make one partition look like two.
    """
    by_name: dict[str, dict] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        parts = s.split("|")
        if len(parts) != len(_SINFO_FIELDS):
            # Not a row we pinned the shape of — skip rather than
            # mis-assign fields by position.
            continue
        row = dict(zip(_SINFO_FIELDS, (p.strip() for p in parts)))
        name = row["partition"]
        if not name:
            continue
        # `%P` marks the cluster default with a trailing `*`.
        is_default = name.endswith("*")
        name = name.rstrip("*")
        if not _SAFE_PATTERN_CHARS_RE.match(name):
            continue
        gres = row["gres"]
        gpus = _parse_gres_gpus(gres)
        rec = by_name.get(name)
        if rec is None:
            rec = {
                "name":          name,
                "is_default":    is_default,
                "avail":         row["avail"],
                "time_limit":    row["time_limit"],
                "cpus_per_node": row["cpus_per_node"],
                "memory_mb":     row["memory_mb"],
                "gres":          "" if gres.lower() in _GRES_ABSENT else gres,
                "gpus":          gpus,
                "has_gpu":       bool(gpus),
                "states":        [],
                "node_count":    0,
            }
            by_name[name] = rec
        else:
            # A later row may carry the default marker or a gres the first
            # row lacked (mixed hardware in one partition). Never downgrade
            # a fact we already observed.
            rec["is_default"] = rec["is_default"] or is_default
            if gpus and not rec["gpus"]:
                rec["gpus"] = gpus
                rec["has_gpu"] = True
                rec["gres"] = gres
        st = row["state"]
        if st and st not in rec["states"]:
            rec["states"].append(st)
        try:
            rec["node_count"] += int(row["nodes"])
        except ValueError:
            pass
    return sorted(by_name.values(), key=lambda r: r["name"])


def _gpu_convention_candidates(gpu_parts: list[dict]) -> list[dict]:
    """Assemble the `slurm.gpu: {partition, qos}` pairs this cluster would
    actually accept, so the convention is READ off the cluster rather than
    typed from memory.

    CANDIDATES, not a choice. Which GPU a job should ask for is a sizing
    judgement — an A100 partition and a decade-old consumer card are both
    valid answers to "has a GPU" and wildly different answers to "will my
    model fit". That call needs world knowledge about the workload, so it
    stays with the caller, exactly as tool identity stays with the ride.

    A partition whose QoS was never observed yields NO candidate: half a
    convention renders a GPU header that lands the job on the wrong queue,
    which is the failure the convention exists to prevent. It still appears
    in `partitions[]` with `qos_observed: False`, so the gap is visible
    rather than silently dropped."""
    out: list[dict] = []
    for p in gpu_parts:
        if not p.get("qos_observed"):
            continue
        gpu_types = [g["type"] for g in p.get("gpus") or [] if g.get("type")]
        if p.get("qos_unrestricted"):
            # Nothing to name: the partition accepts any QoS, so the convention
            # cannot be completed from here and a guess would be fiction.
            continue
        for q in p.get("allowed_qos") or []:
            out.append({
                "partition":  p["name"],
                "qos":        q,
                "gpu_types":  gpu_types,
                "time_limit": p.get("time_limit", ""),
                "node_count": p.get("node_count", 0),
            })
    return out


def cluster_partitions(project_name: str,
                       compute_env_name: str,
                       pattern: Optional[str] = None,
                       *,
                       access_path: Optional[str] = None,
                       timeout: int = 60) -> dict:
    """List the SLURM partitions on `compute_env_name`.

    Pure-read: runs ONE ssh invocation of `bash -lc 'sinfo -h -o …'`, parses
    the output, returns one record per partition. Submits nothing, loads
    nothing, writes nothing.

    Authorization: requires that `project` has a `compute_env_access` entry
    for `compute_env_name` — the same shape as `cluster_module_avail`. No
    per-directory permission (the filesystem is not touched), and SLURM's own
    ACLs scope what `sinfo` will report.

    Returns:
      {
        "compute_env":    "<env_name>",
        "pattern":        "<pattern>" | None,
        "partitions":     [{name, is_default, avail, time_limit,
                            cpus_per_node, memory_mb, gres, gpus[],
                            has_gpu, states[], node_count,
                            qos_observed, allowed_qos[],
                            qos_unrestricted}, …],
        "partition_count": <int>,
        "gpu_partitions": ["<name>", …],
        "default_partition": "<name>" | None,
        "qos_observable": <bool>,
        "gpu_convention_candidates": [{partition, qos, gpu_types[],
                                       time_limit, node_count}, …],
        "captured_at":    "<iso utc>",
      }
    Returns {"error": "...", "hint": "..."} on any failure.

    ON THE GPU CONVENTION: `gpu_convention_candidates` holds the
    `{partition, qos}` pairs this cluster would accept for
    `slurm.gpu`. They are CANDIDATES — picking among an A100 partition and a
    consumer-card partition is a sizing judgement the caller owns. A
    partition whose QoS was not observed contributes no candidate and is
    visible in `partitions[]` with `qos_observed: False`.
    """
    try:
        norm_pattern = _validate_pattern(pattern)

        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        has_access = any(
            isinstance(b, dict) and b.get("compute_env") == compute_env_name
            for b in (project.get("compute_env_access") or []))
        if not has_access:
            return refused("partitions.no_env_access", error=(
                f"PermissionDenied: project {project_name!r} has no "
                f"compute_env_access entry for compute_env "
                f"{compute_env_name!r}"))

        env_type = env.get("type")
        if env_type != "ssh":
            return refused("partitions.not_ssh_env", error=(
                f"cluster_partitions only supports ssh compute envs; got "
                f"type={env_type!r} on env {compute_env_name!r}"))

        argv = _ssh_argv(env, _build_sinfo_cmd())
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
        if res.returncode != 0:
            hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
            return broke("partitions.sinfo_failed", error=(
                f"sinfo failed (rc={res.returncode}): "
                f"{(res.stderr or '').strip()[:500]}"),
                **({"hint": hint} if hint else {}))

        sinfo_text, scontrol_text = _split_probe_output(res.stdout)
        partitions = _parse_sinfo_output(sinfo_text)
        qos_by_part = _parse_scontrol_partitions(scontrol_text)

        # Merge the QoS half in. A partition scontrol did not describe keeps
        # `qos_observed: False` — absence stated, never defaulted to "none".
        for p in partitions:
            info = qos_by_part.get(p["name"])
            if info is None:
                p["qos_observed"] = False
                p["allowed_qos"] = []
                p["qos_unrestricted"] = None
            else:
                p["qos_observed"] = True
                p["allowed_qos"] = info["allowed_qos"]
                p["qos_unrestricted"] = info["qos_unrestricted"]
                if info["partition_qos"]:
                    p["partition_qos"] = info["partition_qos"]

        if norm_pattern:
            partitions = [p for p in partitions if norm_pattern in p["name"]]

        default = next((p["name"] for p in partitions if p["is_default"]), None)
        gpu_parts = [p for p in partitions if p["has_gpu"]]
        return {
            "compute_env":       compute_env_name,
            "pattern":           norm_pattern,
            "partitions":        partitions,
            "partition_count":   len(partitions),
            "gpu_partitions":    [p["name"] for p in gpu_parts],
            "default_partition": default,
            # Whether the QoS half of the GPU convention was actually READ.
            # False here means scontrol was unavailable or said nothing — not
            # that the cluster has no QoS. See _parse_scontrol_partitions.
            "qos_observable":    bool(qos_by_part),
            "gpu_convention_candidates": _gpu_convention_candidates(gpu_parts),
            "captured_at":       datetime.now(timezone.utc).isoformat(),
        }

    except (ValueError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return refused("partitions.bad_arg", error=f"{type(e).__name__}: {e}")
    except subprocess.TimeoutExpired as e:
        return broke("partitions.timeout",
                     error=f"sinfo timed out after {e.timeout}s")
