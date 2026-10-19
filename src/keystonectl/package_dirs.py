"""Files from a package's own tree, copied for a read-only mount (ADR 0010).

``containers.<role>.package_dir`` asks that one directory of the package appears
inside that container, read-only. The manifest never names a host path: ``from``
is a path inside the package, and keystonectl decides where its copy lives.

The copy is made at ``service apply`` from the pinned package tree into
``.keystonectl/package-dirs/<name>/<version>-<hash12>``. The package cache is
not mounted: a fetch replaces it, and the directory would vanish under a
running container.

Every walk here uses ``lstat`` and never follows a link. Only plain files and
directories are accepted; a symbolic link, a hard link, a device, a FIFO or a
socket anywhere in the subtree is a problem, and so is a link on the way to it.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keystonectl.cachedir import ensure_cache_dir, private_subdir

MAX_BYTES = 900 * 1024
MAX_FILES = 256
COPIES_DIR = "package-dirs"
# The copies are read inside containers by whatever uid the image uses, so they
# are world-readable: directories 0755, files 0644. ``.keystonectl/`` itself stays
# 0700; Docker resolves the bind source as root, and the container sees only the
# copy. Neither ``cachedir._tighten`` nor the layout check touches this subtree.
DIR_MODE = 0o755
FILE_MODE = 0o644

_COPY_RE = re.compile(
    r"^\.keystonectl/package-dirs/(?P<name>[a-z][a-z0-9-]*)/"
    r"(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))-"
    r"(?P<hash>[0-9a-f]{12})$"
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
_WRITE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
)


class PackageDirError(ValueError):
    """The subtree cannot be copied. ``messages`` holds every problem."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)
        super().__init__("\n".join(self.messages))


@dataclass(frozen=True)
class _Entry:
    relative: str
    is_dir: bool
    data: bytes = b""


def declared(manifest: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """``role -> {"from", "path"}`` for every container that sets ``package_dir``."""

    containers = manifest.get("containers")
    if not isinstance(containers, dict):
        return {}
    found: dict[str, dict[str, str]] = {}
    for role, spec in containers.items():
        if not isinstance(spec, dict):
            continue
        item = spec.get("package_dir")
        if not isinstance(item, dict):
            continue
        source = item.get("from")
        target = item.get("path")
        if isinstance(source, str) and isinstance(target, str):
            found[str(role)] = {"from": source, "path": target}
    return found


def from_segments_problem(relative: str) -> str | None:
    """``from`` is ``.`` or plain segments: no empty, ``.`` or ``..`` segment."""

    if relative == ".":
        return None
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return "must be '.' or a relative path without empty, '.' or '..' segments"
    return None


def _component_problems(base: Path, relative: str, label: str) -> list[str]:
    """Each step from ``base`` to ``base/relative`` is a real directory, never a link."""

    if relative == ".":
        parts: list[str] = []
    else:
        problem = from_segments_problem(relative)
        if problem:
            return [f"{label}: {problem}"]
        parts = relative.split("/")
    current = Path(base)
    try:
        info = os.lstat(current)
    except OSError:
        return [f"{label}: the package directory does not exist"]
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return [f"{label}: the package directory is not a plain directory"]
    walked: list[str] = []
    for part in parts:
        current = current / part
        walked.append(part)
        shown = "/".join(walked)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return [f"{label}: {relative} does not exist in the package"]
        except OSError as exc:
            return [f"{label}: {shown} cannot be read: {exc.strerror or exc}"]
        if stat.S_ISLNK(info.st_mode):
            return [f"{label}: {shown} is a symbolic link; links are not allowed"]
        if not stat.S_ISDIR(info.st_mode):
            return [f"{label}: {relative} is not a directory"]
    return []


def _read_file(path: Path, expected_size: int) -> bytes:
    fd = os.open(path, _READ_FLAGS)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(f"{path.name} changed while it was read")
        chunks: list[bytes] = []
        remaining = MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) != expected_size:
        raise OSError(f"{path.name} changed while it was read")
    return data


def _scan(base: Path, relative: str, label: str) -> tuple[list[_Entry], list[str]]:
    """Walk ``base/relative`` without following links. Return entries and problems."""

    problems = _component_problems(base, relative, label)
    if problems:
        return [], problems
    top = Path(base) if relative == "." else Path(base) / relative
    entries: list[_Entry] = []
    total = 0
    files = 0
    over = False
    pending: list[tuple[Path, str]] = [(top, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            with os.scandir(directory) as listing:
                children = sorted(listing, key=lambda item: item.name)
        except OSError as exc:
            problems.append(f"{label}: {prefix or '.'} cannot be listed: {exc.strerror or exc}")
            continue
        for child in children:
            inner = f"{prefix}{child.name}"
            shown = inner if relative == "." else f"{relative}/{inner}"
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                problems.append(f"{label}: {shown} cannot be read: {exc.strerror or exc}")
                continue
            mode = info.st_mode
            if stat.S_ISLNK(mode):
                problems.append(f"{label}: {shown} is a symbolic link; links are not allowed")
            elif stat.S_ISDIR(mode):
                entries.append(_Entry(inner, True))
                pending.append((Path(child.path), f"{inner}/"))
            elif stat.S_ISREG(mode):
                if info.st_nlink != 1:
                    problems.append(f"{label}: {shown} is a hard link; links are not allowed")
                    continue
                files += 1
                total += info.st_size
                if total > MAX_BYTES or files > MAX_FILES:
                    over = True
                if over:
                    continue
                try:
                    data = _read_file(Path(child.path), info.st_size)
                except OSError as exc:
                    problems.append(f"{label}: {shown} cannot be read: {exc}")
                    continue
                entries.append(_Entry(inner, False, data))
            else:
                problems.append(
                    f"{label}: {shown} is not a regular file or directory (device, FIFO or socket)"
                )
    if total > MAX_BYTES:
        problems.append(
            f"{label}: {relative} holds {total} bytes; the limit is {MAX_BYTES} bytes (900 KiB)"
        )
    if files > MAX_FILES:
        problems.append(f"{label}: {relative} holds {files} files; the limit is {MAX_FILES}")
    entries.sort(key=lambda item: os.fsencode(item.relative))
    return entries, problems


def _digest(entries: Iterable[_Entry]) -> str:
    digest = hashlib.sha256()
    for item in sorted(entries, key=lambda entry: os.fsencode(entry.relative)):
        name = os.fsencode(item.relative)
        if item.is_dir:
            digest.update(b"D\0" + name + b"\0")
        else:
            digest.update(b"F\0" + name + b"\0" + len(item.data).to_bytes(8, "big"))
            digest.update(item.data)
    return digest.hexdigest()


def tree_problems(base: Path, relative: str, *, label: str) -> list[str]:
    """Every reason ``base/relative`` cannot be copied; empty when it can."""

    _entries, problems = _scan(base, relative, label)
    return problems


def subtree_digest(base: Path, relative: str, *, label: str = "package_dir") -> str:
    """sha256 of the subtree: each relative path, and each file's bytes, in sorted order.

    The one place this hash is computed. Raises ``PackageDirError`` when the subtree
    breaks a rule.
    """

    entries, problems = _scan(base, relative, label)
    if problems:
        raise PackageDirError(problems)
    return _digest(entries)


def copy_relative(name: str, version: str, digest: str) -> str:
    return f".keystonectl/{COPIES_DIR}/{name}/{version}-{digest[:12]}"


def is_copy_path(value: object, name: str | None = None) -> bool:
    """True for a copy path keystonectl itself writes. Anything else is never mounted."""

    if not isinstance(value, str):
        return False
    match = _COPY_RE.fullmatch(value)
    if match is None:
        return False
    return name is None or match.group("name") == name


def package_root(cache: Path, path: str) -> Path:
    return Path(cache) if path in {"", "."} else Path(cache) / path


def plan(manifest: Mapping[str, Any], cache: Path, path: str) -> dict[str, dict[str, str]]:
    """``role -> {"from", "path", "copy"}`` for this manifest. Reads; never writes.

    ``cache`` is the package cache of the pinned version and ``path`` the catalog
    subdirectory of the package in it. Raises ``PackageDirError``.
    """

    wanted = declared(manifest)
    if not wanted:
        return {}
    name = str(manifest.get("name") or "")
    version = str(manifest.get("version") or "")
    problems = _component_problems(Path(cache), path or ".", "package")
    if problems:
        raise PackageDirError(problems)
    root = package_root(cache, path)
    planned: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for role, item in wanted.items():
        label = f"containers.{role}.package_dir.from"
        try:
            digest = subtree_digest(root, item["from"], label=label)
        except PackageDirError as exc:
            errors.extend(exc.messages)
            continue
        planned[role] = {
            "from": item["from"],
            "path": item["path"],
            "copy": copy_relative(name, version, digest),
        }
    if errors:
        raise PackageDirError(errors)
    return planned


def _copy_ok(root: Path, copy: str) -> bool:
    match = _COPY_RE.fullmatch(copy)
    if match is None:
        return False
    try:
        digest = subtree_digest(Path(root) / copy, ".", label="copy")
    except PackageDirError:
        return False
    return digest[:12] == match.group("hash")


def copy_present(root: Path, copy: str) -> bool:
    """True when the copy exists, is a plain tree, and still matches its hash."""

    return _copy_ok(root, copy)


def _ensure_dir(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        path.mkdir()
        os.chmod(path, DIR_MODE)
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PackageDirError([f"{path.name} is not a plain directory"])
    if stat.S_IMODE(info.st_mode) != DIR_MODE:
        os.chmod(path, DIR_MODE)


def copies_dir(root: Path, name: str | None = None) -> Path:
    base = Path(root) / ".keystonectl" / COPIES_DIR
    return base if name is None else base / name


def _discard(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _write_tree(staging: Path, entries: list[_Entry]) -> None:
    for item in entries:
        target = staging / item.relative
        if item.is_dir:
            target.mkdir()
            os.chmod(target, DIR_MODE)
            continue
        fd = os.open(target, _WRITE_FLAGS, FILE_MODE)
        try:
            view = memoryview(item.data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fchmod(fd, FILE_MODE)
        finally:
            os.close(fd)
    os.chmod(staging, DIR_MODE)


def ensure_copies(
    root: Path,
    manifest: Mapping[str, Any],
    cache: Path,
    path: str,
    planned: Mapping[str, Mapping[str, str]],
) -> list[str]:
    """Make every planned copy. Reuse one that already matches. Return the new ones.

    The subtree is read again here, without following links, and must still hash
    to the value the plan put in the copy path.
    """

    created: list[str] = []
    try:
        _ensure_each(root, manifest, cache, path, planned, created)
    except BaseException:
        discard_copies(root, str(manifest.get("name") or ""), created)
        raise
    return created


def _ensure_each(
    root: Path,
    manifest: Mapping[str, Any],
    cache: Path,
    path: str,
    planned: Mapping[str, Mapping[str, str]],
    created: list[str],
) -> None:
    name = str(manifest.get("name") or "")
    done: set[str] = set()
    base = package_root(cache, path)
    for role, item in planned.items():
        copy = str(item.get("copy") or "")
        if copy in done:
            continue
        if not is_copy_path(copy, name):
            raise PackageDirError([f"containers.{role}.package_dir: unexpected copy path"])
        done.add(copy)
        if _copy_ok(root, copy):
            continue
        label = f"containers.{role}.package_dir.from"
        problems = _component_problems(Path(cache), path or ".", "package")
        if problems:
            raise PackageDirError(problems)
        entries, problems = _scan(base, str(item.get("from") or ""), label)
        if problems:
            raise PackageDirError(problems)
        digest = _digest(entries)
        if copy_relative(name, str(manifest.get("version") or ""), digest) != copy:
            raise PackageDirError([f"{label}: the package files changed during apply"])
        ensure_cache_dir(root)
        _ensure_dir(copies_dir(root))
        _ensure_dir(copies_dir(root, name))
        destination = Path(root) / copy
        staging = Path(tempfile.mkdtemp(prefix="package-dir-", dir=private_subdir(root, "tmp")))
        try:
            _write_tree(staging, entries)
            _discard(destination)
            staging.rename(destination)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        created.append(copy)


def referenced(entry: Mapping[str, Any]) -> set[str]:
    """Copies the lock entry still names: the current one and the previous one."""

    found: set[str] = set()
    for holder in (entry, entry.get("previous")):
        if not isinstance(holder, Mapping):
            continue
        dirs = holder.get("package_dirs")
        if not isinstance(dirs, Mapping):
            continue
        for item in dirs.values():
            if isinstance(item, Mapping) and is_copy_path(item.get("copy")):
                found.add(str(item["copy"]))
    return found


def copies_on_disk(root: Path, name: str) -> list[str]:
    directory = copies_dir(root, name)
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return []
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return []
    return sorted(f".keystonectl/{COPIES_DIR}/{name}/{child.name}" for child in directory.iterdir())


def _drop_empty(root: Path, name: str) -> None:
    directory = copies_dir(root, name)
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return
    if (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not any(directory.iterdir())
    ):
        directory.rmdir()


def prune(root: Path, name: str, keep: Iterable[str]) -> list[str]:
    """Delete every copy of ``name`` not in ``keep``. Return what was deleted."""

    kept = set(keep)
    removed: list[str] = []
    for copy in copies_on_disk(root, name):
        if copy in kept:
            continue
        _discard(Path(root) / copy)
        removed.append(copy)
    _drop_empty(root, name)
    return removed


def discard_copies(root: Path, name: str, copies: Iterable[str]) -> None:
    """Delete the named copies of ``name``, then its directory when it is empty."""

    for copy in copies:
        if is_copy_path(copy, name):
            _discard(Path(root) / copy)
    _drop_empty(root, name)


def remove_all(root: Path, name: str) -> bool:
    """Delete ``.keystonectl/package-dirs/<name>/``. Return True when it existed."""

    directory = copies_dir(root, name)
    if not os.path.lexists(directory):
        return False
    _discard(directory)
    return True


def kept_path(name: str) -> str:
    return f".keystonectl/{COPIES_DIR}/{name}"
