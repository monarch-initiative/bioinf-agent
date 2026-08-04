"""I13, armed by OBSERVATION rather than by the agent's declaration.

Measured 2026-08-04. `license_gated` is an agent-authored field and `redistributable` is
derived from it (`not gated`, freeze_tools.py) — so an agent that never mentions a licence
gets `redistributable: true` for free, and `_clause_license` early-returned NOT_APPLICABLE
without examining anything. It had done that on 13 of 13 registered envs: the republication
firewall had never once been CHECKED on a real record.

Meanwhile the licence is published twice and read neither time: `api.anaconda.org` returns
it in the response `probe_conda` already fetches, and every package's `conda-meta/*.json`
carries it inside the shipped image the SBOM scan was already enumerating by filename.
bioconda's `novoalign` says "Commercial (requires license for use)" and its `sentieon` says
"…; redistribution allowed"; both have a BioContainer the resolver recommends adopting by
digest, and every identity fact about them is correct.
"""

from __future__ import annotations

import pytest

from agent.models import core_data as CD
from agent.skills import env_honesty as EH
from agent.skills.container_build import ContainerBuild, _CONDA_META_SCAN

NOVO = "Commercial (requires license for use)"
SENTIEON = "Commercial (requires license for use; redistribution allowed)"
MEDAKA = "Oxford Nanopore Technologies PLC. Public License Version 1.0"


# ---------------------------------------------------------------------------
# the reader — three states, and the third is the point
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expect", [
    # the two real commercial strings on bioconda today
    (NOVO, "restricted"),
    (SENTIEON, "restricted"),
    ("Proprietary", "restricted"),
    ("Free for academic use", "restricted"),
    # RESTRICTED WINS a tie — the half that should reach a human is the restriction
    ("GPL-3.0 with a commercial exception", "restricted"),
    # the spellings bioconda actually publishes, measured over 43 packages
    ("MIT", "permissive"), ("GPL", "permissive"), ("GPL3", "permissive"),
    ("GPLv3", "permissive"), ("GPLv2", "permissive"), ("GPL >=3", "permissive"),
    ("GPL-3.0-or-later", "permissive"), ("LGPL-3.0-only", "permissive"),
    ("BSD-3-Clause", "permissive"), ("0BSD", "permissive"),
    ("Artistic Licence", "permissive"), ("Open Software License v2.1", "permissive"),
    ("NCBI-PD", "permissive"), ("LicenseRef-Public-Domain", "permissive"),
    # we do not know, and say so
    (MEDAKA, "unrecognized"),
    ("", "unrecognized"),
])
def test_license_disposition_on_strings_that_actually_exist(text, expect):
    assert CD.license_disposition(text) == expect


@pytest.mark.parametrize("trap", ["permitted", "Transmitted", "submitted for review"])
def test_a_substring_is_not_a_licence(trap):
    """`mit` lives inside "permitted". Word-boundary matching, or every English sentence
    in a licence field reads as MIT."""
    assert CD.license_disposition(trap) == "unrecognized"


def test_absence_is_never_a_grant():
    """No licence recorded is MISSING evidence. Rounding it up to permissive is how a
    gated artifact ships; that is the same error as reading `has_description: false` as
    contrary evidence."""
    assert CD.license_disposition("") == "unrecognized"
    assert CD.license_disposition("   ") == "unrecognized"
    assert CD.restricted_packages({"resolved_packages": [
        {"name": "x", "version": "1", "kind": "conda"}]}) == []


def test_restricted_packages_reads_the_sbom_and_is_sorted():
    rec = {"resolved_packages": [
        {"name": "samtools", "version": "1.21", "kind": "conda", "license": "MIT"},
        {"name": "sentieon", "version": "202503.03", "kind": "conda", "license": SENTIEON},
        {"name": "novoalign", "version": "4.03.04", "kind": "conda", "license": NOVO},
        {"name": "medaka", "version": "1.11", "kind": "conda", "license": MEDAKA},
        "not-a-dict",
    ]}
    assert [p["name"] for p in CD.restricted_packages(rec)] == ["novoalign", "sentieon"]
    assert CD.restricted_packages(rec)[0]["license"] == NOVO


def test_a_record_with_no_sbom_licences_yields_nothing_rather_than_raising():
    """Records frozen before the scan captured licences carry no `license` key at all.
    Grandfathered, not backfilled — which is exactly why the clause reports coverage
    instead of letting an empty list read as a clean bill of health."""
    assert CD.restricted_packages({}) == []
    assert CD.restricted_packages({"resolved_packages": None}) == []


# ---------------------------------------------------------------------------
# the scan — the licence comes out of the file the SBOM already opened
# ---------------------------------------------------------------------------
def test_the_conda_meta_scan_reads_a_real_prefix(tmp_path):
    """Runs the ACTUAL shell against real conda-meta JSON, because every interesting way
    this breaks is in the sed: `"license_family"` must not match `"license"`, a greedy
    capture eats a licence's own punctuation, and `null` must yield nothing."""
    import subprocess
    meta = tmp_path / "conda-meta"
    meta.mkdir()
    (meta / "samtools-1.21-h50ea8bc_0.json").write_text(
        '{\n  "name": "samtools",\n  "license_family": "MIT",\n  "license": "MIT",\n'
        '  "files": ["bin/samtools"]\n}\n')
    (meta / "novoalign-4.03.04-h1_0.json").write_text(
        '{\n  "name": "novoalign",\n  "license": "%s"\n}\n' % NOVO)
    (meta / "nolicense-1.0-h0_0.json").write_text('{\n  "license": null\n}\n')

    out = subprocess.run(["sh", "-c", _CONDA_META_SCAN.format(ep=str(tmp_path))],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    by = {p["name"]: p for p in ContainerBuild._parse_prefix_scan(out.stdout)}

    assert by["samtools"]["license"] == "MIT"          # not license_family, same value here
    assert by["novoalign"]["license"] == NOVO          # parens + spaces survive intact
    assert by["nolicense"]["license"] == ""            # null is absence, not a grant
    assert by["samtools"]["version"] == "1.21"         # the tab did not reach the parse


def test_license_family_alone_is_not_read_as_the_licence(tmp_path):
    """`"license_family"` contains the substring `license` and sits on its own line; a
    looser pattern reports the FAMILY under the licence's name — the same class of defect
    as scraping htslib's version out of `bcftools --version`."""
    import subprocess
    meta = tmp_path / "conda-meta"
    meta.mkdir()
    (meta / "weird-1.0-h0_0.json").write_text(
        '{\n  "license_family": "GPL",\n  "name": "weird"\n}\n')
    out = subprocess.run(["sh", "-c", _CONDA_META_SCAN.format(ep=str(tmp_path))],
                         capture_output=True, text=True, timeout=60)
    by = {p["name"]: p for p in ContainerBuild._parse_prefix_scan(out.stdout)}
    assert by["weird"]["license"] == ""


# ---------------------------------------------------------------------------
# the gate — an observation cannot be un-said by an assertion
# ---------------------------------------------------------------------------
def _rec(pkgs, **kw):
    return {"resolved_packages": pkgs, **kw}


_FREE = [{"name": "samtools", "version": "1.21", "kind": "conda", "license": "MIT"}]
_GATED = _FREE + [{"name": "novoalign", "version": "4.03.04", "kind": "conda",
                   "license": NOVO}]


def test_an_undeclared_commercial_package_still_trips_i13():
    """THE HOLE. Nothing declared; `redistributable` defaults true. Before this the clause
    returned NOT_APPLICABLE and the commercial binary shipped in an image stamped
    redistributable."""
    cov, v = EH._clause_license(_rec(_GATED, redistributable=True))
    assert cov.status == EH.CHECKED
    ids = {x["invariant"] for x in v}
    assert "I13.gated_not_redistributable" in ids
    assert "I13.gated_license_recorded" in ids
    # the refusal NAMES the evidence — which package, and its own words
    assert "novoalign" in v[0]["message"] and NOVO in v[0]["message"]


def test_declaring_it_honestly_passes():
    cov, v = EH._clause_license(_rec(_GATED, redistributable=False, license_gated=True,
                                     licenses=["Novocraft commercial licence"]))
    assert cov.status == EH.CHECKED and v == []


def test_a_free_closure_with_nothing_declared_is_not_applicable():
    """The clause must not become a nuisance on the 99% case."""
    cov, v = EH._clause_license(_rec(_FREE, redistributable=True))
    assert cov.status == EH.NOT_APPLICABLE and v == []


def test_the_declared_path_is_unchanged():
    """The pre-existing behaviour — an agent declaring gated=True with a free closure —
    must fire exactly as it always did, with its original message."""
    cov, v = EH._clause_license(_rec(_FREE, redistributable=True, license_gated=True))
    ids = {x["invariant"] for x in v}
    assert ids == {"I13.gated_not_redistributable", "I13.gated_license_recorded"}
    assert "license_gated=true requires redistributable=false" in v[0]["message"]


def test_the_legacy_gated_key_still_arms_the_clause():
    """Records on disk carry `gated`, not `license_gated`; read through record_is_gated."""
    cov, v = EH._clause_license(_rec(_FREE, redistributable=True, gated=True))
    assert cov.status == EH.CHECKED and v


def test_redistribution_allowed_still_has_to_be_declared():
    """sentieon's own licence says "redistribution allowed" — and the artifact still may
    not claim it by DEFAULT. I13's rule is procedural: a gated tool discloses its terms.
    The refusal says so rather than pretending the licence forbids what it permits."""
    cov, v = EH._clause_license(_rec(
        _FREE + [{"name": "sentieon", "version": "202503.03", "kind": "conda",
                  "license": SENTIEON}], redistributable=True))
    assert cov.status == EH.CHECKED
    assert "has to be DECLARED" in v[0]["message"]


def test_an_unrecognized_licence_never_refuses_but_is_counted():
    """medaka is the measured case: a real vendor licence that asserts no restriction and
    matches no OSI identifier. Refusing on it would block a legitimate tool; rounding it
    up to permissive would hide the next novoalign. It is STATED — the same third state
    as test_data_integrity's `unanchored`."""
    cov, v = EH._clause_license(_rec(
        _FREE + [{"name": "medaka", "version": "1.11", "kind": "conda", "license": MEDAKA}],
        redistributable=True))
    assert cov.status == EH.NOT_APPLICABLE and v == []
    assert "1 unrecognised (medaka)" in cov.detail


def test_coverage_states_what_it_could_not_read():
    """"Nothing restricted" must never be readable as "every licence was understood"."""
    cov, _ = EH._clause_license(_rec(
        [{"name": "a", "version": "1", "kind": "conda", "license": "MIT"},
         {"name": "b", "version": "1", "kind": "pypi", "license": ""}],
        redistributable=True))
    assert "1/2 package licences recognised" in cov.detail
    assert "unrecognised (b)" in cov.detail


def test_a_record_with_no_closure_says_so():
    cov, _ = EH._clause_license(_rec([], redistributable=True))
    assert "no conda/pip closure" in cov.detail


# ---------------------------------------------------------------------------
# the two readings are ONE reading
# ---------------------------------------------------------------------------
def test_resolver_and_contract_read_the_licence_through_the_same_leaf():
    """The resolver classifies a licence from the REGISTRY and the contract classifies one
    OBSERVED in the image. Two spellings of the rule is how a tool ends up commercial at
    freeze and free at resolve — so both call core_data.license_disposition, and this test
    fails if either grows its own copy."""
    import inspect
    from agent.skills import resolver as R
    assert "_core_data.license_disposition" in inspect.getsource(R.identity_facts)
    assert "_core_data.restricted_packages" in inspect.getsource(EH._clause_license)
    # and neither has grown a private copy of the keyword lists
    for mod in (R, EH):
        src = inspect.getsource(mod)
        assert "_LICENSE_RESTRICTED" not in src and "_LICENSE_PERMISSIVE" not in src, (
            f"{mod.__name__} re-spells the licence keywords — they live in core_data")
