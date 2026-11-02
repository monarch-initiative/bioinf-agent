"""The freeze cache misses when the draft's install record changed since the cached build.

The request key names the tools asked for; a dependency pinned on the host env after a
build is a different environment under the same key. `freeze` stamps the install record's
digest on the record it registers and hands the draft's current one to `lookup_anchored`,
which reads a difference as a miss — so the next freeze builds again instead of answering
with the image built before the fix.
"""
from __future__ import annotations

from agent.skills import freeze as F


def _draft(*commands_and_rcs):
    return {"install_steps": [{"command": c, "returncode": rc} for c, rc in commands_and_rcs]}


def test_the_digest_reads_completed_install_commands_in_order():
    a = F.install_record_digest(_draft(("pip install spliceai==1.3.1", 0)))
    b = F.install_record_digest(_draft(("pip install spliceai==1.3.1", 0),
                                       ("conda install numpy=1.26.4", 0)))
    assert a and b and a != b
    assert F.install_record_digest(_draft(("conda install numpy=1.26.4", 0),
                                          ("pip install spliceai==1.3.1", 0))) != b


def test_a_failed_install_step_changes_nothing_and_no_draft_is_empty():
    a = F.install_record_digest(_draft(("pip install x", 0)))
    assert F.install_record_digest(_draft(("pip install x", 0), ("pip install y", 1))) == a
    assert F.install_record_digest(None) == ""
    assert F.install_record_digest({}) == ""
    assert F.install_record_digest(_draft()) == ""


def _record(**extra):
    return {"image": "demo:1.0", "image_digest": "sha256:" + "a" * 64, "image_arch": "amd64",
            "platform": "linux/amd64",
            "verifications": [{"label": "demo", "tool": "demo", "check": "demo --version",
                               "passed": True, "control": "discriminating"}],
            "shipped_binaries": [], "tool_identities": [], **extra}


def test_a_changed_install_record_is_a_miss_and_the_same_one_a_hit(tmp_path):
    cache = F.EnvCache(tmp_path / "cache.json")
    built_from = F.install_record_digest(_draft(("pip install spliceai==1.3.1", 0)))
    cache.register("k", _record(install_record_digest=built_from))
    present = lambda ref: True   # noqa: E731
    assert cache.lookup_anchored("k", present, install_record_digest=built_from) is not None
    fixed = F.install_record_digest(_draft(("pip install spliceai==1.3.1", 0),
                                           ("conda install setuptools<71", 0)))
    assert cache.lookup_anchored("k", present, install_record_digest=fixed) is None
    # a freeze with no draft compares nothing: the request alone is the key
    assert cache.lookup_anchored("k", present) is not None


def test_a_record_that_predates_the_field_hits_only_a_draft_with_no_installs(tmp_path):
    cache = F.EnvCache(tmp_path / "cache.json")
    cache.register("k", _record())
    present = lambda ref: True   # noqa: E731
    assert cache.lookup_anchored("k", present, install_record_digest="") is not None
    assert cache.lookup_anchored("k", present, install_record_digest="abc") is None
