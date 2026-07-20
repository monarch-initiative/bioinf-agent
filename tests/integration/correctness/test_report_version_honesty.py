"""REPORT VERSION HONESTY (audit 2026-07-19, Phase 3 / W1·W3·W5·W6).

The user's non-negotiable: an install deliverable must NEVER lie about what is
actually in the shipped image. "Worst case: it says installed 7.0.2 but actually
ships 7.0.1, and I'm not going to check the env by hand."

The silent lie lived on the AUTHORS'-IMAGE path — the one path where you most need
honesty because you did NOT build the image. When a tool is compiled from source in
the authors' Dockerfile it is ABSENT from the image SBOM, so every renderer that fell
back to the REQUESTED version printed that request as if it were the installed fact,
unlabelled, on a green validated row.

Each test here fails on the pre-fix code:
  W1  ENV.html Installed cell echoed `req_v`               -> now observed-or-absent
  W6  list_installed `resolved or pinned`                  -> now observed-or-None
  W3  GUIDE.md title/key-packages from the request spec    -> now observed
  W5  divergence (requested != observed) was never flagged -> now ⚠ on every surface

The rule the fixes encode: the installed column is sourced ONLY from observations of
the shipped image (SBOM / recorded binary / the version the tool PRINTED, htslib-safe),
never the request. Absence renders as absence. Divergence is loud.
"""
from __future__ import annotations

import re

import pytest

from env_records import shipped_binary

from agent.skills.attestation import build_attestation
from agent.skills.env_report_html import render_env_report_html
from agent.skills.resources import _semantic_versions
from agent.skills.user_guide import _version_of, render_user_guide


# ── fixtures: a freeze_from_image ADOPT of the authors' own published image ──────

def _authors_image_adopt_no_observed_version() -> dict:
    """talos compiled from source in the authors' Dockerfile: ABSENT from the SBOM,
    no biocontainer tag, and its in-image evidence prints no version-shaped token.
    The request_key carries the REQUESTED version (7.0.2). There is no honest
    installed version to show — the OLD renderers echoed the request here."""
    return {
        "name": "talos", "mode": "adopt", "build_method": "adopt-image",
        "image": "ghcr.io/populationgenomics/talos@sha256:abc123",
        "image_digest": "sha256:abc123", "content_digest": "sha256:def456",
        "platform": "linux/amd64", "created_at": "2026-07-07T00:00:00Z",
        "request_key": "talos=7.0.2|linux/amd64|none",
        "requested_tools": ["talos"],
        "resolved_packages": [{"name": "cyvcf2", "version": "0.30.0", "kind": "pypi"}],
        "verifications": [{"label": "talos", "tool": "talos", "check": "talos --help",
                           "rc": 0, "passed": True, "out": "usage: talos [-h] ..."}],
        "shipped_binaries": [shipped_binary(
            "talos", version=None, provenance="validated in the adopt-image image")],
        "validation_locus": "adopted",
    }


def _authors_image_adopt_observed_differs() -> dict:
    """Same shape, but this time the tool PRINTS a version in-image — and it is NOT
    what was requested (image ships 7.0.1, the request was 7.0.2). The OLD adopt
    branch returned `req_v` (7.0.2) and HID this divergence entirely."""
    rec = _authors_image_adopt_no_observed_version()
    rec["verifications"] = [{"label": "talos", "tool": "talos", "check": "talos --version",
                             "rc": 0, "passed": True, "out": "talos, version 7.0.1"}]
    return rec


def _clean_build_record() -> dict:
    """A container-native build where requested == installed — the no-false-alarm
    case: nothing must be flagged as diverging."""
    return {
        "name": "aln", "mode": "build", "build_method": "container-native",
        "image": "aln:1.0", "image_digest": "sha256:img", "content_digest": "sha256:c",
        "platform": "linux/amd64", "created_at": "2026-07-07T00:00:00Z",
        "request_key": "samtools=1.21|linux/amd64|none",
        "requested_tools": ["samtools"],
        "resolved_packages": [{"name": "samtools", "version": "1.21", "kind": "conda"}],
        "verifications": [{"label": "samtools", "tool": "samtools", "check": "samtools --version",
                           "rc": 0, "passed": True, "out": "samtools 1.21"}],
        "conda_specs": ["samtools=1.21"],
        "validation_locus": "native",
    }


def _tools_table(html: str) -> str:
    m = re.search(r"<h2>Tools.*?</section>", html, re.DOTALL)
    return m.group(0) if m else html


def _installed_cell(html: str, tool: str) -> str:
    """The rendered Installed-version cell (3rd <td>) for a tool row, tags stripped."""
    m = re.search(rf"<td>{tool}</td>.*?</tr>", html, re.S)
    if not m:
        return ""
    cells = re.findall(r"<td>(.*?)</td>", m.group(0), re.S)
    return re.sub(r"<[^>]+>", " ", cells[2]).strip() if len(cells) > 2 else ""


# ── W1: ENV.html never echoes the request as the installed version ──────────────

@pytest.mark.integration
def test_env_html_installed_cell_never_echoes_request_when_unobserved():
    html = render_env_report_html(_authors_image_adopt_no_observed_version())
    cell = _installed_cell(html, "talos")
    assert "7.0.2" not in cell, (
        f"the REQUESTED version leaked into the Installed cell (W1): {cell!r}")
    assert "not recorded" in cell.lower(), (
        f"absence must render as 'not recorded', got {cell!r}")
    # the request still shows in ITS OWN column
    assert "=7.0.2" in _tools_table(html)


@pytest.mark.integration
def test_env_html_installed_cell_shows_observed_version_when_the_tool_prints_one():
    html = render_env_report_html(_authors_image_adopt_observed_differs())
    cell = _installed_cell(html, "talos")
    assert "7.0.1" in cell, f"observed in-image version must be shown, got {cell!r}"


# ── W5: divergence (requested != observed) is LOUD on every surface ─────────────

@pytest.mark.integration
def test_env_html_flags_version_divergence():
    html = render_env_report_html(_authors_image_adopt_observed_differs())
    assert "≠ requested" in _installed_cell(html, "talos"), (
        "divergence flag missing from the tool row")
    assert "Version check" in html, "no at-a-glance divergence line in the header"
    # both numbers are legible together
    assert "7.0.2" in html and "7.0.1" in html


@pytest.mark.integration
def test_env_html_no_false_divergence_alarm_when_versions_match():
    # scope to the divergence-specific marker: other legitimate ⚠ pills exist
    # (e.g. the shallow "version-only" evidence-depth badge), which must NOT be
    # mistaken for a version mismatch.
    html = render_env_report_html(_clean_build_record())
    assert "≠ requested" not in html, "a false mismatch was flagged (crying wolf)"
    assert "Version check" not in html


# ── W6: list_installed_pipelines never reports the request as installed ─────────

@pytest.mark.integration
def test_list_installed_never_reports_request_as_installed():
    t = {x["tool"]: x for x in _semantic_versions(
        _authors_image_adopt_no_observed_version())}["talos"]
    assert t["installed"] is None, f"unobserved version must be None, got {t['installed']!r}"
    assert t["version"] is None, "back-compat `version` alias must also be honest (not the request)"
    assert t["requested"] == "7.0.2"
    assert t["diverges"] is False, "cannot compare against an unrecorded version — not a mismatch"


@pytest.mark.integration
def test_list_installed_flags_divergence():
    t = {x["tool"]: x for x in _semantic_versions(
        _authors_image_adopt_observed_differs())}["talos"]
    assert t["installed"] == "7.0.1"
    assert t["requested"] == "7.0.2"
    assert t["diverges"] is True


# ── W5: the attestation carries the mismatch for a downstream verifier ──────────

@pytest.mark.integration
def test_attestation_carries_version_mismatch():
    att = build_attestation(_authors_image_adopt_observed_differs())
    mm = att["predicate"]["buildDefinition"]["internalParameters"]["versionMismatch"]
    assert any(m["tool"] == "talos" and m["requested"] == "7.0.2"
               and m["installed"] == "7.0.1" for m in mm), mm


@pytest.mark.integration
def test_attestation_no_mismatch_when_versions_match():
    att = build_attestation(_clean_build_record())
    assert att["predicate"]["buildDefinition"]["internalParameters"]["versionMismatch"] == []


# ── W3: the user guide cites the OBSERVED version, not the requested spec ────────

@pytest.mark.integration
def test_guide_cites_observed_version_not_the_request():
    spec = {
        "pipeline_name": "talos", "workflow_name": "talos",
        "packages": [{"name": "talos", "version": "7.0.2"}],   # request-derived
        "pipeline_steps": [], "usage": {},
    }
    md = render_user_guide(spec, freeze_record=_authors_image_adopt_observed_differs())
    assert "talos 7.0.1" in md, "the guide title/key-packages must cite the OBSERVED version"
    assert "talos=7.0.1" in md
    assert "talos 7.0.2" not in md and "talos=7.0.2" not in md, (
        "the requested version was cited as installed (W3)")


def test_version_of_does_not_scrape_the_request_spec():
    # only the requested conda_spec is present (no resolved `version`) — the honest
    # answer is 'unknown', never the request constraint parsed back out.
    pkg = {"name": "samtools", "install_method": {"conda_spec": "samtools=1.21"}}
    assert _version_of(pkg) == "?", "scraped the requested spec string as the version (W3)"
    # a real release-binary tag IS an install fact and stays
    rel = {"name": "mosdepth", "install_method": {
        "binary_url": "https://github.com/brentp/mosdepth/releases/download/v0.3.6/mosdepth"}}
    assert _version_of(rel) == "0.3.6"
