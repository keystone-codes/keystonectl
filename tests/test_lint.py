"""keystonectl lint and package lint."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from keystonectl.cli import main
from tests.support import EXAMPLES


def _json(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict]:
    code = main(argv)
    return code, json.loads(capsys.readouterr().out)


def _package(tmp_path: Path, text: str, *, name: str = "keystone-service.yaml") -> Path:
    directory = tmp_path / "pkg"
    directory.mkdir()
    (directory / name).write_text(text, encoding="utf-8")
    return directory


def _example_text() -> str:
    return (EXAMPLES / "file-mcp.keystone-service.yaml").read_text(encoding="utf-8")


def test_lint_ok_examples(capsys: pytest.CaptureFixture[str]) -> None:
    paths = sorted(EXAMPLES.glob("*.keystone-service.yaml"))
    assert paths
    for path in paths:
        code, document = _json(capsys, ["--json", "lint", str(path)])
        assert code == 0, document
        assert document["error"] is None


def test_manifest_errors_become_problems(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _example_text().replace(
        "ghcr.io/keystone-codes/file-mcp:1.0.0@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "ghcr.io/keystone-codes/file-mcp:1.0.0",
    )
    directory = _package(tmp_path, text)
    (directory / "policy.example.yaml").write_text("roles: {}\n", encoding="utf-8")
    code, document = _json(capsys, ["--json", "lint", str(directory)])
    assert code == 3
    assert document["error"]["id"] == "manifest_invalid"
    problem = document["result"]["problems"][0]
    assert problem["path"] == "containers.mcp.image"
    assert "must be pinned by digest" in problem["message"]


def test_lint_dir_and_file_argument(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = _package(tmp_path, _example_text())
    (directory / "policy.example.yaml").write_text("roles: {}\n", encoding="utf-8")
    code, _document = _json(capsys, ["--json", "lint", str(directory)])
    assert code == 0
    code, _document = _json(capsys, ["--json", "lint", str(directory / "keystone-service.yaml")])
    assert code == 0


def test_published_name_long_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = _example_text().replace("create_upload_url", "u" * 60)
    directory = _package(tmp_path, text)
    (directory / "policy.example.yaml").write_text("roles: {}\n", encoding="utf-8")
    code, document = _json(capsys, ["--json", "lint", str(directory)])
    assert code == 0
    assert any(item["id"] == "published_name_long" for item in document["warnings"])


def test_missing_policy_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = _package(tmp_path, _example_text())
    code, document = _json(capsys, ["--json", "lint", str(directory)])
    assert code == 3
    assert any(item["path"] == "recommended_policy" for item in document["result"]["problems"])


def test_missing_provision_script(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = (EXAMPLES / "minio.keystone-service.yaml").read_text(encoding="utf-8")
    directory = _package(tmp_path, text)
    code, document = _json(capsys, ["--json", "lint", str(directory)])
    assert code == 3
    assert any("provision.script" in item["path"] for item in document["result"]["problems"])


def test_compose_file_ignored_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = _package(tmp_path, _example_text())
    (directory / "policy.example.yaml").write_text("roles: {}\n", encoding="utf-8")
    (directory / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    code, document = _json(capsys, ["--json", "lint", str(directory)])
    assert code == 0
    warning = next(item for item in document["warnings"] if item["id"] == "compose_file_ignored")
    assert "never reads it" in warning["message"].lower() or "never reads" in warning["message"]


def test_builtin_list_may_be_stale_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _package(tmp_path, _example_text())
    (directory / "policy.example.yaml").write_text("roles: {}\n", encoding="utf-8")
    code, document = _json(
        capsys, ["--json", "lint", "--keystone-mcp-version", "0.9.0", str(directory)]
    )
    assert code == 0
    warning = next(
        item for item in document["warnings"] if item["id"] == "builtin_list_may_be_stale"
    )
    assert "built-in tool names are known up to keystone-mcp 0.5.0" in warning["message"]
    assert "upstream validate during apply" in warning["message"]


def test_lint_catalog_yaml_ok_and_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.support import CATALOG

    path = tmp_path / "catalog.yaml"
    path.write_text(CATALOG, encoding="utf-8")
    code, document = _json(capsys, ["--json", "lint", str(path)])
    assert code == 0
    path.write_text("schema_version: 1\nname: bad\n", encoding="utf-8")
    code, document = _json(capsys, ["--json", "lint", str(path)])
    assert code == 3
    assert document["error"]["id"] == "catalog_invalid"


def test_package_lint_alias(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = _package(tmp_path, _example_text())
    (directory / "policy.example.yaml").write_text("roles: {}\n", encoding="utf-8")
    code, document = _json(capsys, ["--json", "package", "lint", str(directory)])
    assert code == 0
    assert document["command"] == "package lint"


def test_lint_needs_no_root(capsys: pytest.CaptureFixture[str]) -> None:
    path = EXAMPLES / "docs-service.keystone-service.yaml"
    code, document = _json(capsys, ["--json", "lint", str(path)])
    assert code == 0
    assert document["error"] is None


def test_text_output_errors_before_warnings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _example_text().replace(
        "ghcr.io/keystone-codes/file-mcp:1.0.0@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "ghcr.io/keystone-codes/file-mcp:1.0.0",
    )
    directory = _package(tmp_path, text)
    (directory / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    code = main(["lint", str(directory)])
    output = capsys.readouterr().out
    assert code == 3
    assert output.index("error ") < output.index("warning ")
    assert (
        output.strip().splitlines()[-1].endswith("warning") or "warning" in output.splitlines()[-1]
    )
