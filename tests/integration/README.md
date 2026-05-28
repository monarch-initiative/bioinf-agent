# Integration tests

Each test here exercises a real code path end-to-end against real (but minimal)
artifacts — no mocking of the boundary that the bug actually crossed.

These are the *missing middle* layer between `tests/test_invariants.py` (in-process
unit checks against constructed dicts) and the stress campaigns (`scripts/*stress*`
+ subagent runs that drive real installs through the MCP face).

## Why

Every test here was seeded by a real production bug found in stress. The unit
suite happily passed all of them; the bug existed at an interface that the
unit test had mocked. Once one of these bugs lands, an integration test
permanently nails the contract at that interface — re-discovery cost goes to
zero.

## Cost

- `@pytest.mark.integration` — fast, no Docker. <1s each. Runs on every push.
- `@pytest.mark.integration_docker` — needs a Docker daemon. EnvCache hits make
  cold ≫ warm; opt-in via `pytest -m integration_docker`.

## How to run

    pytest tests/integration/                            # fast subset
    pytest tests/integration/ -m integration_docker       # add the Docker tier
    pytest tests/ -m "integration or integration_docker"  # both layers

## What goes here

A test belongs here if **all** of these are true:

1. The behavior crosses a real interface (filesystem, subprocess, conda-meta layout,
   shell semantics, Docker daemon) where mocking would mask the bug class.
2. It can be expressed in <50 lines and runs in <1s on warm cache (Docker excepted).
3. It was — or *could have been* — caught by a real campaign and we never want to
   re-discover it.

Each test header names the finding ID (N1/N3/N4/.../F2/F4/...) it pins, so the
mapping back to the stress-campaign memories stays explicit.
