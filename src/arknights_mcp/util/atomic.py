"""Atomic file write helpers (§V4 atomic promotion).

Promotion swaps ``data/current.json`` atomically so a reader never observes a
half-written manifest: the payload is written to a temporary file in the *same*
directory, flushed + ``fsync``-ed, then ``os.replace``-d over the target (an
atomic rename on the same filesystem, on POSIX and Windows alike). A best-effort
directory ``fsync`` makes the rename durable where the platform supports it.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file + ``os.replace``).

    Creates the parent directory if needed. On any failure the temporary file is
    removed and the original ``path`` (if any) is left untouched.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(target.parent)


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Encode ``text`` and write it atomically via :func:`atomic_write_bytes`."""
    atomic_write_bytes(path, text.encode(encoding))


def _fsync_dir(directory: Path) -> None:
    """Best-effort ``fsync`` of a directory so a rename survives a crash.

    Silently a no-op where the platform cannot open/sync a directory handle
    (e.g. Windows); the ``os.replace`` itself is still atomic there.
    """
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent (e.g. Windows)
        return
    try:
        os.fsync(dir_fd)
    except OSError:  # pragma: no cover - platform dependent
        pass
    finally:
        os.close(dir_fd)
