"""Freeze-path regression tests for the CARGO + GO tiers (P2-C tier-breadth slice).

The tier recipes (scripts/freeze_tiers.py) name two engine-coupled tools, each with
FUNCTIONAL evidence (validated==ran) —

  cargo → nanoq 0.10.0 (esteinig/nanoq, a real Nanopore read-QC tool): filters an
          inline fastq and writes output.
  go    → gofasta v1.2.3 (virus-evolution/gofasta, a real SARS-CoV-2 genomics tool):
          `gofasta snps` calls a SNP between two inline aligned fastas. (seqkit was
          the obvious pick but is NOT `go install`-able — its go.mod carries `replace`
          directives, which `go install pkg@v` refuses; the go tier needs a clean go.mod.)

Both close the SAME evidence-threading gap the jar slice closed — before this slice,
the cargo/go branches of `_map_install` were the LAST two non-conda tiers that dropped
a recorded smoke, always falling back to `ic.cargo`/`ic.go`'s `--help||--version`
default, so a cargo/go tool could never carry a functional VALIDATED_IN_IMAGE (unlike
source/jar/synthesized/r_install). They now thread `evidence`/`verify_command`, and the
`install_cargo_tool`/`install_go_tool` producers gained a `verify_command` param so a
REAL user (not just the grid recipe) can record the smoke.
"""
import importlib.util
from pathlib import Path

import pytest

from agent.skills.env_freeze import _map_install
from agent.skills import env_honesty


def _ft():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("freeze_tiers", root / "scripts" / "freeze_tiers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cargo_install(im_extra: dict) -> dict:
    im = {"type": "cargo", "name": "nanoq", "crate": "nanoq", "version": "0.10.0",
          "binary_name": "nanoq"}
    im.update(im_extra)
    return {"name": "nanoq", "type": "cargo", "install_method": im}


def _go_install(im_extra: dict) -> dict:
    im = {"type": "go", "name": "gofasta", "package": "github.com/virus-evolution/gofasta",
          "version": "v1.2.3", "binary_name": "gofasta"}
    im.update(im_extra)
    return {"name": "gofasta", "type": "go", "install_method": im}


# ── #A the evidence-threading wiring (cargo) ──────────────────────────────────

def test_map_install_cargo_threads_recorded_evidence():
    """A cargo tool that recorded a self-contained smoke ships THAT as the in-image
    evidence — the fix that lets a cargo tool prove validated==ran."""
    smoke = "cd /tmp && nanoq -i /tmp/in.fq -o /tmp/out.fq && test -s /tmp/out.fq"
    m = _map_install(_cargo_install({"evidence": smoke}))
    assert m["spec"]["evidence"] == smoke


def test_map_install_cargo_verify_command_is_the_fallback_key():
    smoke = "nanoq -i /tmp/in.fq -o /tmp/out.fq"
    m = _map_install(_cargo_install({"verify_command": smoke}))
    assert m["spec"]["evidence"] == smoke


def test_map_install_cargo_evidence_wins_over_verify_command():
    m = _map_install(_cargo_install({"evidence": "nanoq A", "verify_command": "nanoq B"}))
    assert m["spec"]["evidence"] == "nanoq A"


def test_map_install_cargo_without_a_smoke_keeps_the_generator_default():
    """No recorded smoke → ic.cargo's `--help||--version||-h||command -v` default.
    Threading must not fabricate an evidence; absence stays absence."""
    m = _map_install(_cargo_install({}))
    assert m["spec"]["evidence"] == (
        "nanoq --help >/dev/null 2>&1 || nanoq --version >/dev/null 2>&1 || "
        "nanoq -h >/dev/null 2>&1 || command -v nanoq")


def test_map_install_cargo_still_attaches_provenance():
    """Threading evidence must not disturb the C5 provenance disclosure. A locally
    built cargo binary can't be wrong-arch → the honest anchor is cli_which; the
    replay assurance is filled by _map_install's wrapper (tier=cargo)."""
    m = _map_install(_cargo_install({"evidence": "nanoq X"}))
    prov = m["spec"]["provenance"]
    assert prov["tier"] == "cargo"


# ── #A the evidence-threading wiring (go) ─────────────────────────────────────

def test_map_install_go_threads_recorded_evidence():
    smoke = "cd /tmp && gofasta snps -r /tmp/r.fa -q /tmp/q.fa -o /tmp/out.csv && test -s /tmp/out.csv"
    m = _map_install(_go_install({"evidence": smoke}))
    assert m["spec"]["evidence"] == smoke


def test_map_install_go_verify_command_is_the_fallback_key():
    smoke = "gofasta snps -r /tmp/r.fa -q /tmp/q.fa"
    m = _map_install(_go_install({"verify_command": smoke}))
    assert m["spec"]["evidence"] == smoke


def test_map_install_go_without_a_smoke_keeps_the_generator_default():
    m = _map_install(_go_install({}))
    assert m["spec"]["evidence"] == (
        "gofasta --help >/dev/null 2>&1 || gofasta --version >/dev/null 2>&1 || "
        "gofasta -h >/dev/null 2>&1 || command -v gofasta")


def test_map_install_go_still_attaches_provenance():
    m = _map_install(_go_install({"evidence": "gofasta X"}))
    assert m["spec"]["provenance"]["tier"] == "go"


# ── #B the grid recipes' evidence is genuinely FUNCTIONAL + non-cheat ─────────

@pytest.mark.parametrize("tname,tool", [("cargo", "nanoq"), ("go", "gofasta")])
def test_grid_recipe_evidence_is_functional_depth(tname, tool):
    """Each recipe's evidence must classify FUNCTIONAL (it RUNS the built binary on
    data) — not 'version'/'presence' — so the report honestly renders validated==ran.
    This is the reports-never-lie honesty anchor for the tier."""
    ev = _ft().tier(tname)["build"]["install_method"]["evidence"]
    assert env_honesty.evidence_depth(ev, tool) == "functional"
    assert not env_honesty.is_shallow_evidence(ev, tool)


@pytest.mark.parametrize("tname,tool", [("cargo", "nanoq"), ("go", "gofasta")])
def test_grid_recipe_evidence_passes_the_anti_cheat_shape_rule(tname, tool):
    """It must NOT trip the echo/printf bare-cheat guard (it leads with `cd`, not
    printf) and must reference the tool as a real word-boundary invocation."""
    ev = _ft().tier(tname)["build"]["install_method"]["evidence"]
    assert env_honesty.evidence_shape_violation(ev, tool) is None
    assert env_honesty.evidence_shape_violation(f"printf '{tool} 1.0'", tool) is not None


def test_grid_cargo_recipe_is_a_wired_container_native_probe():
    """The cargo row is now a real probe (builder set + a build recipe, version-
    PINNED), so the recipe stays reproducible; the drift guard asserts the tier set."""
    row = _ft().tier("cargo")
    assert row["builder"] == "container_native"
    assert row["probe_tool"] == "nanoq"
    im = row["build"]["install_method"]
    assert im["type"] == "cargo" and im["crate"] == "nanoq"
    assert im["version"] == "0.10.0"          # PINNED — reproducibility, not `latest`


def test_grid_go_recipe_is_a_wired_container_native_probe():
    row = _ft().tier("go")
    assert row["builder"] == "container_native"
    assert row["probe_tool"] == "gofasta"
    im = row["build"]["install_method"]
    assert im["type"] == "go" and "gofasta" in im["package"]
    assert im["version"] == "v1.2.3"          # PINNED tag
    # the go tier needs a replace-free go.mod — the package must NOT be a shenwei356
    # tool (seqkit/csvtk vendor libs via `replace`, which `go install pkg@v` refuses)
    assert "shenwei356" not in im["package"]


# ── #A the producer side: install_{cargo,go}_tool record the smoke ────────────

def _ps_in_tmp(tmp_path, monkeypatch):
    from agent import mcp_server as _ms
    from agent.skills.pipeline_state import PipelineState
    (tmp_path / "drafts").mkdir(); (tmp_path / "reports").mkdir()
    cfg = {**_ms.config, "paths": {**_ms.config.get("paths", {}),
                                   "drafts_dir": str(tmp_path / "drafts"),
                                   "pipelines_dir": str(tmp_path / "reports")}}
    ps = PipelineState(cfg)
    monkeypatch.setattr(_ms, "_pipeline_state", ps)
    return _ms, ps


def test_install_cargo_tool_records_the_smoke_as_freeze_evidence(tmp_path, monkeypatch):
    """The producer that makes the threading reachable by a REAL user (not only the
    grid recipe): a cargo install with a verify_command writes it into
    install_method["evidence"] so freeze re-runs it in-image as VALIDATED_IN_IMAGE."""
    from agent.mcp_tools import env_tools
    _ms, ps = _ps_in_tmp(tmp_path, monkeypatch)

    class _FakeMgr:
        def install_cargo_tool(self, env_name, crate, version, binary_name, git_url):
            return {"success": True, "crate": crate, "binary_name": binary_name or crate,
                    "verify_command": f"which {binary_name or crate}", "verify_output": "/e/bin/nanoq",
                    "install_method": {"type": "cargo", "source": f"crates.io:{crate}",
                                       "crate": crate, "version": version, "git_url": git_url,
                                       "binary_name": binary_name or crate, "rust_version": "1.79.0"}}
        def run_in_env(self, env_name, command, timeout=300):
            return {"returncode": 0, "stdout": "ran\n", "stderr": ""}
    monkeypatch.setattr(_ms, "_env_mgr", _FakeMgr())

    pid = ps.start("cargo_smoke_test", "x")["pipeline_id"]
    smoke = "cd /tmp && nanoq -i /tmp/in.fq -o /tmp/out.fq && test -s /tmp/out.fq"
    env_tools.install_cargo_tool(env_name="e", crate="nanoq", version="0.10.0",
                                 verify_command=smoke, pipeline_id=pid)

    im = ps.get_draft(pid)["install_steps"][0]["installed_packages"][0]["install_method"]
    assert im["type"] == "cargo"
    assert im["evidence"] == smoke            # → threaded by _map_install at freeze


def test_install_go_tool_records_the_smoke_as_freeze_evidence(tmp_path, monkeypatch):
    from agent.mcp_tools import env_tools
    _ms, ps = _ps_in_tmp(tmp_path, monkeypatch)

    class _FakeMgr:
        def install_go_tool(self, env_name, package, version, binary_name):
            bn = binary_name or package.rstrip("/").split("/")[-1]
            return {"success": True, "package": package, "binary_name": bn,
                    "verify_command": f"which {bn}", "verify_output": "/e/bin/seqkit",
                    "install_method": {"type": "go", "source": f"{package}@{version}",
                                       "package": package, "version": version,
                                       "binary_name": bn, "go_version": "1.22.0"}}
        def run_in_env(self, env_name, command, timeout=300):
            return {"returncode": 0, "stdout": "ran\n", "stderr": ""}
    monkeypatch.setattr(_ms, "_env_mgr", _FakeMgr())

    pid = ps.start("go_smoke_test", "x")["pipeline_id"]
    smoke = "cd /tmp && gofasta snps -r /tmp/r.fa -q /tmp/q.fa -o /tmp/out.csv && test -s /tmp/out.csv"
    env_tools.install_go_tool(env_name="e", package="github.com/virus-evolution/gofasta",
                              version="v1.2.3", verify_command=smoke, pipeline_id=pid)

    im = ps.get_draft(pid)["install_steps"][0]["installed_packages"][0]["install_method"]
    assert im["type"] == "go"
    assert im["evidence"] == smoke


def test_install_cargo_tool_without_a_smoke_records_no_evidence(tmp_path, monkeypatch):
    """No smoke given → no fabricated evidence; the cargo tool's evidence honestly
    falls back to the generator default at freeze."""
    from agent.mcp_tools import env_tools
    _ms, ps = _ps_in_tmp(tmp_path, monkeypatch)

    class _FakeMgr:
        def install_cargo_tool(self, env_name, crate, version, binary_name, git_url):
            return {"success": True, "crate": crate, "binary_name": crate,
                    "install_method": {"type": "cargo", "crate": crate, "version": version,
                                       "binary_name": crate}}
    monkeypatch.setattr(_ms, "_env_mgr", _FakeMgr())

    pid = ps.start("cargo_nosmoke_test", "x")["pipeline_id"]
    env_tools.install_cargo_tool(env_name="e", crate="nanoq", version="0.10.0", pipeline_id=pid)
    im = ps.get_draft(pid)["install_steps"][0]["installed_packages"][0]["install_method"]
    assert "evidence" not in im
