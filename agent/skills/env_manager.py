"""
EnvManager — conda environment lifecycle operations.

All conda commands are run via subprocess so they use the system conda
(or the one active in PATH). The env prefix is set relative to the
project root so envs are portable and easy to locate.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


class EnvManager:
    def __init__(self, config: dict):
        self.config = config
        self.project_root = Path(__file__).parent.parent.parent.resolve()
        self.envs_dir = self.project_root / config["paths"]["conda_envs_prefix"]
        self.envs_dir.mkdir(parents=True, exist_ok=True)

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
            "conda", "create",
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
            ["conda", "install", "--prefix", str(env_path), "--yes", "--quiet"]
            + channel_args
            + specs
        )
        result = self._run(cmd, timeout=self.config["agent"]["install_timeout_seconds"])

        if result["returncode"] != 0:
            # Try mamba as fallback if available
            if shutil.which("mamba"):
                cmd[0] = "mamba"
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
        inputs: list[str] | None = None,
        watch_dir: str | None = None,
    ) -> dict[str, Any]:
        env_path = self.envs_dir / env_name

        watch = Path(watch_dir) if watch_dir else (Path(working_dir) if working_dir else None)
        before = self._snapshot(watch)

        cmd = ["conda", "run", "--prefix", str(env_path), "--no-capture-output",
               "/bin/bash", "-c", command]

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
        """Return filenames of files created or modified since the snapshot."""
        if not directory or not directory.exists():
            return []
        result = []
        for p in directory.rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(directory))
            if rel not in before or p.stat().st_mtime > before[rel]:
                result.append(p.name)
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
