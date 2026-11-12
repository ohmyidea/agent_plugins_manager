"""Coordinate access to plugin staging directories."""

import errno
from contextlib import contextmanager
from pathlib import Path

from .tree import SyncError, exists, is_link, path_parts


@contextmanager
def plugin_lock(root: Path, name: str):
    if len(path_parts(name)) != 1:
        raise SyncError("Expected a single plugin directory name")
    scratch = root / ".tmp"
    if is_link(scratch) or (exists(scratch) and not scratch.is_dir()):
        raise SyncError(f"Expected a real directory: {scratch}")
    root.mkdir(parents=True, exist_ok=True)
    lock = scratch / f"marketplace-sync-{name}.lock"
    while True:
        try:
            scratch.mkdir()
        except FileExistsError:
            pass
        try:
            lock.mkdir()
        except FileNotFoundError:
            # Another updater may have removed the empty scratch directory.
            continue
        except FileExistsError as error:
            raise SyncError(f"Update locked for {name}: {lock}") from error
        break
    try:
        yield lock
    finally:
        if not exists(lock / "previous"):
            lock.rmdir()
            try:
                scratch.rmdir()
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                    raise
