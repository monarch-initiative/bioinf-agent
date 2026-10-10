#!/usr/bin/env python3
"""experiments.py — run the agent headless, from cold, and measure it.

An EXPERIMENT is a YAML file (see experiments/ for the ones this repo keeps): one prompt,
the models to put it to, how many repeats. Each run is a fresh headless Claude Code
session started in this checkout with the bioinf server attached — so the model works
through the MCP face exactly as a user's session would — and pointed at an EMPTY
workspace of its own, so nothing it finds was left by an earlier run:

    <experiments zone>/<experiment>/<model>__r<repeat>__<stamp>/
        experiment.json     what ran: the resolved definition, the command, the env, timing
        transcript.jsonl    the stream-json transcript, as Claude Code wrote it
        stderr.log          the client's stderr
        workspace/          $BIOINF_WORKSPACE for that run — environments/, scratch/, pipelines/
        envs/               $BIOINF_ENVS for that run (unless the experiment shares the host's)
        projects_access.yaml  the run's compute-env declaration, when the experiment prepares one
        metrics.json        the row scripts/experiment_metrics.py read off the above

and after the runs, `report.html` beside them (scripts/experiment_report.py).

    python scripts/experiments.py run experiments/c1_seqkit.yaml
    python scripts/experiments.py run experiments/c1_seqkit.yaml --models sonnet --repeats 1
    python scripts/experiments.py run … --dry-run       # print the command, run nothing
    python scripts/experiments.py report <experiment dir> […]   # (re)render, any set of experiments side by side
    python scripts/experiments.py parse <run dir>              # re-read one run's row

The tool grant is `allowed_tools` (default: every bioinf tool). It is a grant, not a wall:
the client still runs what its own rules call safe (a read-only shell line, say) without
asking, and refuses the rest. `other_tool_calls` on the row counts what went past the
server; `disallowed_tools` closes a tool outright for an experiment that wants it closed.

What a run shares with this machine is declared, per experiment, in `share`: `resources`
(the test-data corpus; shared by default, since fetching it is not what is measured),
`envs` (host conda envs) and `projects_access` (the host's compute-env declarations).
Instead of sharing the host's file, `projects_access: <yaml>` names a prepared one (a path
relative to the definition) that is copied into the run with `{run_dir}` substituted, so a
run can declare a local compute env on directories inside itself. The Docker daemon is
shared — an image an earlier run built or pulled is found by digest — so `cleanup: [images]`
(the default) removes the images and containers a run added once it ends; images that were
there before the run are never touched, and nothing is ever pruned. Auto-memory is off
unless the experiment asks for it: the memory directory a session in this checkout would
load is the developer's, not the system's. Every one of these is written into the run's
experiment.json as `isolation`, which is what the report's isolation row reads.

A run has a dollar cap (`budget_usd`, passed to the client) and a wall-clock cap
(`timeout_s`, enforced here; the row says `timed_out`). Runs are sequential: builds are
heavy and the numbers are cleaner one at a time.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

from agent.skills import compute_access, workspace  # noqa: E402
from experiment_metrics import SUCCESS_RULES, TIER_DEFAULT_RULE, aggregate, load_rows, parse_run, write_row  # noqa: E402
from experiment_report import render_index, write_report  # noqa: E402

TIERS = ("C0", "C1", "C2", "C3")
SHAREABLE = ("resources", "envs", "projects_access")
CLEANABLE = ("images",)
DEFAULTS = {
    "repeats": 1,
    "success": None,
    "expected_codes": [],
    "required_codes": [],
    "share": ["resources"],
    "projects_access": None,
    "cleanup": ["images"],
    "allowed_tools": ["mcp__bioinf__*"],
    "disallowed_tools": [],
    "effort": None,
    "budget_usd": 10.0,
    "timeout_s": 3600,
    "memory": False,
    "notes": "",
    "corpus": None,
}
REQUIRED = ("name", "tier", "prompt", "models")
KNOWN = set(REQUIRED) | set(DEFAULTS)


class ExperimentError(ValueError):
    pass


def load_experiment(path: Path) -> dict:
    """The definition, validated: required keys present, no unknown keys, values in range."""
    d = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(d, dict):
        raise ExperimentError(f"{path}: not a mapping")
    missing = [k for k in REQUIRED if k not in d]
    unknown = sorted(set(d) - KNOWN)
    if missing or unknown:
        raise ExperimentError(f"{path}: missing {missing}, unknown {unknown}; known keys are {sorted(KNOWN)}")
    exp = {**DEFAULTS, **d}
    # The corpus a definition belongs to: its own `corpus` key, else the directory it sits in.
    exp["corpus"] = str(exp["corpus"] or Path(path).resolve().parent.name)
    if exp["tier"] not in TIERS:
        raise ExperimentError(f"{path}: tier {exp['tier']!r} is not one of {TIERS}")
    if not isinstance(exp["models"], list) or not all(isinstance(m, str) and m for m in exp["models"]):
        raise ExperimentError(f"{path}: models must be a non-empty list of model names")
    if not isinstance(exp["prompt"], str) or not exp["prompt"].strip():
        raise ExperimentError(f"{path}: prompt is empty")
    if exp["success"] is None:
        exp["success"] = TIER_DEFAULT_RULE[exp["tier"]]
    if exp["success"] not in SUCCESS_RULES:
        raise ExperimentError(f"{path}: success {exp['success']!r} is not one of {SUCCESS_RULES}")
    bad = sorted(set(exp["share"]) - set(SHAREABLE))
    if bad:
        raise ExperimentError(f"{path}: share names {bad}; shareable zones are {SHAREABLE}")
    bad = sorted(set(exp["cleanup"]) - set(CLEANABLE))
    if bad:
        raise ExperimentError(f"{path}: cleanup names {bad}; cleanable things are {CLEANABLE}")
    if not isinstance(exp["expected_codes"], list) or not all(isinstance(c, str) and c for c in exp["expected_codes"]):
        raise ExperimentError(f"{path}: expected_codes must be a list of outcome-code patterns")
    if exp["expected_codes"] and exp["success"] not in ("refused", "declined"):
        raise ExperimentError(f"{path}: expected_codes only mean something under the refused and declined rules")
    if not isinstance(exp["required_codes"], list) or not all(isinstance(c, str) and c for c in exp["required_codes"]):
        raise ExperimentError(f"{path}: required_codes must be a list of outcome-code patterns")
    if exp["projects_access"] is not None:
        if "projects_access" in exp["share"]:
            raise ExperimentError(f"{path}: projects_access names a prepared file AND share lists the host's; pick one")
        pa = Path(path).resolve().parent / str(exp["projects_access"])
        if not pa.is_file():
            raise ExperimentError(f"{path}: projects_access {exp['projects_access']!r} is not a file beside the definition")
        exp["projects_access"] = str(pa)
    if int(exp["repeats"]) < 1 or float(exp["budget_usd"]) <= 0 or int(exp["timeout_s"]) <= 0:
        raise ExperimentError(f"{path}: repeats, budget_usd and timeout_s must be positive")
    exp["name"] = str(exp["name"]).strip()
    if not exp["name"] or "/" in exp["name"]:
        raise ExperimentError(f"{path}: name must be a plain directory name")
    exp["source"] = str(Path(path).resolve())
    return exp


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------

def build_command(exp: dict, model: str, add_dirs: tuple[str, ...] = ()) -> list[str]:
    """The headless session. `add_dirs` are granted to the harness's own file tools (the
    run directory, so Bash/Read reach the run's workspace without a permission denial)."""
    cmd = ["claude", "-p", exp["prompt"], "--model", model,
           "--output-format", "stream-json", "--verbose", "--no-session-persistence",
           "--mcp-config", ".mcp.json", "--strict-mcp-config",
           "--max-budget-usd", str(exp["budget_usd"])]
    for d in add_dirs:
        cmd += ["--add-dir", d]
    if exp["allowed_tools"]:
        cmd += ["--allowedTools", *exp["allowed_tools"]]
    if exp["disallowed_tools"]:
        cmd += ["--disallowedTools", *exp["disallowed_tools"]]
    if exp["effort"]:
        cmd += ["--effort", str(exp["effort"])]
    return cmd


def build_env(exp: dict, run_dir: Path, host_zones: dict, base: dict | None = None) -> dict:
    """The run's environment: the four workspace seams point into the run directory
    unless the experiment shares that zone with the host; auto-memory off; the server's
    file-watch reload off (the code under test does not change mid-run)."""
    env = dict(os.environ if base is None else base)
    share = set(exp["share"])
    env["BIOINF_WORKSPACE"] = str(run_dir / "workspace")
    env["BIOINF_ENVS"] = host_zones["envs"] if "envs" in share else str(run_dir / "envs")
    env["BIOINF_RESOURCES"] = host_zones["resources"] if "resources" in share else str(run_dir / "resources")
    env["BIOINF_PROJECTS_ACCESS"] = (host_zones["projects_access"] if "projects_access" in share
                                     else str(run_dir / "projects_access.yaml"))
    env["BIOINF_MCP_AUTO_RELOAD"] = "0"
    if exp["memory"]:
        env.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
    else:
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


def _git(args: list[str]) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


_RUN_DIR_SLOT = "{run_dir}"
_PATH_KEY = re.compile(r"^(\s*path:\s*)(\S+)\s*$", re.M)


def prepare_projects_access(template: Path, run_dir: Path) -> dict:
    """Copy a prepared projects_access.yaml into the run with `{run_dir}` substituted,
    create every directory it declares inside the run (a local compute env's zones are
    the run's own), and validate it the way the server will. Returns what it declares."""
    text = Path(template).read_text(encoding="utf-8").replace(_RUN_DIR_SLOT, str(run_dir))
    out = run_dir / "projects_access.yaml"
    out.write_text(text, encoding="utf-8")
    for m in _PATH_KEY.finditer(text):
        p = Path(m.group(2).strip("'\""))
        if p.is_absolute() and str(p).startswith(str(run_dir) + os.sep):
            p.mkdir(parents=True, exist_ok=True)
    access = compute_access.load_access(out)     # raises ConfigError on a bad template
    return {"template": str(template), "file": str(out),
            "compute_envs": [e["name"] for e in access.get("compute_envs") or []],
            "projects": [p["name"] for p in access.get("projects") or []]}


def _docker_ids(kind: str) -> set[str] | None:
    """The ids the daemon holds now: `kind` is image or container. None when there is
    no daemon to ask, so a cleanup can say it was skipped rather than report nothing."""
    cmd = ["docker", "image", "ls", "-aq", "--no-trunc"] if kind == "image" else ["docker", "ps", "-aq", "--no-trunc"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return {line.strip() for line in r.stdout.splitlines() if line.strip()}


def _docker_tags() -> dict[str, str] | None:
    """{repo:tag: id} for every tagged image the daemon holds; None without a daemon."""
    try:
        r = subprocess.run(["docker", "image", "ls", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    tags: dict[str, str] = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and not parts[0].startswith("<none>"):
            tags[parts[0]] = parts[1]
    return tags


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def _job_alive(pid: int) -> bool:
    """Is the job's process still running? A zombie (exited, not yet reaped by its parent)
    counts as gone — the work has stopped."""
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True


def stop_live_jobs(jobs_dir: Path, grace_s: float = 10.0) -> list[str]:
    """Stop every job a killed run left running. A session's detached jobs survive the
    session by design; a run the runner killed must not keep building after it, or the
    docker cleanup that follows misses the images they finish. Each job the run's own
    status files call `running` gets SIGTERM then SIGKILL on its process group, and its
    status file says the runner killed it."""
    stopped: list[str] = []
    jobs_dir = Path(jobs_dir)
    for sf in sorted(jobs_dir.glob("*.status.json")) if jobs_dir.is_dir() else []:
        try:
            d = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("state") != "running":
            continue
        pgid = int(d.get("pgid") or d.get("pid") or 0)
        if pgid <= 0:
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                break
            t = time.monotonic()
            while time.monotonic() - t < grace_s and _job_alive(int(d.get("pid") or pgid)):
                time.sleep(0.2)
            if not _job_alive(int(d.get("pid") or pgid)):
                break
        d.update({"state": "killed", "killed_by": "the experiment runner, at the run's timeout",
                  "end_time_iso": datetime.now(timezone.utc).isoformat()})
        sf.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        stopped.append(str(d.get("job_id") or sf.name.removesuffix(".status.json")))
    return stopped


def docker_snapshot() -> dict:
    return {"image": _docker_ids("image"), "container": _docker_ids("container"), "tags": _docker_tags()}


def docker_cleanup(before: dict) -> dict:
    """Remove what the run added to the daemon — the containers first, then the images —
    and only that. An id present before the run is never touched; nothing is pruned.

    A run that names an env the host already has re-points the host's tag at its own
    new image, and removing that image leaves the host's image untagged. Every tag the
    host held before is put back on its id when that id still exists; a tag whose id is
    gone is reported, since nothing can restore it."""
    result: dict = {"containers_removed": [], "images_removed": [], "failed": [],
                    "host_tags_restored": [], "host_tags_lost": []}
    after = docker_snapshot()
    for kind, rm in (("container", ["docker", "rm", "-f"]), ("image", ["docker", "image", "rm", "-f"])):
        if before.get(kind) is None or after.get(kind) is None:
            result["skipped"] = "no Docker daemon answered"
            continue
        for ident in sorted(after[kind] - before[kind]):
            r = subprocess.run([*rm, ident], capture_output=True, text=True)
            if r.returncode == 0:
                result[f"{kind}s_removed"].append(ident)
            else:
                result["failed"].append({"kind": kind, "id": ident, "stderr": r.stderr.strip()[-400:]})
    now = _docker_tags()
    for tag, ident in sorted((before.get("tags") or {}).items()):
        if now is None or now.get(tag) == ident:
            continue
        if _docker("image", "inspect", ident).returncode == 0 and _docker("tag", ident, tag).returncode == 0:
            result["host_tags_restored"].append(tag)
        else:
            result["host_tags_lost"].append({"tag": tag, "id": ident})
    return result


def isolation_of(exp: dict, access: dict | None) -> dict:
    """What a run of this experiment shares with the host, as a record the report reads
    off rather than re-derives. One entry per seam."""
    share = set(exp["share"])
    if access:
        pa = {"kind": "prepared", "template": access["template"], "compute_envs": access["compute_envs"],
              "projects": access["projects"]}
    elif "projects_access" in share:
        pa = {"kind": "host"}
    else:
        pa = {"kind": "none"}
    return {
        "workspace": "fresh",
        "envs": "host" if "envs" in share else "fresh",
        "resources": "host" if "resources" in share else "fresh",
        "projects_access": pa,
        "docker": "images and containers the run adds are removed after it" if "images" in exp["cleanup"]
                  else "images the run adds stay on the daemon",
        "memory": "on" if exp["memory"] else "off",
    }


def run_once(exp: dict, model: str, repeat: int, root: Path, dry_run: bool = False) -> Path | None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = root / exp["name"] / f"{model}__r{repeat}__{stamp}"
    host = workspace.zones()
    cmd = build_command(exp, model, add_dirs=(str(run_dir),))
    env = build_env(exp, run_dir, host)
    overrides = {k: env[k] for k in ("BIOINF_WORKSPACE", "BIOINF_ENVS", "BIOINF_RESOURCES",
                                      "BIOINF_PROJECTS_ACCESS", "BIOINF_MCP_AUTO_RELOAD",
                                      "CLAUDE_CODE_DISABLE_AUTO_MEMORY") if k in env}
    if dry_run:
        print(f"# {run_dir}")
        if exp["projects_access"]:
            print(f"  projects_access.yaml prepared from {exp['projects_access']}")
        if "images" in exp["cleanup"]:
            print("  docker images and containers added by the run are removed after it")
        print("  " + " ".join(f"{k}={shlex.quote(v)}" for k, v in overrides.items()))
        print("  (cd " + shlex.quote(str(ROOT)) + " && " + " ".join(shlex.quote(c) for c in cmd) + ")")
        return None

    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "workspace").mkdir()
    access = prepare_projects_access(Path(exp["projects_access"]), run_dir) if exp["projects_access"] else None
    before = docker_snapshot() if "images" in exp["cleanup"] else None
    meta = {
        "name": exp["name"], "tier": exp["tier"], "model": model, "repeat": repeat,
        "success": exp["success"], "expected_codes": exp["expected_codes"], "required_codes": exp["required_codes"],
        "share": exp["share"], "projects_access": access, "cleanup": exp["cleanup"], "memory": exp["memory"],
        "isolation": isolation_of(exp, access),
        "budget_usd": exp["budget_usd"], "timeout_s": exp["timeout_s"], "effort": exp["effort"],
        "allowed_tools": exp["allowed_tools"], "disallowed_tools": exp["disallowed_tools"],
        "notes": exp["notes"], "source": exp["source"], "corpus": exp["corpus"],
        "prompt": exp["prompt"], "command": cmd, "env_overrides": overrides,
        "workspace": str(run_dir / "workspace"), "cwd": str(ROOT),
        "code_rev": _git(["rev-parse", "HEAD"]), "code_dirty": bool(_git(["status", "--porcelain"])),
        "started_at": datetime.now(timezone.utc).isoformat(), "timed_out": False,
    }
    (run_dir / "experiment.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"→ {exp['name']} · {model} · repeat {repeat} · {run_dir.name}", flush=True)

    t0 = time.monotonic()
    with open(run_dir / "transcript.jsonl", "wb") as out, open(run_dir / "stderr.log", "wb") as err:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                start_new_session=True)
        try:
            rc = proc.wait(timeout=exp["timeout_s"])
        except subprocess.TimeoutExpired:
            meta["timed_out"] = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                rc = proc.wait(timeout=30)
            except Exception:
                os.killpg(proc.pid, signal.SIGKILL)
                rc = proc.wait()
            meta["jobs_stopped"] = stop_live_jobs(run_dir / "workspace" / "scratch" / "jobs")
    meta.update({"ended_at": datetime.now(timezone.utc).isoformat(),
                 "wall_ms": int((time.monotonic() - t0) * 1000), "returncode": rc})
    if before is not None:
        meta["cleanup_result"] = docker_cleanup(before)
    (run_dir / "experiment.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    row = parse_run(run_dir)
    write_row(run_dir, row)
    cost = f"${row['cost_usd']:.3f}" if row["cost_usd"] is not None else "unreported"
    print(f"   {'ok ' if row['success'] else 'no '} success={row['success']} cost={cost} "
          f"tool_calls={row['tool_calls']} mcp={row['mcp_calls']} refused={row['mcp_refused']} "
          f"broke={row['mcp_broke']} wall={row['wall_ms'] / 1000:.0f}s"
          f"{' TIMED OUT' if meta['timed_out'] else ''}{f' rc={rc}' if rc else ''}", flush=True)
    if meta.get("jobs_stopped"):
        print(f"   stopped {len(meta['jobs_stopped'])} job(s) the killed session left running: "
              f"{', '.join(meta['jobs_stopped'])}", flush=True)
    cl = meta.get("cleanup_result")
    if cl:
        print(f"   cleanup: {len(cl['images_removed'])} images, {len(cl['containers_removed'])} containers removed"
              f"{'; ' + str(len(cl['failed'])) + ' failed' if cl['failed'] else ''}"
              f"{'; host tags restored: ' + ', '.join(cl['host_tags_restored']) if cl.get('host_tags_restored') else ''}"
              f"{'; HOST TAGS LOST: ' + str(cl['host_tags_lost']) if cl.get('host_tags_lost') else ''}"
              f"{'; ' + cl['skipped'] if cl.get('skipped') else ''}", flush=True)
    return run_dir


def experiment_setups(experiment_dirs: list[Path]) -> dict[str, list[dict]]:
    """Every run's experiment.json under the given experiment directories, grouped by
    experiment name, oldest first — the conditions the report states."""
    out: dict[str, list[dict]] = {}
    for exp in experiment_dirs:
        for run in sorted(Path(exp).iterdir()) if Path(exp).is_dir() else []:
            mj = run / "experiment.json"
            if mj.exists():
                m = json.loads(mj.read_text(encoding="utf-8"))
                out.setdefault(m.get("name", Path(exp).name), []).append(m)
    return out


def print_scoreboard(rows: list[dict]) -> None:
    print(f"{'experiment':<18} {'model':<10} {'n':>2} {'pass@1':>6} {'pass^k':>6} {'$/run':>7} {'$/pass':>7} "
          f"{'out tok':>8} {'tools':>5} {'mcp':>4} {'srch':>4} {'refusd':>6} {'wall':>6}")
    for g in aggregate(rows):
        pct = lambda v: "—" if v is None else f"{100 * v:.0f}%"   # noqa: E731
        usd = lambda v: "—" if v is None else f"{v:.2f}"          # noqa: E731
        run_cost = "—" if g["cost_usd_mean"] is None else f"{g['cost_usd_mean']:.3f}"
        print(f"{g['experiment'][:18]:<18} {g['model'][:10]:<10} {g['n']:>2} {pct(g['pass_at_1']):>6} "
              f"{pct(g['pass_pow_k']):>6} {run_cost:>7} {usd(g['cost_of_pass']):>7} "
              f"{g['output_tokens_mean']:>8.0f} {g['tool_calls_mean']:>5.1f} {g['mcp_calls_mean']:>4.1f} "
              f"{g['tool_search_calls_mean']:>4.1f} {g['mcp_refused_mean']:>6.1f} {g['wall_ms_mean'] / 1000:>5.0f}s")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_run(a: argparse.Namespace) -> int:
    exp = load_experiment(Path(a.experiment))
    if a.models:
        exp["models"] = a.models
    if a.repeats:
        exp["repeats"] = a.repeats
    if a.budget:
        exp["budget_usd"] = a.budget
    if a.timeout:
        exp["timeout_s"] = a.timeout
    root = Path(a.root) if a.root else workspace.experiments_dir()
    for model in exp["models"]:
        for repeat in range(1, int(exp["repeats"]) + 1):
            run_once(exp, model, repeat, root, dry_run=a.dry_run)
    if a.dry_run:
        return 0
    exp_dir = root / exp["name"]
    rows = load_rows([exp_dir])
    page = write_report(rows, exp_dir, experiment_setups([exp_dir]), title=f"Agent experiments — {exp['name']}")
    print_scoreboard(rows)
    print(f"report: {page}")
    return 0


def write_corpus_reports(rows: list[dict], dirs: list[Path], root: Path, title: str) -> dict[str, Path]:
    """One report per corpus under `root/<corpus>/`, and `root/index.html` above them. A
    corpus is read off each row, so the cross-revision table of one corpus never carries
    another's runs."""
    by_corpus: dict[str, list[dict]] = {}
    for r in rows:
        by_corpus.setdefault(str(r.get("corpus") or "uncategorised"), []).append(r)
    pages: dict[str, Path] = {}
    index: list[dict] = []
    for corpus in sorted(by_corpus):
        crows = by_corpus[corpus]
        names = {r["experiment"] for r in crows}
        cdirs = [d for d in dirs if d.name in names]
        pages[corpus] = write_report(crows, root / corpus, experiment_setups(cdirs), title=f"{title} — {corpus}")
        judged = [r for r in crows if r.get("success") is not None]
        index.append({
            "corpus": corpus, "href": f"{corpus}/report.html", "experiments": len(names), "runs": len(crows),
            "pass_at_1": (sum(1 for r in judged if r["success"]) / len(judged)) if judged else None,
            "revisions": len({r.get("code_rev") for r in crows}),
            "last_run": max((str(r.get("started_at") or "") for r in crows), default="")[:16].replace("T", " "),
        })
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(render_index(index, title), encoding="utf-8")
    return pages


def cmd_report(a: argparse.Namespace) -> int:
    explicit = [Path(d) for d in a.experiment_dirs]
    dirs = explicit or sorted(
        p for p in workspace.experiments_dir().iterdir() if p.is_dir() and not p.name.startswith("_"))
    rows = load_rows(dirs, reparse=a.reparse)
    if not rows:
        print("no runs found under " + ", ".join(str(d) for d in dirs), file=sys.stderr)
        return 1
    if explicit or a.out:
        out = Path(a.out) if a.out else (dirs[0] if len(dirs) == 1 else workspace.experiments_dir() / "_report")
        page = write_report(rows, out, experiment_setups(dirs), title=a.title)
        print_scoreboard(rows)
        print(f"report: {page}")
        return 0
    root = workspace.experiments_dir() / "_report"
    pages = write_corpus_reports(rows, dirs, root, a.title)
    for corpus, page in pages.items():
        print(f"\n== corpus {corpus}")
        print_scoreboard([r for r in rows if str(r.get("corpus") or "uncategorised") == corpus])
        print(f"report: {page}")
    print(f"index: {root / 'index.html'}")
    return 0


def cmd_parse(a: argparse.Namespace) -> int:
    for d in a.run_dirs:
        row = parse_run(Path(d))
        write_row(Path(d), row)
        print(json.dumps(row, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run an experiment file: every model × every repeat, then the report")
    r.add_argument("experiment")
    r.add_argument("--models", nargs="+", help="override the file's models")
    r.add_argument("--repeats", type=int)
    r.add_argument("--budget", type=float, help="override budget_usd")
    r.add_argument("--timeout", type=int, help="override timeout_s")
    r.add_argument("--root", help="experiments root (default: the experiments zone)")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_run)
    rp = sub.add_parser("report", help="render a report over one or more experiment directories")
    rp.add_argument("experiment_dirs", nargs="*")
    rp.add_argument("--out", help="directory for report.html (default: the experiment dir, or _report/ for several)")
    rp.add_argument("--title", default="Agent experiments")
    rp.add_argument("--reparse", action="store_true", help="re-read every transcript instead of its metrics.json")
    rp.set_defaults(fn=cmd_report)
    ps = sub.add_parser("parse", help="re-parse one or more run directories")
    ps.add_argument("run_dirs", nargs="+")
    ps.set_defaults(fn=cmd_parse)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except ExperimentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
