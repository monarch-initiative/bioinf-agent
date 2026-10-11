"""Looking inside an image is a tool's job, not a shell detour.

When evidence fails because the command is not there, the refusal says where the tool's
name occurs in the image, so the next call names the right command. `inspect_image` runs
one read-only command in any image. Evidence that is slow to start gets a settable timeout
the record carries. Evidence that only inspects files is disclosed as presence. A
run-by-path repo's verify runs from its clone directory in the build container, as it does
on the host and in the shipped image. The resolver reads the user's own words with their
separators folded.
"""
from __future__ import annotations

import pytest
from freeze_fixtures import FakeCache, mock_docker

from agent.skills import container_build as CB
from agent.skills import env_honesty as EH
from agent.skills import freeze_from_image as F
from agent.skills import resolver as R


# ---------------------------------------------------------------------------
# evidence depth: a file inspection is presence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("evidence, tool, depth", [
    ('bash -c "grep -qi apollo /launch.sh 2>&1 && test -f /launch.sh && grep -qi \'apollo\\|tomcat\' /launch.sh"',
     "apollo", "presence"),
    ("grep -q apollo /etc/apollo.conf", "apollo", "presence"),
    ("ls /opt/tools/seqkit/bin && test -x /opt/tools/seqkit/bin/seqkit", "seqkit", "presence"),
    ("apollo --version", "apollo", "version"),
    ("apollo -i a.vcf -o out.vcf", "apollo", "functional"),
    ("python -c 'import talos'", "talos", "import"),
    ("cat data.txt | seqkit stats", "seqkit", "functional"),
    ("/HiC-Pro_3.0.0/bin/HiC-Pro -h", "HiC-Pro", "help"),
])
def test_evidence_that_only_inspects_files_is_presence(evidence, tool, depth):
    assert EH.evidence_depth(evidence, tool) == depth


def test_unwrap_shell_reads_the_inner_command():
    assert EH._unwrap_shell('bash -c "grep x /f"') == "grep x /f"
    assert EH._unwrap_shell("sh -lc 'ls /opt'") == "ls /opt"
    assert EH._unwrap_shell("grep x /f") == "grep x /f"


# ---------------------------------------------------------------------------
# a missing command says where the tool is
# ---------------------------------------------------------------------------

_PROBE_OUT = """PATH=/usr/local/sbin:/usr/local/bin:/usr/bin
---executables---
/HiC-Pro_3.0.0/bin/HiC-Pro
/HiC-Pro_3.0.0/bin/utils/HiC-Pro_convert
---console_scripts---
run_workflow = talos.cpg_internal_scripts.run_workflow:cli_main @ /talos/.venv/bin/run_workflow
"""


def _run_with_probe(evidence_rc: int, evidence_out: str, seen: list):
    def _run(image, platform, command, timeout=300, maxlen=400):
        seen.append({"command": command, "timeout": timeout})
        if "---executables---" in command:
            return {"rc": 0, "out": _PROBE_OUT}
        if image == F._CONTROL_IMAGE:
            return {"rc": 127, "out": "command not found"}
        if "importlib.metadata" in command:
            return {"rc": 0, "out": "[]"}
        return {"rc": evidence_rc, "out": evidence_out}
    return _run


def test_a_command_not_found_refusal_says_where_the_name_occurs(tmp_path, monkeypatch):
    mock_docker(monkeypatch)
    seen: list = []
    monkeypatch.setattr(F, "_run_in_image", _run_with_probe(127, "bash: line 1: HiC-Pro: command not found", seen))
    out = F.freeze_from_image(image="nservant/hicpro:3.0.0", name="hicpro", version="3.0.0",
                              tools=[{"name": "HiC-Pro", "evidence": "HiC-Pro -h"}],
                              env_cache=FakeCache(), env_dir=tmp_path)
    assert (out["outcome"], out["code"]) == ("refused", "freeze_from_image.honesty_violation")
    where = out["verifications"][0]["where"]
    assert where["executables"] == ["/HiC-Pro_3.0.0/bin/HiC-Pro", "/HiC-Pro_3.0.0/bin/utils/HiC-Pro_convert"]
    assert where["console_scripts"] == ["run_workflow = talos.cpg_internal_scripts.run_workflow:cli_main @ /talos/.venv/bin/run_workflow"]
    assert where["path"].startswith("/usr/local/sbin")
    assert out["not_found"] == [
        "HiC-Pro: no such command on the image's PATH (/usr/local/sbin:/usr/local/bin:/usr/bin); the name occurs at: "
        "/HiC-Pro_3.0.0/bin/HiC-Pro; /HiC-Pro_3.0.0/bin/utils/HiC-Pro_convert; "
        "run_workflow = talos.cpg_internal_scripts.run_workflow:cli_main @ /talos/.venv/bin/run_workflow"]
    assert "/HiC-Pro_3.0.0/bin/HiC-Pro" in out["error"]
    probe = [c for c in seen if "---executables---" in c["command"]]
    assert len(probe) == 1 and probe[0]["timeout"] == F._PROBE_TIMEOUT_S
    assert "-iname '*HiC-Pro*'" in probe[0]["command"] and "entry_points.txt" in probe[0]["command"]


def test_an_evidence_that_fails_for_another_reason_is_not_probed(tmp_path, monkeypatch):
    mock_docker(monkeypatch)
    seen: list = []
    monkeypatch.setattr(F, "_run_in_image", _run_with_probe(2, "usage error: missing --input", seen))
    out = F.freeze_from_image(image="img:1", name="x", tools=[{"name": "x", "evidence": "x run"}],
                              env_cache=FakeCache(), env_dir=tmp_path)
    assert out["code"] == "freeze_from_image.honesty_violation"
    assert "where" not in out["verifications"][0] and "not_found" not in out
    assert not any("---executables---" in c["command"] for c in seen)


def test_an_empty_probe_is_stated_as_empty():
    assert F._where_summary("x", {"path": "/bin", "executables": [], "console_scripts": []}) == (
        "x: no such command on the image's PATH (/bin), and no executable or console script in the image carries the name")


# ---------------------------------------------------------------------------
# the evidence timeout is the caller's, bounded, and recorded
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("asked, used", [(None, 300), (0, 300), (900, 900), (5000, 1800), (5, 30), ("x", 300)])
def test_the_evidence_timeout_is_bounded(asked, used):
    assert F.evidence_timeout(asked) == used


def test_the_evidence_runs_under_the_requested_timeout_and_the_record_says_so(tmp_path, monkeypatch):
    mock_docker(monkeypatch)
    seen: list = []
    monkeypatch.setattr(F, "_run_in_image", _run_with_probe(0, "ran", seen))
    cache = FakeCache()
    out = F.freeze_from_image(image="img:1", name="talos", version="12",
                              tools=[{"name": "talos", "evidence": "talos run --dry a.vcf"}],
                              evidence_timeout_s=900, env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] in ("proven", "degraded"), out
    ev = [c for c in seen if c["command"] == "talos run --dry a.vcf"]
    assert ev and ev[0]["timeout"] == 900
    assert out["verifications"][0]["timeout_s"] == 900


def test_the_mcp_wrappers_take_the_timeout():
    import inspect
    from agent.mcp_tools import freeze_tools as FT
    for fn in (FT.freeze_from_image, FT.build_env_from_authors_recipe):
        target = getattr(fn, "fn", None) or getattr(fn, "__wrapped__", None) or fn
        assert "evidence_timeout_s" in inspect.signature(target).parameters, fn


# ---------------------------------------------------------------------------
# inspect_image: one read-only command, the output back, nothing recorded
# ---------------------------------------------------------------------------

def test_inspect_image_returns_the_commands_own_exit_code_and_output(monkeypatch):
    monkeypatch.setattr(F, "_image_present", lambda image: True)
    monkeypatch.setattr(F, "_run_in_image",
                        lambda image, platform, command, timeout=300, maxlen=400: {"rc": 2, "out": f"ran {command} in {timeout}s"})
    out = F.inspect_image(image="gmod/apollo:release-2.9.0", command="ls /var/lib/tomcat9/webapps", timeout_s=30)
    assert (out["outcome"], out["code"]) == ("proven", "inspect_image.ran")
    assert out["rc"] == 2 and out["out"] == "ran ls /var/lib/tomcat9/webapps in 30s" and out["timeout_s"] == 30
    assert F.inspect_image(image="x", command="  ")["code"] == "inspect_image.no_command"
    assert F.inspect_image(image="", command="ls")["code"] == "inspect_image.no_image"


def test_inspect_image_pulls_an_absent_image_and_reports_a_failed_pull(monkeypatch):
    monkeypatch.setattr(F, "_image_present", lambda image: False)
    monkeypatch.setattr(F, "_sh", lambda argv, timeout=300, **kw: {"rc": 1, "out": "", "err": "manifest unknown"})
    out = F.inspect_image(image="nope/none:1", command="ls")
    assert (out["outcome"], out["code"]) == ("broke", "inspect_image.image_unavailable")
    assert "manifest unknown" in out["error"]
    assert F.inspect_image(image="nope/none:1", command="ls", pull_if_absent=False)["code"] == "inspect_image.image_unavailable"


def test_inspect_image_is_a_served_query_primitive():
    import asyncio
    from agent.mcp_server import mcp
    from agent.skills import tool_surface as ts
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "inspect_image" in names
    assert ts.REGISTRY["inspect_image"].position == ts.PRIMITIVE and not ts.REGISTRY["inspect_image"].mutates


# ---------------------------------------------------------------------------
# the build runs a script repo's verify from its clone directory
# ---------------------------------------------------------------------------

def _builder(monkeypatch):
    cb = CB.ContainerBuild(platform="linux/amd64", engine=CB.PixiEngine("linux/amd64"))
    cb.cid = "cid"
    calls: list[str] = []

    def fake_sh(argv, timeout=300, **kw):
        calls.append(argv[-1])
        return {"returncode": 0, "stdout": "ok", "stderr": ""}
    monkeypatch.setattr(cb, "_sh", fake_sh)
    monkeypatch.setattr(cb, "_emulated", lambda: False)
    return cb, calls


def test_the_evidence_runs_from_the_specs_workdir_and_the_install_from_the_default(monkeypatch):
    cb, calls = _builder(monkeypatch)
    out = cb.install({"command": "git clone x /opt/tools/gab", "evidence": "cd HIC_ASSEMBLER && python run.py -h",
                      "tool": "gab", "purpose": "gab (script repo)", "evidence_workdir": "/opt/tools/gab"})
    assert out["outcome"] == "proven", out
    install_sh, evidence_sh = calls[-2], calls[-1]
    assert f"cd {cb.workdir} 2>/dev/null || true; git clone x /opt/tools/gab" in install_sh
    assert "cd /opt/tools/gab || { echo \"evidence workdir /opt/tools/gab does not exist" in evidence_sh
    assert evidence_sh.endswith("cd HIC_ASSEMBLER && python run.py -h")
    assert cb.longtail[-1]["evidence"] == "cd HIC_ASSEMBLER && python run.py -h", "the recorded evidence stays the author's string"


def test_a_spec_without_a_workdir_runs_its_evidence_from_the_default(monkeypatch):
    cb, calls = _builder(monkeypatch)
    cb.install({"command": "true", "evidence": "seqkit version", "tool": "seqkit"})
    assert f"cd {cb.workdir} 2>/dev/null || true; seqkit version" in calls[-1]


# ---------------------------------------------------------------------------
# the resolver reads the user's words with their separators folded
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool, said, expect", [
    ("Genome_Assembly_Booster", "install the Genome Assembly Booster at v1", True),
    ("HiC-Pro", "hic pro please", True),
    ("HiC-Pro", "HiC-Pro from https://github.com/nservant/HiC-Pro", True),
    ("bwa", "bowtie2 please", False),
    ("cat", "concatenate the files", False),
    ("", "anything", False),
])
def test_user_said_mentions_folds_case_and_separators(tool, said, expect):
    assert R.user_said_mentions(tool, said) is expect
