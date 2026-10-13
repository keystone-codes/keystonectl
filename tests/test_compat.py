"""keystone-mcp version detection and package compatibility."""

from __future__ import annotations

from pathlib import Path

import yaml

from keystonectl.catalog import is_compatible
from keystonectl.compat import detect_keystone_mcp_version
from keystonectl.output import SecretRegistry
from keystonectl.plan import run_service
from keystonectl.runner import CommandResult, FakeCommandRunner
from tests.support import EXAMPLES, catalog_text, seed_catalog

IMAGE = "ghcr.io/keystone-codes/keystone-mcp:0.5.0"
DIGEST = "a" * 64


def _lock(image: str, *, version: str | None = None, build: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "layout_version": 1,
        "updated_at": "2026-10-13T00:00:00Z",
        "updated_by": "keystonectl 0.1.0",
        "layout": {
            "flavor": "custom",
            "project": "demo",
            "compose_files": ["docker-compose.yml", "docker-compose.packages.yml"],
            "keystone_service": "keystone",
            "profiles": [],
            "keystone_image": image,
            "keystone_build": build,
            "keystone_mcp_version": version,
            "env_file": ".env",
            "policy": None,
            "upstreams_network": {
                "name": "keystone-mcp-upstreams",
                "attached_by": "keystonectl",
            },
        },
        "services": [],
        "resources": [],
    }


def _write_lock(root: Path, document: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "services.lock.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )


def test_version_from_image_tag(tmp_path: Path) -> None:
    _write_lock(tmp_path, _lock(IMAGE))
    found = detect_keystone_mcp_version(
        tmp_path, lock=yaml.safe_load((tmp_path / "services.lock.yaml").read_text())
    )
    assert found.version == "0.5.0"
    assert found.via == "image"


def test_version_from_tag_with_digest(tmp_path: Path) -> None:
    image = f"{IMAGE}@sha256:{DIGEST}"
    _write_lock(tmp_path, _lock(image))
    found = detect_keystone_mcp_version(tmp_path, lock=_lock(image))
    assert found.version == "0.5.0"


def test_version_from_mirror_registry(tmp_path: Path) -> None:
    image = "registry.example/mirror/keystone-mcp:0.5.0"
    found = detect_keystone_mcp_version(tmp_path, lock=_lock(image))
    assert found.version == "0.5.0"


def test_version_from_build_context_pyproject(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "keystone-mcp"\nversion = "0.5.0"\n',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("KEYSTONE_MCP_REPO_PATH=./checkout\n", encoding="utf-8")
    lock = _lock("keystone-mcp:local", build="${KEYSTONE_MCP_REPO_PATH:-./keystone-mcp}")
    found = detect_keystone_mcp_version(tmp_path, lock=lock)
    assert found.version == "0.5.0"
    assert found.via == "build"


def test_latest_tag_asks_version(tmp_path: Path) -> None:
    from keystonectl.cli import main

    lock = _lock("keystone-mcp:latest")
    lock["services"] = [
        {
            "name": "file-mcp",
            "role": "service",
            "managed": "remote",
            "registry_name": "file-mcp",
            "namespace": "files",
            "url": "http://file-mcp:8081/mcp",
            "compat_keystone_mcp": ">=0.5.0,<2.0",
            "tools": [],
            "env": {"keystone": [], "files": [], "generated": []},
        }
    ]
    _write_lock(tmp_path, lock)
    for name in ("docker-compose.yml", "docker-compose.packages.yml"):
        (tmp_path / name).write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "packages").mkdir()
    (tmp_path / "keystonectl").mkdir()
    (tmp_path / "keystonectl" / "upstreams.yaml").write_text(
        "schema_version: 1\n"
        "entries:\n"
        "  - name: file-mcp\n"
        "    namespace: files\n"
        "    url: http://file-mcp:8081/mcp\n"
        "    auth:\n"
        "      bearer_env: KEYSTONE_MCP_UPSTREAM_FILES_BEARER\n"
        "      identity_secret_env: KEYSTONE_MCP_UPSTREAM_FILES_IDENTITY_SECRET\n",
        encoding="utf-8",
    )
    (tmp_path / "keystonectl" / "catalog-snapshot.yaml").write_text(
        "active: []\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "KEYSTONE_MCP_UPSTREAM_FILES_BEARER=bearer-placeholder\n"
        "KEYSTONE_MCP_UPSTREAM_FILES_IDENTITY_SECRET=identity-placeholder\n",
        encoding="utf-8",
    )
    lock["services"][0]["env"]["keystone"] = [
        "KEYSTONE_MCP_UPSTREAM_FILES_BEARER",
        "KEYSTONE_MCP_UPSTREAM_FILES_IDENTITY_SECRET",
    ]
    _write_lock(tmp_path, lock)
    code = main(["--json", "--root", str(tmp_path), "check"])
    assert code == 2


def test_answered_version_reused_until_image_changes(tmp_path: Path) -> None:
    lock = _lock("keystone-mcp:latest", version="0.5.0")
    found = detect_keystone_mcp_version(tmp_path, lock=lock)
    assert found.version == "0.5.0"
    assert found.via == "lock"
    changed = _lock("keystone-mcp:other")
    found = detect_keystone_mcp_version(tmp_path, lock=changed)
    assert found.version is None
    assert found.question is not None
    assert found.question.var == "KEYSTONE_SETUP_KEYSTONE_MCP_VERSION"


def test_compat_range_ok_and_refused() -> None:
    spec = ">=0.5.0,<2.0"
    assert is_compatible(spec, "0.5.0")
    assert not is_compatible(spec, "0.4.0")


def _runner(manifest: str) -> FakeCommandRunner:
    from tests.support import git_runner

    runner = git_runner()

    def clone(args, **_kwargs):
        dest = Path(args[-1])
        dest.mkdir(parents=True, exist_ok=True)
        url = str(args[-2])
        if "catalog" in url:
            (dest / "catalog.yaml").write_text(catalog_text("local"), encoding="utf-8")
        else:
            (dest / "keystone-service.yaml").write_text(manifest, encoding="utf-8")
        (dest / ".git").mkdir()
        return CommandResult(tuple(args), 0, "", "")

    runner.rules = [
        (prefix, outcome) for prefix, outcome in runner.rules if prefix != ["git", "clone"]
    ]
    runner.rules.insert(0, (["git", "clone"], clone))
    return runner


def _plan(tmp_path: Path, manifest: str, ref: str = "file-mcp@1.0.0"):
    seed_catalog(tmp_path)
    return run_service(
        tmp_path,
        "plan",
        ref,
        runner=_runner(manifest),
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"},
        interactive=False,
    )


def test_manifest_errors_become_manifest_invalid(tmp_path: Path) -> None:
    result = _plan(tmp_path, "name: file-mcp\n")
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "manifest_invalid"


def test_manifest_catalog_mismatch(tmp_path: Path) -> None:
    text = (EXAMPLES / "file-mcp.keystone-service.yaml").read_text(encoding="utf-8")
    text = text.replace('version: "1.0.0"', 'version: "1.2.0"', 1)
    result = _plan(tmp_path, text)
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "manifest_invalid"
    assert any(item["path"] == "version" for item in result.result["problems"])


def test_compat_mismatch_warns(tmp_path: Path) -> None:
    text = (EXAMPLES / "file-mcp.keystone-service.yaml").read_text(encoding="utf-8")
    text = text.replace('keystone_mcp: ">=0.5.0,<2"', 'keystone_mcp: ">=0.5.0,<1.9"', 1)
    result = _plan(tmp_path, text)
    assert result.code == 2, result.error
    assert any(item["id"] == "compat_mismatch" for item in result.warnings)
    assert any(item["var"] == "KEYSTONE_SETUP_RESOURCE_S3" for item in result.missing)


def test_requires_missing(tmp_path: Path) -> None:
    text = (EXAMPLES / "file-mcp.keystone-service.yaml").read_text(encoding="utf-8")
    text = text.replace("schema_version: 1\n", "schema_version: 1\nrequires:\n  - minio\n", 1)
    result = _plan(tmp_path, text)
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "requires_missing"
