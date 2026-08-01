"""Nothing from the user's private cluster config may reach a TRACKED file.

This is a public repo. `projects_access.yaml` is gitignored and personal — it names a real
institution's cluster, a real account, and real filesystem paths. The standing rule is that
none of that appears in a commit message, a PR body, or any tracked artifact; the write-up
says "the cluster".

WHY A TEST AND NOT A HABIT. The rule has been broken twice and needed two scrub commits
(docs/audit_2026-07-16.md, docs/tier7_verification.md). The mechanism both times was a
human reading live run output and pasting a fragment into curated prose — so it is not a
property of any one generator, and a redaction step inside any one renderer would not have
caught either. 170 gitignored artifacts on disk carry the real name today (145 transfer
records, 17 submission manifests, 6 env reports), which is correct and is exactly why the
paste is so easy to make.

TWO THINGS THAT MAKE THIS WORK RATHER THAN JUST EXIST, both learned the hard way:

  * SCAN `git ls-files`, NEVER THE WORKTREE. The gitignored records are supposed to contain
    the real name. A worktree scan is permanently red and gets deleted within a week.
  * THE DENYLIST IS CURATED, NOT DERIVED. Sweeping every scalar out of the config and
    forbidding all of them ships a test that fails on arrival: the ssh host alias is a
    deliberately generic name documented in CLAUDE.md and 7 other tracked files, and the
    maintainer's email domain is in pyproject.toml on purpose. So every candidate that
    legitimately appears in the tree must be named in PUBLIC_BY_DESIGN with a reason. The
    allowlist is the artifact a human curates; everything else is refused by default.

Local-only by construction — CI has no config to read the tokens from, so it skips. That is
the right place for it anyway: the leak happens on the machine where the commit is written.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "projects_access.yaml"

#: Config values that are SUPPOSED to be in the tree. Each needs a reason, because each is
#: a hole in the guard and a reviewer should be able to judge it in one line.
PUBLIC_BY_DESIGN = {
    # Deliberately generic ssh alias; documented in CLAUDE.md and the bridge playbooks so a
    # reader can reproduce the ControlMaster setup. Names no institution.
    "hpc-agent",
    # The maintainer's own address, published in pyproject.toml.
    "aaron@tislab.org", "tislab.org", "aaron",
    # Project name shipped in the tracked template agent/skills/projects_access.yaml.example
    # and used by the env-inventory tests. Generic; names no institution.
    "envs_catalog",
    # The local (laptop) compute env in the zone-parity setup. Named in docs/audit_2026-07-16.md.
    "my_mac",
    # The demo project in docs/hpc_bridge_phase_b_playbook.md — a worked example, not a
    # real workspace.
    "phase_b_samtools_demo",
}

#: Tokens too short or too generic for a word-boundary scan to be meaningful. Kept
#: explicit rather than expressed as a length floor, because the institution abbreviation
#: this rule exists for is itself only three characters — a floor would have excluded the
#: single most important token.
_GENERIC = {
    "local", "ssh", "true", "false", "none", "null", "upload", "download", "exec",
    "file_name_only", "scratch", "common_data", "work", "users", "home", "proj", "data",
    "tmp", "var", "opt", "bin", "nf", "sif", "globus", "scp_head_node", "slurm",
}


#: The keys whose VALUES identify a real place or person. Deliberately a whitelist of
#: keys rather than a sweep of every scalar: `description:` is free prose the user writes,
#: and sweeping it yielded candidate "tokens" like `(inputs` — which promptly matched three
#: tracked source files and made the guard fail on its first run for no reason at all. A
#: guard whose first output is a false positive does not get a second run.
#:
#: Narrow ON PURPOSE — `path` is NOT here. Path values are mostly generic directory
#: conventions this repo documents openly (CLAUDE_CONTAINERS, CLAUDE_SCRATCH, the
#: biocontainer and nextflow dirs), and sweeping them produced 20 "leaks" of which zero
#: were leaks. Twenty false positives is not a stricter guard, it is a guard that gets
#: silenced. What is inherently identifying is WHO and WHERE: the account, the hostname,
#: the env and project names, the Globus endpoint labels. The username is also the part
#: of a real path that identifies anyone, and `user` catches it wherever it appears.
#:
#: Stated limit: an identifying string that appears ONLY as a path component and is not
#: also a name/host/user is out of scope for this guard.
_IDENTIFYING_KEYS = {
    "name", "host", "user", "account",
    "local_endpoint_display_name", "remote_endpoint_display_name", "display_name",
}

#: Identifier-shaped only. Prose, punctuation and bare numbers are not names of anything.
_TOKENISH = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{2,}$")


def _candidate_tokens(node, out: set[str], *, at_identifying_key: bool = False) -> None:
    """Every identifying string in the config, path components included."""
    if isinstance(node, dict):
        for k, v in node.items():
            _candidate_tokens(v, out,
                              at_identifying_key=str(k).lower() in _IDENTIFYING_KEYS)
    elif isinstance(node, list):
        for v in node:
            _candidate_tokens(v, out, at_identifying_key=at_identifying_key)
    elif isinstance(node, str) and at_identifying_key:
        for piece in re.split(r"[/:\s,]+", node):
            piece = piece.strip().strip(".-_")
            if piece and not piece.isdigit() and _TOKENISH.match(piece):
                out.add(piece)


#: NOT `\b`. Regex word boundaries treat `_` as a word character, so `\b<env>\b` does NOT
#: match `<env>_test` — and the project name in this very config IS the env name plus a
#: suffix. A guard built on `\b` would have sailed past the most likely spelling of the
#: leak it exists to catch. Measured while writing this: `\bbioinf\b` returns ZERO hits in
#: a repo whose every other path says `bioinf_agent`. So the boundary is spelled explicitly
#: as "not preceded/followed by an alphanumeric", which makes `_` and `-` both separators
#: while still keeping the 3-letter abbreviation from matching `function`.
#:
#: This comment is itself the proof. Its first draft used the real names as examples; the
#: guard — which had just been written, and was passing — went red the moment the file was
#: staged, and flagged BOTH, including the suffixed one that `\b` could not have seen.
def _boundary(token: str) -> str:
    return rf"(^|[^A-Za-z0-9]){re.escape(token)}([^A-Za-z0-9]|$)"


#: This file necessarily contains sentinel tokens (`zzzz…`, the bare abbreviation) as
#: string literals, so once it is tracked it matches its own probes. The SELF-TEST cases
#: therefore exclude it — but the real scan above does NOT, and that is deliberate: the
#: first draft of the boundary comment below used the true names as examples and this
#: guard caught it the moment the file was staged. Exempting the whole file would have
#: made that impossible.
_SELF = f":!{Path(__file__).relative_to(ROOT)}"


def _tracked_hits(token: str, exclude_self: bool = False) -> list[str]:
    """Lines in TRACKED files containing `token` as a whole word, case-insensitively."""
    pathspec = [".", _SELF] if exclude_self else ["."]
    r = subprocess.run(
        ["git", "grep", "-inE", _boundary(token), "--", *pathspec],
        cwd=str(ROOT), capture_output=True, text=True)
    if r.returncode not in (0, 1):          # 1 == no matches, which is the good case
        pytest.fail(f"git grep failed for a token: {r.stderr.strip()}")
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _redact(token: str) -> str:
    """Never echo the private token verbatim — a pasted test failure is how it spreads."""
    return f"{token[0]}{'*' * (len(token) - 1)} ({len(token)} chars)"


@pytest.mark.skipif(not CONFIG.exists(),
                    reason="no projects_access.yaml — nothing to leak (CI, fresh clone)")
def test_no_private_config_token_appears_in_a_tracked_file():
    yaml = pytest.importorskip("yaml")
    cfg = yaml.safe_load(CONFIG.read_text()) or {}

    tokens: set[str] = set()
    _candidate_tokens(cfg, tokens)
    tokens = {t for t in tokens
              if t.lower() not in _GENERIC
              and t not in PUBLIC_BY_DESIGN
              and t.lower() not in {p.lower() for p in PUBLIC_BY_DESIGN}
              and len(t) >= 3}

    leaks: dict[str, list[str]] = {}
    for tok in sorted(tokens):
        hits = _tracked_hits(tok)
        if hits:
            leaks[tok] = hits

    assert not leaks, (
        "private config value(s) reached TRACKED files — this repo is public:\n"
        + "\n".join(
            f"  {_redact(tok)} in {len(hits)} line(s), first: {hits[0].split(':')[0]}"
            for tok, hits in sorted(leaks.items()))
        + "\n\nEither scrub the token, or — if it is genuinely meant to be public — add it "
          "to PUBLIC_BY_DESIGN in this file WITH A REASON.")


@pytest.mark.skipif(not CONFIG.exists(), reason="no projects_access.yaml")
def test_the_guard_reads_the_index_and_not_the_worktree():
    """The guard's load-bearing property, proved BEHAVIOURALLY rather than by grepping
    its own source (the first version of this test asserted `"rglob" not in src` and
    failed on the very line that said so).

    The config file itself is the proof: it is on disk, it is full of private tokens, and
    it is gitignored. A worktree-walking scanner would find every one of them and the
    guard would be permanently red — the state in which guards get deleted."""
    yaml = pytest.importorskip("yaml")
    tokens: set[str] = set()
    _candidate_tokens(yaml.safe_load(CONFIG.read_text()) or {}, tokens)
    private = sorted(t for t in tokens if len(t) >= 6
                     and t not in PUBLIC_BY_DESIGN and t.lower() not in _GENERIC)
    assert private, "expected the config to contain at least one private-looking token"

    # Present on disk...
    on_disk = subprocess.run(["grep", "-c", private[0], str(CONFIG)],
                             capture_output=True, text=True)
    assert on_disk.stdout.strip() not in ("", "0"), \
        "test setup: the chosen token is not actually in the config file"
    # ...and invisible to the scanner, because the file is not tracked.
    assert not _tracked_hits(private[0]), \
        "the scanner saw a token that lives only in a gitignored file — it is walking " \
        "the worktree, not the index, and will be red forever"


def test_a_planted_leak_is_detected_end_to_end():
    """The whole detection path — extract → filter → scan → report — driven against a
    synthetic config whose "private" env name is a token that really is in the tree.

    Without this, everything green means only "no leak today", which is also what a
    completely inert guard says. Planting the REAL token in a tracked file to prove the
    point is obviously not an option, so the config is the thing that gets faked."""
    fake_cfg = {
        "compute_envs": [{"name": "bioinf_agent", "host": "zzzznotarealhostzzzz",
                          "user": "zzzznotarealuserzzzz",
                          "description": "prose that must not be swept for tokens"}],
        "projects": [{"name": "zzzznotarealprojectzzzz"}],
    }
    tokens: set[str] = set()
    _candidate_tokens(fake_cfg, tokens)
    assert "bioinf_agent" in tokens, "extraction missed a value at an identifying key"
    assert not any(t.startswith("prose") for t in tokens), \
        "free-prose description was swept into the denylist"

    leaks = {t: h for t in tokens if (h := _tracked_hits(t, exclude_self=True))}
    assert set(leaks) == {"bioinf_agent"}, (
        f"the guard must flag exactly the planted token and nothing else, got {set(leaks)}")
    assert _redact("bioinf_agent") == "b*********** (12 chars)", \
        "the failure message must not echo the private token verbatim"


def test_the_guard_would_actually_catch_a_leak():
    """A guard nobody has watched fire is a guard nobody knows is wired up.

    `bioinf_agent` is really in the tree; `zzzz…` is not. Note the first spelling of this
    test used the token `bioinf` and found NOTHING — which is what surfaced the `\\b`
    underscore hole above. Keeping a token WITH an underscore here is deliberate: it is
    the shape that was broken."""
    assert _tracked_hits("bioinf_agent", exclude_self=True), \
        "the scanner found nothing for a token that is definitely tracked — it is inert"
    assert not _tracked_hits("zzzznotarealtokenzzzz", exclude_self=True), \
        "the scanner reports hits for a token that does not exist"


def test_an_underscore_is_a_boundary_so_a_suffixed_leak_is_still_found():
    """The hole that `\\b` left. The project name in the private config is the env name
    plus `_test`, so the single most likely spelling of a real leak is TOKEN_something —
    precisely what `\\b` cannot see, because `_` is a word character to a regex engine."""
    hits = _tracked_hits("bioinf", exclude_self=True)
    assert hits, (
        "`bioinf` matched nothing, so the boundary reverted to \\b — a leak spelled "
        "`<token>_test` would go undetected")


def test_word_boundaries_are_used_not_substrings():
    """The trap that makes the naive version useless: a bare case-insensitive substring
    search for the three-letter institution abbreviation matches `function`, `truncate`
    and `uncached` — measured at 1370 lines across 143 tracked files. Word-anchored, it
    matches nothing. So the boundary is not a nicety, it is the difference between a
    usable guard and 1370 false positives."""
    assert not _tracked_hits("unc", exclude_self=True), (
        "a word-boundary scan for the abbreviation must be clean; if this fires, either "
        "there is a real leak or the regex lost its \\b anchors")
