"""`freeze(evidence=)` is one contract for both ways an image is obtained.

The adopt path runs caller-authored evidence inside the adopted image and in a control
image that lacks the tool. The container-native build path must take the same dict:
the authored command becomes that tool's in-image verification in place of the
synthesized presence probe, and EnvBuild holds it to the shape rule and the control
experiment like every other verification. A tool nothing was authored for keeps the
default probe, and the ENV report says so.
"""
from __future__ import annotations

from agent.skills import env_freeze


class _Build:
    """A stand-in for EnvBuild that records what the builder asked it to verify."""
    made: list["_Build"] = []

    def __init__(self, *a, **k):
        self.conda_verify: list[tuple[str, str]] = []
        self.pip_verify: list[tuple[str, str]] = []
        _Build.made.append(self)

    def add_conda(self, specs, verify):
        self.conda_verify = list(verify)
        return self

    def add_pip(self, specs, verify):
        self.pip_verify = list(verify)
        return self

    def add_tool(self, spec):
        return self

    def run(self):
        return {"success": True, "verifications": []}

    def request_key(self):
        return "k"


def _build(monkeypatch, spec: dict, **kw) -> _Build:
    monkeypatch.setattr(env_freeze, "EnvBuild", _Build)
    _Build.made.clear()
    env_freeze.build_env_image(spec, name="x", platform="linux/amd64", **kw)
    assert _Build.made, "build_env_image never constructed the builder"
    return _Build.made[-1]


AUTHORED = "samtools sort /t.bam -o /o.bam && test -s /o.bam"


def test_authored_evidence_is_the_conda_tools_in_image_verification(monkeypatch):
    eb = _build(monkeypatch, {}, conda_deps=["samtools=1.21", "bwa=0.7.17"],
                primary_tools=["samtools", "bwa"], evidence={"samtools": AUTHORED})
    verify = dict(eb.conda_verify)
    assert verify["samtools"] == AUTHORED
    # the tool nothing was authored for keeps the synthesized probe
    assert verify["bwa"] == env_freeze._conda_presence_check("bwa")


def test_without_evidence_every_conda_tool_gets_the_presence_probe(monkeypatch):
    eb = _build(monkeypatch, {}, conda_deps=["samtools=1.21"], primary_tools=["samtools"])
    assert eb.conda_verify == [("samtools", env_freeze._conda_presence_check("samtools"))]


def test_an_empty_evidence_entry_does_not_erase_the_probe(monkeypatch):
    eb = _build(monkeypatch, {}, conda_deps=["samtools=1.21"], primary_tools=["samtools"],
                evidence={"samtools": ""})
    assert dict(eb.conda_verify)["samtools"] == env_freeze._conda_presence_check("samtools")


def test_authored_evidence_reaches_a_pip_tool_too(monkeypatch):
    spec = {"install_steps": [{
        "tool": "pip", "subcommand": "install", "returncode": 0, "command": "pip install pysam",
        "installed_packages": [{"name": "pysam", "version": "0.22",
                                "install_method": {"type": "pip"}}]}]}
    eb = _build(monkeypatch, spec, conda_deps=[], primary_tools=["pysam"],
                evidence={"pysam": "python -c 'import pysam; pysam.AlignmentFile(\"/t.bam\")'"})
    assert dict(eb.pip_verify)["pysam"].startswith("python -c 'import pysam; pysam.AlignmentFile")
