"""
Integration tests organized by purpose:

  honesty/<L-level>/    — guards a CHEAT_GUARDS.md cheat-surface level.
                          What the agent must not be able to fake.
  correctness/          — capability / plumbing / wire. The system works.

Markers:
  @pytest.mark.integration         — fast tier (<6s), runs on every push.
  @pytest.mark.integration_docker  — needs a live Docker daemon. Opt-in.

See tests/CHEAT_GUARDS.md for the canonical cheat-surface taxonomy.
"""
