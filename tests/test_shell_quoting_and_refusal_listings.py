"""
Two properties of the skills that build shell lines from agent-supplied text:

  1. Every value that originates from the agent or the user (a package name, a path,
     a URL, a flag) reaches the shell as ONE token via shlex.quote; a whole `-c` / `-e`
     payload is quoted as a whole; a remote `bash -lc` body is quoted as a whole rather
     than hand-wrapped in single quotes. The assertion shape is the same everywhere:
     the rendered line splits back (shlex.split) into exactly the tokens that were
     meant, with the hostile value intact.
  2. A refusal that says "not found" also says what IS there — the registered
     phenopacket ids, the supported install types, the local docker images, the keys
     the EnvCache holds.
"""
from __future__ import annotations

import ast
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from agent.skills import (acquire_data, core_test_data, env_freeze, evidence,
                          freeze_from_image, install_commands as ic, package_search,
                          stage_apptainer, submit_workflow)


HOSTILE = "we ird'name"          # a space AND a single quote: breaks bare and hand-quoted splices


class _CapturingEM:
    def __init__(self):
        self.commands: list[str] = []

    def run_in_env(self, env_name, cmd, timeout=None):
        self.commands.append(cmd)
        return {"returncode": 1, "stdout": ""}


def _run_sh(line: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-c", line], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def _printf_line(command: str, out: Path) -> str:
    """The wrapper-writing `printf … > /usr/local/bin/<wrap>` step of a generated install
    command, redirected to `out` so the test can read what the wrapper would contain."""
    part = next(p for p in command.split("; ") if p.startswith("printf"))
    return part.rsplit(" > ", 1)[0] + f" > {shlex.quote(str(out))}"


# ── evidence.py ───────────────────────────────────────────────────────────────

def test_cli_which_quotes_the_name():
    em = _CapturingEM()
    evidence.cli_which(em, "env", HOSTILE)
    assert em.commands == [f"which {shlex.quote(HOSTILE)} 2>/dev/null"]


def test_r_namespace_quotes_the_whole_rscript_payload():
    em = _CapturingEM()
    evidence.r_namespace(em, "env", "r-ape")
    argv = shlex.split(em.commands[0])
    assert argv[:2] == ["Rscript", "-e"] and len(argv) == 3
    assert argv[2] == ('quit(status=if(requireNamespace("ape",quietly=TRUE) || '
                       'requireNamespace("r-ape",quietly=TRUE)) 0 else 1)')


# ── package_search.py / core_test_data.py ─────────────────────────────────────

def test_conda_search_fallback_passes_argv_not_a_shell_string(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw

        class P:
            stdout = "{}"
        return P()
    monkeypatch.setattr(package_search.subprocess, "run", fake_run)
    r = package_search.PackageSearch({})._conda_search(HOSTILE, "1.0")
    assert r == {"found": False}
    assert isinstance(seen["argv"], list) and "shell" not in seen["kw"]
    assert seen["argv"][-2:] == [f"{HOSTILE}=1.0", "--json"]


def test_stream_subset_quotes_url_and_destination(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd

        class P:
            returncode = 1
        return P()
    monkeypatch.setattr(core_test_data.subprocess, "run", fake_run)
    url = "https://example.invalid/a b/x'y.fastq.gz"
    dst = tmp_path / "out dir" / "r1.fastq.gz"
    assert core_test_data._stream_subset(url, dst, 10) is False
    assert shlex.quote(url) in seen["cmd"]
    assert seen["cmd"].endswith(f"| gzip > {shlex.quote(str(dst.with_suffix('.tmp.gz')))}")
    assert "head -40" in seen["cmd"]


def test_phenopacket_to_vcf_refusal_lists_registered_ids(monkeypatch, tmp_path):
    pk = tmp_path / "core_test_data_hg38" / "phenopackets"
    pk.mkdir(parents=True)
    for pid in ("PMID_1_ACTB", "PMID_2_BRCA1"):
        (pk / f"{pid}_meta.yaml").write_text("x: 1\n")
    monkeypatch.setattr(core_test_data.workspace, "resources_root", lambda: tmp_path)
    r = core_test_data.phenopacket_to_vcf({}, "nope", str(tmp_path / "o.vcf"))
    assert r["code"] == "core_test_data.phenopacket_meta_missing"
    assert r["registered_phenopackets"] == ["PMID_1_ACTB", "PMID_2_BRCA1"]
    assert "PMID_1_ACTB" in r["error"] and "PMID_2_BRCA1" in r["error"]


def test_phenopacket_to_vcf_refusal_says_when_nothing_is_registered(monkeypatch, tmp_path):
    monkeypatch.setattr(core_test_data.workspace, "resources_root", lambda: tmp_path)
    r = core_test_data.phenopacket_to_vcf({}, "nope", str(tmp_path / "o.vcf"))
    assert r["code"] == "core_test_data.phenopacket_meta_missing"
    assert r["registered_phenopackets"] == [] and "add_phenopacket" in r["error"]


# ── env_freeze.py presence checks ─────────────────────────────────────────────

def _python_literal_in(probe: str) -> str:
    consts = [n.value for n in ast.walk(ast.parse(probe)) if isinstance(n, ast.Constant)]
    assert len(consts) == 1
    return consts[0]


def test_pip_presence_check_is_one_quoted_python_payload():
    argv = shlex.split(env_freeze._pip_presence_check(HOSTILE))
    assert argv[:2] == ["python", "-c"] and len(argv) == 3
    assert _python_literal_in(argv[2]) == HOSTILE


def test_conda_presence_check_quotes_every_name_splice():
    chk = env_freeze._conda_presence_check(HOSTILE)
    assert chk.startswith(f"command -v {shlex.quote(HOSTILE)} || ")
    shlex.split(chk)                                   # balanced — no unterminated quote
    tail = chk.rsplit(" || ", 1)[1]
    argv = shlex.split(tail)
    assert argv[:2] == ["python", "-c"] and _python_literal_in(argv[2]) == HOSTILE
    # ordinary names render without any quoting at all
    plain = env_freeze._conda_presence_check("python-louvain")
    assert plain.startswith("command -v python-louvain || ")
    assert plain.endswith('''python -c 'import importlib.metadata as _m; _m.distribution("python-louvain")\'''')


def test_r_presence_check_is_one_quoted_rscript_payload():
    argv = shlex.split(env_freeze._r_presence_check("bioconductor-de'seq"))
    assert argv[:2] == ["Rscript", "-e"] and len(argv) == 3
    assert argv[2] == ('q(status=as.integer(!(tolower("de\'seq") %in% '
                       'tolower(rownames(installed.packages())))))')


def test_unknown_install_type_refusal_lists_the_supported_types():
    r = env_freeze._map_install_spec({"name": "x", "type": "nope", "install_method": {}})
    assert r["code"] == "build.unknown_install_type"
    assert r["supported_types"] == list(env_freeze._CONTAINER_NATIVE_TYPES)
    for t in env_freeze._CONTAINER_NATIVE_TYPES:
        assert t in r["error"]


@pytest.mark.parametrize("t", env_freeze._CONTAINER_NATIVE_TYPES)
def test_every_listed_install_type_has_a_dispatch_branch(t):
    """The roster the refusal advertises must be the roster the dispatch honours: a type
    named as supported never comes back as unknown (whatever else its record lacks)."""
    r = env_freeze._map_install_spec(
        {"name": "x", "type": t, "install_method": {}},
        resolve_linux_asset=lambda *a, **k: {"found": False, "error": "stub"},
        sha256_of_url=lambda *a, **k: {"ok": False})
    assert r.get("code") != "build.unknown_install_type", r


# ── install_commands.py generators ────────────────────────────────────────────

@pytest.mark.parametrize("gen", [
    lambda: ic.release_binary(HOSTILE, f"https://x/{HOSTILE}.tar.gz", sha256="AB"),
    lambda: ic.release_binary(HOSTILE, f"https://x/{HOSTILE}", sha256="AB"),
    lambda: ic.local_artifact(HOSTILE, f"{HOSTILE}.zip", sha256="AB"),
    lambda: ic.jar(HOSTILE, f"https://x/{HOSTILE}.jar", java_flags=["-Dx=a b"]),
    lambda: ic.jar(HOSTILE, f"https://x/{HOSTILE}.zip"),
    lambda: ic.source(HOSTILE, "https://x/r", ref="v1", bin_path=HOSTILE),
    lambda: ic.script_repo(HOSTILE, "https://x/r", script_rel=f"bin/{HOSTILE}.py", interpreter="python"),
    lambda: ic.cargo(HOSTILE, binary_name=HOSTILE),
    lambda: ic.go(HOSTILE, "github.com/x/y", binary_name=HOSTILE),
    lambda: ic.pip_install_with_flags(HOSTILE, flags=["--no-binary", ":all:"]),
])
def test_generated_command_and_evidence_are_shell_balanced(gen):
    """Every line parses as shell — `bash -n` is a syntax check that runs nothing, and an
    unterminated quote fails it (shlex cannot be the oracle here: a `$(…)` nested inside
    double quotes is beyond it). The tool is named in the evidence as ONE token, and the
    install target is one token, never a bare splice of the name."""
    spec = gen()
    for line in (spec["command"], spec["evidence"]):
        r = subprocess.run(["bash", "-n", "-c", line], capture_output=True, text=True)
        assert r.returncode == 0, (line, r.stderr)
    assert any(HOSTILE in tok for tok in shlex.split(spec["evidence"]))
    if "/usr/local/bin/" in spec["command"]:
        assert f"/usr/local/bin/{shlex.quote(HOSTILE)}" in spec["command"]


def test_jar_wrapper_quotes_flags_and_jar_path_for_the_wrapper_shell(tmp_path):
    cmd = ic.jar("pic ard", "https://x/pic ard.jar", java_flags=["-Xmx2g", "-Dx=a b"])["command"]
    out = tmp_path / "wrapper"
    assert _run_sh(_printf_line(cmd, out)).returncode == 0
    assert out.read_text() == (
        '#!/bin/sh\nexec java -Xmx2g \'-Dx=a b\' -jar \'/opt/tools/pic ard/pic ard.jar\' "$@"\n')


def test_jar_zip_wrapper_takes_the_selected_jar_from_the_build_shell(tmp_path):
    cmd = ic.jar("exo", "https://x/exo.zip", java_flags=["-Xmx8g"])["command"]
    out = tmp_path / "wrapper"
    assert _run_sh(_printf_line(cmd, out), env={"JAR": "/opt/tools/exo/lib/exo-cli.jar"}).returncode == 0
    assert out.read_text() == '#!/bin/sh\nexec java -Xmx8g -jar /opt/tools/exo/lib/exo-cli.jar "$@"\n'


def test_script_repo_wrapper_quotes_the_entry_and_keeps_the_interpreter_prefix(tmp_path):
    cmd = ic.script_repo("my tool", "https://x/r", script_rel="bin/run.py", interpreter="python -u")["command"]
    out = tmp_path / "wrapper"
    assert _run_sh(_printf_line(cmd, out)).returncode == 0
    assert out.read_text() == '#!/bin/sh\nexec python -u \'/opt/tools/my tool/bin/run.py\' "$@"\n'


def test_ordinary_names_render_without_quotes():
    """shlex.quote leaves a safe token alone, so the common case reads exactly as before."""
    assert ic.perl_cpanm("Bio::DB::HTS")["evidence"] == "perl -MBio::DB::HTS -e1"
    assert ic.cargo("nanoq")["evidence"].startswith("nanoq --help ")
    j = ic.jar("picard", "https://x/picard.jar")["command"]
    assert "exec java -Xmx4g -jar /opt/tools/picard/picard.jar \"$@\"" in j
    assert j.endswith("chmod +x /usr/local/bin/picard")


def test_dq_literal_is_a_python_and_r_double_quoted_literal():
    lit = ic.dq_literal('a"b\\c')
    assert lit == '"a\\"b\\\\c"'
    assert ast.literal_eval(lit) == 'a"b\\c'


# ── freeze_from_image.py ──────────────────────────────────────────────────────

def test_self_reported_version_quotes_the_tool(monkeypatch):
    seen = []
    monkeypatch.setattr(freeze_from_image, "_run_in_image",
                        lambda image, platform, command, timeout=300: seen.append(command) or {"rc": 1, "out": ""})
    assert freeze_from_image._self_reported_version("img", "linux/amd64", HOSTILE) is None
    assert seen == [f"{shlex.quote(HOSTILE)} --version 2>&1"]


def test_local_image_tags_is_bounded_and_best_effort(monkeypatch):
    monkeypatch.setattr(freeze_from_image, "_sh",
                        lambda argv, timeout=300: {"rc": 0, "out": "b:2\n<none>:<none>\na:1\n\n", "err": ""})
    assert freeze_from_image._local_image_tags() == ["a:1", "b:2"]
    assert freeze_from_image._local_image_tags(limit=1) == ["a:1"]
    monkeypatch.setattr(freeze_from_image, "_sh",
                        lambda argv, timeout=300: {"rc": 127, "out": "", "err": "docker: not found"})
    assert freeze_from_image._local_image_tags() == []


def test_image_absent_refusal_lists_the_local_images(monkeypatch, tmp_path):
    monkeypatch.setattr(freeze_from_image, "_image_present", lambda image: False)
    monkeypatch.setattr(freeze_from_image, "_local_image_tags", lambda limit=50: ["quay.io/x/samtools:1.21"])
    r = freeze_from_image.freeze_from_image(
        image="nope:1", tools=[{"name": "t", "evidence": "t --version"}], name="n",
        env_cache=None, env_dir=tmp_path, pull_if_absent=False)
    assert r["code"] == "freeze_from_image.image_absent"
    assert r["local_images"] == ["quay.io/x/samtools:1.21"]
    assert "quay.io/x/samtools:1.21" in r["error"]


# ── remote `bash -lc` bodies: stage_apptainer / submit_workflow / acquire_data ──

def _remote_argv(cmd: str) -> list[str]:
    argv = shlex.split(cmd)
    assert argv[:2] == ["bash", "-lc"] and len(argv) == 3, cmd
    return argv


def test_remote_sif_probe_body_is_one_quoted_argument(monkeypatch, tmp_path):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv[-1])

        class P:
            stdout, returncode = "EXISTS\n", 0
        return P()
    monkeypatch.setattr(stage_apptainer.subprocess, "run", fake_run)
    sif = "/w/CLAUDE_CONTAINERS/it's a.sif"
    assert stage_apptainer._remote_sif_exists({"host": "hpc.example.invalid"}, sif) is True
    body = _remote_argv(seen[0])[2]
    assert body == f"test -f {shlex.quote(sif)} && echo EXISTS || echo MISSING"
    # the body itself runs: against a real file whose name carries the quote
    real = tmp_path / "it's a.sif"
    real.write_bytes(b"x")
    local = f"test -f {shlex.quote(str(real))} && echo EXISTS || echo MISSING"
    assert "EXISTS" in subprocess.run(["bash", "-c", local], capture_output=True, text=True).stdout


def test_inspect_cmd_body_is_one_quoted_argument():
    sif = "/w/CLAUDE_CONTAINERS/it's a.sif"
    body = _remote_argv(stage_apptainer._build_inspect_cmd(sif))[2]
    q = shlex.quote(sif)
    assert body.startswith("module load apptainer >/dev/null 2>&1 || true; ")
    assert f"if [ ! -f {q} ]; then echo SIF_MISSING; exit 3; fi; sha256sum {q} 2>/dev/null; echo ---INSPECT---; " in body
    assert body.endswith(f"apptainer inspect --json {q} 2>/dev/null || echo INSPECT_FAILED")


def test_sbatch_via_ssh_body_is_one_quoted_argument(monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv[-1])

        class P:
            stdout, stderr, returncode = "4242\n", "", 0
        return P()
    monkeypatch.setattr(submit_workflow.subprocess, "run", fake_run)
    r = submit_workflow.sbatch_via_ssh({"host": "hpc.example.invalid", "user": "u"}, "/work/u/run",
                                       sbatch_args=("--time=01:00:00",), script_args=("-resume",))
    assert r["job_id"] == "4242"
    assert _remote_argv(r["sbatch_command"])[2] == \
        "cd /work/u/run && sbatch --parsable --time=01:00:00 launcher.sh -resume"
    assert seen == [r["sbatch_command"]]


def test_cluster_probe_body_is_one_quoted_argument(monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv[-1])

        class P:
            stdout, stderr, returncode = "EXISTS=1\nSIZE=12\n", "", 0
        return P()
    monkeypatch.setattr(acquire_data.subprocess, "run", fake_run)
    path = "/data/ref dir/it's.fa"
    r = acquire_data._probe_cluster_path({"host": "hpc.example.invalid"}, path)
    assert r == {"exists": True, "size_bytes": 12, "sha256": None}
    body = _remote_argv(seen[0])[2]
    q, q_side = shlex.quote(path), shlex.quote(path + ".source.sha256")
    assert body == (f"if [ -e {q} ]; then echo EXISTS=1; else echo EXISTS=0; fi; "
                    f"if [ -e {q} ]; then du -sb {q} 2>/dev/null | cut -f1 | sed \"s/^/SIZE=/\"; fi; "
                    f"if [ -f {q_side} ]; then head -n1 {q_side} | sed \"s/^/SHA=/\"; fi")
