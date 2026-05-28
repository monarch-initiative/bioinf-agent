"""
Cheat-guard tests, organized by cheat-surface level. See tests/CHEAT_GUARDS.md
for the canonical taxonomy. Each L<N>_<topic>/ subdirectory holds the tests
that GUARD that cheat level — what the orchestrating agent must not be able
to fake.

New honesty tests go in the L-level they guard. New cheat surfaces get a row
in CHEAT_GUARDS.md first, then a directory + tests here. If a test fits no
L-level, it's probably correctness — see tests/integration/correctness/.
"""
