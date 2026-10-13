"""provision.sh is invoked with secrets only in the process environment."""

from __future__ import annotations

import json
from pathlib import Path

from keystonectl.output import SecretRegistry
from keystonectl.resources.provision import existing_consumer_secret, run_provision
from keystonectl.runner import CommandResult, FakeCommandRunner

IMAGE = "minio/mc@sha256:" + ("ab" * 32)
PG_IMAGE = "postgres:16@sha256:" + ("cd" * 32)
ADMIN_USER = "root-user-value-0123456789abcdef"
ADMIN_PASSWORD = "root-password-value-0123456789abcdef"


def _runner(code: int = 0, stdout: str = "", stderr: str = "") -> FakeCommandRunner:
    def docker(args, **_kwargs):
        if tuple(args[:3]) == ("docker", "network", "inspect"):
            return CommandResult(tuple(args), 0, "{}", "")
        return CommandResult(tuple(args), code, stdout, stderr)

    return FakeCommandRunner(
        [
            (["docker", "network", "inspect"], docker),
            (["docker", "run"], docker),
        ]
    )


def _run(tmp_path: Path, runner: FakeCommandRunner, **kwargs):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir(exist_ok=True)
    (package_dir / "provision.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    admin = kwargs.pop(
        "admin",
        {"MINIO_ROOT_USER": ADMIN_USER, "MINIO_ROOT_PASSWORD": ADMIN_PASSWORD},
    )
    secrets = kwargs.pop("secrets", SecretRegistry())
    return (
        run_provision(
            runner,
            root=tmp_path,
            kind=kwargs.pop("kind", "s3"),
            image=kwargs.pop("image", IMAGE),
            script="provision.sh",
            package_dir=package_dir,
            consumer=kwargs.pop("consumer", "docs-mcp"),
            admin=admin,
            secret=kwargs.pop("secret", None),
            secrets=secrets,
            **kwargs,
        ),
        package_dir,
        secrets,
    )


def _run_args(runner: FakeCommandRunner) -> list[str]:
    return next(list(call.args) for call in runner.calls if call.args[:2] == ("docker", "run"))


def test_provision_invocation_contract_s3(tmp_path: Path) -> None:
    payload = {
        "interface": "s3/v1",
        "endpoint": "http://minio-server:9000",
        "region": "us-east-1",
        "bucket": "docs-mcp",
        "access_key": "docs-mcp",
        "path_style": True,
        "secret_key_file": "/out/docs-mcp.secret",
    }
    runner = _runner(stdout=json.dumps(payload) + "\n")
    result, package_dir, _secrets = _run(
        tmp_path,
        runner,
        endpoint="http://minio-server:9000",
    )
    assert result.ok
    args = _run_args(runner)
    out = next(item[: -len(":/out")] for item in args if item.endswith(":/out"))
    assert args == [
        "docker",
        "run",
        "--rm",
        "--network",
        "keystone-resources",
        "-e",
        "MINIO_ROOT_USER",
        "-e",
        "MINIO_ROOT_PASSWORD",
        "-e",
        "MC_CONFIG_DIR=/tmp/mc",
        "-v",
        f"{package_dir}:/pkg:ro",
        "-v",
        f"{out}:/out",
        "--entrypoint",
        "/bin/sh",
        IMAGE,
        "/pkg/provision.sh",
        "--consumer",
        "docs-mcp",
        "--secret-file",
        "/out/docs-mcp.secret",
        "--endpoint",
        "http://minio-server:9000",
    ]
    secret_file = Path(out) / "docs-mcp.secret"
    assert not secret_file.exists()


def test_provision_invocation_contract_postgres(tmp_path: Path) -> None:
    payload = {
        "interface": "postgres/v1",
        "host": "postgres-db",
        "port": 5432,
        "database": "file_mcp",
        "user": "file_mcp",
        "sslmode": "disable",
        "password_file": "/out/file-mcp.secret",
        "extensions": ["pgcrypto"],
    }
    runner = _runner(stdout=json.dumps(payload) + "\n")
    result, package_dir, _secrets = _run(
        tmp_path,
        runner,
        kind="postgres",
        image=PG_IMAGE,
        consumer="file-mcp",
        admin={"POSTGRES_USER": "postgres", "POSTGRES_PASSWORD": ADMIN_PASSWORD},
        host="postgres-db",
        port=5432,
        database="file_mcp",
        extensions=["pgcrypto"],
    )
    assert result.ok
    args = _run_args(runner)
    out = next(item[: -len(":/out")] for item in args if item.endswith(":/out"))
    assert args == [
        "docker",
        "run",
        "--rm",
        "--network",
        "keystone-resources",
        "-e",
        "POSTGRES_USER",
        "-e",
        "POSTGRES_PASSWORD",
        "-v",
        f"{package_dir}:/pkg:ro",
        "-v",
        f"{out}:/out",
        "--entrypoint",
        "/bin/sh",
        PG_IMAGE,
        "/pkg/provision.sh",
        "--consumer",
        "file-mcp",
        "--secret-file",
        "/out/file-mcp.secret",
        "--host",
        "postgres-db",
        "--port",
        "5432",
        "--database",
        "file_mcp",
        "--extensions",
        "pgcrypto",
    ]


def test_admin_values_only_in_env(tmp_path: Path) -> None:
    payload = {
        "interface": "s3/v1",
        "endpoint": "http://minio-server:9000",
        "region": "us-east-1",
        "bucket": "docs-mcp",
        "access_key": "docs-mcp",
        "path_style": True,
        "secret_key_file": "/out/docs-mcp.secret",
    }
    runner = _runner(stdout=json.dumps(payload))
    _run(tmp_path, runner, endpoint="http://minio-server:9000")
    call = next(item for item in runner.calls if item.args[:2] == ("docker", "run"))
    assert call.env is not None
    assert call.env["MINIO_ROOT_PASSWORD"] == ADMIN_PASSWORD
    for arg in call.args:
        assert ADMIN_PASSWORD not in arg
        assert ADMIN_USER not in arg


def test_tmp_dir_removed(tmp_path: Path) -> None:
    seen: list[Path] = []

    def docker(args, **_kwargs):
        if tuple(args[:2]) == ("docker", "run"):
            out = next(item[: -len(":/out")] for item in args if str(item).endswith(":/out"))
            seen.append(Path(out))
            assert (Path(out) / "docs-mcp.secret").stat().st_mode & 0o777 == 0o600
            assert Path(out).stat().st_mode & 0o777 == 0o700
        payload = {
            "interface": "s3/v1",
            "endpoint": "http://minio-server:9000",
            "region": "us-east-1",
            "bucket": "docs-mcp",
            "access_key": "docs-mcp",
            "path_style": False,
            "secret_key_file": "/out/docs-mcp.secret",
        }
        stdout = "" if tuple(args[:2]) != ("docker", "run") else json.dumps(payload)
        return CommandResult(tuple(args), 0, stdout, "")

    runner = FakeCommandRunner([(["docker"], docker)])
    result, _package_dir, _secrets = _run(tmp_path, runner, endpoint="http://minio-server:9000")
    assert result.ok
    assert seen
    assert not seen[0].exists()


def test_provision_reuses_existing_consumer_secret(tmp_path: Path) -> None:
    from keystonectl.resources.postgres_probe import PgParams, build_url

    password = "p@ss/w%rd-extra-0123456789"
    url = build_url(
        PgParams(
            host="db.example",
            port=5432,
            database="notesdb",
            user="notes",
            password=password,
            sslmode="require",
        )
    )
    cases = [
        (
            {"FILE_MCP_S3_SECRET_KEY": "s3-secret-value-0123456789abcdef"},
            {"secret_key": "FILE_MCP_S3_SECRET_KEY"},
        ),
        (
            {"NOTES_PG_PASSWORD": "pg-secret-value-0123456789abcdef"},
            {"password": "NOTES_PG_PASSWORD"},
        ),
        ({"APP_DATABASE_URL": url}, {"url": "APP_DATABASE_URL"}),
    ]
    for env, bind in cases:
        expected = existing_consumer_secret(env, bind)
        assert expected
        captured: list[str] = []

        def docker(args, found=captured, **_kwargs):
            if tuple(args[:2]) == ("docker", "run"):
                out = next(item[: -len(":/out")] for item in args if str(item).endswith(":/out"))
                consumer = args[args.index("--consumer") + 1]
                found.append((Path(out) / f"{consumer}.secret").read_text(encoding="utf-8"))
            payload = {
                "interface": "s3/v1",
                "endpoint": "http://minio-server:9000",
                "region": "us-east-1",
                "bucket": "docs-mcp",
                "access_key": "docs-mcp",
                "path_style": True,
                "secret_key_file": "/out/docs-mcp.secret",
            }
            stdout = "" if tuple(args[:2]) != ("docker", "run") else json.dumps(payload)
            return CommandResult(tuple(args), 0, stdout, "")

        runner = FakeCommandRunner([(["docker"], docker)])
        result, _package_dir, _secrets = _run(
            tmp_path,
            runner,
            secret=expected,
            endpoint="http://minio-server:9000",
        )
        assert result.ok
        assert captured == [expected]


def test_provision_result_bound_to_env(tmp_path: Path) -> None:
    rewritten = "rewritten-secret-0123456789abcdef"

    def docker(args, **_kwargs):
        if tuple(args[:2]) == ("docker", "run"):
            out = next(item[: -len(":/out")] for item in args if str(item).endswith(":/out"))
            secret = Path(out) / "docs-mcp.secret"
            secret.write_text(rewritten, encoding="utf-8")
        payload = {
            "interface": "s3/v1",
            "endpoint": "http://minio-server:9000",
            "region": "us-east-1",
            "bucket": "docs-mcp",
            "access_key": "docs-mcp",
            "path_style": True,
            "secret_key_file": "/out/docs-mcp.secret",
        }
        stdout = "" if tuple(args[:2]) != ("docker", "run") else json.dumps(payload)
        return CommandResult(tuple(args), 0, stdout, "")

    runner = FakeCommandRunner([(["docker"], docker)])
    result, _package_dir, _secrets = _run(tmp_path, runner, endpoint="http://minio-server:9000")
    assert result.ok
    assert result.values["secret_key"] == rewritten
    assert result.values["bucket"] == "docs-mcp"
    assert rewritten not in json.dumps(
        {key: value for key, value in result.values.items() if key != "secret_key"}
    )


def test_provision_bad_json_or_interface(tmp_path: Path) -> None:
    for stdout in ("not-json", '{"interface": "other/v1"}', '{"interface": "s3/v1"}'):
        runner = _runner(stdout=stdout)
        result, _package_dir, _secrets = _run(tmp_path, runner, endpoint="http://minio-server:9000")
        assert result.ok is False
        assert result.error_id == "provision_failed"


def test_provision_exit_3_retryable_exit_2_not(tmp_path: Path) -> None:
    retryable, _dir, _secrets = _run(
        tmp_path, _runner(code=3, stderr="not ready"), endpoint="http://x"
    )
    assert retryable.error_id == "provision_failed"
    assert retryable.retryable is True
    refused, _dir, _secrets = _run(
        tmp_path, _runner(code=2, stderr="bad package"), endpoint="http://x"
    )
    assert refused.error_id == "provision_failed"
    assert refused.retryable is False


def test_stderr_redacted_in_error(tmp_path: Path) -> None:
    secret = "provision-secret-value-0123456789abcdef"
    runner = _runner(code=2, stderr=f"leaked {secret}\n")
    result, _package_dir, _secrets = _run(
        tmp_path,
        runner,
        secret=secret,
        endpoint="http://minio-server:9000",
    )
    assert result.error_id == "provision_failed"
    assert secret not in result.message
    assert "***" in result.message
