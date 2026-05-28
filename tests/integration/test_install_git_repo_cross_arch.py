"""
`install_git_repo(host_build=False)` — the CROSS-ARCH escape.

The host can't always build the target (e.g. x86 SIMD intrinsics under Apple
Silicon clang; an osx-arm64 dev driving a `freeze(platform=linux/amd64)`).
Pre-fix the only option was to hand-edit the draft yaml to set bin_path +
returncode=0; that's the "host-build is mandatory" trap.

The fix adds `host_build=False`: clone + checkout + rev-parse STILL run on the
host (cheap, gives the immutable commit_sha anchor — the source-replay needs
it). The build, bin_path existence check, wrapper write, AND verify_command
are DEFERRED to freeze's in-container source-replay. The install_method still
carries `build_command + bin_path` (or `entrypoint`) so `_map_install` routes
to the source generator unchanged.

Why integration, not unit:
- The branch logic AND its disk side effects need to be exercised together —
  the bug shape is "we wrote a wrapper that points at a non-existent binary"
  OR "we tried to compile on the wrong arch and the install_step said rc=0
  anyway", and both only surface when the real filesystem participates.
- `_map_install` is the dependent — a host_build=False install_method must
  still produce a clean `ic.source(...)` spec downstream; we re-check that
  here so the contract is end-to-end.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent.skills import env_freeze
from agent.skills.env_manager import EnvManager


# A tiny bare git repo backed by tmp_path — hermetic (no network), real on disk.
def _make_repo(tmp_path: Path) -> tuple[str, str]:
    """Create a bare upstream + a working clone, commit a Makefile + a fake
    'src' marker file, push back. Returns (repo_url, head_sha)."""
    bare = tmp_path / "upstream.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                   capture_output=True)
    subprocess.run(["git", "clone", str(bare), str(work)], check=True,
                   capture_output=True)
    # Identity for the test commit.
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    # The Makefile we'd build IF we were host_build=True (we're not).
    (work / "Makefile").write_text(
        "all:\n\techo '#!/bin/sh' > mytool && echo 'echo ok' >> mytool && chmod +x mytool\n")
    # A file that proves the clone+checkout actually landed (the entrypoint
    # case's free integrity check, even when host_build=False).
    (work / "entry.py").write_text("print('hi')\n")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "init"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(work), "push", "origin", "HEAD:main"],
                   check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    return str(bare), head


@pytest.fixture
def _envmgr(tmp_path: Path, monkeypatch):
    """A real EnvManager pointed at tmp_path + a fake env on disk + a
    run_in_env that runs the command on the host (no conda wrapping). The
    install_git_repo logic — host_build branching, clone/checkout/rev-parse,
    wrapper-write decisions — is what we test; the conda env wrapping is
    orthogonal to this fix."""
    envs_dir = tmp_path / "envs"
    envs_dir.mkdir()
    (envs_dir / "fake").mkdir()       # so env_path.exists() passes

    cfg = {"paths": {"conda_envs_prefix": str(envs_dir)},
           "conda": {"python_version": "3.11"}}
    # Bypass _detect_conda (it requires conda on PATH; we don't need it for
    # the host_build=False path which never solves).
    monkeypatch.setattr(EnvManager, "_detect_conda", staticmethod(lambda: "conda"))
    em = EnvManager(cfg)
    em.envs_dir = envs_dir            # force the fixture path (constructor
                                      # resolves vs project_root by default)

    def _fake_run_in_env(self, env_name, command, working_dir=None,
                         timeout=120, inputs=None, watch_dir=None):
        # Just shell out — the real install_git_repo only needs git operations.
        res = subprocess.run(
            command, shell=True, cwd=working_dir or str(tmp_path),
            capture_output=True, text=True, timeout=timeout,
        )
        return {"returncode": res.returncode, "stdout": res.stdout,
                "stderr": res.stderr, "command": command, "env_name": env_name}

    monkeypatch.setattr(type(em), "run_in_env", _fake_run_in_env)
    return em


@pytest.mark.integration
def test_host_build_false_skips_build_and_wrapper(_envmgr, tmp_path):
    """The cross-arch headline: host_build=False clones + checks out + records
    the commit SHA, but does NOT run the build, NOT write a wrapper, NOT trip
    the bin_path existence check. The result still reports success=True and
    the commit_sha anchor."""
    repo_url, head = _make_repo(tmp_path)

    result = _envmgr.install_git_repo(
        env_name="fake",
        repo_url=repo_url,
        tool_name="mytool",
        ref=head,
        build_command="make",
        bin_path="mytool",          # the binary the build WOULD produce
        host_build=False,
    )
    assert result["success"], f"host_build=False install reported failure: {result}"
    assert result["commit_sha"] == head, "commit_sha anchor must match upstream HEAD"
    assert result["host_build"] is False

    # No wrapper was written on host (the binary doesn't exist on host — the
    # WHOLE POINT of host_build=False; freeze's in-container replay writes it).
    assert result["wrapper_path"] == "", \
        f"host_build=False wrote a host wrapper, but the binary wasn't built: {result['wrapper_path']!r}"
    assert not (_envmgr.envs_dir / "fake" / "bin" / "mytool").exists()

    # And the unbuilt binary on disk is NOT present — we proved the build was
    # genuinely skipped, not silently succeeded.
    assert not (_envmgr.envs_dir / "fake" / "share" / "mytool" / "mytool").exists()


@pytest.mark.integration
def test_host_build_false_requires_bin_path_or_entrypoint(_envmgr, tmp_path):
    """A host_build=False install with neither bin_path NOR entrypoint is
    underspecified — freeze's source-replay can't know what to wrap. The
    install MUST fail loud, not silently install nothing."""
    repo_url, _head = _make_repo(tmp_path)
    result = _envmgr.install_git_repo(
        env_name="fake",
        repo_url=repo_url,
        tool_name="mytool",
        host_build=False,
        # Neither bin_path nor entrypoint — the failure mode the contract guards.
    )
    assert result["success"] is False
    assert "bin_path" in result["error"] and "entrypoint" in result["error"], \
        f"error message did not name both replay shapes: {result['error']!r}"


@pytest.mark.integration
def test_host_build_false_entrypoint_still_validated(_envmgr, tmp_path):
    """The entrypoint shape's integrity check (entry script exists on disk
    post-clone) STILL runs under host_build=False. That's a free check — the
    clone has already happened — and catches a typo'd entrypoint path before
    it reaches freeze's source-replay where the failure mode is far noisier."""
    repo_url, _head = _make_repo(tmp_path)
    # 'entry.py' exists in the fixture repo; happy path.
    ok = _envmgr.install_git_repo(
        env_name="fake", repo_url=repo_url, tool_name="thing",
        entrypoint="entry.py", interpreter="python", host_build=False,
    )
    assert ok["success"], f"valid entrypoint flagged as failure: {ok}"

    # 'nope.py' doesn't exist → clean failure with a pinpoint error.
    bad = _envmgr.install_git_repo(
        env_name="fake", repo_url=repo_url, tool_name="thing2",
        entrypoint="nope.py", interpreter="python", host_build=False,
    )
    assert bad["success"] is False
    assert "entrypoint not found" in bad["error"], \
        f"entrypoint existence check did not fire: {bad['error']!r}"


@pytest.mark.integration
def test_host_build_false_default_is_true_backcompat(_envmgr, tmp_path):
    """Callers that don't pass host_build still get the legacy behavior —
    `host_build=True` runs the build_command on the host. We confirm by giving
    a build_command that LANDS the binary, then asserting the wrapper got
    written. Pre-fix behavior MUST be preserved."""
    repo_url, _head = _make_repo(tmp_path)
    result = _envmgr.install_git_repo(
        env_name="fake", repo_url=repo_url, tool_name="mytool",
        build_command="make", bin_path="mytool",
        # host_build defaults to True
    )
    assert result["success"], f"default-host_build install failed: {result}"
    assert result["host_build"] is True
    assert result["wrapper_path"], \
        "host_build=True did not write a wrapper after a successful build"
    assert (_envmgr.envs_dir / "fake" / "bin" / "mytool").exists()


@pytest.mark.integration
def test_host_build_false_install_method_routes_to_source_replay(_envmgr, tmp_path):
    """End-to-end: a host_build=False install must produce an install_method
    record that freeze's `_map_install` routes cleanly to the source
    generator — because the in-image source-replay is the WHOLE POINT of
    deferring the build. If _map_install errors here, host_build=False would
    install a tool that can't actually be frozen, which is worse than nothing."""
    repo_url, head = _make_repo(tmp_path)
    result = _envmgr.install_git_repo(
        env_name="fake", repo_url=repo_url, tool_name="mytool",
        ref=head, build_command="make", bin_path="mytool", host_build=False,
    )
    assert result["success"]

    # Reconstruct the {name, type, install_method} shape that
    # freeze.non_conda_installs produces for _map_install (its sole consumer).
    install_record: dict[str, Any] = {
        "name": "mytool",
        "type": "source",
        "install_method": {
            "type":          "source",
            "source":        repo_url,
            "commit_sha":    result["commit_sha"],
            "ref":           result["ref"],
            "local_path":    result["clone_path"],
            "build_command": "make",
            "bin_path":      "mytool",
            "entrypoint":    "",
            "interpreter":   "",
        },
    }
    # Stub the network-touching helpers — _map_install for source doesn't
    # call them, but the signature requires them. (defensive: keep the fakes.)
    mapped = env_freeze._map_install(
        install_record, platform="linux/amd64",
        resolve_linux_asset=lambda _u: {"found": False},
        sha256_of_url=lambda _u: {"ok": False},
    )
    assert "spec" in mapped, \
        f"_map_install rejected the host_build=False install_method: {mapped.get('error')!r}"
    spec = mapped["spec"]
    # Source generator emits a wrapper-smoke evidence chain (post-N3 fix).
    assert spec["tool"] == "mytool"
    assert "mytool --help" in spec["evidence"] or "command -v mytool" in spec["evidence"], \
        f"source-replay evidence didn't include a smoke chain: {spec['evidence']!r}"
