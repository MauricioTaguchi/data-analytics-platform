"""Streaming immutable-file checks, independent of database and dataframe imports."""
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import stat


@dataclass(frozen=True)
class VerifiedFile:
    size_bytes: int
    sha256: str
    device: int
    inode: int
    modified_ns: int

    def assert_unchanged(self, path: str | Path) -> None:
        current = Path(path).stat(follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode) or
                (current.st_size, current.st_dev, current.st_ino, current.st_mtime_ns) !=
                (self.size_bytes, self.device, self.inode, self.modified_ns)):
            raise ValueError("The snapshot changed during verification; the active dataset was not changed.")


def digest_file(path: str | Path) -> VerifiedFile:
    """Stream bytes outside account/dataset locks and detect concurrent replacement."""
    path = Path(path)
    try:
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size == 0:
            raise OSError("Snapshot is not a nonempty regular file.")
        digest = sha256()
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise OSError("Snapshot changed while opening.")
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        result = VerifiedFile(before.st_size, digest.hexdigest(), before.st_dev, before.st_ino, before.st_mtime_ns)
        result.assert_unchanged(path)
        return result
    except OSError as exc:
        raise ValueError("The previous snapshot is unavailable; the active dataset was not changed.") from exc
