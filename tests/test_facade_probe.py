"""Facade health probes from the upstreams network."""

from __future__ import annotations

from pathlib import Path

from keystonectl.generate.docker import UPSTREAMS_NETWORK, facade_probe_host, facade_probe_plan


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


def test_facade_probe_plan_uses_public_domain_in_request_url(tmp_path: Path) -> None:
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
    inspected = {
        "NetworkSettings": {
            "Networks": {UPSTREAMS_NETWORK: {"IPAddress": "172.21.0.5"}},
        }
    }
    url, extra = facade_probe_plan(
        root,
        manifest,
        service="file-mcp-mcp",
        port=8081,
        healthz="/healthz",
        inspected=inspected,
    )
    assert url == "http://files.e2e.test:8081/healthz"
    assert extra == ["--add-host", "files.e2e.test:172.21.0.5"]
