"""Bootstrap has ONE mode: everything upfront (ruling 2026-09-21).

`--minimal` (skip the read-dataset corpus) and setup.sh's `--full` were deleted
together: a partial bootstrap made "is this machine ready?" a two-valued
question, and the doctor, the docs and the corpus-dependent tests each answered
it differently. Now a bootstrapped clone always holds the whole corpus, so the
flag must stay gone — these tests pin both the default behavior and the
refusal of the deleted flag.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BOOTSTRAP = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_core.py"


def _load_bootstrap():
    # Import under a unique name so the __main__ guard stays False (no auto-run) and
    # there is no sys.modules collision with any real bootstrap import.
    spec = importlib.util.spec_from_file_location("bootstrap_core_undertest", _BOOTSTRAP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_steps(bc, monkeypatch, calls):
    monkeypatch.setattr(bc, "log", lambda *a, **k: None)
    monkeypatch.setattr(bc, "load_config", lambda: {"core_tools": {"env_name": "core_tools"}})
    monkeypatch.setattr(bc, "load_datasets", lambda: {})
    monkeypatch.setattr(bc, "install_core_tools",
                        lambda cfg: {"env_path": "/x", "saved_yaml": ""})
    monkeypatch.setattr(bc, "download_and_index_genome",
                        lambda cfg, gb: {"fasta": "/x/chr22.fa"})

    def _dl(*a, **k):
        calls["download_datasets"] += 1
        return {"short_read": [], "long_read": [], "pod5": [], "phenopackets": []}
    monkeypatch.setattr(bc, "download_datasets", _dl)

    def _smoke(*a, **k):
        calls["smoke_test"] += 1
        return {"passed": True, "sample": "NA12878", "subset": "chr22",
                "size_bytes": 4096}
    monkeypatch.setattr(bc, "smoke_test", _smoke)


def test_the_default_run_downloads_the_corpus_and_smokes(monkeypatch):
    bc = _load_bootstrap()
    calls = {"download_datasets": 0, "smoke_test": 0}
    _stub_steps(bc, monkeypatch, calls)
    monkeypatch.setattr(sys, "argv", ["bootstrap_core"])
    bc.main()   # returns cleanly on success (no sys.exit)
    assert calls["download_datasets"] == 1
    assert calls["smoke_test"] == 1


def test_the_minimal_flag_stays_deleted(monkeypatch):
    """A partial bootstrap is a mode nothing downstream can distinguish from a
    broken one; re-adding the flag must be a deliberate act, not a revert."""
    bc = _load_bootstrap()
    calls = {"download_datasets": 0, "smoke_test": 0}
    _stub_steps(bc, monkeypatch, calls)
    monkeypatch.setattr(sys, "argv", ["bootstrap_core", "--minimal"])
    with pytest.raises(SystemExit) as exc:
        bc.main()
    assert exc.value.code == 2
    assert calls["download_datasets"] == 0
