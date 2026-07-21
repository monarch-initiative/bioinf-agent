"""Freeze-path regression tests for the PERL tier (P2-C tier-breadth slice).

Anchored to a REAL build: the freeze-tier grid now bakes an engine-coupled CPAN
module container-native and proves it with a run-on-data smoke —

  perl → Set::IntervalTree 0.12 (Ben Booth), a real Ensembl VEP dependency and a
         genomic interval tree. It's a C++ XS module, so the build compiles XS
         against the conda perl (5.32) with the conda cxx-compiler + the xlocale.h
         shim — the tier's actual mechanism, which a pure-perl module would skip.
         The evidence builds an interval tree, inserts two disjoint intervals, and
         asserts fetch() returns the overlapping value AND excludes the far one.

perl was the LAST non-conda tier whose `_map_install` branch dropped a recorded
smoke — it always fell back to `ic.perl_cpanm`'s `perl -M{module} -e1` import-only
default, so a perl module could never carry a run-on-data VALIDATED_IN_IMAGE (unlike
source/cargo/go/jar/synthesized/r_install). It now threads `evidence`/`verify_command`,
and `install_perl_package` gained a `verify_command` param so a REAL user (not only
the grid recipe) can record the smoke.

THE HONEST NUANCE (reports-never-lie): the depth CLASSIFIER discloses this evidence
as 'import', NOT 'functional' — perl's `-Mmodule` glue is structurally indistinguishable
from an import-only load, so the classifier cannot see that the `-e` body actually runs
the XS methods. That is an honest UNDER-disclosure (conservative; it never over-claims a
functional depth). These tests PIN that 'import' label so a future classifier change that
promoted it to 'functional' would flag for review rather than silently alter a report — and
separately assert that the evidence IS a genuine run (a redirect + `test -s`, distinct from
the import-only default), which is the real strengthening this slice delivers.
"""
import importlib.util
from pathlib import Path

from agent.skills.env_freeze import _map_install
from agent.skills import env_honesty


def _ft():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("freeze_tiers", root / "scripts" / "freeze_tiers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _perl_install(im_extra: dict) -> dict:
    im = {"type": "perl", "name": "Set::IntervalTree", "module": "Set::IntervalTree",
          "distribution": "Set::IntervalTree@0.12"}
    im.update(im_extra)
    return {"name": "Set::IntervalTree", "type": "perl", "install_method": im}


# ── #A the evidence-threading wiring ──────────────────────────────────────────

def test_map_install_perl_threads_recorded_evidence():
    """A perl module that recorded a self-contained smoke ships THAT as the in-image
    evidence — the fix that lets a perl module prove validated==ran (its compiled XS
    methods actually work), not merely that `perl -M{module} -e1` loads the .so."""
    smoke = "cd /tmp && perl -MSet::IntervalTree -e '...' > /tmp/o.txt && test -s /tmp/o.txt"
    m = _map_install(_perl_install({"evidence": smoke}))
    assert m["spec"]["evidence"] == smoke


def test_map_install_perl_verify_command_is_the_fallback_key():
    smoke = "cd /tmp && perl -MSet::IntervalTree -e '...' > /tmp/o.txt && test -s /tmp/o.txt"
    m = _map_install(_perl_install({"verify_command": smoke}))
    assert m["spec"]["evidence"] == smoke


def test_map_install_perl_evidence_wins_over_verify_command():
    m = _map_install(_perl_install({"evidence": "perl -MSet::IntervalTree A",
                                    "verify_command": "perl -MSet::IntervalTree B"}))
    assert m["spec"]["evidence"] == "perl -MSet::IntervalTree A"


def test_map_install_perl_without_a_smoke_keeps_the_import_default():
    """No recorded smoke → ic.perl_cpanm's `perl -M{module} -e1` import default.
    Threading must not fabricate an evidence; absence stays absence."""
    m = _map_install(_perl_install({}))
    assert m["spec"]["evidence"] == "perl -MSet::IntervalTree -e1"


def test_map_install_perl_command_carries_the_xlocale_shim_and_pinned_dist():
    """The generated build command shims xlocale.h (conda perl 5.32 still #includes
    it) BEFORE cpanm, and installs the version-PINNED distribution — the tier's real
    XS-against-conda-perl mechanism, reproducibly."""
    cmd = _map_install(_perl_install({}))["spec"]["command"]
    assert "xlocale.h" in cmd
    assert "cpanm --notest Set::IntervalTree@0.12" in cmd


def test_map_install_perl_still_attaches_provenance():
    """Threading evidence must not disturb the C5 provenance disclosure. A cpanm
    install is TOFU (no CPAN content-hash pin in the recipe) → assurance cpan_tofu."""
    m = _map_install(_perl_install({"evidence": "perl -MSet::IntervalTree X"}))
    prov = m["spec"]["provenance"]
    assert prov["tier"] == "perl"
    assert prov["verified"] is False
    assert prov["assurance"] == "cpan_tofu"


# ── #B the grid recipe's evidence: a genuine run, honestly disclosed ──────────

def test_grid_recipe_evidence_passes_the_anti_cheat_shape_rule():
    """It must NOT trip the echo/printf bare-cheat guard (it leads with `cd`, not
    printf) and must reference the module as a real word-boundary invocation (the
    perl `-M` glue exception in _references_tool)."""
    tool = "Set::IntervalTree"
    ev = _ft().tier("perl")["build"]["install_method"]["evidence"]
    assert env_honesty.evidence_shape_violation(ev, tool) is None
    assert env_honesty._references_tool(ev, tool) is True
    assert env_honesty.evidence_shape_violation(f"printf '{tool} 1.0'", tool) is not None


def test_grid_recipe_evidence_is_a_genuine_run_not_the_import_default():
    """The strengthening this slice delivers: the recipe evidence is a run-on-data
    smoke (redirects a real print to a file and `test -s`-checks it), DISTINCT from
    the import-only `perl -M{module} -e1` default — it exercises the compiled XS
    query methods (fetch discriminates an overlap from a non-overlap)."""
    ev = _ft().tier("perl")["build"]["install_method"]["evidence"]
    assert ev != "perl -MSet::IntervalTree -e1"
    assert "> /tmp/sit_out.txt" in ev and "test -s /tmp/sit_out.txt" in ev
    assert "->insert(" in ev and "->fetch(" in ev


def test_grid_recipe_evidence_depth_is_import_an_honest_underdisclosure():
    """reports-never-lie anchor + change detector. The depth classifier reads this
    genuine run as 'import' (perl's `-M` glue is indistinguishable from an import-
    only load), which UNDER-discloses the real depth — conservative, never an over-
    claim. Pinned here so a classifier change that promoted it to 'functional' fails
    this test and gets REVIEWED rather than silently altering the env report."""
    ev = _ft().tier("perl")["build"]["install_method"]["evidence"]
    assert env_honesty.evidence_depth(ev, "Set::IntervalTree") == "import"
    # 'import' is a shallow depth — the report honestly renders it as such, never as a run.
    assert env_honesty.is_shallow_evidence(ev, "Set::IntervalTree") is True


def test_grid_perl_recipe_is_a_wired_container_native_probe():
    """The perl row is now a real probe (builder set + a build recipe, version-
    PINNED via cpanm Module@version), so the breadth meter counts it; the drift guard
    asserts the tier set stays == InstallMethod.type."""
    row = _ft().tier("perl")
    assert row["builder"] == "container_native"
    assert row["probe_tool"] == "Set::IntervalTree"
    im = row["build"]["install_method"]
    assert im["type"] == "perl" and im["module"] == "Set::IntervalTree"
    assert im["distribution"] == "Set::IntervalTree@0.12"   # PINNED — reproducibility


# ── #A the producer side: install_perl_package records the smoke ──────────────

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


def test_install_perl_package_records_the_smoke_as_freeze_evidence(tmp_path, monkeypatch):
    """The producer that makes the threading reachable by a REAL user (not only the
    grid recipe): a perl install with a verify_command writes it into
    install_method["evidence"] so freeze re-runs it in-image as VALIDATED_IN_IMAGE."""
    from agent.mcp_tools import env_tools
    _ms, ps = _ps_in_tmp(tmp_path, monkeypatch)

    class _FakeMgr:
        def install_perl_package(self, env_name, module, distribution, cpanm_flags, build_env):
            return {"success": True, "module": module,
                    "verify_command": f"perl -M{module} -e1", "verify_output": "",
                    "install_method": {"type": "perl", "module": module,
                                       "distribution": distribution, "name": module}}
        def run_in_env(self, env_name, command, timeout=300):
            return {"returncode": 0, "stdout": "1\n", "stderr": ""}
    monkeypatch.setattr(_ms, "_env_mgr", _FakeMgr())

    pid = ps.start("perl_smoke_test", "x")["pipeline_id"]
    smoke = "cd /tmp && perl -MSet::IntervalTree -e '...' > /tmp/o.txt && test -s /tmp/o.txt"
    env_tools.install_perl_package(env_name="e", module="Set::IntervalTree",
                                   distribution="Set::IntervalTree@0.12",
                                   verify_command=smoke, pipeline_id=pid)

    im = ps.get_draft(pid)["install_steps"][0]["installed_packages"][0]["install_method"]
    assert im["type"] == "perl"
    assert im["evidence"] == smoke            # → threaded by _map_install at freeze


def test_install_perl_package_without_a_smoke_records_no_evidence(tmp_path, monkeypatch):
    """No smoke given → no fabricated evidence; the perl module's evidence honestly
    falls back to the `perl -M{module} -e1` import default at freeze."""
    from agent.mcp_tools import env_tools
    _ms, ps = _ps_in_tmp(tmp_path, monkeypatch)

    class _FakeMgr:
        def install_perl_package(self, env_name, module, distribution, cpanm_flags, build_env):
            return {"success": True, "module": module,
                    "install_method": {"type": "perl", "module": module, "name": module}}
    monkeypatch.setattr(_ms, "_env_mgr", _FakeMgr())

    pid = ps.start("perl_nosmoke_test", "x")["pipeline_id"]
    env_tools.install_perl_package(env_name="e", module="Set::IntervalTree", pipeline_id=pid)
    im = ps.get_draft(pid)["install_steps"][0]["installed_packages"][0]["install_method"]
    assert "evidence" not in im
