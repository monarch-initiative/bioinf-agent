"""
store_lock — the one cross-process lock for the agent's shared JSON/YAML stores.

WHY THIS EXISTS. Every shared store in this codebase does read-modify-write:

    data = self._load()      # read the whole file
    data[key] = record       # modify
    self._save(data)         # write the whole file back

`_save` is already atomic (mkstemp + fsync + `os.replace`), which prevents a
TORN file — a half-written record nobody can parse. It does nothing about a LOST
UPDATE, where two writers each read the same snapshot and the second `os.replace`
discards everything the first wrote. Atomicity and mutual exclusion are different
guarantees, and having the first is exactly what makes the absence of the second
hard to notice: the file always parses.

Measured, not theorised: 3 processes x 60 `EnvCache.register` calls left 84 of
180 records on disk. 96 frozen envs — each one a real image built at real cost —
were silently erased, with no error anywhere and a perfectly valid JSON file at
the end.

CONCURRENCY IS NOT HYPOTHETICAL HERE. `freeze(background=True)` is a shipped
feature that spawns a DETACHED SUBPROCESS per call, and that subprocess registers
into the same EnvCache and writes the same draft as its parent. Two background
freezes, or one background freeze plus any foreground work, is the documented way
to use this system — so the window is open in normal single-agent operation, not
only under some future parallel-install feature.

WHAT THIS IS
    An advisory `fcntl.flock` on a sibling `<store>.lock` file, held across the
    whole read-modify-write. Advisory means it binds the processes that ask; every
    writer in this codebase goes through the store classes, and the store classes
    ask. A reader that bypasses them can still see a stale-but-valid file, which is
    the pre-existing behaviour and is safe (`os.replace` is atomic).

WHAT THIS IS NOT
    Not a distributed lock. `flock` is per-machine, so this protects processes on
    one host, which is the only place these stores live (they are laptop-side; the
    cluster side is SLURM's problem). Not a fix for an in-memory cache that is
    never re-read — that is a separate defect and needs re-reading, not locking.

TIMEOUT. A blocked acquire retries until `timeout`, then raises `StoreLockTimeout`.
Deadlock is not silently waited out forever, and it is not silently skipped either:
a store that cannot take its lock REFUSES to write, because writing without it is
the thing that loses data. Callers surface that as a `broke` outcome.
"""
from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

#: Long enough to outlast a slow conda solve writing its record, short enough that a
#: genuinely stuck holder surfaces as an error inside one MCP call rather than hanging
#: past the agent's ~600s stream watchdog.
DEFAULT_TIMEOUT_S = 60.0

#: How often to retry a contended lock. Small enough to feel instant, large enough not
#: to spin a core.
_POLL_S = 0.02


class StoreLockTimeout(RuntimeError):
    """Raised when a store lock could not be acquired within the timeout. The caller
    must NOT proceed with the write: an unlocked read-modify-write is precisely how
    records get lost."""


def lock_path_for(store_path) -> Path:
    """The lock file that guards `store_path`.

    A SIBLING file, never the store itself. Locking the store directly does not work
    here because `_save` replaces it via `os.replace` — the new inode has no lock, and
    a second writer holding a descriptor on the OLD inode is excluded from nothing."""
    p = Path(store_path)
    return p.with_name(p.name + ".lock")


@contextmanager
def locked(store_path, *, timeout: float = DEFAULT_TIMEOUT_S, shared: bool = False):
    """Hold an advisory lock on `store_path` for the duration of the block.

        with locked(self.cache_file):
            data = self._load()
            data[key] = record
            self._save(data)

    `shared=True` takes a read lock (many readers, no writer) for a caller that must
    see a consistent snapshot without modifying it. The default is exclusive.

    Raises StoreLockTimeout if the lock cannot be taken in `timeout` seconds."""
    lp = lock_path_for(store_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    # Opened 'a' so the file is created if absent and never truncated — the lock file's
    # CONTENTS are irrelevant, only its inode's lock state matters, and truncating it
    # would be a pointless write on every acquire.
    fd = os.open(str(lp), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, flags | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise StoreLockTimeout(
                        f"could not acquire the {'shared' if shared else 'exclusive'} lock on "
                        f"{lp} within {timeout:g}s — another process is holding it. Refusing "
                        f"to read-modify-write without it: that is how records get lost.")
                time.sleep(_POLL_S)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
