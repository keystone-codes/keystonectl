"""Generated secrets stay out of the shared file, the lock, and command arguments."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from keystonectl.envfiles import read_env
from keystonectl.generate.tokens import token_urlsafe
from tests.support import mode
from tests.support.deploylab import apply_package

PACKAGES = Path(__file__).resolve().parent / "data" / "packages"


def _docs() -> str:
    document = yaml.safe_load((PACKAGES / "docs-service" / "keystone-service.yaml").read_text())
    document.pop("needs", None)
    return yaml.safe_dump(document)


def _resources() -> list:
    return [
        {
            "kind": "s3",
            "interface_version": 1,
            "origin": "external",
            "provider": None,
            "params": {"endpoint": "http://s3.internal", "region": "us-east-1", "path_style": True},
            "consumers": [
                {
                    "service": "docs-service",
                    "bucket": "docs",
                    "provisioned_at": "2026-10-13T00:00:00Z",
                }
            ],
        },
        {
            "kind": "postgres",
            "interface_version": 1,
            "origin": "external",
            "provider": None,
            "params": {"host": "db.internal", "port": 5432, "sslmode": "disable"},
            "consumers": [
                {
                    "service": "docs-service",
                    "database": "docs",
                    "user": "docs",
                    "extensions": ["pgcrypto"],
                    "provisioned_at": "2026-10-13T00:00:00Z",
                }
            ],
        },
    ]


def _binds() -> dict[str, str]:
    return {
        "packages/docs-service.postgres.env": "DOCS_DATABASE_URL=postgres://db/docs\n",
        "packages/docs-service.s3.env": (
            "DOCS_S3_ENDPOINT=http://s3\nDOCS_S3_REGION=us-east-1\nDOCS_S3_BUCKET=docs\n"
            "DOCS_S3_ACCESS_KEY=k\nDOCS_S3_SECRET_KEY=s\nDOCS_S3_PATH_STYLE=true\n"
        ),
    }


def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    values = iter(f"tok-{index}" for index in range(1, 30))

    def token(_nbytes: int) -> str:
        return next(values)

    monkeypatch.setattr("keystonectl.generate.install.token_urlsafe", token)


def _apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, reset: bool = True):
    _patch(monkeypatch)
    return apply_package(
        tmp_path / "root",
        "docs-service",
        {"docs-service": _docs()},
        reset=reset,
    )


def test_bearer_and_identity_generated_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _runner, _box = _apply(tmp_path, monkeypatch)
    assert result.code == 0, result.error
    mcp = read_env(tmp_path / "root" / "packages" / "docs-service.mcp.env")
    first = mcp["DOCS_MCP_BEARER"]
    again, _runner, _box = apply_package(
        tmp_path / "root",
        "docs-service",
        {"docs-service": _docs()},
        reset=False,
    )
    assert again.code == 0, again.error
    assert (
        read_env(tmp_path / "root" / "packages" / "docs-service.mcp.env")["DOCS_MCP_BEARER"]
        == first
    )


def test_identity_secret_at_least_32_bytes() -> None:
    assert len(token_urlsafe(32).encode()) >= 32


def test_same_value_for_keystone_and_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _runner, _box = _apply(tmp_path, monkeypatch)
    assert result.code == 0, result.error
    root = tmp_path / "root"
    mcp = read_env(root / "packages" / "docs-service.mcp.env")
    shared = read_env(root / ".env")
    assert mcp["DOCS_MCP_BEARER"] == shared["KEYSTONE_MCP_UPSTREAM_DOCS_BEARER"]
    assert mcp["DOCS_IDENTITY_SECRET"] == shared["KEYSTONE_MCP_UPSTREAM_DOCS_IDENTITY_SECRET"]
    assert "DOCS_MCP_BEARER" not in read_env(root / "packages" / "docs-service.env")
    partial = read_env(root / "packages" / "docs-service.api-mcp.env")
    assert partial["DOCS_INTERNAL_TOKEN"]
    assert "DOCS_INTERNAL_TOKEN" not in read_env(root / "packages" / "docs-service.env")


def test_env_files_mode_600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _apply(tmp_path, monkeypatch)
    root = tmp_path / "root"
    for name in ("docs-service.env", "docs-service.mcp.env", "docs-service.api-mcp.env"):
        assert mode(root / "packages" / name) == 0o600
    assert mode(root / ".env") == 0o600


def test_required_secret_is_file_question(tmp_path: Path) -> None:
    document = yaml.safe_load((PACKAGES / "hello-worker" / "keystone-service.yaml").read_text())
    document.pop("needs", None)
    document["containers"]["worker"]["secrets"] = [{"name": "HELLO_TOKEN", "required": True}]
    result, _runner, _box = apply_package(
        tmp_path / "root",
        "hello-worker",
        {"hello-worker": yaml.safe_dump(document)},
    )
    assert result.code == 2
    assert result.missing[0]["var"] == "KEYSTONE_SETUP_SERVICE_HELLO_WORKER_SECRET_HELLO_TOKEN_FILE"


def test_allow_insecure_http_added(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _apply(tmp_path, monkeypatch)
    env = read_env(tmp_path / "root" / ".env")
    assert env["KEYSTONE_MCP_UPSTREAM_ALLOW_INSECURE_HTTP"] == "true"


def test_narrowed_generate_secret_leaves_the_shared_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = yaml.safe_load((PACKAGES / "hello-worker" / "keystone-service.yaml").read_text())
    document.pop("needs", None)
    image = document["containers"]["worker"]["image"]
    health = {"command": ["hello-worker", "health"]}
    token = {"name": "HELLO_TOKEN", "generate": {"bytes": 16}}
    document["containers"]["worker"]["secrets"] = [token]
    document["containers"]["worker"]["healthcheck"] = health
    document["containers"]["helper"] = {
        "image": image,
        "healthcheck": health,
        "secrets": [token],
    }
    _patch(monkeypatch)
    root = tmp_path / "root"
    first, _runner, _box = apply_package(
        root, "hello-worker", {"hello-worker": yaml.safe_dump(document)}
    )
    assert first.code == 0, first.error
    shared_path = root / "packages" / "hello-worker.env"
    shared_path.write_text(shared_path.read_text(encoding="utf-8") + "OPERATOR_NOTE=keep\n")
    original = read_env(shared_path)["HELLO_TOKEN"]
    narrowed = yaml.safe_load(yaml.safe_dump(document))
    narrowed["containers"]["worker"].pop("secrets")
    again, _runner, _box = apply_package(
        root,
        "hello-worker",
        {"hello-worker": yaml.safe_dump(narrowed)},
        reset=False,
    )
    assert again.code == 0, again.error
    shared = read_env(shared_path)
    assert "HELLO_TOKEN" not in shared
    assert shared["OPERATOR_NOTE"] == "keep"
    partial = read_env(root / "packages" / "hello-worker.helper.env")
    assert partial["HELLO_TOKEN"] == original
    fragment = yaml.safe_load((root / "packages" / "hello-worker.yml").read_text())
    worker_files = fragment["services"]["hello-worker-worker"]["env_file"]
    helper_files = fragment["services"]["hello-worker-helper"]["env_file"]
    assert "./hello-worker.helper.env" not in worker_files
    assert "./hello-worker.helper.env" in helper_files
    for relative in worker_files:
        assert "HELLO_TOKEN" not in read_env(root / "packages" / relative.removeprefix("./"))


def test_no_secret_in_output_lock_or_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, runner, _box = _apply(tmp_path, monkeypatch)
    assert result.code == 0, result.error
    root = tmp_path / "root"
    values = [
        read_env(root / "packages" / "docs-service.mcp.env")["DOCS_MCP_BEARER"],
        read_env(root / "packages" / "docs-service.mcp.env")["DOCS_IDENTITY_SECRET"],
        read_env(root / "packages" / "docs-service.api-mcp.env")["DOCS_INTERNAL_TOKEN"],
    ]
    blob = result.to_dict()
    rendered = yaml.safe_dump(blob)
    lock = (root / "services.lock.yaml").read_text()
    fragment = (root / "packages" / "docs-service.yml").read_text()
    argv = "\n".join(" ".join(call.args) for call in runner.calls)
    for value in values:
        assert value not in rendered
        assert value not in lock
        assert value not in fragment
        assert value not in argv
