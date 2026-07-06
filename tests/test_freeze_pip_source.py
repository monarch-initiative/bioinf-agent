"""Freeze-path regression tests for the pip-installable git-repo class.

Anchored to a REAL run: freezing Talos v11.0.1 (populationgenomics/talos) — a
git-vendored python package built `pip install -e ".[test]"`, importing Hail
(JVM/Spark). Getting one honest install through `freeze` surfaced six general
gaps in the source/script_repo freeze path; each test below pins one fix so it
can't silently regress. None are Talos-specific — they cover any pip-installable
git repo + any cross-arch (QEMU) build of an import-heavy tool.

Fixes covered:
  #2 engine-coupling  — a python-interpreter source build runs inside the conda
                        env (pixi run) so `pip`/`python` are on PATH.
  #3 pip-in-env       — a source build that shells to pip declares `pip` (pixi/uv
                        envs don't ship it).
  #4 python-pin       — the injected python is pinned to the env's validated
                        version (a source's pyproject ceiling is invisible to the
                        conda solver, which would otherwise grab the newest).
  #5 timeout-safety   — a slow/hung in-container step returns rc=124 instead of
                        crashing the freeze with an unhandled TimeoutExpired; and
                        the evidence timeout scales up for emulated (QEMU) builds.
  #6 verify-evidence  — freeze uses the agent's recorded verify_command as the
                        in-image evidence (lighter + more meaningful than a
                        generic `{tool} --help`).
"""
import subprocess

import pytest


# ---- #2 engine-coupling -------------------------------------------------------

def test_script_repo_python_interpreter_is_engine_coupled():
    """A run-by-path repo whose interpreter is a conda-provided runtime (python)
    must be engine_coupled so BOTH its `pip install -e .` build AND its wrapper
    evidence run inside the activated env (pixi run). Pre-fix: `pip: not found`."""
    from agent.skills import install_commands as ic
    sr = ic.script_repo("talos", "https://github.com/populationgenomics/talos",
                        ref="c5a8f07", script_rel="src/talos/x.py",
                        interpreter="python", build_command='pip install -e ".[test]"')
    assert sr["engine_coupled"] is True


@pytest.mark.parametrize("interp", ["python", "python3", "Rscript", "perl"])
def test_script_repo_conda_runtimes_all_couple(interp):
    from agent.skills import install_commands as ic
    sr = ic.script_repo("t", "https://x", ref="a", script_rel="e", interpreter=interp)
    assert sr["engine_coupled"] is True


def test_script_repo_system_interpreter_stays_uncoupled():
    """node/yarn (Apollo3) and make/gcc (C tools) are system toolchains — still on
    PATH inside or outside pixi run — so they must NOT be coupled (blast-radius
    guard: the coupling fix is scoped to conda-provided language runtimes)."""
    from agent.skills import install_commands as ic
    node = ic.script_repo("apollo3", "https://x", ref="a", script_rel="dist/main.js",
                          interpreter="node", build_command="yarn build")
    assert node["engine_coupled"] is False


# ---- #6 verify_command as in-image evidence -----------------------------------

def test_map_install_uses_recorded_verify_command_as_evidence():
    """freeze re-runs the tool's evidence IN the image. For an import-heavy python
    entrypoint the default wrapper-smoke (`talos --help`) drags in the whole
    import chain (cpg_flow+hail) — minutes under QEMU. The agent's recorded
    verify_command (`import talos`) is lighter AND is the check it chose; freeze
    must prefer it."""
    from agent.skills.env_freeze import _map_install
    im = {"type": "source", "source": "https://github.com/populationgenomics/talos",
          "commit_sha": "c5a8f07", "entrypoint": "src/talos/x.py",
          "interpreter": "python", "build_command": 'pip install -e ".[test]"',
          "verify_command": 'python -c "import talos"'}
    spec = _map_install({"name": "talos", "type": "source", "install_method": im})["spec"]
    assert spec["evidence"] == 'python -c "import talos"'


def test_map_install_falls_back_to_wrapper_smoke_without_verify_command():
    """No recorded verify_command ⇒ keep the wrapper-smoke default (the N3 shape
    that actually INVOKES the wrapper, not bare `command -v`)."""
    from agent.skills.env_freeze import _map_install
    im = {"type": "source", "source": "https://x", "commit_sha": "a",
          "entrypoint": "run.py", "interpreter": "python"}
    spec = _map_install({"name": "mytool", "type": "source", "install_method": im})["spec"]
    assert "mytool --help" in spec["evidence"]


def test_recorded_verify_command_import_shape_passes_the_anti_cheat_rule():
    """The lighter evidence must still satisfy the anti-echo-cheat shape rule:
    reference the tool token via a real invocation (import), not a bare echo."""
    from agent.skills.env_honesty import evidence_shape_violation
    assert evidence_shape_violation('python -c "import talos; print(talos.__file__)"',
                                    "talos") is None


# ---- #3 + #4 pip-in-env and python-pin (via build_env_image) ------------------

def _capture_conda_specs(monkeypatch, spec, conda_deps):
    """Run build_env_image far enough to capture the conda specs it hands to
    EnvBuild.add_conda, then short-circuit — no docker build."""
    import agent.skills.env_freeze as ef
    captured = {}

    class _StopBuild(Exception):
        pass

    def fake_add_conda(self, specs, verify=None):
        captured["specs"] = list(specs)
        raise _StopBuild

    monkeypatch.setattr(ef.EnvBuild, "add_conda", fake_add_conda, raising=True)
    try:
        ef.build_env_image(spec, name="t", conda_deps=conda_deps, platform="linux/amd64")
    except _StopBuild:
        pass
    return captured.get("specs", [])


def _talos_spec(py="3.11"):
    return {
        "python_version": py,
        "install_steps": [{
            "installed_packages": [{
                "name": "talos",
                "install_method": {
                    "type": "source",
                    "source": "https://github.com/populationgenomics/talos",
                    "commit_sha": "c5a8f07",
                    "entrypoint": "src/talos/cpg_internal_scripts/run_workflow.py",
                    "interpreter": "python",
                    "build_command": 'pip install -e ".[test]"',
                    "verify_command": 'python -c "import talos"',
                },
            }],
        }],
    }


def test_source_pip_build_declares_pip_in_conda_env(monkeypatch):
    """#3 — a `pip install -e .` source build needs a real pip in the env; pixi/uv
    envs don't ship it, so freeze must declare it."""
    specs = _capture_conda_specs(monkeypatch, _talos_spec(), conda_deps=["openjdk=11"])
    assert "pip" in specs, f"pip not declared for a pip-building source install: {specs}"


def test_source_pip_build_pins_python_to_validated_version(monkeypatch):
    """#4 — the source's pyproject python ceiling (<3.12) is invisible to the conda
    solver; without a pin it grabs the newest (3.14) and the build dies. Pin to the
    version the env was validated on (validated == shipped)."""
    specs = _capture_conda_specs(monkeypatch, _talos_spec(py="3.11"), conda_deps=["openjdk=11"])
    assert "python=3.11" in specs, f"python not pinned to 3.11: {specs}"
    # exactly one python spec — the pin, not a pin + a bare `python`
    assert sum(1 for s in specs if s.split("=")[0].split()[0] == "python") == 1


# ---- #5 timeout crash-safety + emulation-aware evidence timeout ---------------

def test_container_build_sh_survives_timeout_as_rc124():
    """A hung/slow in-container step must NOT crash the freeze with an unhandled
    TimeoutExpired — _sh returns a structured rc=124 so the honesty check reports
    a clean failure."""
    from agent.skills.container_build import ContainerBuild
    r = ContainerBuild._sh(["sleep", "5"], timeout=1)
    assert r["returncode"] == 124
    assert "timed out" in r["stderr"]


def test_container_build_emulation_detection():
    """Evidence timeout scales up ONLY when the build is cross-arch (QEMU). Detect
    by comparing amd64-ness of host arch vs target platform."""
    import platform as _plat
    from agent.skills.container_build import ContainerBuild
    cb = ContainerBuild.__new__(ContainerBuild)
    host_amd = _plat.machine().lower() in ("x86_64", "amd64")
    cb.platform = "linux/amd64"
    assert cb._emulated() is (not host_amd)
    cb.platform = "linux/arm64"
    assert cb._emulated() is host_amd
