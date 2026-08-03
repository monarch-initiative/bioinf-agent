"""Freeze-path regression tests for the JAR tier (P2-C tier-breadth slice).

The tier recipe (scripts/freeze_tiers.py) names Picard 3.4.0 (Broad) — the canonical
Java bioinformatics tool — with FUNCTIONAL evidence (CreateSequenceDictionary RUNS on
an inline fasta and writes a .dict). These tests pin the WIRING of that recipe; no
real-bytes build result is committed anywhere (the freeze-tier meter that used to
publish one was deleted for counting hand-built records). Two gaps surfaced while the
recipe was authored, and are each pinned here so they can't silently regress:

  #A evidence-threading — before this slice, the jar branch of `_map_install`
     was the ONE non-conda tier that dropped a recorded smoke: it always fell
     back to `ic.jar`'s presence default (`command -v picard && java -version`),
     so a jar's VALIDATED_IN_IMAGE could never be "validated==ran", unlike
     source (verify_command) / synthesized (evidence) / r_install
     (functional_evidence). It now threads `evidence`/`verify_command`.
  #B functional depth — the grid recipe's evidence must actually RUN Picard
     (env_honesty classifies it 'functional', not 'presence') AND pass the
     anti-echo-cheat shape rule, so the artifact the report renders is honest:
     validated==ran, not merely present.
"""
import pytest

from agent.skills.env_freeze import _map_install
from agent.skills import env_honesty


_OK_HASH = lambda _u: {"ok": True, "sha256": "d" * 64}   # no network in a unit test
_JAR_URL = "https://github.com/broadinstitute/picard/releases/download/3.4.0/picard.jar"


def _jar_install(im_extra: dict) -> dict:
    im = {"type": "jar", "name": "picard", "source": _JAR_URL}
    im.update(im_extra)
    return {"name": "picard", "type": "jar", "install_method": im}


# ── #A the evidence-threading wiring ──────────────────────────────────────────

def test_map_install_jar_threads_recorded_evidence():
    """A jar that recorded a self-contained smoke ships THAT as the in-image
    evidence — the fix that lets a jar prove validated==ran."""
    smoke = "cd /tmp && picard CreateSequenceDictionary -R r.fa -O r.dict && test -s r.dict"
    m = _map_install(_jar_install({"evidence": smoke}), sha256_of_url=_OK_HASH)
    assert m["spec"]["evidence"] == smoke


def test_map_install_jar_verify_command_is_the_fallback_key():
    """`verify_command` is honored too (the key install_git_repo-style records use),
    so a jar recorded through either surface threads its smoke."""
    smoke = "picard MarkDuplicates --version"
    m = _map_install(_jar_install({"verify_command": smoke}), sha256_of_url=_OK_HASH)
    assert m["spec"]["evidence"] == smoke


def test_map_install_jar_evidence_wins_over_verify_command():
    """When both are present, the explicit `evidence` key wins (it is the one the
    grid recipe + a functional-smoke capture set)."""
    m = _map_install(_jar_install({"evidence": "picard A", "verify_command": "picard B"}),
                     sha256_of_url=_OK_HASH)
    assert m["spec"]["evidence"] == "picard A"


def test_map_install_jar_without_a_smoke_keeps_the_presence_default():
    """No recorded smoke → `ic.jar`'s presence default. Threading must not fabricate
    an evidence; absence stays absence (the report then honestly shows presence)."""
    m = _map_install(_jar_install({}), sha256_of_url=_OK_HASH)
    # ic.jar default: `command -v {wrap} && java -version`
    assert m["spec"]["evidence"] == "command -v picard && java -version"


def test_map_install_jar_still_attaches_provenance():
    """Threading evidence must not disturb the C5 provenance disclosure (tier=jar,
    pinned_tofu when the freeze re-fetch hashed the bytes)."""
    m = _map_install(_jar_install({"evidence": "picard X"}), sha256_of_url=_OK_HASH)
    prov = m["spec"]["provenance"]
    assert prov["tier"] == "jar" and prov["verified"] is False
    assert prov["assurance"] == "pinned_tofu"          # a hash was captured
    assert prov["asset_url"] == _JAR_URL


def test_map_install_jar_unanchored_when_hash_fails():
    """No checksum captured (jars rarely publish one, and the fetch may fail) → the
    honest disclosure is `unanchored`, never a fabricated assurance."""
    m = _map_install(_jar_install({"evidence": "picard X"}),
                     sha256_of_url=lambda _u: {"ok": False})
    assert m["spec"]["provenance"]["assurance"] == "unanchored"


# ── #B the grid recipe's evidence is genuinely FUNCTIONAL + non-cheat ──────────

def _grid_jar_evidence() -> str:
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("freeze_tiers", root / "scripts" / "freeze_tiers.py")
    ft = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ft)
    return ft.tier("jar")["build"]["install_method"]["evidence"]


def test_grid_jar_recipe_evidence_is_functional_depth():
    """The recipe's evidence must classify FUNCTIONAL (it RUNS Picard on data),
    not 'presence' — otherwise the report would over- or under-claim what the jar
    tier proved. This is the reports-never-lie honesty anchor for the tier."""
    ev = _grid_jar_evidence()
    assert env_honesty.evidence_depth(ev, "picard") == "functional"
    assert not env_honesty.is_shallow_evidence(ev, "picard")


def test_grid_jar_recipe_evidence_passes_the_anti_cheat_shape_rule():
    """It must NOT trip the echo/printf bare-cheat guard (it leads with `cd`, not
    printf) and must reference `picard` as a real word-boundary invocation."""
    ev = _grid_jar_evidence()
    assert env_honesty.evidence_shape_violation(ev, "picard") is None
    # the shape guard would fire if the evidence merely echoed the tool name:
    assert env_honesty.evidence_shape_violation("printf 'picard 3.4.0'", "picard") is not None


# ── #A the producer side: install_jar_tool records the smoke freeze re-runs ────

def test_install_jar_tool_records_the_smoke_as_freeze_evidence(tmp_path, monkeypatch):
    """The producer that makes the threading reachable by a REAL user (not only the
    grid recipe): a jar install with a verify_command must write it into
    install_method so freeze re-runs it in-image as VALIDATED_IN_IMAGE evidence."""
    from agent import mcp_server as _ms
    from agent.mcp_tools import env_tools
    from agent.skills.pipeline_state import PipelineState

    (tmp_path / "drafts").mkdir(); (tmp_path / "reports").mkdir()
    cfg = {**_ms.config, "paths": {**_ms.config.get("paths", {}),
                                   "drafts_dir": str(tmp_path / "drafts"),
                                   "pipelines_dir": str(tmp_path / "reports")}}
    ps = PipelineState(cfg)
    monkeypatch.setattr(_ms, "_pipeline_state", ps)

    class _FakeMgr:
        def install_jar_tool(self, **k):
            return {"success": True, "jar_path": "/e/share/picard/picard.jar",
                    "wrapper_path": "/e/bin/picard"}
        def run_in_env(self, env_name, command, timeout=300):
            return {"returncode": 0, "stdout": "ran\n", "stderr": ""}
    monkeypatch.setattr(_ms, "_env_mgr", _FakeMgr())

    pid = ps.start("jar_smoke_test", "x")["pipeline_id"]
    smoke = "cd /tmp && picard CreateSequenceDictionary -R r.fa -O r.dict && test -s r.dict"
    env_tools.install_jar_tool(
        env_name="e", tool_name="picard",
        jar_url="https://github.com/broadinstitute/picard/releases/download/3.4.0/picard.jar",
        verify_command=smoke, pipeline_id=pid)

    im = ps.get_draft(pid)["install_steps"][0]["installed_packages"][0]["install_method"]
    assert im["type"] == "jar"
    assert im["verify_command"] == smoke           # → threaded by _map_install at freeze


def test_install_jar_tool_without_a_smoke_records_no_verify_command(tmp_path, monkeypatch):
    """No smoke given → no fabricated verify_command; the jar's evidence honestly
    falls back to the presence probe at freeze."""
    from agent import mcp_server as _ms
    from agent.mcp_tools import env_tools
    from agent.skills.pipeline_state import PipelineState

    (tmp_path / "drafts").mkdir(); (tmp_path / "reports").mkdir()
    cfg = {**_ms.config, "paths": {**_ms.config.get("paths", {}),
                                   "drafts_dir": str(tmp_path / "drafts"),
                                   "pipelines_dir": str(tmp_path / "reports")}}
    ps = PipelineState(cfg)
    monkeypatch.setattr(_ms, "_pipeline_state", ps)

    class _FakeMgr:
        def install_jar_tool(self, **k):
            return {"success": True, "jar_path": "/e/p.jar", "wrapper_path": "/e/bin/p"}
    monkeypatch.setattr(_ms, "_env_mgr", _FakeMgr())

    pid = ps.start("jar_nosmoke_test", "x")["pipeline_id"]
    env_tools.install_jar_tool(env_name="e", tool_name="picard",
                               jar_url="https://x/picard.jar", pipeline_id=pid)
    im = ps.get_draft(pid)["install_steps"][0]["installed_packages"][0]["install_method"]
    assert "verify_command" not in im


def test_grid_jar_recipe_is_a_wired_container_native_probe():
    """The jar row is now a real probe (builder set + a build recipe), so the
    recipe stays reproducible — the drift guard already asserts the tier set."""
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("freeze_tiers", root / "scripts" / "freeze_tiers.py")
    ft = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ft)
    row = ft.tier("jar")
    assert row["builder"] == "container_native"
    assert row["probe_tool"] == "picard"
    assert row["build"]["install_method"]["type"] == "jar"
    assert "picard.jar" in row["build"]["install_method"]["source"]
