"""The pip, R and reference-database tools state their own outcome.

Each used to hand back `run_in_env`'s tag, which says only that a command ran: a
verify or functional check that failed the install left the result reading
`proven env_manager.run_in_env_ok`, and nothing on the row said which route the env
took. One code per route, success and failure, read by the honesty ledger and by an
experiment's `required_codes`.
"""
from __future__ import annotations

import agent.mcp_server as ms
from agent.mcp_tools import data_tools, env_tools


def _env_exists(name: str) -> None:
    """The install routes refuse an env that is not there; these test the install."""
    (ms._env_mgr.envs_dir / name).mkdir(parents=True, exist_ok=True)


def _fake_run_in_env(script):
    def run_in_env(env_name, command, timeout=0):
        return script(command)
    return run_in_env


def test_pip_install_that_verifies_is_pip_installed(monkeypatch):
    _env_exists("e")
    monkeypatch.setattr(ms._env_mgr, "run_in_env", _fake_run_in_env(
        lambda cmd: {"returncode": 0, "stdout": "ok", "stderr": "", "command": cmd,
                     "outcome": "proven", "code": "env_manager.run_in_env_ok"}))
    r = env_tools.install_pip_package(env_name="e", name="pysam", pipeline_id="")
    assert (r["outcome"], r["code"]) == ("proven", "env_manager.pip_installed")
    assert r["verify_command"]


def test_pip_install_whose_import_fails_is_pip_install_failed(monkeypatch):
    _env_exists("e")
    def script(cmd):
        if "pip install" in cmd:
            return {"returncode": 0, "stdout": "", "stderr": "", "command": cmd,
                    "outcome": "proven", "code": "env_manager.run_in_env_ok"}
        return {"returncode": 1, "stdout": "", "stderr": "ModuleNotFoundError", "command": cmd}
    monkeypatch.setattr(ms._env_mgr, "run_in_env", _fake_run_in_env(script))
    r = env_tools.install_pip_package(env_name="e", name="pysam", pipeline_id="")
    assert (r["outcome"], r["code"]) == ("broke", "env_manager.pip_install_failed")
    assert r["success"] is False and "verify failed" in r["stderr"]


def test_r_install_that_loads_is_r_installed(monkeypatch):
    _env_exists("e")
    monkeypatch.setattr(ms._env_mgr, "run_in_env", _fake_run_in_env(
        lambda cmd: {"returncode": 0, "stdout": "1.2.3", "stderr": "", "command": cmd,
                     "outcome": "proven", "code": "env_manager.run_in_env_ok"}))
    r = env_tools.install_r_package(env_name="e", name="DESeq2", source="bioconductor", pipeline_id="")
    assert (r["outcome"], r["code"]) == ("proven", "env_manager.r_installed")
    assert r["resolved_version"] == "1.2.3"


def test_r_install_that_does_not_load_is_r_install_failed(monkeypatch):
    _env_exists("e")
    monkeypatch.setattr(ms._env_mgr, "run_in_env", _fake_run_in_env(
        lambda cmd: {"returncode": 1, "stdout": "", "command": cmd,
                     "stderr": "Error : Package 'S4Vectors' not available after install attempt",
                     "outcome": "broke", "code": "env_manager.run_in_env_failed"}))
    r = env_tools.install_r_package(env_name="e", name="DESeq2", source="bioconductor", pipeline_id="")
    assert (r["outcome"], r["code"]) == ("broke", "env_manager.r_install_failed")
    assert r["missing_packages"] == ["S4Vectors"]


def test_reference_database_download_states_that_it_started(monkeypatch, tmp_path):
    started = {}
    def start(cmd, job_id="", tool=""):
        started.update(cmd=cmd, job_id=job_id, tool=tool)
        return {"job_id": job_id, "status_path": "s", "log_path": "l"}
    monkeypatch.setattr(ms._job_manager, "start", start)
    r = data_tools.download_reference_database(
        name="gtf", url="https://example.org/a.gtf.gz", local_path=str(tmp_path / "a.gtf.gz"))
    assert (r["outcome"], r["code"]) == ("proven", "data.refdb_download_started")
    assert r["job_id"] == started["job_id"] and started["tool"] == "download_reference_database"


def test_reference_database_download_treats_an_http_error_as_a_failed_download(monkeypatch, tmp_path):
    """A 404 page saved under the archive's name is not a reference database: the download
    must stop at curl with its own error, so nothing hashes, unpacks or resumes onto the stub."""
    started = {}
    def start(cmd, job_id="", tool=""):
        started.update(cmd=cmd, job_id=job_id, tool=tool)
        return {"job_id": job_id, "status_path": "s", "log_path": "l"}
    monkeypatch.setattr(ms._job_manager, "start", start)
    for url in ("https://example.org/2512_hg38.zip", "https://example.org/cache.tar.gz",
                "https://example.org/bundle.tar", "https://example.org/a.gtf.gz"):
        data_tools.download_reference_database(
            name="db", url=url, local_path=str(tmp_path / "db"), extract=True)
        curl = started["cmd"].split("&&")[0]
        assert "--fail" in curl, curl
        assert "-C -" in curl, "resume stays: a complete file answers 416, which --fail lets through"


def test_an_unpacked_archive_is_recorded_by_the_directory_it_unpacks_into(monkeypatch):
    """With no local_path given, the archive used to be downloaded to the very path the chain
    then tried to mkdir and unpack into, so `mkdir -p` failed on the archive after the whole
    download. The record names the unpacked directory; the archive lands beside it."""
    started = {}
    def start(cmd, job_id="", tool=""):
        started.update(cmd=cmd)
        return {"job_id": job_id, "status_path": "s", "log_path": "l"}
    monkeypatch.setattr(ms._job_manager, "start", start)

    r = data_tools.download_reference_database(
        name="exomiser_hg38", url="https://example.org/exomiser/2406_hg38.zip", extract=True)
    assert r["local_path"].endswith("/exomiser_hg38/2406_hg38")
    assert f"-o {r['local_path']}.zip " in started["cmd"]
    assert f"mkdir -p {r['local_path']} " in started["cmd"]

    r = data_tools.download_reference_database(
        name="vep", url="https://example.org/vep_cache.tar.gz", extract=True)
    assert r["local_path"].endswith("/vep/vep_cache")
    assert f"-o {r['local_path']}.tar.gz " in started["cmd"]
    assert f"-C {r['local_path']}" in started["cmd"]

    r = data_tools.download_reference_database(
        name="gtf", url="https://example.org/genes.gtf.gz", extract=True)
    assert r["local_path"].endswith("/gtf/genes.gtf.gz"), "not an archive the tool unpacks: the file keeps its name"

    r = data_tools.download_reference_database(
        name="bundle", url="https://example.org/2406_hg38.zip", extract=False)
    assert r["local_path"].endswith("/bundle/2406_hg38.zip"), "kept as an archive: recorded under its own name"
