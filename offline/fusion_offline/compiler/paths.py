"""Local pinned input and atomic compiler artifact publication."""

from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256 as hash_bytes
import os
from pathlib import Path, PureWindowsPath
import re
import stat
from tempfile import NamedTemporaryFile, TemporaryDirectory


_WINDOWS_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9\u00b9\u00b2\u00b3])")
_WINDOWS_FORBIDDEN = re.compile(r'[<>:"|?*\x00-\x1f]')


def _invalid_windows_component(part: str) -> bool:
    stem = part.partition(".")[0].rstrip(" ").upper()
    return (
        part.endswith((".", " "))
        or _WINDOWS_FORBIDDEN.search(part) is not None
        or _WINDOWS_DEVICE.fullmatch(stem) is not None
    )


def normalize_artifact_path(path: str) -> str:
    """Return a portable relative path without accepting traversal or drives."""
    portable = path.replace("\\", "/")
    parts = [part for part in portable.split("/") if part not in ("", ".")]
    if (portable.startswith("/") or PureWindowsPath(path).drive
            or any(_invalid_windows_component(part) for part in parts)):
        raise ValueError(f"artifact path must be relative and contained: {path!r}")
    normalized = "/".join(parts)
    if not normalized:
        raise ValueError("artifact path must name a file")
    return normalized


def _local_path(path: str | Path, *, require_absolute: bool = False) -> Path:
    raw = os.fspath(path)
    if raw.replace("\\", "/").startswith("//") or "\x00" in raw:
        raise ValueError(f"path must be local: {raw!r}")
    raw.encode("utf-8")
    result = Path(raw)
    if not result.is_absolute():
        if require_absolute or result.anchor:
            raise ValueError(f"path must be an absolute local path: {raw!r}")
        result = Path.cwd() / result
    if ".." in result.parts:
        raise ValueError(f"path cannot contain parent traversal: {raw!r}")
    if os.name == "nt":
        from ctypes import windll

        if (any(_invalid_windows_component(part) for part in result.parts[1:])
                or windll.kernel32.GetDriveTypeW(result.anchor) == 4):  # DRIVE_REMOTE
            raise ValueError(f"path must be on a local drive: {raw!r}")
    return result


def _checked_stat(path: Path) -> os.stat_result:
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError(f"symlink or junction is not allowed: {path}")
    return info


def _check_parents(path: Path) -> None:
    for parent in reversed(path.parents):
        if not stat.S_ISDIR(_checked_stat(parent).st_mode):
            raise ValueError(f"parent must be a directory: {parent}")


def read_pinned_file(path: str, sha256: str, max_bytes: int) -> bytes:
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        raise ValueError("pin must be a 64-hex SHA-256")
    data = _read_local_file(path, max_bytes)
    if hash_bytes(data).hexdigest() != sha256.lower():
        raise ValueError(f"pinned input SHA-256 mismatch: {path}")
    return data


def _read_local_file(path: str | Path, max_bytes: int) -> bytes:
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("size limit must be a nonnegative integer")
    source = _local_path(path, require_absolute=True)
    _check_parents(source)
    expected = _checked_stat(source)
    if not stat.S_ISREG(expected.st_mode):
        raise ValueError(f"pinned input must be a regular file: {source}")
    if expected.st_size > max_bytes:
        raise ValueError(f"pinned input exceeds size limit of {max_bytes} bytes: {source}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(source, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(expected, opened):
            raise ValueError(f"pinned input changed before reading: {source}")
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"pinned input exceeds size limit of {max_bytes} bytes: {source}")
    return data


def _check_output(path: Path, *, directory: bool = False) -> bool:
    try:
        mode = _checked_stat(path).st_mode
    except FileNotFoundError:
        return False
    if directory:
        if not stat.S_ISDIR(mode) or next(path.iterdir(), None) is not None:
            raise ValueError(f"target is not an empty directory: {path} (nonempty or non-directory)")
    elif not stat.S_ISREG(mode):
        raise ValueError(f"target is not a regular file: {path}")
    return True


def atomic_write(path: Path, data: bytes) -> None:
    path = _local_path(path)
    _check_parents(path)
    _check_output(path)
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            _check_parents(path)
            _check_output(path)
            temp.replace(path)
        finally:
            stream.close()
            temp.unlink(missing_ok=True)


@contextmanager
def atomic_directory(target: Path) -> Iterator[Path]:
    target = _local_path(target)
    _check_parents(target)
    _check_output(target, directory=True)
    with TemporaryDirectory(dir=target.parent, prefix=f".{target.name}-") as name:
        staging = Path(name)
        yield staging
        _check_parents(target)
        # Windows cannot replace an empty directory with a rename.
        if _check_output(target, directory=True) and os.name == "nt":
            target.rmdir()
        staging.rename(target)
