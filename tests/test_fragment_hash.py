"""Fragment hash, the hand-edit guard, and override check warnings."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from keystonectl.check import run_check
from keystonectl.errors import fragment_modified_message
from keystonectl.generate.compose import fragment_digest
from tests.support.deploylab import apply_package

PACKAGES = Path(__file__).resolve().parent / "data" / "packages"


def _facade() -> str:
    document = yaml.safe_load((PACKAGES / "file-mcp" / "keystone-service.yaml").read_text())
    document.pop("needs", None)
    document["containers"]["mcp"].pop("env", None)
    return yaml.safe_dump(document)


def test_fragment_sha256_written_to_lock(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    assert result.code == 0, result.error
    text = (root / "packages" / "file-mcp.yml").read_text()
    lock = yaml.safe_load((root / "services.lock.yaml").read_text())
    entry = lock["services"][0]
    assert entry["fragment_sha256"] == fragment_digest(text)
    assert entry["fragment_sha256"] == hashlib.sha256(text.encode()).hexdigest()


def test_apply_refuses_hand_edited_fragment(tmp_path: Path) -> None:
    root = tmp_path / "root"
    first, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    assert first.code == 0, first.error
    path = root / "packages" / "file-mcp.yml"
    path.write_text(path.read_text() + "# edited\n", encoding="utf-8")
    second, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()}, reset=False)
    assert second.code == 3
    assert second.error["id"] == "fragment_modified"
    assert "override" in second.error["hint"]
    assert fragment_modified_message("file-mcp") == second.error["hint"]


def test_check_warns_fragment_modified(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    path = root / "packages" / "file-mcp.yml"
    path.write_text(path.read_text() + "# edited\n", encoding="utf-8")
    result = run_check(root)
    assert any(item["id"] == "fragment_modified" for item in result.warnings)


def test_check_warns_override_not_included(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    override = root / "packages" / "file-mcp.override.yml"
    override.write_text("services: {}\n", encoding="utf-8")
    packages = (root / "docker-compose.packages.yml").read_text()
    (root / "docker-compose.packages.yml").write_text(
        packages.replace("packages/file-mcp.override.yml", "packages/file-mcp.yml"),
        encoding="utf-8",
    )
    result = run_check(root)
    assert any(item["id"] == "override_not_included" for item in result.warnings)


def test_check_warns_override_widens_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    from keystonectl.generate import checks as checks_mod

    payload = '{"services": {"file-mcp-mcp": {"privileged": true, "ports": ["0.0.0.0:8081:8081"]}}}'
    monkeypatch.setattr(checks_mod, "read_compose_config", lambda _root: (0, payload))
    monkeypatch.setattr(checks_mod, "read_compose_version", lambda: (0, "2.29.1\n"))
    result = run_check(root)
    assert any(item["id"] == "override_widens_access" for item in result.warnings)
