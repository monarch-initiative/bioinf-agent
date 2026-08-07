"""`scripts/rerender_env_reports.py` — the instrument that makes a Layer-1 renderer fix reach
a page a human opens.

WHY IT EXISTS. Layer-1 deliverables are write-once: freeze() renders the ENV report, the
attestation and both recipe forms, and nothing touches them again. That is right for the
RECORDS — the EnvCache entry, `recipe.yaml` and `attestation.json` are digest-pinned
provenance. It is wrong for the VIEWS. `ENV.html` and `recipe.md` are rendered purely from
a record and have no provenance to preserve, so when the renderer is corrected every page
on disk keeps showing the old thing forever. Re-freezing does not help: a rebuild produces
a NEW record with a NEW digest, so "just re-freeze" means discarding the artifact you were
trying to correct.

Measured when this landed: the ENV report had just been taught to draw
`BuildContract.violations`, and **18 of 18 pages in the real corpus were stale**. Without
this script that fix reached zero readers, which is the same as not making it — the exact
argument `rerender_run_dashboards.py` makes for Layer 2, where a renderer fix reached 4 of
5 dashboards too late.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from env_records import env_record

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "rerender_env_reports.py"


def _run(out_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--dir", str(out_dir), *args],
        capture_output=True, text=True, timeout=180)


def _corpus(tmp_path: Path, records: list[dict]) -> Path:
    d = tmp_path / "env_reports"
    d.mkdir()
    (d / "_env_cache.json").write_text(json.dumps(
        {f"key{i}": r for i, r in enumerate(records)}))
    for r in records:
        (d / f"{r['name']}.ENV.html").write_text("<html>STALE PAGE</html>")
    return d


def test_a_stale_page_is_rewritten_from_the_record(tmp_path):
    d = _corpus(tmp_path, [env_record(name="alpha")])
    r = _run(d)
    assert r.returncode == 0, r.stdout + r.stderr
    page = (d / "alpha.ENV.html").read_text()
    assert "STALE PAGE" not in page
    assert "Bioinfo install report" in page


def test_it_is_idempotent(tmp_path):
    d = _corpus(tmp_path, [env_record(name="alpha")])
    _run(d)
    first = (d / "alpha.ENV.html").read_text()
    second_run = _run(d)
    assert "0 stale" in second_run.stdout
    assert (d / "alpha.ENV.html").read_text() == first


def test_check_writes_nothing_and_exits_nonzero_when_stale(tmp_path):
    d = _corpus(tmp_path, [env_record(name="alpha")])
    r = _run(d, "--check")
    assert r.returncode == 1, r.stdout
    assert (d / "alpha.ENV.html").read_text() == "<html>STALE PAGE</html>"
    assert "STALE" in r.stdout


def test_check_is_green_once_current(tmp_path):
    d = _corpus(tmp_path, [env_record(name="alpha")])
    _run(d)
    assert _run(d, "--check").returncode == 0


def test_it_never_touches_the_records(tmp_path):
    """The whole safety argument. A re-render restates a claim already recorded; it must
    not be able to upgrade one. If this script could rewrite `_env_cache.json`,
    `recipe.yaml` or `attestation.json`, a renderer bug would become a provenance bug."""
    d = _corpus(tmp_path, [env_record(name="alpha")])
    (d / "alpha.recipe.yaml").write_text(yaml.safe_dump(
        {"name": "alpha", "version": "1", "content_digest": "sha256:" + "ab" * 32}))
    (d / "alpha.attestation.json").write_text('{"provenance": "original"}')
    before = {p.name: p.read_bytes() for p in d.iterdir()
              if p.name in ("_env_cache.json", "alpha.recipe.yaml", "alpha.attestation.json")}
    _run(d)
    for name, blob in before.items():
        assert (d / name).read_bytes() == blob, f"{name} was modified"


def test_a_broken_recipe_render_does_not_block_the_env_report(tmp_path):
    """THE ONE THAT CHANGED THE DESIGN.

    The first cut rendered both views under one try-block, and the first real record it met
    undid it: `talos_v11`'s `shipped_binaries` uses the old key dialect, so
    `render_recipe_markdown` raises ValidationError and the WHOLE record was skipped —
    including its ENV.html, which is the page that now carries the very violation
    (`WELL_FORMED.shipped_binaries`) describing that malformation.

    The page that REPORTS a defect must not be blocked by the defect it reports."""
    rec = env_record(name="talos_like",
                     shipped_binaries=[{"command": "make install", "name": "talos",
                                        "assurance": "commit_pin", "verified": True}])
    d = _corpus(tmp_path, [rec])
    (d / "talos_like.recipe.yaml").write_text(yaml.safe_dump(
        {"name": "talos_like", "version": "1", "shipped_binaries": rec["shipped_binaries"]}))
    (d / "talos_like.recipe.md").write_text("STALE RECIPE")
    r = _run(d)
    # the ENV report was corrected…
    assert "STALE PAGE" not in (d / "talos_like.ENV.html").read_text()
    assert "FAILS the honesty contract" in (d / "talos_like.ENV.html").read_text()
    # …the unrenderable view was left alone rather than replaced by a guess…
    assert (d / "talos_like.recipe.md").read_text() == "STALE RECIPE"
    # …and the failure was NAMED, not swallowed.
    assert "would not render" in r.stdout
    assert r.returncode == 2, "an unrenderable view must not exit green"


def test_naming_an_env_that_does_not_exist_says_so(tmp_path):
    d = _corpus(tmp_path, [env_record(name="alpha")])
    r = _run(d, "ghost")
    assert "ghost" in r.stdout and "no record by that name" in r.stdout


def test_an_empty_corpus_is_not_an_error(tmp_path):
    d = tmp_path / "env_reports"
    d.mkdir()
    r = _run(d)
    assert r.returncode == 0
    assert "no freeze records" in r.stdout


@pytest.mark.parametrize("flag", ["--check", "-h"])
def test_the_flags_do_not_write(tmp_path, flag):
    """`rekey_terminal_coverage.py --help` PERFORMS ITS WRITE because an unrecognised flag
    falls through to the default action. That wart is not repeated here: argparse owns the
    flags, so `-h` prints usage and exits without touching a byte."""
    d = _corpus(tmp_path, [env_record(name="alpha")])
    before = (d / "alpha.ENV.html").read_text()
    _run(d, flag)
    assert (d / "alpha.ENV.html").read_text() == before
