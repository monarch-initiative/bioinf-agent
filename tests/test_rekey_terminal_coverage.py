"""The re-key must be a MIGRATION, never a laundering.

`scripts/rekey_terminal_coverage.py` carries a coverage verdict across a diff instead of
re-running the 202s measurement. That is the move this repo distrusts most — I5's cluster
branch spent months "verifying" a hash it had just overwritten — so the carry is fenced by
two gates and a disclosure, and this module is what holds them shut.

The two bugs below were both found by RUNNING the script against a real diff, not by
reading it, and each has a test here that fails if the fix is reverted:

  * module-level pieces keyed by POSITION — appending one test function renumbered every
    statement after it, so an untouched constant reported as edited and the gate refused
    every realistic PR.
  * the function gate written as an all-or-nothing REFUSAL — six terminals in one edited
    function threw away 548 good verdicts, which is the whole value of the script.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "rekey_terminal_coverage", ROOT / "scripts" / "rekey_terminal_coverage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rk = _load()


# --------------------------------------------------------------- pairing identity

def test_a_terminal_that_only_moved_keeps_its_pairing_identity():
    """The entire premise. If a pure line shift changed the pair key there would be
    nothing to carry and the script would be an expensive no-op."""
    before = [{"where": "a/b.py:10", "file": "a/b.py", "func": "f", "code": "x.ok",
               "outcome": "proven", "source": "return"}]
    after = [{**before[0], "where": "a/b.py:42"}]
    assert set(rk.keyed(before)) == set(rk.keyed(after))


def test_the_same_code_twice_in_one_function_stays_two_distinct_terminals():
    """`code` alone collides 19x in the real ledger and file+func+code still collides
    12x — one function legitimately emits one code from several branches. Without the
    ordinal those rows would collapse and one verdict would be carried onto both."""
    rows = [{"where": "a/b.py:10", "file": "a/b.py", "func": "f", "code": "x.ok",
             "outcome": "proven", "source": "return"},
            {"where": "a/b.py:20", "file": "a/b.py", "func": "f", "code": "x.ok",
             "outcome": "proven", "source": "return"}]
    assert len(rk.keyed(rows)) == 2


def test_a_genuinely_different_terminal_does_not_pair():
    a = [{"where": "a/b.py:10", "file": "a/b.py", "func": "f", "code": "x.ok",
          "outcome": "proven", "source": "return"}]
    b = [{"where": "a/b.py:10", "file": "a/b.py", "func": "f", "code": "x.broke",
          "outcome": "broke", "source": "return"}]
    assert not (set(rk.keyed(a)) & set(rk.keyed(b)))


# --------------------------------------------------- the positional-key bug (real)

_BASE = '''\
import os

FULLY_TAGGED = ["a", "b"]

def existing():
    return 1
'''

_APPENDED = '''\
import os

FULLY_TAGGED = ["a", "b"]

def existing():
    return 1

def brand_new():
    return 2
'''


def test_appending_a_function_does_not_report_untouched_statements_as_edited():
    """THE BUG, verbatim. `_module_pieces` keyed module-level statements by their index
    in `tree.body`, so adding one function renumbered everything after it. The first real
    `--check` refused with `FULLY_TAGGED = [` "removed or edited" — a line nothing had
    touched. A gate that fires on every PR is a gate people delete."""
    old, new = rk._module_pieces(_BASE), rk._module_pieces(_APPENDED)
    lost = [name for name, body in old.items() if new.get(name) != body]
    assert not lost, f"untouched constructs reported as edited: {lost}"
    assert len(set(new) - set(old)) == 1, "the added function should be the only new piece"


def test_editing_an_existing_construct_is_still_caught():
    """The other half — the guard above must not have been bought by blinding it."""
    edited = _BASE.replace('FULLY_TAGGED = ["a", "b"]', 'FULLY_TAGGED = ["a"]')
    old, new = rk._module_pieces(_BASE), rk._module_pieces(edited)
    assert [n for n, b in old.items() if new.get(n) != b], \
        "an edited module-level constant must be detected"

    body_changed = _BASE.replace("return 1", "return 999")
    old2, new2 = rk._module_pieces(_BASE), rk._module_pieces(body_changed)
    assert [n for n, b in old2.items() if new2.get(n) != b], \
        "an edited function body must be detected"


def test_a_function_that_moved_but_did_not_change_is_not_an_edit():
    """The carry's whole justification: byte-identical is unchanged, wherever it sits."""
    moved = "\n\n# a new comment block\n\n" + _BASE
    old, new = rk._module_pieces(_BASE), rk._module_pieces(moved)
    assert all(new.get(n) == b for n, b in old.items()), \
        "a wholesale move must not read as an edit"


def test_an_unparseable_module_is_treated_as_fully_changed_not_as_empty():
    """Returning {} for a broken file would make every old piece 'absent but unchanged'
    by vacuity, and the gate would wave it through."""
    assert rk._module_pieces("def f(:\n  pass") != {}


# ------------------------------------------------------------- the gates, on real git

@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A throwaway git repo standing in for ROOT, so the gates run against real git
    plumbing rather than a mock of it."""
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True,
                       capture_output=True, text=True)
    git("init", "-q")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(_BASE)
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "m.py").write_text(_BASE)
    git("add", "-A")
    git("commit", "-qm", "base")
    monkeypatch.setattr(rk, "ROOT", tmp_path)
    return tmp_path


def test_suite_risk_allows_an_added_test_and_says_so(repo):
    (repo / "tests" / "test_a.py").write_text(_APPENDED)
    reason, detail = rk.suite_risk("HEAD")
    assert reason == "", f"appending a test must not block the carry: {detail}"
    assert detail, "an added test is still worth disclosing — it means we under-report"


def test_suite_risk_refuses_an_edited_test(repo):
    """Rule 3, the one the earlier carry-forward implementation did not have. Coverage is a property of
    the SUITE: an edited test can flip a terminal to dark with its function untouched, and
    carrying the old verdict would OVER-report."""
    (repo / "tests" / "test_a.py").write_text(_BASE.replace("return 1", "return 2"))
    reason, _ = rk.suite_risk("HEAD")
    assert reason, "an edited test construct must block the carry"


def test_suite_risk_refuses_a_deleted_test_file(repo):
    (repo / "tests" / "test_a.py").unlink()
    reason, _ = rk.suite_risk("HEAD")
    assert reason, "a deleted test file must block the carry"


def test_suite_risk_refuses_a_changed_non_python_test_asset(repo):
    """A fixture yaml or a marker config can change what the suite reaches, and no
    construct-level reasoning applies to it. Refuse rather than pretend."""
    (repo / "tests" / "fixture.yaml").write_text("a: 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "add fixture"], cwd=repo, check=True,
                   capture_output=True)
    (repo / "tests" / "fixture.yaml").write_text("a: 2\n")
    reason, _ = rk.suite_risk("HEAD")
    assert reason, "a changed non-python test asset must block the carry"


def test_changed_functions_ignores_a_pure_move(repo):
    (repo / "agent" / "m.py").write_text("# shifted\n\n" + _BASE)
    assert rk.changed_functions("HEAD", {"agent/m.py"}) == {}


def test_changed_functions_catches_a_real_edit(repo):
    (repo / "agent" / "m.py").write_text(_BASE.replace("return 1", "return 7"))
    assert "existing" in rk.changed_functions("HEAD", {"agent/m.py"}).get("agent/m.py", set())


# ------------------------------------------------------- carry semantics, end to end

def _ledger(rows):
    return [{"where": w, "file": f, "func": fn, "code": c, "outcome": "proven",
             "source": "return", "named_in_test": nit}
            for (w, f, fn, c, nit) in rows]


def test_the_carry_under_reports_and_never_over_reports(repo, monkeypatch):
    """The load-bearing honesty property. A terminal whose verdict cannot be transferred
    is written DARK — a coverage meter may never claim more coverage than it has — and it
    must be NAMED so the understatement is auditable rather than silent."""
    rows = [(f"agent/m.py:{10+i}", "agent/m.py", "existing", f"x.c{i}", True)
            for i in range(4)]
    (repo / "docs").mkdir()
    (repo / "docs" / "outcomes_ledger.json").write_text(json.dumps(_ledger(rows)))
    (repo / "docs" / "terminal_coverage.json").write_text(json.dumps({
        "measured_terminals": 4, "verified": 4, "exercised": 0, "dark": 0,
        "by_where": {r[0]: True for r in rows}}))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "ledger"], cwd=repo, check=True,
                   capture_output=True)
    monkeypatch.setattr(rk, "LEDGER", repo / "docs" / "outcomes_ledger.json")
    monkeypatch.setattr(rk, "OVERLAY", repo / "docs" / "terminal_coverage.json")

    # Same four terminals, all shifted down 100 lines. Nothing else changed.
    moved = [(f"agent/m.py:{110+i}", "agent/m.py", "existing", f"x.c{i}", True)
             for i in range(4)]
    rk.LEDGER.write_text(json.dumps(_ledger(moved)))

    assert rk.rekey() == 0
    out = json.loads(rk.OVERLAY.read_text())
    assert out["verified"] == 4, "a pure line-motion diff must carry every verdict intact"
    assert set(out["by_where"]) == {r[0] for r in moved}, "keys must be the NEW lines"
    cf = out["carried_forward"]
    assert cf["carried"] == 4 and cf["moved_lines"] == 4
    assert cf["measured_sha"], "the carry must keep naming where the numbers were observed"
    assert not cf["unmeasured"]


def test_a_rekey_never_claims_to_be_a_measurement(repo, monkeypatch):
    """`measured_sha` must keep pointing at the revision the numbers were OBSERVED at,
    through any number of re-keys. Advancing it would be the I5 laundering bug in a new
    costume: the artifact would claim an observation it never made."""
    (repo / "docs").mkdir()
    rows = [("agent/m.py:10", "agent/m.py", "existing", "x.c0", True)]
    (repo / "docs" / "outcomes_ledger.json").write_text(json.dumps(_ledger(rows)))
    (repo / "docs" / "terminal_coverage.json").write_text(json.dumps({
        "measured_terminals": 1, "verified": 1, "exercised": 0, "dark": 0,
        "by_where": {"agent/m.py:10": True},
        "carried_forward": {"measured_sha": "deadbee", "rekeys": 3}}))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "l"], cwd=repo, check=True, capture_output=True)
    monkeypatch.setattr(rk, "LEDGER", repo / "docs" / "outcomes_ledger.json")
    monkeypatch.setattr(rk, "OVERLAY", repo / "docs" / "terminal_coverage.json")
    rk.LEDGER.write_text(json.dumps(_ledger(
        [("agent/m.py:80", "agent/m.py", "existing", "x.c0", True)])))

    assert rk.rekey() == 0
    cf = json.loads(rk.OVERLAY.read_text())["carried_forward"]
    assert cf["measured_sha"] == "deadbee", \
        "measured_sha was advanced by a re-key — that is a claim to an unmade observation"
    assert cf["rekeys"] == 4, "the number of hops from the real measurement must accrue"
