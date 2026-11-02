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
        self.tool_specs = getattr(self, "tool_specs", []) + [spec]
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


_JAR_URL = "https://github.com/exomiser/Exomiser/releases/download/15.1.0/exomiser-cli-15.1.0-distribution.zip"
PIPED = "exomiser --help 2>&1 | grep -i exomiser"
AUTHORED_JAR = "exomiser --help > /tmp/h.txt 2>&1 && grep -q Exomiser /tmp/h.txt"


def _jar_spec() -> dict:
    return {"install_steps": [{
        "tool": "install_jar_tool", "subcommand": "", "returncode": 0,
        "command": f"install_jar_tool --jar-url {_JAR_URL}",
        "installed_packages": [{"name": "exomiser", "version": "15.1.0", "type": "jar",
                                "install_method": {"type": "jar", "name": "exomiser", "source": _JAR_URL,
                                                   "verify_command": PIPED}}]}]}


def test_authored_evidence_replaces_a_jar_tools_recorded_verify_command(monkeypatch):
    """A jar's recorded verify_command was a piped check the shape rule refuses; `evidence=`
    for the tool changed nothing because it reached only conda and pip tools, and five
    freezes refused the same command while the caller changed the evidence each time."""
    orig = env_freeze._map_install
    monkeypatch.setattr(env_freeze, "_map_install",
                        lambda x, platform: orig(x, platform, sha256_of_url=lambda _u: {"ok": True, "sha256": "d" * 64}))
    eb = _build(monkeypatch, _jar_spec(), conda_deps=[], primary_tools=["exomiser"], evidence={"exomiser": AUTHORED_JAR})
    [spec] = eb.tool_specs
    assert spec["tool"] == "exomiser" and spec["evidence"] == AUTHORED_JAR
    eb = _build(monkeypatch, _jar_spec(), conda_deps=[], primary_tools=["exomiser"])
    assert eb.tool_specs[0]["evidence"] == PIPED, "nothing authored: the recorded check stands"
