"""Facade health probes from the upstreams network."""

from __future__ import annotations

from pathlib import Path

import yaml

from keystonectl.generate.compose import render_fragment
from keystonectl.generate.docker import facade_probe_url
from keystonectl.generate.registry import registry_url

PACKAGES = Path(__file__).resolve().parent / "data" / "packages"


def _manifest() -> dict:
    document = yaml.safe_load((PACKAGES / "file-mcp" / "keystone-service.yaml").read_text())
    document.pop("needs", None)
    return document


def test_facade_probe_url_uses_package_alias() -> None:
    manifest = _manifest()
    assert facade_probe_url(manifest, 8081, "/healthz") == "http://file-mcp:8081/healthz"


def test_registry_and_probe_hosts_match() -> None:
    manifest = _manifest()
    probe = facade_probe_url(manifest, 8081, "/healthz")
    registry = registry_url(manifest)
    assert probe.startswith("http://file-mcp:")
    assert registry.startswith("http://file-mcp:")


def test_upstreams_network_alias_on_mcp_container() -> None:
    text = render_fragment(_manifest(), {})
    assert "file-mcp-upstreams: {aliases: [file-mcp]}" in text
