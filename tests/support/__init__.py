"""Shared fixtures for command tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yaml

from keystonectl.runner import CommandResult, FakeCommandRunner

SHA = "a" * 40
ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "docs" / "spec" / "examples"
FIXTURES = ROOT / "tests" / "fixtures" / "layouts"

CATALOG = """\
schema_version: 1
name: local
description: Local test catalog
packages:
  file-mcp:
    kind: service
    summary: File links
    source: https://example.com/file-mcp.git
    versions:
      - version: 1.0.0
        ref: v1.0.0
        keystone_mcp: ">=0.5.0,<2.0"
      - version: 1.1.0
        ref: v1.1.0
        keystone_mcp: ">=0.5.0,<2.0"
      - version: 0.9.0
        ref: v0.9.0
        keystone_mcp: ">=0.4.0,<0.5.0"
"""

EMPTY_LOCK = """\
schema_version: 1
layout_version: 1
updated_at: "2026-10-13T00:00:00Z"
updated_by: keystonectl 0.1.0
layout: null
services: []
resources: []
"""


def catalog_text(name: str = "local", package: str = "file-mcp") -> str:
    return CATALOG.replace("name: local", f"name: {name}", 1).replace("file-mcp", package)


def write_empty_lock(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "services.lock.yaml").write_text(EMPTY_LOCK, encoding="utf-8")


def seed_catalog(root: Path, *, source: str = "local") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "sources.yaml").write_text(
        "schema_version: 1\n"
        "sources:\n"
        f"  - name: {source}\n"
        "    url: https://example.com/catalog.git\n"
        "    ref: v1.0.0\n"
        "    priority: 100\n",
        encoding="utf-8",
    )
    cache = root / ".keystonectl" / "catalogs" / source
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "catalog.yaml").write_text(catalog_text(source), encoding="utf-8")
    if not (root / "services.lock.yaml").is_file():
        write_empty_lock(root)


def file_hashes(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if not root.exists():
        return found
    for path in sorted(root.rglob("*")):
        if path.is_file():
            found[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def git_runner(catalog: str = CATALOG) -> FakeCommandRunner:
    def ls_remote(args, **_kwargs):
        last = args[-1]
        if str(last).startswith("refs/tags/"):
            tag = str(last).removeprefix("refs/tags/")
            stdout = f"{SHA}\trefs/tags/{tag}\n"
        else:
            stdout = f"{SHA}\trefs/tags/v1.0.0\n{SHA}\trefs/tags/v1.2.0\n"
        return CommandResult(tuple(args), 0, stdout, "")

    def clone(args, **_kwargs):
        dest = Path(args[-1])
        dest.mkdir(parents=True, exist_ok=True)
        url = str(args[-2])
        if "catalog" in url:
            (dest / "catalog.yaml").write_text(catalog, encoding="utf-8")
        else:
            (dest / "keystone-service.yaml").write_text("name: file-mcp\n", encoding="utf-8")
        (dest / ".git").mkdir()
        return CommandResult(tuple(args), 0, "", "")

    def init(args, **_kwargs):
        Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return CommandResult(tuple(args), 0, "", "")

    ok = CommandResult((), 0, "", "")
    return FakeCommandRunner(
        [
            (["git", "ls-remote"], ls_remote),
            (["git", "clone"], clone),
            (["git", "init"], init),
            (["git", "fetch"], ok),
            (["git", "checkout"], ok),
            (["git", "rev-parse", "HEAD"], CommandResult((), 0, SHA + "\n", "")),
        ]
    )


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def private_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, text.encode("utf-8"))
    os.close(fd)
