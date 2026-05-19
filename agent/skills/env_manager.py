"""
EnvManager — conda environment lifecycle operations.

All conda commands are run via subprocess so they use the system conda
(or the one active in PATH). The env prefix is set relative to the
project root so envs are portable and easy to locate.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


# conda spec grammar: "{name}{op?}{version?}{=build?}".
# Operators: =, ==, >=, <=, >, <, != (the last one is rare but legal).
# Name token is [A-Za-z0-9_.-]+ — anything else terminates the name.
_CONDA_SPEC_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)"
    r"(?P<op>[<>=!]+)?"
    r"(?P<version>[^<>=!]*)$"
)


_VERSION_FROM_URL_PATTERNS = (
    # GitHub release URL: .../releases/download/{tag}/{file}
    re.compile(r"/releases/download/v?([0-9][0-9A-Za-z.\-_+]*)/", re.IGNORECASE),
    # GitHub tag page: .../releases/tag/{tag}
    re.compile(r"/releases/tag/v?([0-9][0-9A-Za-z.\-_+]*)/?$", re.IGNORECASE),
    # Generic: trailing version-looking segment in the filename
    re.compile(r"[-_]v?(\d+(?:\.\d+){1,3})(?=[._-]|$)"),
)


def parse_version_from_url(url: str) -> str:
    """Extract a version string from a download URL.

    Recognises GitHub `releases/download/{tag}/...` and tag-page URLs, plus
    a generic trailing version pattern in the filename. Returns "" if no
    plausible version is found — caller can fall back to "latest" or probe
    the binary.
    """
    if not url:
        return ""
    for pat in _VERSION_FROM_URL_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return ""


def parse_conda_spec(spec: str) -> dict:
    """Parse a conda package spec into {name, version, constraint}.

    Returns {name: ..., version: <without operator>, constraint: <operator | ''>}.
    Used by install_packages to record clean PackageRecord fields rather than
    splitting on '=' (which puts the '>' from '>=' onto the name).
    """
    s = (spec or "").strip()
    if not s:
        return {"name": "", "version": "", "constraint": ""}
    m = _CONDA_SPEC_RE.match(s)
    if not m:
        return {"name": s, "version": "", "constraint": ""}
    return {
        "name":       m.group("name"),
        "constraint": m.group("op") or "",
        "version":    (m.group("version") or "").strip(),
    }


class EnvManager:
    def __init__(self, config: dict):
        self.config = config
        self.project_root = Path(__file__).parent.parent.parent.resolve()
        self.envs_dir = self.project_root / config["paths"]["conda_envs_prefix"]
        self.envs_dir.mkdir(parents=True, exist_ok=True)
        self._conda_exe = self._detect_conda()

    @staticmethod
    def _detect_conda() -> str:
        """Return the best available conda-compatible solver.

        Prefers `conda` over standalone `mamba` because modern conda (24.x+)
        ships libmamba as its default solver — already as fast as mamba — and
        sidesteps brittle Python entry points in some miniforge installs where
        the standalone `mamba` binary fails on import with ImportError on
        conda.cli.main."""
        for exe in ("conda", "mamba"):
            if shutil.which(exe):
                return exe
        raise RuntimeError("No conda or mamba executable found in PATH")

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def create(self, env_name: str, python_version: str | None = None) -> dict[str, Any]:
        env_path = self.envs_dir / env_name
        py_ver = python_version or self.config["conda"]["python_version"]

        if env_path.exists():
            return {
                "success": True,
                "env_name": env_name,
                "env_path": str(env_path),
                "note": "Environment already exists — reusing it.",
            }

        cmd = [
            self._conda_exe, "create",
            "--prefix", str(env_path),
            f"python={py_ver}",
            "--yes", "--quiet",
        ]
        result = self._run(cmd)
        if result["returncode"] != 0:
            return {"success": False, "env_name": env_name, "error": result["stderr"]}

        return {
            "success": True,
            "env_name": env_name,
            "env_path": str(env_path),
            "python_version": py_ver,
        }

    def install(self, env_name: str, packages: list[dict]) -> dict[str, Any]:
        """
        Install a list of packages into env_name.

        packages: [{"spec": "bwa=0.7.17", "channel": "bioconda"}, ...]

        Groups packages by channel to minimise solver calls, but always
        runs a single solve across all channels for best dependency resolution.
        """
        env_path = self.envs_dir / env_name
        channels = []
        specs = []

        for pkg in packages:
            ch = pkg.get("channel", "conda-forge")
            if ch not in channels:
                channels.append(ch)
            specs.append(pkg["spec"])

        # Always include base channels so dependencies resolve
        for base_ch in self.config["conda"]["base_channels"]:
            if base_ch not in channels:
                channels.append(base_ch)

        channel_args = []
        for ch in channels:
            channel_args += ["-c", ch]

        # Also install conda-pack so we can build Docker images later
        if "conda-pack" not in " ".join(specs):
            specs.append("conda-pack")

        cmd = (
            [self._conda_exe, "install", "--prefix", str(env_path), "--yes", "--quiet"]
            + channel_args
            + specs
        )
        result = self._run(cmd, timeout=self.config["agent"]["install_timeout_seconds"])

        return {
            "success": result["returncode"] == 0,
            "env_name": env_name,
            "packages_requested": [p["spec"] for p in packages],
            "stdout": result["stdout"][-3000:],
            "stderr": result["stderr"][-3000:],
            "returncode": result["returncode"],
        }

    def install_pip(self, env_name: str, pip_specs: list[str]) -> dict[str, Any]:
        env_path = self.envs_dir / env_name
        pip_bin = env_path / "bin" / "pip"

        cmd = [str(pip_bin), "install"] + pip_specs
        result = self._run(cmd, timeout=self.config["agent"]["install_timeout_seconds"])

        return {
            "success": result["returncode"] == 0,
            "env_name": env_name,
            "packages_requested": pip_specs,
            "stdout": result["stdout"][-2000:],
            "stderr": result["stderr"][-2000:],
        }

    def verify(self, env_name: str, package_name: str, check_command: str) -> dict[str, Any]:
        result = self.run_in_env(env_name, check_command, timeout=30)
        output = (result.get("stdout", "") + result.get("stderr", "")).strip()
        success = result.get("returncode", 1) == 0

        return {
            "success": success,
            "package_name": package_name,
            "check_command": check_command,
            "output": output[:500],
            "returncode": result.get("returncode"),
        }

    def run_in_env(
        self,
        env_name: str,
        command: str,
        working_dir: str | None = None,
        timeout: int = 1800,
        inputs: list | None = None,
        watch_dir: str | None = None,
    ) -> dict[str, Any]:
        env_path = self.envs_dir / env_name

        watch = Path(watch_dir) if watch_dir else (Path(working_dir) if working_dir else None)
        before = self._snapshot(watch)

        # `set -o pipefail` makes `cmd | tail` propagate cmd's non-zero exit
        # status instead of swallowing it. Without this a step like
        # `flye --nano-raw X 2>&1 | tail -50` reports rc=0 even when flye crashed
        # because tail itself succeeded — masking real failures.
        wrapped_command = f"set -o pipefail; {command}"
        cmd = [self._conda_exe, "run", "--prefix", str(env_path), "--no-capture-output",
               "/bin/bash", "-c", wrapped_command]

        t0 = time.monotonic()
        result = self._run(
            cmd,
            cwd=working_dir or str(self.project_root),
            timeout=timeout,
        )
        return {
            "returncode": result["returncode"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "success": result["returncode"] == 0,
            "command": command,
            "runtime_seconds": round(time.monotonic() - t0, 2),
            "inputs": inputs or [],
            "detected_outputs": self._diff_snapshot(before, watch),
        }

    def env_path(self, env_name: str) -> Path:
        return self.envs_dir / env_name

    def install_jar_tool(
        self,
        env_name: str,
        tool_name: str,
        jar_url: str,
        java_flags: list[str] | None = None,
        wrapper_name: str = "",
    ) -> dict[str, Any]:
        """Install a Java JAR-based tool into the conda env, end-to-end.

        Collapses the Exomiser/Picard/GATK pattern (curl JAR → unzip if needed →
        wrapper script that calls `java -jar`) into one operation.  Assumes the
        env already has `openjdk` and `curl` (and `unzip` if the URL ends .zip).

        For a single .jar URL, the JAR lands at:
            {env}/share/{tool_name}/{tool_name}.jar
        For a .zip URL (e.g. Exomiser's distribution zip), the archive is
        unpacked into {env}/share/{tool_name}/ and the JAR is auto-located
        (first *.jar with the tool name as a substring; falls back to first
        *.jar found).

        The wrapper at {env}/bin/{wrapper_name or tool_name} runs:
            java {java_flags} -jar <jar_path> "$@"

        Returns: {success, wrapper_path, jar_path, share_dir, log}.
        Idempotent: re-running with the same args overwrites the wrapper but
        skips the download if the JAR already exists.
        """
        env_path  = self.envs_dir / env_name
        if not env_path.exists():
            return {"success": False, "error": f"env not found: {env_path}"}

        share_dir = env_path / "share" / tool_name
        bin_dir   = env_path / "bin"
        share_dir.mkdir(parents=True, exist_ok=True)
        log: list[str] = []
        java_flags = java_flags or ["-Xmx4g"]
        wrapper    = bin_dir / (wrapper_name or tool_name)

        is_zip = jar_url.endswith(".zip")
        download_target = share_dir / Path(jar_url).name
        existing_jars = list(share_dir.rglob("*.jar"))

        # Skip download if a JAR already exists in share_dir.
        if existing_jars:
            log.append(f"JAR already present ({len(existing_jars)} found) — skipping download.")
            jar_path = self._select_jar(existing_jars, tool_name)
        else:
            # curl in normal mode (progress bar to stderr) — watchdog-friendly.
            curl = self.run_in_env(
                env_name,
                f"curl -L --progress-bar -o {download_target} '{jar_url}'",
                timeout=3600,
            )
            log.append(f"curl rc={curl['returncode']}")
            if curl["returncode"] != 0:
                return {"success": False, "error": "JAR download failed",
                        "stderr": (curl.get("stderr") or "")[-500:], "log": log}

            if is_zip:
                unz = self.run_in_env(
                    env_name,
                    f"cd {share_dir} && unzip -o {download_target.name} && rm {download_target.name}",
                    timeout=600,
                )
                log.append(f"unzip rc={unz['returncode']}")
                if unz["returncode"] != 0:
                    return {"success": False, "error": "unzip failed",
                            "stderr": (unz.get("stderr") or "")[-500:], "log": log}
                jars = list(share_dir.rglob("*.jar"))
                if not jars:
                    return {"success": False, "error": "no *.jar found after unzip",
                            "share_dir": str(share_dir), "log": log}
                jar_path = self._select_jar(jars, tool_name)
            else:
                jar_path = download_target

        # Write the wrapper.
        flags = " ".join(java_flags)
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            f"# Wrapper for {tool_name} (auto-generated by bioinf_agent.install_jar_tool)\n"
            f'exec java {flags} -jar "{jar_path}" "$@"\n'
        )
        wrapper.chmod(0o755)
        log.append(f"wrapper written: {wrapper}")

        return {
            "success":      True,
            "tool_name":    tool_name,
            "wrapper_path": str(wrapper),
            "jar_path":     str(jar_path),
            "jar_url":      jar_url,
            "share_dir":    str(share_dir),
            "java_flags":   java_flags,
            "log":          log,
        }

    @staticmethod
    def _select_jar(jars: list[Path], tool_name: str) -> Path:
        """Pick the most likely "primary" JAR from a directory of jars.

        Prefer files whose name contains the tool name; among those, prefer the
        shortest name (e.g. exomiser-cli-15.0.0.jar vs
        exomiser-spring-data-genome-1.0.0.jar).
        """
        nm = tool_name.lower()
        matches = [j for j in jars if nm in j.name.lower()]
        candidates = matches or list(jars)
        return min(candidates, key=lambda p: len(p.name))

    # -----------------------------------------------------------------------
    # Authoritative version probes — used at finalize-time to reconcile
    # what's actually installed against what the draft thinks is installed.
    # -----------------------------------------------------------------------

    def list_conda_packages(self, env_name: str) -> dict[str, str]:
        """Return {package_name: version} for everything conda knows about in the env.

        Source of truth for resolved_version of conda packages — search_package
        records the channel's 'latest' but the solver may pin differently based
        on co-installed packages' constraints (e.g. multtest downgraded for r-base 4.4).
        """
        return {n: rec["version"] for n, rec in self.list_conda_package_records(env_name).items()}

    def list_conda_package_records(self, env_name: str) -> dict[str, dict]:
        """Return {name: {version, channel, build_string}} from `conda list --json`.

        The channel field on each record is conda's authoritative answer to "where
        did this package come from" — the spec's PackageRecord.channel should be
        derived from here, not from whatever the agent passed to install_packages
        (which is the channel hint, not the resolved channel).
        """
        env_path = self.envs_dir / env_name
        if not env_path.exists():
            return {}
        result = self._run(
            [self._conda_exe, "list", "--prefix", str(env_path), "--json"],
            cwd=str(self.project_root),
            timeout=120,
        )
        if result["returncode"] != 0:
            return {}
        import json
        try:
            entries = json.loads(result["stdout"])
        except Exception:
            return {}
        records = {}
        for e in entries:
            name = e.get("name", "")
            if not name:
                continue
            records[name] = {
                "version":       e.get("version", ""),
                "channel":       e.get("channel", ""),
                "build_string":  e.get("build_string", ""),
            }
        return records

    def list_explicit_conda_packages(self, env_name: str) -> set[str]:
        """Return the *explicitly-requested* package names from conda's history db.

        Distinct from list_conda_packages, which returns every installed package
        (including transitive deps). This is the set conda export --from-history
        would dump — what the user actually asked for. The right filter for the
        spec's `packages` field, which should be the tool list, not the closure.

        Returns an empty set on failure.
        """
        env_yml = self.export_environment_yml(env_name, from_history=True)
        if not env_yml:
            return set()
        import yaml as _yaml
        try:
            data = _yaml.safe_load(env_yml) or {}
        except Exception:
            return set()
        names: set[str] = set()
        for entry in data.get("dependencies", []) or []:
            if isinstance(entry, str):
                # conda spec like "r-base=4.4" or "samtools" — keep the bare name
                names.add(entry.split("=", 1)[0].split(" ", 1)[0])
            elif isinstance(entry, dict) and "pip" in entry:
                # nested pip block: list of pip spec strings
                for pip_spec in entry["pip"] or []:
                    if isinstance(pip_spec, str):
                        names.add(pip_spec.split("=", 1)[0].split("<", 1)[0].split(">", 1)[0].strip())
        return names

    def export_environment_yml(self, env_name: str, from_history: bool = True) -> str:
        """Return the conda environment definition as YAML text.

        `from_history=True` exports only the packages explicitly requested
        (clean, portable, matches what someone reading the file expects).
        `from_history=False` exports the full solved env including transitive
        deps (lossless, but bulky and OS/arch-coupled).

        Returns "" on failure. Caller writes the text to a `.environment.yml`
        file alongside the spec.
        """
        env_path = self.envs_dir / env_name
        if not env_path.exists():
            return ""
        cmd = [self._conda_exe, "env", "export", "--prefix", str(env_path)]
        if from_history:
            cmd.append("--from-history")
        result = self._run(cmd, cwd=str(self.project_root), timeout=120)
        return result["stdout"] if result["returncode"] == 0 else ""

    def export_explicit_lock(self, env_name: str) -> str:
        """Return a `conda list --explicit` lock file content (URL-pinned).

        Recreating the env from this lock guarantees the *exact* same package
        builds. Architecture-coupled (osx-arm64 vs linux-64 etc.) but
        bombproof for the platform it was generated on.
        """
        env_path = self.envs_dir / env_name
        if not env_path.exists():
            return ""
        result = self._run(
            [self._conda_exe, "list", "--prefix", str(env_path), "--explicit"],
            cwd=str(self.project_root),
            timeout=120,
        )
        return result["stdout"] if result["returncode"] == 0 else ""

    def r_package_version(self, env_name: str, package_name: str) -> str:
        """Return the version `packageVersion('X')` reports for an R package,
        or '' if not installed / not loadable.

        Used to reconcile resolved_version for run_install_command packages
        (BiocManager / install_github / CRAN install.packages) where the conda
        list doesn't know them, and where the R-package intrinsic version is
        what users actually want to see.
        """
        env_path = self.envs_dir / env_name
        if not env_path.exists():
            return ""
        # Escape single quotes in package name (defensive — R package names
        # don't normally contain them, but the input crosses a tool boundary).
        safe_name = package_name.replace("'", "\\'")
        rscript = (
            f"v <- tryCatch(as.character(packageVersion('{safe_name}')),"
            f" error=function(e) ''); cat(v)"
        )
        result = self._run(
            [self._conda_exe, "run", "--prefix", str(env_path), "--no-capture-output",
             "Rscript", "-e", rscript],
            cwd=str(self.project_root),
            timeout=60,
        )
        if result["returncode"] != 0:
            return ""
        return result["stdout"].strip()

    def start_service(
        self,
        env_name: str,
        service_name: str,
        start_command: str,
        health_check_command: str,
        health_check_timeout_seconds: int = 30,
        working_dir: str | None = None,
        env_vars: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Start a background service inside the env and wait until healthy."""
        env_path = self.envs_dir / env_name
        pid_dir = Path("/tmp/bioinf_services")
        pid_dir.mkdir(parents=True, exist_ok=True)
        pid_file = pid_dir / f"{service_name}.pid"
        log_file = pid_dir / f"{service_name}.log"

        wrapped = (
            f"nohup bash -c {repr(start_command)} > {log_file} 2>&1 & echo $! > {pid_file}"
        )
        cmd = ["conda", "run", "--prefix", str(env_path), "--no-capture-output",
               "/bin/bash", "-c", wrapped]

        extra_env = os.environ.copy()
        if env_vars:
            extra_env.update(env_vars)

        launch = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            cwd=working_dir or str(self.project_root), env=extra_env,
        )
        if launch.returncode != 0:
            return {"success": False, "service_name": service_name, "error": launch.stderr[-500:]}

        pid = pid_file.read_text().strip() if pid_file.exists() else ""
        deadline = time.monotonic() + health_check_timeout_seconds
        while time.monotonic() < deadline:
            health = self.check_service_health(env_name, health_check_command, working_dir)
            if health["healthy"]:
                return {"success": True, "service_name": service_name, "pid": pid, "log": str(log_file)}
            time.sleep(2)

        return {
            "success": False, "service_name": service_name, "pid": pid,
            "error": f"Service did not become healthy within {health_check_timeout_seconds}s",
            "log": str(log_file),
        }

    def stop_service(
        self,
        env_name: str,
        service_name: str,
        stop_command: str = "",
        working_dir: str | None = None,
    ) -> dict[str, Any]:
        """Stop a background service by running stop_command or killing by PID file."""
        if stop_command:
            result = self.run_in_env(env_name, stop_command, working_dir=working_dir, timeout=30)
            return {"success": result["returncode"] == 0, "service_name": service_name, "method": "stop_command"}

        pid_file = Path("/tmp/bioinf_services") / f"{service_name}.pid"
        if not pid_file.exists():
            return {"success": False, "service_name": service_name, "error": "No PID file and no stop_command"}
        pid = pid_file.read_text().strip()
        try:
            subprocess.run(["kill", pid], check=True, timeout=10)
            pid_file.unlink(missing_ok=True)
            return {"success": True, "service_name": service_name, "pid": pid, "method": "kill"}
        except Exception as e:
            return {"success": False, "service_name": service_name, "pid": pid, "error": str(e)}

    def check_service_health(
        self,
        env_name: str,
        health_check_command: str,
        working_dir: str | None = None,
    ) -> dict[str, Any]:
        """Run a health-check command to verify a background service is responding."""
        result = self.run_in_env(env_name, health_check_command, working_dir=working_dir, timeout=15)
        return {
            "healthy": result["returncode"] == 0,
            "returncode": result["returncode"],
            "stdout": result.get("stdout", "")[:500],
            "stderr": result.get("stderr", "")[:500],
        }

    # -----------------------------------------------------------------------
    # Filesystem snapshot helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _snapshot(directory: Path | None) -> dict[str, float]:
        """Return {relative_path: mtime} for every file under directory."""
        if not directory or not directory.exists():
            return {}
        return {
            str(p.relative_to(directory)): p.stat().st_mtime
            for p in directory.rglob("*") if p.is_file()
        }

    @staticmethod
    def _diff_snapshot(before: dict[str, float], directory: Path | None) -> list[str]:
        """Return absolute paths of files created or modified since the snapshot.

        Returns absolute paths so downstream tools (validate_output, the next
        run_in_env step's `inputs`) can use them directly without manual joins.
        Files in subdirectories of `directory` (e.g. Flye's `00-assembly/`,
        `20-repeat/`) are preserved with their full path — the prior basename-
        only output dropped subdir info and broke pipeline lineage.
        """
        if not directory or not directory.exists():
            return []
        result = []
        for p in directory.rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(directory))
            if rel not in before or p.stat().st_mtime > before[rel]:
                result.append(str(p.resolve()))
        return result

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _run(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout: int = 300,
    ) -> dict:
        env = os.environ.copy()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd or str(self.project_root),
                env=env,
            )
            return {
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        except subprocess.TimeoutExpired:
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s: {' '.join(cmd)}",
            }
        except Exception as e:
            return {"returncode": -1, "stdout": "", "stderr": str(e)}
