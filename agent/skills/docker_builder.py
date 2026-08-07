"""
DockerBuilder — run-side container utilities for the frozen env image.

Post container-native pivot, image *building* lives in env_freeze /
container_build (install + validate INSIDE the ship image). What remains here is
the run/ship surface that operates on an already-built image:

  - run_in_container(): the validation-locus pivot — run a workflow step INSIDE
    the shipped image and capture outputs + resource_usage (I7) via GNU `time -v`
    (in-container) with a host `docker stats` fallback.
  - save_archive(): `docker save` an image to a tarball (Apptainer docker-archive
    delivery; used by freeze()).
  - image_digest() / image_present(): daemon queries.

The conda-pack tarball builder + its Dockerfile templates were retired with the
host build path.
"""

import re
import subprocess
from pathlib import Path
from typing import Any

from agent.skills.outcomes import proven, broke


class DockerBuilder:
    def __init__(self, config: dict):
        self.config = config
        self.project_root = Path(__file__).parent.parent.parent.resolve()
        self.envs_dir = self.project_root / config["paths"]["conda_envs_prefix"]
        # NO `self.output_dir`. It was read from `paths.docker_output_dir`, used only
        # to mkdir itself, and never consulted again — while the sole producer of a
        # freeze tarball hardcodes `<repo>/docker_images/<name>/` and `save_archive`
        # mkdirs that parent on demand. So setting the key created an empty directory
        # and moved nothing. Deleted with the key on 2026-08-06.

    # -----------------------------------------------------------------------
    # Run a step INSIDE the env image — the validation-locus pivot.
    #
    # The honest model: the artifact we ship is the artifact we validate. Instead
    # of running on the host (a different arch) and shipping a separately-built
    # image, run the workflow step in the SHIPPED image and capture its outputs +
    # resources. Resource usage (I7) is sampled from the HOST via `docker stats`
    # so it works for ANY image — our recipe builds AND adopted biocontainers —
    # with no in-container dependency (mirrors the host psutil monitor's sampling).
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_mem_mb(tok: str) -> float | None:
        tok = tok.strip()
        m = re.match(r"([0-9.]+)\s*([A-Za-z]+)", tok)
        if not m:
            return None
        val, unit = float(m.group(1)), m.group(2).lower()
        scale = {"b": 1 / 2**20, "kib": 1 / 2**10, "kb": 1 / 2**10,
                 "mib": 1.0, "mb": 1.0, "gib": 2**10, "gb": 2**10,
                 "tib": 2**20, "tb": 2**20}.get(unit)
        return val * scale if scale is not None else None

    @staticmethod
    def _parse_pct(tok: str) -> float | None:
        try:
            return float(tok.strip().rstrip("%"))
        except (ValueError, AttributeError):
            return None

    _RUNDIR = "/bioinf_run"  # fixed scratch mount (cmd script + rusage), kept off the data mount

    def run_in_container(
        self, image: str, command: str, *,
        mounts: list[tuple[str, str]] | None = None,
        workdir: str | None = None,
        platform: str = "linux/amd64",
        timeout: int = 1800,
        poll_interval: float = 0.25,
    ) -> dict[str, Any]:
        """Run `command` (via bash) inside `image`, bind-mounting `mounts`
        [(host, container), …], and capture {returncode, stdout, stderr,
        resource_usage, resource_method}.

        Resource capture is two-tier so I7 is honest for any run length AND any
        image: GNU `time -v` (getrusage → exact peak RSS even for sub-second
        tools) writes to a private scratch mount when the image has it (our
        recipe images always do); host-side `docker stats` polling is the
        universal fallback (sampled) for images without `time` (e.g. some adopted
        biocontainers). The command is staged as a script on the scratch mount so
        no user quoting can break the wrapper, and the data mount stays clean."""
        import tempfile, time as _time
        mounts = list(mounts or [])
        scratch = Path(tempfile.mkdtemp(prefix="bioinf_run_"))
        (scratch / "cmd.sh").write_text(command + "\n")
        mounts.append((str(scratch), self._RUNDIR))
        rd = self._RUNDIR
        # GNU time if present → exact rusage; else run plainly (stats fallback covers it).
        launcher = (f'if command -v /usr/bin/time >/dev/null 2>&1; then '
                    f'/usr/bin/time -v -o {rd}/rusage bash {rd}/cmd.sh; '
                    f'else bash {rd}/cmd.sh; fi')

        run_cmd = ["docker", "run", "-d", "--platform", platform]
        for host, cont in mounts:
            run_cmd += ["-v", f"{host}:{cont}"]
        if workdir:
            run_cmd += ["-w", workdir]
        # bash -c (NOT -lc): honor the image's ENV PATH (which points at the conda
        # ENV), exactly as `apptainer exec`/`docker run <cmd>` does. A login shell
        # re-runs conda init and auto-activates BASE, shadowing the env's tools
        # (e.g. the env perl → wrong @INC) — tools in /usr/local/bin survive that,
        # conda-env tools do not.
        run_cmd += [image, "bash", "-c", launcher]

        started = self._run(run_cmd, timeout=300)
        if started["returncode"] != 0:
            import shutil as _sh; _sh.rmtree(scratch, ignore_errors=True)
            return {"returncode": -1, "stdout": "", "image": image,
                    "stderr": "docker run failed: " + (started["stderr"] or "")[-400:],
                    "resource_usage": None}
        cid = (started["stdout"] or "").strip().split("\n")[-1]

        t0 = _time.time()
        peak_rss_mb = 0.0
        max_cpu = 0.0
        samples = 0
        deadline = t0 + timeout
        killed = False
        while True:
            st = self._run(["docker", "stats", "--no-stream", "--format",
                            "{{.MemUsage}}|{{.CPUPerc}}", cid], timeout=30)
            if st["returncode"] == 0 and "|" in (st["stdout"] or ""):
                mem, _, cpu = st["stdout"].strip().partition("|")
                rss = self._parse_mem_mb(mem.split("/")[0])
                cpv = self._parse_pct(cpu)
                if rss is not None:
                    peak_rss_mb = max(peak_rss_mb, rss)
                    samples += 1
                if cpv is not None:
                    max_cpu = max(max_cpu, cpv)
            running = self._run(["docker", "inspect", "-f", "{{.State.Running}}", cid], timeout=30)
            if (running["stdout"] or "").strip() != "true":
                break
            if _time.time() > deadline:
                self._run(["docker", "kill", cid], timeout=60)
                killed = True
                break
            _time.sleep(poll_interval)
        wall = _time.time() - t0

        ins = self._run(["docker", "inspect", "-f", "{{.State.ExitCode}}", cid], timeout=30)
        try:
            rc = int((ins["stdout"] or "").strip())
        except ValueError:
            rc = -1
        logs = self._run(["docker", "logs", cid], timeout=120)
        self._run(["docker", "rm", "-f", cid], timeout=60)

        # Prefer exact rusage (GNU time) over the sampled values when available.
        method = "docker_stats_sampled"
        ru_file = scratch / "rusage"
        accurate = self._parse_gnu_time(ru_file.read_text()) if ru_file.exists() else None
        import shutil as _sh; _sh.rmtree(scratch, ignore_errors=True)
        if accurate:
            method = "gnu_time_rusage"
            peak_rss_mb = accurate["peak_rss_mb"]
            if accurate.get("max_cpu_percent") is not None:
                max_cpu = accurate["max_cpu_percent"]
            if accurate.get("wall_seconds") is not None:
                wall = accurate["wall_seconds"]

        # Capped for the same reason and by the same helper as the host runner — see
        # env_manager._STDOUT_KEEP_CHARS. This is the path `run_step_in_container` takes,
        # and on the 5-tool flagship pipeline it was 35,188 tokens across 7 calls: 60% of
        # that whole pipeline's MCP traffic, and the single largest line item anywhere in
        # the system. The full stream spills to data/step_logs/ and the note names it.
        from agent.skills.env_manager import (_STDERR_KEEP_CHARS, _STDOUT_KEEP_CHARS,
                                              cap_stream)
        _root = Path(__file__).resolve().parents[2]
        _out, _out_note = cap_stream(logs.get("stdout", ""), _STDOUT_KEEP_CHARS,
                                     kind="stdout", project_root=_root)
        _err, _err_note = cap_stream(
            logs.get("stderr", "") + (" [killed: timeout]" if killed else ""),
            _STDERR_KEEP_CHARS, kind="stderr", project_root=_root)
        return {
            "returncode": -1 if killed else rc,
            "stdout": _out,
            "stderr": _err,
            **({"stdout_truncated": _out_note} if _out_note else {}),
            **({"stderr_truncated": _err_note} if _err_note else {}),
            "image": image,
            "resource_method": method,
            "resource_usage": {
                "wall_seconds":    round(wall, 2),
                "peak_rss_mb":     round(peak_rss_mb, 1),
                "max_cpu_percent": round(max_cpu, 1),
                "sample_count":    samples,
            },
        }

    @staticmethod
    def _parse_gnu_time(text: str) -> dict | None:
        """Parse `/usr/bin/time -v` output → {peak_rss_mb, max_cpu_percent,
        wall_seconds}. Returns None if the key line is absent."""
        rss_kb = None
        cpu = None
        wall = None
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("Maximum resident set size"):
                m = re.search(r"(\d+)", s)
                if m:
                    rss_kb = int(m.group(1))
            elif s.startswith("Percent of CPU"):
                m = re.search(r"(\d+)", s)
                if m:
                    cpu = float(m.group(1))
            elif s.startswith("Elapsed (wall clock)"):
                # The label itself contains colons ("(h:mm:ss or m:ss)") — the
                # actual value is the trailing token, e.g. "0:01.08" or "1:02:03".
                parts = (s.split()[-1] if s.split() else "").split(":")
                try:
                    if len(parts) == 3:
                        wall = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                    elif len(parts) == 2:
                        wall = int(parts[0]) * 60 + float(parts[1])
                except ValueError:
                    wall = None
        if rss_kb is None:
            return None
        return {"peak_rss_mb": round(rss_kb / 1024, 1), "max_cpu_percent": cpu,
                "wall_seconds": round(wall, 2) if wall is not None else None}

    def image_digest(self, image_tag: str) -> str:
        """Local content digest of a built image (its config Id sha256). This is
        the immutable handle freeze() records for a built (non-adopted) image —
        the build's analog of an adopted biocontainer's manifest digest."""
        res = self._run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image_tag], timeout=60
        )
        return (res.get("stdout") or "").strip() if res["returncode"] == 0 else ""

    def save_archive(self, image_tag: str, out_path: str | Path) -> dict:
        """`docker save` the image to a tarball for the registry-free HPC route
        (scp → `apptainer build X.sif docker-archive://X.tar`). The default
        delivery path, and the ONLY one allowed for license-gated images."""
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        res = self._run(["docker", "save", "-o", str(out), image_tag], timeout=900)
        if res["returncode"] == 0 and out.exists():
            return proven(
                "docker.save_archive_ok",
                success=True,
                tarball=str(out),
                stderr=res["stderr"][-500:],
            )
        return broke(
            "docker.save_archive_failed",
            success=False,
            tarball=str(out),
            stderr=res["stderr"][-500:],
        )

    def _run(self, cmd: list[str], timeout: int = 300) -> dict:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.project_root),
            )
            return {
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        except subprocess.TimeoutExpired:
            return {"returncode": -1, "stdout": "", "stderr": f"Timed out after {timeout}s"}
        except Exception as e:
            return {"returncode": -1, "stdout": "", "stderr": str(e)}
