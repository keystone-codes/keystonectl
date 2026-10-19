"""Packages of the deployment's own ``local/`` folder, without git (ADR 0011).

When ``<root>/local/`` exists, a source named ``local`` is always present. It is
not written in ``sources.yaml``. Each first-level folder ``local/<name>/`` that
holds ``keystone-service.yaml`` is one package with one version: the one in its
manifest.

The folder is the operator's and the agent's. keystonectl reads it and never
writes into it. A fetch reads the whole package folder under the ``package_dir``
rules (plain files and folders only, no links, 900 KiB and 256 files at most),
copies it into the package cache, and pins it by ``content_sha256``: the
``subtree_digest`` of the folder.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from packaging.version import InvalidVersion, Version

from keystonectl.cachedir import private_subdir
from keystonectl.catalog import CatalogError, Source
from keystonectl.package_dirs import MAX_BYTES, PackageDirError, read_tree, write_tree

LOCAL_SOURCE = "local"
LOCAL_DIR = "local"
LOCAL_REF = "local"
# Never a valid git URL, so a source from sources.yaml cannot look like this one.
LOCAL_URL = "local/"
MANIFEST = "keystone-service.yaml"
# O_NONBLOCK: a FIFO in place of the manifest must not hang the read.
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


class LocalPackageError(Exception):
    """The package folder cannot be used. ``messages`` holds every problem."""

    def __init__(self, error_id: str, messages: list[str]) -> None:
        super().__init__("; ".join(messages))
        self.error_id = error_id
        self.messages = list(messages)


@dataclass(frozen=True)
class LocalFetch:
    """A package folder copied into a staging directory under ``.keystonectl/tmp``."""

    tree: Path
    content_sha256: str
    manifest: dict[str, Any]


def local_root(root: Path) -> Path:
    return Path(root) / LOCAL_DIR


def local_present(root: Path) -> bool:
    """True when ``<root>/local`` is a plain directory. A link there is not followed."""

    try:
        info = os.lstat(local_root(root))
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def local_source() -> Source:
    return Source(LOCAL_SOURCE, LOCAL_URL, LOCAL_REF, -1)


def is_local_source(source: Source) -> bool:
    return source.name == LOCAL_SOURCE and source.url == LOCAL_URL


def is_local_entry(entry: Mapping[str, Any]) -> bool:
    """A lock entry installed from ``local/``: source and ref are both ``local``."""

    return entry.get("source") == LOCAL_SOURCE and entry.get("ref") == LOCAL_REF


def git_sources(sources: list[Source]) -> list[Source]:
    return [source for source in sources if not is_local_source(source)]


def with_local(root: Path, sources: list[Source]) -> list[Source]:
    """``sources`` plus the ``local`` source when ``<root>/local`` exists.

    Raises ``CatalogError`` when ``sources.yaml`` also names a source ``local``.
    """

    if not local_present(root):
        return list(sources)
    if any(source.name == LOCAL_SOURCE for source in git_sources(sources)):
        raise CatalogError(
            [
                (
                    "sources.yaml names a source local, and the name local is reserved "
                    "for the local/ folder; rename that source in sources.yaml"
                )
            ]
        )
    return [*git_sources(sources), local_source()]


def _read_manifest_file(path: Path) -> bytes:
    fd = os.open(path, _READ_FLAGS)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        if info.st_size > MAX_BYTES:
            raise OSError(f"larger than {MAX_BYTES} bytes")
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
    if len(data) > MAX_BYTES:
        raise OSError(f"larger than {MAX_BYTES} bytes")
    return data


def _parse_manifest(data: bytes, shown: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        loaded = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return None, f"{shown}: not valid YAML"
    if not isinstance(loaded, dict):
        return None, f"{shown}: must be a mapping"
    return loaded, None


def read_manifest(root: Path, name: str) -> tuple[dict[str, Any] | None, str | None]:
    """``local/<name>/keystone-service.yaml`` without following links: ``(manifest, problem)``."""

    shown = f"{LOCAL_DIR}/{name}/{MANIFEST}"
    folder = local_root(root) / name
    try:
        info = os.lstat(folder)
    except OSError:
        return None, f"{LOCAL_DIR}/{name} does not exist"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None, f"{LOCAL_DIR}/{name} is not a plain directory"
    try:
        data = _read_manifest_file(folder / MANIFEST)
    except FileNotFoundError:
        return None, f"{shown} does not exist"
    except OSError as exc:
        return None, f"{shown} cannot be read: {exc.strerror or exc}"
    return _parse_manifest(data, shown)


def _version(manifest: Mapping[str, Any]) -> str | None:
    value = manifest.get("version")
    if not isinstance(value, str):
        return None
    try:
        Version(value)
    except InvalidVersion:
        return None
    return value


def _entry(name: str, manifest: dict[str, Any] | None, problem: str | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"source": "", "path": ".", "versions": []}
    if manifest is None:
        entry["kind"] = "service"
        entry["problem"] = problem
        return entry
    kind = manifest.get("kind") if manifest.get("kind") in {"service", "resource"} else "service"
    entry["kind"] = kind
    if manifest.get("name") != name:
        entry["problem"] = (
            f"{LOCAL_DIR}/{name}/{MANIFEST}: name is {manifest.get('name')!r}; "
            f"the folder must be named after the package"
        )
    provides = manifest.get("provides")
    if kind == "resource" and isinstance(provides, dict):
        entry["provides"] = {"kind": provides.get("kind"), "version": provides.get("version")}
    version = _version(manifest)
    if version is None:
        entry.setdefault("problem", f"{LOCAL_DIR}/{name}/{MANIFEST}: version is not X.Y.Z")
        return entry
    row: dict[str, Any] = {"version": version, "ref": LOCAL_REF}
    compat = manifest.get("compat") if isinstance(manifest.get("compat"), dict) else {}
    if kind == "service" and isinstance(compat.get("keystone_mcp"), str):
        row["keystone_mcp"] = compat["keystone_mcp"]
    entry["versions"] = [row]
    return entry


def local_catalog(root: Path) -> dict[str, Any]:
    """A catalog-shaped view of ``local/``. Internal: an entry may carry ``problem``."""

    packages: dict[str, Any] = {}
    base = local_root(root)
    if not local_present(root):
        return {"name": LOCAL_SOURCE, "packages": packages}
    try:
        with os.scandir(base) as listing:
            children = sorted(listing, key=lambda item: item.name)
    except OSError:
        return {"name": LOCAL_SOURCE, "packages": packages}
    for child in children:
        if not child.is_dir(follow_symlinks=False):
            continue
        try:
            os.lstat(Path(child.path) / MANIFEST)
        except OSError:
            continue
        manifest, problem = read_manifest(root, child.name)
        packages[child.name] = _entry(child.name, manifest, problem)
    return {"name": LOCAL_SOURCE, "packages": packages}


def folder_version(root: Path, name: str) -> str | None:
    manifest, _problem = read_manifest(root, name)
    return _version(manifest) if manifest is not None and manifest.get("name") == name else None


def fetch_local(root: Path, name: str) -> LocalFetch:
    """Copy ``local/<name>`` into a new directory under ``.keystonectl/tmp``.

    The folder is read once, without following links, under the ``package_dir``
    rules; the copy and ``content_sha256`` come from that one read. Raises
    ``LocalPackageError``.
    """

    try:
        tree = read_tree(local_root(root), name, label=LOCAL_DIR)
    except PackageDirError as exc:
        raise LocalPackageError("manifest_invalid", exc.messages) from exc
    shown = f"{LOCAL_DIR}/{name}/{MANIFEST}"
    data = tree.file_bytes(MANIFEST)
    if data is None:
        raise LocalPackageError("manifest_invalid", [f"{shown} does not exist"])
    manifest, problem = _parse_manifest(data, shown)
    if manifest is None:
        raise LocalPackageError("manifest_invalid", [problem or f"{shown}: not valid"])
    if manifest.get("name") != name:
        raise LocalPackageError(
            "manifest_invalid",
            [
                (
                    f"{shown}: name is {manifest.get('name')!r}; "
                    "the folder must be named after the package"
                )
            ],
        )
    staging = Path(tempfile.mkdtemp(prefix="local-", dir=private_subdir(root, "tmp")))
    try:
        write_tree(staging, tree)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return LocalFetch(staging, tree.digest, manifest)


def changed_message(name: str, version: str) -> str:
    return (
        f"local/{name} changed, and its version is still {version}; "
        "the installed content is pinned by content_sha256"
    )


def older_message(name: str, have: str, installed: str) -> str:
    return (
        f"local/{name} holds {have}, and {installed} is installed; "
        "local package is older than the installed version"
    )


def newer_message(name: str, have: str, installed: str) -> str:
    return (
        f"local/{name} holds {have}, and {installed} is installed; "
        f"run keystonectl service upgrade {name}"
    )


def other_version_message(name: str, have: str, wanted: str) -> str:
    return f"local/{name} holds version {have}, not {wanted}; only that version can be installed"


def missing_message(name: str) -> str:
    return f"the folder local/{name} is gone; upgrade and reinstall need it, rollback does not"
