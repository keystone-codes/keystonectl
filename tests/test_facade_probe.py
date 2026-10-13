"""Facade health probes from the upstreams network."""

from __future__ import annotations

from pathlib import Path

from keystonectl.generate.docker import facade_probe_host


def test_facade_probe_host_reads_public_base_url(tmp_path: Path) -> None:
    root = tmp_path
    (root / "packages").mkdir()
    (root / "packages" / "file-mcp.env").write_text(
        "FILE_MCP_PUBLIC_BASE_URL=https://files.e2e.test\n", encoding="utf-8"
    )
    manifest = {
        "name": "file-mcp",
        "public_paths": ["/u/"],
        "public_base_url_env": "FILE_MCP_PUBLIC_BASE_URL",
    }
    assert facade_probe_host(root, manifest) == "files.e2e.test"


def test_facade_probe_host_absent_without_public_paths(tmp_path: Path) -> None:
    root = tmp_path
    (root / "packages").mkdir()
    (root / "packages" / "file-mcp.env").write_text(
        "FILE_MCP_PUBLIC_BASE_URL=https://files.e2e.test\n", encoding="utf-8"
    )
    manifest = {"name": "file-mcp", "public_base_url_env": "FILE_MCP_PUBLIC_BASE_URL"}
    assert facade_probe_host(root, manifest) is None
