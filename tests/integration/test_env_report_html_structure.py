"""
C2/C3 (batch-3 Apollo3 followups): the HTML env report is the canonical
Layer-1 view after the .md retirement. Two structural contracts the
renderer must keep:

  C2: the renderer no longer references the retired .md file (no
      'ENV.md', no 'environment_report.md', no '.md' media type
      anywhere in the rendered output).
  C3: both build AND adopt modes emit the SAME h2 section ordering:
      Tools → Along for the ride → Install commands → System packages (apt)
      → Artifacts → Declared policy → How this was verified.

      The user's rule: "all reports should have the same set of sections.
      So if there is zero that is fine. We want to know that." Install
      commands was promoted from an <h3 class="sub"> inside Along for the
      ride to its own <section> with <h2> so the section ordering is
      IDENTICAL across the two modes.

Why integration, not unit: the bug class is renderer-state drift.
render_env_report_html is a pure function over a dict, but it has many
branches (is_adopt / build mode, present/absent fields). A unit test that
checks one mode's render misses the parity-across-modes contract. This
sweep renders BOTH modes from minimal-yet-realistic records and asserts
the section invariants hold across them.
"""
from __future__ import annotations

import re

import pytest

from agent.skills.env_report_html import render_env_report_html


# The seven mandatory h2 sections, in the order they must appear.
_REQUIRED_SECTIONS = (
    "Tools",
    "Along for the ride",
    "Install commands",
    "System packages",
    "Artifacts",
    "Declared policy",
    "How this was verified",
)


def _h2_titles(html: str) -> list[str]:
    """Extract h2 inner text, stripping any nested <span>/<note> pills."""
    titles: list[str] = []
    for m in re.finditer(r"<h2[^>]*>(.*?)</h2>", html, re.DOTALL):
        inner = m.group(1)
        inner = re.sub(r"<span\b[^>]*>.*?</span>", "", inner, flags=re.DOTALL)
        inner = re.sub(r"<[^>]+>", "", inner)
        titles.append(inner.strip())
    return titles


def _build_record() -> dict:
    """A minimal BUILD-mode record exercising every required section."""
    return {
        "name": "test_env",
        "mode": "build",
        "build_method": "container-native",
        "image": "test_env:1.0",
        "image_digest": "sha256:deadbeef",
        "content_digest": "sha256:cafef00d",
        "platform": "linux/amd64",
        "created_at": "2026-05-28T00:00:00Z",
        "requested_tools": ["samtools", "bcftools"],
        "resolved_packages": [
            {"name": "samtools", "version": "1.21", "channel": "bioconda"},
            {"name": "bcftools", "version": "1.21", "channel": "bioconda"},
            {"name": "htslib",   "version": "1.21", "channel": "bioconda"},  # ride
        ],
        "system_packages": [{"name": "libgomp1", "version": "12"}],
        "verifications": [
            {"name": "samtools", "passed": True, "version": "1.21"},
            {"name": "bcftools", "passed": True, "version": "1.21"},
        ],
        "shipped_binaries": [
            {"name": "samtools (conda)", "command": "conda install ..."},
        ],
        "conda_specs": ["samtools=1.21", "bcftools=1.21"],
        "validation_locus": "native",
    }


def _adopt_record() -> dict:
    """A minimal ADOPT-mode record. Adopt has no install_commands of its
    own — the section appears with a 'zero' note so the section set is
    constant across modes."""
    return {
        "name": "test_env_adopt",
        "mode": "adopt",
        "image": "quay.io/biocontainers/samtools:1.21",
        "image_digest": "sha256:adopted",
        "content_digest": "sha256:cafef00d",
        "platform": "linux/amd64",
        "created_at": "2026-05-28T00:00:00Z",
        "requested_tools": ["samtools"],
        "resolved_packages": [
            {"name": "samtools", "version": "1.21", "channel": "bioconda"},
        ],
        "verifications": [],
        "conda_specs": ["samtools=1.21"],
        "validation_locus": "native",
    }


@pytest.mark.integration
def test_build_mode_emits_all_seven_required_sections_in_order():
    html = render_env_report_html(_build_record())
    titles = _h2_titles(html)
    # Filter to ONLY the canonical h2 titles we expect (a renderer can
    # safely add new section headers; we only assert ours are present in
    # order). matched[i] is the index where _REQUIRED_SECTIONS[i] was found.
    matched = [next((i for i, t in enumerate(titles) if t.startswith(name)), -1)
               for name in _REQUIRED_SECTIONS]
    assert all(m >= 0 for m in matched), \
        f"missing required sections in build mode: " \
        f"{[name for name, m in zip(_REQUIRED_SECTIONS, matched) if m < 0]}\nfound: {titles!r}"
    assert matched == sorted(matched), \
        f"section ordering violated in build mode: titles={titles!r}, matched={matched}"


@pytest.mark.integration
def test_adopt_mode_emits_the_same_section_set_as_build():
    """C3 contract: section set is invariant across modes."""
    html = render_env_report_html(_adopt_record())
    titles = _h2_titles(html)
    matched = [next((i for i, t in enumerate(titles) if t.startswith(name)), -1)
               for name in _REQUIRED_SECTIONS]
    assert all(m >= 0 for m in matched), \
        f"adopt mode missing required sections: " \
        f"{[name for name, m in zip(_REQUIRED_SECTIONS, matched) if m < 0]}\nfound: {titles!r}"
    assert matched == sorted(matched), \
        f"section ordering violated in adopt mode: titles={titles!r}, matched={matched}"


@pytest.mark.integration
def test_install_commands_is_its_own_top_level_section_not_a_subsection():
    """C3 specific: Install commands lives in its OWN section (<section
    class=bx><h2>), not as an <h3 class="sub"> inside Along for the ride."""
    html = render_env_report_html(_build_record())
    # Crude but effective: count Install commands appearances as h2 vs h3
    install_h2 = bool(re.search(r"<h2[^>]*>\s*Install commands", html))
    install_h3 = bool(re.search(r"<h3[^>]*>\s*Install commands", html))
    assert install_h2, "Install commands is not an <h2> (C3 contract)"
    assert not install_h3, "Install commands still appears as <h3> somewhere"


@pytest.mark.integration
def test_html_report_has_no_lingering_md_references():
    """C2: the .md env report was retired. No remaining string references
    to '.ENV.md', 'environment_report.md', or the old 'text/markdown' media
    type should appear in the rendered HTML."""
    html_build = render_env_report_html(_build_record())
    html_adopt = render_env_report_html(_adopt_record())
    for label, html in (("build", html_build), ("adopt", html_adopt)):
        assert ".ENV.md" not in html, f"{label} mode mentions .ENV.md (C2 retired)"
        assert "environment_report.md" not in html, f"{label} mode mentions retired .md"
        assert "text/markdown" not in html, f"{label} mode mentions text/markdown"


@pytest.mark.integration
def test_renderer_is_pure_no_filesystem_side_effects(tmp_path, monkeypatch):
    """The renderer is documented as pure-over-record. It must not touch
    the filesystem. Run it from inside a tmp_path cwd and assert nothing
    appears."""
    monkeypatch.chdir(tmp_path)
    _ = render_env_report_html(_build_record())
    created = list(tmp_path.iterdir())
    assert not created, f"renderer created files: {created}"
