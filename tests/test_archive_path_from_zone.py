"""The docker-save archive is found by zone and name, never by a remembered path.

A freeze record is immutable and the workspace is not: its zones move. The record
keeps the archive's NAME; `freeze.tarball_path` supplies the directory from the
environments zone at read time, and every reader (the ENV report's artifact row,
`stage_apptainer_image`'s local source) goes through it.
"""
from __future__ import annotations

from agent.skills import workspace
from agent.skills.env_report_html import render_env_report_html
from agent.skills.freeze import tarball_path
from env_records import env_record


def test_the_archive_lives_in_the_envs_zone_directory_under_its_recorded_name():
    rec = {"name": "rnaseq_cli", "tarball": "/somewhere/else/containers/rnaseq_cli/rnaseq_cli.tar"}
    assert tarball_path(rec) == workspace.environments_dir() / "rnaseq_cli" / "rnaseq_cli.tar"


def test_a_record_without_a_name_uses_the_archives_stem():
    assert tarball_path({"tarball": "/x/y/demo.tar"}) == workspace.environments_dir() / "demo" / "demo.tar"


def test_an_adopted_record_has_no_archive():
    assert tarball_path({"name": "x"}) is None
    assert tarball_path({"name": "x", "tarball": None}) is None


def test_the_env_report_names_the_archive_where_the_zone_holds_it():
    rec = env_record(name="demo", tarball="/retired/zone/containers/demo/demo.tar")
    html = render_env_report_html(rec)
    assert str(workspace.environments_dir() / "demo" / "demo.tar") in html
    assert "/retired/zone" not in html
