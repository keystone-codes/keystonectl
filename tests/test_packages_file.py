"""The second Compose file and the Compose version gate."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from keystonectl.check import run_check
from keystonectl.errors import ERRORS
from keystonectl.generate.packages_file import override_is_present, render_packages_file
from tests.support.deploylab import DockerBox, apply_package

GOLDEN = Path(__file__).resolve().parent / "data" / "golden"
PACKAGES = Path(__file__).resolve().parent / "data" / "packages"


def _text(name: str) -> str:
    return (PACKAGES / name / "keystone-service.yaml").read_text()


def test_packages_file_matches_golden() -> None:
    text = render_packages_file(
        keystone_service="keystone",
        packages=[
            ("docs-service", False),
            ("file-mcp", False),
            ("hello-worker", False),
            ("minio", False),
        ],
    )
    assert text == (GOLDEN / "docker-compose.packages.yml").read_text()


def test_packages_file_empty() -> None:
    text = render_packages_file(keystone_service="keystone", packages=[])
    assert text == (GOLDEN / "docker-compose.packages.empty.yml").read_text()
    assert "include:" not in text
    assert "KEYSTONE_MCP_UPSTREAMS_PATH" in text


def test_packages_file_extends_keystone_service() -> None:
    text = render_packages_file(keystone_service="keystone", packages=[("file-mcp", False)])
    assert "KEYSTONE_MCP_UPSTREAMS_PATH: /etc/keystone-mcp/keystonectl/upstreams.yaml" in text
    assert (
        "KEYSTONE_MCP_RBAC_CATALOG_SNAPSHOT: /etc/keystone-mcp/keystonectl/catalog-snapshot.yaml"
        in text
    )
    assert "./keystonectl:/etc/keystone-mcp/keystonectl:ro" in text
    assert "keystone-mcp-upstreams" in text


def test_override_included_in_same_entry_when_exists() -> None:
    text = render_packages_file(
        keystone_service="keystone",
        packages=[("docs-service", True), ("file-mcp", False)],
    )
    assert text == (GOLDEN / "docker-compose.packages.override.yml").read_text()
    assert "packages/docs-service.yml" in text
    assert "packages/docs-service.override.yml" in text


def test_override_never_written_or_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "packages" / "docs-service.override.yml"
    path.parent.mkdir()
    payload = b"this: is not opened\n"
    path.write_bytes(payload)
    real_open = open

    def guarded(file, *args, **kwargs):
        if "override.yml" in str(file):
            raise AssertionError("override was opened")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded)
    assert override_is_present(path) is True
    render_packages_file(keystone_service="keystone", packages=[("docs-service", True)])
    assert path.read_bytes() == payload


def _facade() -> str:
    document = yaml.safe_load(_text("file-mcp"))
    document.pop("needs", None)
    document["containers"]["mcp"].pop("env", None)
    return yaml.safe_dump(document)


def test_compose_too_old(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    box = DockerBox()
    box.version = "2.23.3\n"
    result, _runner, _box = apply_package(
        tmp_path / "old", "file-mcp", {"file-mcp": _facade()}, box=box
    )
    assert result.code == 3
    assert result.error["id"] == "compose_too_old"
    assert result.retryable is False
    assert ERRORS["compose_too_old"].retryable is False

    box.version = "2.24.0\n"
    later, _runner, _box = apply_package(
        tmp_path / "new", "file-mcp", {"file-mcp": _facade()}, box=box
    )
    assert later.error is None or later.error["id"] != "compose_too_old"

    from keystonectl.generate import checks as checks_mod

    monkeypatch.setattr(checks_mod, "read_compose_version", lambda: (0, "2.23.3\n"))
    monkeypatch.setattr(checks_mod, "read_compose_config", lambda _root: (1, ""))
    root = tmp_path / "check"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    checked = run_check(root)
    assert any(
        item["id"] == "compose_version" and item["status"] == "error"
        for item in checked.result["checks"]
    )
