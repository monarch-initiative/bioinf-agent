"""Authored evidence may invoke the command a package ships rather than the package's name.

The conda package google-cloud-sdk ships `gcloud`; the token rule, keyed on the package
name, refused `gcloud version` on the build and adopt paths while the image paths accepted
the image's own command names. Both paths now read the package's commands off the image
(its conda-meta record) and hold the evidence to the rule with those names: the build path
defers the clause until the image exists, the adopt path probes before judging.
"""
from __future__ import annotations

import pytest

from agent.skills import container_build as CB
from agent.skills import env_build as eb
from agent.skills.env_honesty import evidence_shape_violation


def test_the_probe_reads_every_conda_prefix_and_the_parser_keeps_only_command_names():
    sh = CB.package_commands_sh("google-cloud-sdk")
    assert "/opt/conda/conda-meta/google-cloud-sdk-*.json" in sh
    assert "/usr/local/conda-meta/google-cloud-sdk-*.json" in sh and sh.endswith("exit 0")
    assert CB.parse_command_names("gcloud\ngsutil\n\nbq\nnot a name!\n") == ["bq", "gcloud", "gsutil"]


def test_the_token_clause_can_be_deferred_and_is_satisfied_by_a_shipped_command():
    assert "never references the tool token" in evidence_shape_violation("gcloud version", "google-cloud-sdk")
    assert evidence_shape_violation("gcloud version", "google-cloud-sdk", require_tool_token=False) is None
    assert evidence_shape_violation("gcloud version", "google-cloud-sdk", ["gcloud"]) is None
    # the other clauses still refuse with the token clause deferred
    assert evidence_shape_violation("true || gcloud version", "google-cloud-sdk",
                                    require_tool_token=False) is not None


def _build_inst(verifications):
    class FakeCB:
        platform = "linux/amd64"
        longtail: list = []

        def start(self):
            return {"success": False, "error": "stopped by the test after the preflight"}

        def validate_in_image(self, image, checks, probe_tools=None, **_kw):
            return {"success": True, "checks": {c: {"rc": 0, "out": "ok"} for c in checks}, "banners": {}}

        def probe_versions(self, image, probes):
            return {}

        def package_commands(self, image, name):
            return self.names

    inst = eb.EnvBuild.__new__(eb.EnvBuild)
    inst.cb = FakeCB()
    inst.captured_versions = {}
    inst.verifications = verifications
    inst.prebaked_lock_files = None
    inst.conda_specs, inst.pip_specs, inst.tools = [], [], []
    return inst


def test_the_build_defers_only_the_token_clause_and_still_refuses_a_cheat():
    inst = _build_inst([{"label": "google-cloud-sdk", "tool": "google-cloud-sdk",
                         "check": "gcloud version", "engine_coupled": True}])
    r = inst.build()
    assert r["code"] == "env_build.start_failed", r          # the preflight let it through
    assert inst.verifications[0]["tool_aliases_pending"] is True
    inst = _build_inst([{"label": "google-cloud-sdk", "tool": "google-cloud-sdk",
                         "check": "true || gcloud version", "engine_coupled": True}])
    r = inst.build()
    assert r["code"] == "env_build.evidence_shape" and r["stage"] == "evidence_preflight"


def test_the_built_image_says_which_commands_satisfy_the_clause(monkeypatch):
    monkeypatch.setattr("agent.skills.freeze_from_image._evidence_discriminates",
                        lambda platform, ev: ("discriminating", "stub"))
    inst = _build_inst([{"label": "google-cloud-sdk", "tool": "google-cloud-sdk",
                         "check": "gcloud version", "engine_coupled": True, "tool_aliases_pending": True}])
    inst.cb.names = ["bq", "gcloud", "gsutil"]
    out = inst.verify_in_image("img")
    assert out["success"] is True, out
    assert out["verifications"][0]["tool_aliases"] == ["bq", "gcloud", "gsutil"]
    assert "tool_aliases_pending" not in inst.verifications[0]

    inst = _build_inst([{"label": "google-cloud-sdk", "tool": "google-cloud-sdk",
                         "check": "gcloud version", "engine_coupled": True, "tool_aliases_pending": True}])
    inst.cb.names = []
    out = inst.verify_in_image("img")
    assert out["code"] == "env_build.evidence_shape" and "ships no bin/ command" in out["error"]


def test_the_adopt_path_probes_the_image_for_the_packages_commands(monkeypatch):
    import agent.mcp_tools.freeze_tools as ft
    probed = []

    class _Docker:
        def run_in_container(self, image, cmd, platform=None, timeout=None):
            if "conda-meta" in cmd and "sed -nE" in cmd:
                probed.append(cmd)
                return {"returncode": 0, "stdout": "gcloud\ngsutil\n", "stderr": ""}
            return {"returncode": 0, "stdout": "ok", "stderr": ""}

    class _EF:
        @staticmethod
        def _conda_presence_check(tool):
            return f"ls /opt/conda/conda-meta/{tool}-*.json"

    monkeypatch.setattr(ft._ms, "_docker", _Docker(), raising=False)
    monkeypatch.setattr(ft._ms, "_env_freeze", _EF(), raising=False)
    monkeypatch.setattr("agent.skills.freeze_from_image._evidence_discriminates",
                        lambda platform, ev: ("discriminating", "stub"))
    rows = ft._validate_tools_in_image("img", "linux/amd64", ["google-cloud-sdk", "samtools"],
                                       evidence={"google-cloud-sdk": "gcloud version",
                                                 "samtools": "samtools --version"})
    assert rows[0]["shape_violation"] == "" and rows[0]["tool_aliases"] == ["gcloud", "gsutil"]
    assert rows[1]["shape_violation"] == "" and rows[1]["tool_aliases"] == []
    assert len(probed) == 1, "only an evidence that names another command pays for the probe"
