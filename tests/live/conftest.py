"""Make the `live` tier genuinely opt-in — not opt-in by convention.

`integration_docker` is documented as opt-in but is still COLLECTED by a bare
`pytest tests/`; it gets away with that because a missing Docker daemon makes it skip
itself. The live tier has no such luck: the network is almost always up, so a bare
`pytest tests/` would silently start hammering PyPI / anaconda.org / CRAN / GitHub on
every run — slow, rate-limitable, and flaky in exactly the way that teaches people to
ignore a red suite.

So the marker must be ASKED FOR. `-m live` (or `-m "live or ..."`) runs it; nothing else
does. This is deliberately stricter than the surrounding convention.
"""
from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    if "live" in (config.getoption("-m") or ""):
        return                                    # explicitly requested — run them
    skip = pytest.mark.skip(reason="live registry tier — opt in with `pytest -m live`")
    for item in items:
        # `get_closest_marker`, NOT `"live" in item.keywords`. This directory is named
        # `live/`, and pytest folds every path component into an item's keywords — so the
        # keyword form matched EVERY test in this folder, including the pure corpus-integrity
        # checks that need no network and must run on every push. The directory name shadowed
        # the marker name and silently skipped 112 tests. Ask for the MARKER.
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip)
