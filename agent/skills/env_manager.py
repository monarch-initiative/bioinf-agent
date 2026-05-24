"""
EnvManager — conda environment lifecycle operations.

All conda commands are run via subprocess so they use the system conda
(or the one active in PATH). The env prefix is set relative to the
project root so envs are portable and easy to locate.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from agent.skills import evidence


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

    def create(
        self,
        env_name: str,
        python_version: str | None = None,
        subdir: str | None = None,
    ) -> dict[str, Any]:
        """Create a conda env.

        `subdir` pins the env's platform (e.g. "osx-64") for tools with no
        native build on the host arch — the common case being osx-64-only
        bioconda packages (plink, plink2, many older C/C++ tools) on Apple
        Silicon, which then run under Rosetta 2. The subdir is set for the
        create solve AND persisted to the env's .condarc via
        `conda config --env --set subdir`, so every later install into this
        env honors it. Without persistence, a follow-up `conda install` would
        solve for the host arch again and fail.
        """
        env_path = self.envs_dir / env_name
        py_ver = python_version or self.config["conda"]["python_version"]

        if env_path.exists():
            return {
                "success": True,
                "env_name": env_name,
                "env_path": str(env_path),
                "subdir": subdir or None,
                "note": "Environment already exists — reusing it.",
            }

        # CONDA_SUBDIR on the create solve selects the platform's packages.
        create_env = os.environ.copy()
        if subdir:
            create_env["CONDA_SUBDIR"] = subdir

        cmd = [
            self._conda_exe, "create",
            "--prefix", str(env_path),
            f"python={py_ver}",
            "--yes", "--quiet",
        ]
        result = self._run(cmd, env=create_env)
        if result["returncode"] != 0:
            return {"success": False, "env_name": env_name, "error": result["stderr"]}

        if subdir:
            # Persist so subsequent `conda install` into this env keep the subdir.
            persist = self._run([
                self._conda_exe, "config", "--env", "--set", "subdir", subdir,
            ], env={**os.environ, "CONDA_PREFIX": str(env_path)})
            # `conda config --env` needs the env active to target its .condarc;
            # fall back to writing the .condarc directly if the call didn't.
            condarc = env_path / ".condarc"
            if persist["returncode"] != 0 or not condarc.exists():
                existing = condarc.read_text() if condarc.exists() else ""
                if "subdir:" not in existing:
                    condarc.write_text(existing + f"subdir: {subdir}\n")

        return {
            "success": True,
            "env_name": env_name,
            "env_path": str(env_path),
            "python_version": py_ver,
            "subdir": subdir or None,
        }

    def install(self, env_name: str, packages: list[dict]) -> dict[str, Any]:
        """
        Install a list of packages into env_name.

        packages: [{"spec": "bwa=0.7.17", "channel": "bioconda"}, ...]

        Groups packages by channel to minimise solver calls, but always
        runs a single solve across all channels for best dependency resolution.

        Auto-creates the env if it doesn't exist — install_packages used to fail
        with EnvironmentLocationNotFound when an agent forgot to call create()
        first. Now: missing env → create with default python version → install.
        """
        env_path = self.envs_dir / env_name
        if not env_path.exists():
            create_result = self.create(env_name)
            if not create_result.get("success"):
                return {
                    "success":  False,
                    "env_name": env_name,
                    "error":    f"env auto-create failed: {create_result.get('error','')}",
                    "stderr":   create_result.get("error", "")[:500],
                    "returncode": 1,
                }
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
        m = self.apply(
            env_name, cmd, in_env=False,
            timeout=self.config["agent"]["install_timeout_seconds"],
        )

        return {
            "success": m["success"],
            "env_name": env_name,
            "packages_requested": [p["spec"] for p in packages],
            "stdout": m["stdout"][-3000:],
            "stderr": m["stderr"][-3000:],
            "returncode": m["returncode"],
        }

    def install_pip(self, env_name: str, pip_specs: list[str]) -> dict[str, Any]:
        env_path = self.envs_dir / env_name
        pip_bin = env_path / "bin" / "pip"

        cmd = [str(pip_bin), "install"] + pip_specs
        m = self.apply(
            env_name, cmd, in_env=False,
            timeout=self.config["agent"]["install_timeout_seconds"],
        )

        return {
            "success": m["success"],
            "env_name": env_name,
            "packages_requested": pip_specs,
            "stdout": m["stdout"][-2000:],
            "stderr": m["stderr"][-2000:],
        }

    def apply(
        self,
        env_name: str,
        command,
        *,
        in_env: bool = True,
        timeout: int | None = None,
        watch_dir: str | None = None,
    ) -> dict[str, Any]:
        """Universal mutation primitive — run a command that changes the env and
        capture it as a Mutation.

        This is the single chokepoint the install re-spine routes through:
        install()/install_pip() (and, as the re-spine proceeds, every other
        install tier) delegate execution here so the capture shape — command,
        returncode, success, stdout/stderr, and (in-env) resource usage +
        detected outputs — is produced in exactly ONE place rather than
        re-derived per method. Pairing a Mutation with an evidence strategy
        (see agent.skills.evidence) is what `verify()` and, later, `seal()` do.

        in_env=True  → run inside the env via `conda run` (run_in_env);
                       `command` is a shell string. Captures resource_usage
                       and detected_outputs.
        in_env=False → run a raw command list from the base context (e.g.
                       `conda install --prefix …`, the env's `pip install …`)
                       via _run; `command` is a list[str].
        """
        if in_env:
            if not isinstance(command, str):
                raise TypeError("apply(in_env=True) expects a shell-string command")
            res = self.run_in_env(
                env_name, command,
                timeout=timeout if timeout is not None else 1800,
                watch_dir=watch_dir,
            )
            return {
                "command":          command,
                "returncode":       res["returncode"],
                "success":          res["returncode"] == 0,
                "stdout":           res["stdout"],
                "stderr":           res["stderr"],
                "runtime_seconds":  res.get("runtime_seconds"),
                "resource_usage":   res.get("resource_usage"),
                "detected_outputs": res.get("detected_outputs", []),
            }

        if isinstance(command, str):
            raise TypeError("apply(in_env=False) expects a command list[str]")
        res = self._run(command, timeout=timeout if timeout is not None else 300)
        return {
            "command":    command,
            "returncode": res["returncode"],
            "success":    res["returncode"] == 0,
            "stdout":     res["stdout"],
            "stderr":     res["stderr"],
        }

    def _package_in_registry(self, env_name: str, package_name: str) -> bool:
        """Runtime-controlled presence anchor: is `package_name` actually
        recorded in the env's conda / pip / R registry? Used by verify() to
        block the library-only echo cheat. The agent's check_command cannot
        influence this — it queries the registries directly with the package
        name. Delegates to the named conda/pip/R strategies in
        agent.skills.evidence (single source of truth for presence probes).
        """
        return evidence.registry_anchor(self, env_name, package_name)["anchored"]

    def verify(self, env_name: str, package_name: str, check_command: str) -> dict[str, Any]:
        """Run check_command in env to confirm the package works.

        Hardened to block trivial cheats like `echo "samtools 1.21"` that
        produce a plausible verify_output without ever invoking the tool.
        Two anchors layered together:

          1. The check_command must contain the package name as a word-
             boundary token (case-insensitive). Bare `echo "v1.21"` fails.
          2. A second source of truth — `which <package_name>` in the env
             OR a conda/pip/R record — is captured separately as
             `installed_at` and surfaced in the spec. If both `which`
             fails AND no install record exists, verification fails even
             if check_command happened to return zero. This kills the
             `echo "samtools 1.21"` cheat: even if the agent picks a
             command that mentions the name, the binary still has to be
             present in the env.

        Returns: {success, package_name, check_command, output, returncode,
                  installed_at, name_token_present}.
        """
        import re

        # Conda packages frequently use a language prefix (r-locfit,
        # bioconductor-deseq2, python-foo, perl-bar) where the natural verify
        # invokes the unprefixed library name: `library(locfit)`,
        # `library(DESeq2)`, `import foo`. Accept either the full conda name OR
        # the unprefixed suffix as the cheat-block token, so the agent doesn't
        # have to use awkward `conda list` probes for natively-loadable libs.
        # The token still has to appear — `echo "fake"` is still rejected.
        candidate_tokens: list[str] = [package_name]
        for prefix in ("r-", "bioconductor-", "python-", "perl-"):
            if package_name.lower().startswith(prefix):
                suffix = package_name[len(prefix):]
                if suffix:
                    candidate_tokens.append(suffix)

        token_present = any(
            re.search(rf"\b{re.escape(t)}\b", check_command, flags=re.IGNORECASE)
            for t in candidate_tokens
        )

        result = self.run_in_env(env_name, check_command, timeout=30)
        output = (result.get("stdout", "") + result.get("stderr", "")).strip()
        rc      = result.get("returncode", 1)
        cmd_ok  = rc == 0

        # Independent presence anchor #1: `which <name>` in the env. Catches CLI
        # tools (samtools, picard wrapper, …).
        which_ev = evidence.cli_which(self, env_name, package_name)
        installed_at = which_ev["detail"] if which_ev["anchored"] else None

        # Independent presence anchor #2: the env's package REGISTRY (conda
        # list / pip show / R library). This is runtime-controlled — the
        # agent's check_command cannot influence it — so it closes the
        # library-only cheat where a check like `python -c "print('numpy 2.4')"`
        # satisfies the token gate without anything being installed. Library
        # packages (numpy, scipy, python-louvain→community) have no CLI binary,
        # so the `which` anchor is null for them; the registry probe is theirs.
        registry_present = False
        if not installed_at:
            registry_present = self._package_in_registry(env_name, package_name)
        present_anchor = bool(installed_at) or registry_present

        # Hard gate: check_command must (a) exit 0, (b) reference the package
        # (or its natural library name) as a word-boundary token, AND (c) the
        # package must actually be present in the env per an anchor the agent
        # can't fake. R / pip / JAR / conda all naturally satisfy (b); (c)
        # blocks `echo "fake 1.21"` even when the name appears in the string.
        success = bool(cmd_ok and token_present and present_anchor)

        rejection_reason = None
        if cmd_ok and not token_present:
            rejection_reason = (
                f"check_command does not invoke '{package_name}' (or its natural library "
                f"name without conda prefix) as a recognizable token. Tried: "
                f"{candidate_tokens}. Use a real probe: '{package_name} --version' for CLIs, "
                f"'Rscript -e \"library(X)\"' for R packages, 'python -c \"import X\"' for "
                f"pip, or 'conda list -p \"$CONDA_PREFIX\" {package_name}' for any conda "
                f"package whose binary/import name doesn't match the conda name (e.g. r-base). "
                f"Echo / printf cheats are rejected."
            )
        elif cmd_ok and token_present and not present_anchor:
            rejection_reason = (
                f"check_command exited 0 and names '{package_name}', but the package is "
                f"not present in the env: `which {package_name}` found nothing AND it is "
                f"not in the conda/pip registry. This is the library-only echo cheat — a "
                f"command that prints a plausible version string without anything installed. "
                f"Install the package, or for a source/git tool use install_git_repo "
                f"(which records its own provenance) instead of verify_installation."
            )

        return {
            "success":            success,
            "package_name":       package_name,
            "check_command":      check_command,
            "output":             output[:500],
            "returncode":         rc,
            "installed_at":       installed_at,
            "registry_present":   registry_present,
            "name_token_present": token_present,
            "rejection_reason":   rejection_reason,
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
        #
        # PATH prefix: macOS Command Line Tools install Python at
        # /Library/Developer/CommandLineTools/.../Python3.framework/Versions/3.9/bin
        # and that path ends up ahead of $CONDA_PREFIX/bin in subshell PATH on
        # some setups — `subprocess.run(["python", "--version"])` inside the
        # conda env binary then picks up Python 3.9 instead of the env's 3.10+.
        # Forcing $CONDA_PREFIX/bin to the front eliminates this whole class.
        wrapped_command = (
            'export PATH="$CONDA_PREFIX/bin:$PATH"; '
            f'set -o pipefail; {command}'
        )
        cmd = [self._conda_exe, "run", "--prefix", str(env_path), "--no-capture-output",
               "/bin/bash", "-c", wrapped_command]

        result = self._run_monitored(
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
            "runtime_seconds":  result["resource_usage"]["wall_seconds"],
            "resource_usage":   result["resource_usage"],
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

    def install_git_repo(
        self,
        env_name: str,
        repo_url: str,
        tool_name: str,
        ref: str = "",
        build_command: str = "",
        verify_command: str = "",
    ) -> dict[str, Any]:
        """Vendor a git repository as a source-installed tool, end-to-end.

        For the very common academic pattern that doesn't ship as a
        conda/pip/jar package — a repo of scripts you clone and run
        (`python run_thing.py …`). Mirrors install_jar_tool's shape:

            {env}/share/{tool_name}/   ← the clone

        Steps:
          1. clone repo_url into {env}/share/{tool_name} (re-clone if present)
          2. checkout `ref` (branch / tag / commit) if given
          3. resolve the commit SHA via `git rev-parse HEAD` — the immutable
             content anchor recorded in the spec (git's own integrity)
          4. optionally run `build_command` inside the env at the clone dir
             (e.g. `pip install -e .`, `make`)
          5. optionally run `verify_command` (inside the env, cwd=clone dir) as
             a smoke test; otherwise the rev-parse output is the verify anchor

        Returns: {success, tool_name, clone_path, commit_sha, repo_url, ref,
                  verify_command, verify_output, log}.
        """
        env_path = self.envs_dir / env_name
        if not env_path.exists():
            return {"success": False, "error": f"env not found: {env_path}"}

        share_dir = env_path / "share" / tool_name
        log: list[str] = []

        # Fresh clone — remove any prior copy so re-runs are deterministic.
        if share_dir.exists():
            shutil.rmtree(share_dir, ignore_errors=True)
            log.append(f"removed existing {share_dir}")
        share_dir.parent.mkdir(parents=True, exist_ok=True)

        clone = self.run_in_env(
            env_name, f"git clone {shlex.quote(repo_url)} {shlex.quote(str(share_dir))}",
            timeout=1800,
        )
        log.append(f"git clone rc={clone['returncode']}")
        if clone["returncode"] != 0:
            return {"success": False, "error": "git clone failed",
                    "stderr": (clone.get("stderr") or "")[-500:], "log": log}

        if ref:
            co = self.run_in_env(
                env_name,
                f"git -C {shlex.quote(str(share_dir))} checkout {shlex.quote(ref)}",
                timeout=120,
            )
            log.append(f"git checkout {ref} rc={co['returncode']}")
            if co["returncode"] != 0:
                return {"success": False, "error": f"git checkout {ref} failed",
                        "stderr": (co.get("stderr") or "")[-500:], "log": log}

        rev = self.run_in_env(
            env_name, f"git -C {shlex.quote(str(share_dir))} rev-parse HEAD", timeout=30
        )
        commit_sha = (rev.get("stdout") or "").strip()
        if rev["returncode"] != 0 or not commit_sha:
            return {"success": False, "error": "could not resolve commit SHA after clone",
                    "stderr": (rev.get("stderr") or "")[-500:], "log": log}
        log.append(f"commit_sha={commit_sha}")

        if build_command:
            build = self.run_in_env(
                env_name, build_command, working_dir=str(share_dir), timeout=1800
            )
            log.append(f"build_command rc={build['returncode']}")
            if build["returncode"] != 0:
                return {"success": False, "error": "build_command failed",
                        "stderr": (build.get("stderr") or "")[-500:],
                        "commit_sha": commit_sha, "clone_path": str(share_dir), "log": log}

        # Verify anchor. Default: the rev-parse we already ran proves the exact
        # pinned commit is on disk. A caller-supplied verify_command (e.g. a
        # python import smoke or `--help`) runs in the env at the clone dir.
        if verify_command:
            vr = self.run_in_env(
                env_name, verify_command, working_dir=str(share_dir), timeout=120
            )
            verify_output = ((vr.get("stdout") or "") + (vr.get("stderr") or "")).strip()[:500]
            verify_ok = vr["returncode"] == 0
            log.append(f"verify_command rc={vr['returncode']}")
        else:
            verify_command = f"git -C {share_dir} rev-parse HEAD"
            verify_output  = commit_sha
            verify_ok      = True

        return {
            "success":        verify_ok,
            "tool_name":      tool_name,
            "clone_path":     str(share_dir),
            "commit_sha":     commit_sha,
            "repo_url":       repo_url,
            "ref":            ref or "HEAD",
            "verify_command": verify_command,
            "verify_output":  verify_output,
            "log":            log,
        }

    # conda is suffix-agnostic about archives; we recognize the common release
    # tarball/zip shapes so a single asset URL "just works".
    _ARCHIVE_SUFFIXES = (
        ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar", ".zip",
    )

    @staticmethod
    def _is_archive(name: str) -> bool:
        n = name.lower()
        return any(n.endswith(s) for s in EnvManager._ARCHIVE_SUFFIXES)

    @staticmethod
    def _sha256_file(path) -> str:
        """sha256 of a file, streamed (handles multi-GB binaries)."""
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _locate_binary(root: Path, tool_name: str, binary_in_archive: str) -> Path | None:
        """Find the executable inside an extracted release archive.

        Explicit `binary_in_archive` wins (exact relative path, then basename
        match). Otherwise heuristics ordered by confidence: exact name match →
        extensionless name containing the tool → any extensionless file under a
        bin/ dir. Returns None if nothing plausible — the caller then asks for
        an explicit binary_in_archive rather than guessing wrong.
        """
        if binary_in_archive:
            cand = root / binary_in_archive
            if cand.is_file():
                return cand
            base = Path(binary_in_archive).name
            matches = [p for p in root.rglob("*") if p.is_file() and p.name == base]
            return matches[0] if matches else None
        files = [p for p in root.rglob("*") if p.is_file()]
        nm = tool_name.lower()
        exact = [p for p in files if p.name.lower() == nm]
        if exact:
            return min(exact, key=lambda p: len(p.parts))
        contains = [p for p in files if nm in p.name.lower() and not p.suffix]
        if contains:
            return min(contains, key=lambda p: len(p.name))
        binu = [p for p in files if p.parent.name == "bin" and not p.suffix]
        if binu:
            return min(binu, key=lambda p: len(p.name))
        return None

    def _stage_release_binary(
        self, share_dir: Path, bin_dir: Path, downloaded: Path,
        tool_name: str, binary_in_archive: str, wrapper_name: str, log: list,
    ) -> dict[str, Any]:
        """Post-download staging for install_release_binary: extract if the
        asset is an archive, locate + chmod the executable, write a PATH
        launcher. Factored out (and using stdlib tar/zip, not the shell) so it
        is unit-testable without a network download."""
        import stat as _stat
        import tarfile
        import zipfile

        bin_dir.mkdir(parents=True, exist_ok=True)
        if self._is_archive(downloaded.name):
            try:
                if downloaded.name.lower().endswith(".zip"):
                    with zipfile.ZipFile(downloaded) as zf:
                        zf.extractall(share_dir)
                else:
                    with tarfile.open(downloaded) as tf:
                        tf.extractall(share_dir)
            except Exception as e:
                return {"success": False, "error": f"archive extraction failed: {e}"}
            log.append(f"extracted {downloaded.name}")
            binary_path = self._locate_binary(share_dir, tool_name, binary_in_archive)
            if not binary_path:
                return {"success": False,
                        "error": f"could not locate an executable for '{tool_name}' in the "
                                 f"archive — pass binary_in_archive=<path inside archive>",
                        "extracted_to": str(share_dir)}
        else:
            binary_path = downloaded

        binary_path.chmod(
            binary_path.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH
        )
        wrapper = bin_dir / (wrapper_name or tool_name)
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            f"# Wrapper for {tool_name} (auto-generated by bioinf_agent.install_release_binary)\n"
            f'exec "{binary_path}" "$@"\n'
        )
        wrapper.chmod(0o755)
        log.append(f"wrapper written: {wrapper}")
        return {"success": True, "binary_path": str(binary_path), "wrapper_path": str(wrapper)}

    def install_release_binary(
        self,
        env_name: str,
        tool_name: str,
        url: str,
        sha256: str = "",
        binary_in_archive: str = "",
        wrapper_name: str = "",
    ) -> dict[str, Any]:
        """Install a precompiled release binary — the Tier-3 resolver path for
        tools that ship a static binary on GitHub releases / a vendor URL
        (mosdepth, somalier, slivar, sylph, dorado, cellranger). No conda, no
        build: download → sha256-anchor → (extract) → chmod → PATH launcher.

            asset      → {env}/share/{tool_name}/
            launcher   → {env}/bin/{wrapper_name or tool_name}

        sha256: if given, the downloaded asset MUST hash to it — a mismatch is a
        HARD FAIL, so a tampered or wrong-arch asset never gets installed. If
        omitted, the computed sha256 is recorded so I14 can re-anchor it at
        finalize. Either way the spec carries an immutable content anchor.

        binary_in_archive: for .tar.gz/.zip assets, the executable's path inside
        the archive (basename also accepted). Omit for single-binary downloads.

        Returns: {success, tool_name, binary_path, wrapper_path, url, sha256,
                  install_method, log}.
        """
        env_path = self.envs_dir / env_name
        if not env_path.exists():
            return {"success": False, "error": f"env not found: {env_path}"}

        from urllib.parse import urlparse
        share_dir = env_path / "share" / tool_name
        bin_dir   = env_path / "bin"
        share_dir.mkdir(parents=True, exist_ok=True)
        log: list[str] = []

        asset_name = Path(urlparse(url).path).name or f"{tool_name}.download"
        download_target = share_dir / asset_name

        # --fail so an HTML 404 isn't silently saved as the "binary".
        curl = self.run_in_env(
            env_name,
            f"curl -L --fail --progress-bar -o {shlex.quote(str(download_target))} {shlex.quote(url)}",
            timeout=3600,
        )
        log.append(f"curl rc={curl['returncode']}")
        if curl["returncode"] != 0 or not download_target.exists():
            return {"success": False, "error": "binary download failed",
                    "stderr": (curl.get("stderr") or "")[-500:], "log": log}

        digest = self._sha256_file(download_target)
        log.append(f"sha256={digest}")
        if sha256 and digest.lower() != sha256.lower():
            return {"success": False,
                    "error": "sha256 mismatch — refusing to install a non-matching asset",
                    "expected_sha256": sha256.lower(), "actual_sha256": digest, "log": log}
        recorded_sha = sha256.lower() if sha256 else digest

        staged = self._stage_release_binary(
            share_dir, bin_dir, download_target, tool_name,
            binary_in_archive, wrapper_name, log,
        )
        if not staged.get("success"):
            return {**staged, "log": log}

        return {
            "success":      True,
            "tool_name":    tool_name,
            "binary_path":  staged["binary_path"],
            "wrapper_path": staged["wrapper_path"],
            "url":          url,
            "sha256":       recorded_sha,
            "install_method": {
                "type":       "binary",
                "binary_url": url,
                "sha256":     recorded_sha,
                "local_path": staged["binary_path"],
            },
            "log": log,
        }

    def install_perl_package(
        self, env_name: str, module: str, distribution: str = "", cpanm_flags: str = "",
    ) -> dict[str, Any]:
        """Install a Perl/CPAN module via cpanm into the env's perl site lib —
        the Perl tier (Ensembl VEP, BioPerl) that conda/pip don't cover.
        Requires perl + cpanm in the env (conda: perl perl-app-cpanminus).

        `module` is the Perl package name (Bio::DB::HTS) used for the
        load-or-die verify; `distribution` overrides the cpanm install target
        when the CPAN distribution name differs from the module name.
        """
        env_path = self.envs_dir / env_name
        if not env_path.exists():
            return {"success": False, "error": f"env not found: {env_path}"}
        target = distribution or module
        flags  = cpanm_flags or "--notest"
        log: list[str] = []
        inst = self.run_in_env(env_name, f"cpanm {flags} {shlex.quote(target)}", timeout=1800)
        log.append(f"cpanm rc={inst['returncode']}")
        if inst["returncode"] != 0:
            return {"success": False, "error": "cpanm install failed",
                    "stderr": (inst.get("stderr") or "")[-800:], "log": log}
        ev = evidence.perl_module_load(self, env_name, module)
        return {
            "success":        ev["anchored"],
            "module":         module,
            "distribution":   target,
            "verify_command": f"perl -M{module} -e1",
            "verify_output":  f"{module} loaded (perl -M{module} -e1 rc=0)" if ev["anchored"] else "",
            "install_method": {"type": "perl", "source": f"cpanm {target}"},
            "log":            log,
        }

    def install_cargo_tool(
        self, env_name: str, crate: str, version: str = "",
        binary_name: str = "", git_url: str = "",
    ) -> dict[str, Any]:
        """Install a Rust crate's binary via `cargo install --root {env}` so it
        lands on the env PATH — the cargo tier for Rust tools not on bioconda.
        Requires rust+cargo in the env (conda: rust). `binary_name` (defaults to
        crate) is what the cli_which anchor checks; `git_url` installs from a git
        repo instead of crates.io. A locally-built binary can't be wrong-arch, so
        presence (cli_which) is the honest anchor here."""
        env_path = self.envs_dir / env_name
        if not env_path.exists():
            return {"success": False, "error": f"env not found: {env_path}"}
        bin_name = binary_name or crate
        if git_url:
            src = f"--git {shlex.quote(git_url)}"
        else:
            src = shlex.quote(crate) + (f" --version {shlex.quote(version)}" if version else "")
        log: list[str] = []
        inst = self.run_in_env(
            env_name, f"cargo install {src} --root {shlex.quote(str(env_path))}", timeout=3600,
        )
        log.append(f"cargo install rc={inst['returncode']}")
        if inst["returncode"] != 0:
            return {"success": False, "error": "cargo install failed",
                    "stderr": (inst.get("stderr") or "")[-800:], "log": log}
        ev = evidence.cli_which(self, env_name, bin_name)
        return {
            "success":        ev["anchored"],
            "crate":          crate,
            "binary_name":    bin_name,
            "verify_command": f"which {bin_name}",
            "verify_output":  ev["detail"] or "",
            "install_method": {"type": "cargo",
                               "source": git_url or f"crates.io:{crate}" + (f"@{version}" if version else "")},
            "log":            log,
        }

    def install_go_tool(
        self, env_name: str, package: str, version: str = "latest", binary_name: str = "",
    ) -> dict[str, Any]:
        """Install a Go tool via `go install pkg@version` into {env}/bin — the go
        tier for Go tools not on bioconda. Requires go in the env (conda: go).
        Sets GOBIN={env}/bin so the binary lands on the env PATH; presence
        (cli_which) is the anchor (a locally-built binary can't be wrong-arch)."""
        env_path = self.envs_dir / env_name
        if not env_path.exists():
            return {"success": False, "error": f"env not found: {env_path}"}
        bin_name = binary_name or package.rstrip("/").split("/")[-1]
        spec = f"{package}@{version}" if version else package
        log: list[str] = []
        inst = self.run_in_env(
            env_name,
            f"GOBIN={shlex.quote(str(env_path / 'bin'))} go install {shlex.quote(spec)}",
            timeout=3600,
        )
        log.append(f"go install rc={inst['returncode']}")
        if inst["returncode"] != 0:
            return {"success": False, "error": "go install failed",
                    "stderr": (inst.get("stderr") or "")[-800:], "log": log}
        ev = evidence.cli_which(self, env_name, bin_name)
        return {
            "success":        ev["anchored"],
            "package":        package,
            "binary_name":    bin_name,
            "verify_command": f"which {bin_name}",
            "verify_output":  ev["detail"] or "",
            "install_method": {"type": "go", "source": f"{package}@{version}"},
            "log":            log,
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

    def list_pip_packages(self, env_name: str) -> dict[str, str]:
        """Return {package_name: version} for pip-installed packages in the env.

        Source of truth for resolved_version when an install_step did `pip install X`
        without pinning a version — pip's catalog is authoritative. Names are
        returned in their canonical pip form (open-cravat, not opencravat).
        Falls back to empty dict on any failure.
        """
        env_path = self.envs_dir / env_name
        pip_bin = env_path / "bin" / "pip"
        if not pip_bin.exists():
            return {}
        try:
            result = self._run(
                [str(pip_bin), "list", "--format=json"],
                cwd=str(self.project_root),
                timeout=60,
            )
        except Exception:
            return {}
        if result.get("returncode") != 0:
            return {}
        import json
        try:
            entries = json.loads(result["stdout"])
        except Exception:
            return {}
        return {e.get("name", ""): e.get("version", "") for e in entries if e.get("name")}

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

        Post-processes the channels list so the agent's standard base channels
        (bioconda, conda-forge, defaults) are always present — `conda env export`
        only lists channels that appeared explicitly in `conda install -c`
        invocations, which excludes bioconda when packages came in transitively
        or were specified without -c. Result is portable: anyone can
        `conda env create -f` it on any platform without needing to know to
        add bioconda manually.

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
        if result["returncode"] != 0:
            return ""

        # Merge in the agent's standard base channels so any importer of this
        # YAML has the same channel priority as we did at install time.
        try:
            import yaml as _yaml
            data = _yaml.safe_load(result["stdout"]) or {}
            base_channels = self.config.get("conda", {}).get("base_channels") or []
            channels = list(data.get("channels") or [])
            for ch in base_channels:
                if ch not in channels:
                    channels.append(ch)
            if channels:
                data["channels"] = channels
            return _yaml.dump(data, default_flow_style=False, sort_keys=False)
        except Exception:
            # If post-processing fails for any reason, fall back to the raw conda output.
            return result["stdout"]

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

        # start_new_session=True puts `conda run` (and the backgrounded service
        # it spawns) into a NEW session/process group, distinct from this
        # server's. Without it, the service shares the server's process group —
        # and the timeout-cleanup killpg below would then signal the server
        # itself (observed: a service that never becomes healthy took the whole
        # MCP server down). Detaching is also just correct for a daemon.
        launch = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            cwd=working_dir or str(self.project_root), env=extra_env,
            start_new_session=True,
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

        # Health check timed out. The service is probably still alive in the
        # background — clean it up so the caller doesn't get a silent leak.
        # SIGTERM the process group first, then SIGKILL after a short grace
        # window. PID file is removed.
        cleanup_log = self._kill_service_pid(pid)
        if pid_file.exists():
            pid_file.unlink()

        return {
            "success": False, "service_name": service_name, "pid": pid,
            "error": f"Service did not become healthy within {health_check_timeout_seconds}s",
            "log": str(log_file),
            "cleanup": cleanup_log,
        }

    @staticmethod
    def _kill_service_pid(pid: str) -> list[str]:
        """Terminate a service process (SIGTERM → grace → SIGKILL).

        Prefers signalling the whole process group so child processes die too,
        but NEVER signals this server's own process group — if the service was
        somehow launched into our group (detachment failed), fall back to
        signalling the single PID. A regression here previously killed the
        server itself; this guard makes that impossible.
        """
        import signal as _signal

        cleanup_log: list[str] = []
        if not pid:
            return cleanup_log
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            return [f"unparseable pid {pid!r}"]

        own_pgrp = os.getpgrp()

        def _signal_target(sig: int, label: str) -> bool:
            """Return True if the process is now gone."""
            try:
                pgid = os.getpgid(pid_int)
            except ProcessLookupError:
                return True
            if pgid == own_pgrp:
                # SAFETY: never signal our own group. Single-pid only.
                try:
                    os.kill(pid_int, sig)
                    cleanup_log.append(f"{label} sent to pid {pid_int} (own-group guard: not killpg)")
                except ProcessLookupError:
                    return True
            else:
                try:
                    os.killpg(pgid, sig)
                    cleanup_log.append(f"{label} sent to pgid {pgid}")
                except ProcessLookupError:
                    return True
            return False

        if _signal_target(_signal.SIGTERM, "SIGTERM"):
            cleanup_log.append("process already gone before SIGTERM")
            return cleanup_log

        # Grace window
        gone = False
        for _ in range(25):
            try:
                os.kill(pid_int, 0)
            except ProcessLookupError:
                gone = True
                break
            time.sleep(0.2)

        if not gone:
            if not _signal_target(_signal.SIGKILL, "SIGKILL"):
                cleanup_log.append("SIGKILL escalation needed")
        return cleanup_log

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

    @staticmethod
    def cleanup_orphan_service_pids() -> dict[str, Any]:
        """Scan /tmp/bioinf_services/ for stale PID files (the process no
        longer exists) and remove them. Called at MCP server startup to keep
        the service registry clean across agent restarts.

        Does NOT kill living processes — only reaps the file-system droppings
        of services whose owning process has already exited (crash, kill,
        previous agent session).
        """
        pid_dir = Path("/tmp/bioinf_services")
        if not pid_dir.exists():
            return {"checked": 0, "removed": []}
        removed: list[str] = []
        checked = 0
        for pid_file in pid_dir.glob("*.pid"):
            checked += 1
            try:
                pid = int(pid_file.read_text().strip())
            except (ValueError, OSError):
                pid_file.unlink(missing_ok=True)
                removed.append(pid_file.name)
                continue
            try:
                os.kill(pid, 0)  # signal 0 = existence probe; doesn't actually signal
            except ProcessLookupError:
                pid_file.unlink(missing_ok=True)
                # Also drop the .log sibling if present (informational only)
                log_file = pid_file.with_suffix(".log")
                if log_file.exists():
                    log_file.unlink(missing_ok=True)
                removed.append(pid_file.name)
            except PermissionError:
                # Process exists but not ours — leave it alone
                pass
        return {"checked": checked, "removed": removed}

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
        env: dict | None = None,
    ) -> dict:
        run_env = env if env is not None else os.environ.copy()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd or str(self.project_root),
                env=run_env,
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

    def _run_monitored(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout: int = 300,
    ) -> dict:
        """Run a subprocess while polling its full process tree with psutil.

        Returns the standard {returncode, stdout, stderr} plus a resource_usage
        dict: {wall_seconds, peak_rss_mb, max_cpu_percent, sample_count}. The
        peak is the max-over-time of (sum across the Popen + all descendants),
        so a tool that spawns child workers gets its full footprint recorded
        rather than just the wrapper's. Polls every 0.3s.

        Honesty contract: this is the SOLE path by which pipeline_step
        resource_usage gets populated. Invariant I7 refuses to finalize if a
        rc=0 step has no resource_usage, so an agent cannot synthesize one
        without going through this monitor.
        """
        import threading
        try:
            import psutil
        except ImportError:
            psutil = None   # graceful: monitoring just records wall time

        env = os.environ.copy()
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd or str(self.project_root),
                env=env,
            )
        except Exception as e:
            return {
                "returncode": -1, "stdout": "", "stderr": str(e),
                "resource_usage": {
                    "wall_seconds": 0.0, "peak_rss_mb": 0.0,
                    "max_cpu_percent": 0.0, "sample_count": 0,
                },
            }

        peak_rss_bytes = 0
        max_cpu = 0.0
        sample_count = 0
        stop_event = threading.Event()

        def monitor():
            nonlocal peak_rss_bytes, max_cpu, sample_count
            if psutil is None:
                return
            try:
                root = psutil.Process(proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return
            # Prime cpu_percent — first call always returns 0; subsequent calls
            # measure delta since the prior call. Prime once per process we see.
            primed: set[int] = set()
            while not stop_event.is_set():
                try:
                    procs = [root] + root.children(recursive=True)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    break
                rss_total = 0
                cpu_total = 0.0
                for p in procs:
                    try:
                        if p.pid not in primed:
                            p.cpu_percent(interval=None)
                            primed.add(p.pid)
                        rss_total += p.memory_info().rss
                        cpu_total += p.cpu_percent(interval=None)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                if rss_total > peak_rss_bytes:
                    peak_rss_bytes = rss_total
                if cpu_total > max_cpu:
                    max_cpu = cpu_total
                sample_count += 1
                if stop_event.wait(0.3):
                    break

        t = threading.Thread(target=monitor, daemon=True)
        t.start()
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            stderr = (stderr or "") + f"\nCommand timed out after {timeout}s: {' '.join(cmd)}"
            rc = -1
        except Exception as e:
            stop_event.set()
            return {
                "returncode": -1, "stdout": "", "stderr": str(e),
                "resource_usage": {
                    "wall_seconds": round(time.monotonic() - t0, 2),
                    "peak_rss_mb": 0.0, "max_cpu_percent": 0.0, "sample_count": 0,
                },
            }
        finally:
            stop_event.set()
            t.join(timeout=2)

        wall = round(time.monotonic() - t0, 2)
        return {
            "returncode": rc,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "resource_usage": {
                "wall_seconds":    wall,
                "peak_rss_mb":     round(peak_rss_bytes / (1024 * 1024), 1),
                "max_cpu_percent": round(max_cpu, 1),
                "sample_count":    sample_count,
            },
        }
