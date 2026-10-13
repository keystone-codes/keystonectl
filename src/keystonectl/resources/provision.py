"""Run a provider's provision.sh. Secrets stay in the process environment."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex
from typing import Any
from urllib.parse import unquote, urlparse

from keystonectl.output import SecretRegistry
from keystonectl.runner import CommandRunner

RESOURCE_NETWORK = "keystone-resources"
# The installer owns the server and the consumer secret. Stdout may name an
# object on that server (bucket, region, path_style, access_key). host, port,
# endpoint, sslmode, database, and user would send the secret somewhere else.
_STDOUT_DROPPED = frozenset(
    {
        "extensions",
        "url",
        "password",
        "secret_key",
        "host",
        "port",
        "endpoint",
        "sslmode",
        "database",
        "user",
    }
)


@dataclass
class ProvisionResult:
    ok: bool
    values: dict[str, str] = field(default_factory=dict)
    extensions: list[str] = field(default_factory=list)
    error_id: str | None = None
    message: str = ""
    retryable: bool = False


def consumer_ident(package: str) -> str:
    """Postgres database and user: package name with '-' replaced by '_'."""
    return package.replace("-", "_")


def consumer_secret_path(root: Path, package: str, kind: str) -> Path:
    """Mode 600 env snippet that keeps a generated consumer secret across a failed probe."""
    return Path(root) / ".keystonectl" / "secrets" / f"{package}.{kind}.env"


def existing_consumer_secret(
    env: Mapping[str, str],
    bind_env: Mapping[str, str],
) -> str | None:
    """Return a consumer secret already stored for this package, if any."""
    for param in ("secret_key", "password"):
        name = bind_env.get(param)
        if isinstance(name, str) and env.get(name):
            return env[name]
    url_name = bind_env.get("url")
    if isinstance(url_name, str) and env.get(url_name):
        parsed = urlparse(env[url_name])
        if parsed.password:
            return unquote(parsed.password)
    return None


def _fail(message: str, *, retryable: bool = False) -> ProvisionResult:
    return ProvisionResult(
        ok=False,
        error_id="provision_failed",
        message=message,
        retryable=retryable,
    )


def _tail(text: str, secrets: SecretRegistry) -> str:
    lines = text.splitlines()
    tail = "\n".join(lines[-20:])
    redacted = secrets.redact(tail).strip()
    return redacted or "The provision script failed."


def _network_present(runner: CommandRunner) -> bool:
    result = runner.run(["docker", "network", "inspect", RESOURCE_NETWORK])
    return result.returncode == 0


def _read_secret(path: Path, fallback: str) -> str:
    if not path.is_file():
        return fallback
    text = path.read_text(encoding="utf-8")
    if text.endswith("\n"):
        text = text[:-1]
    return text


def _write_secret(directory: Path, consumer: str, secret: str) -> None:
    path = directory / f"{consumer}.secret"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # A POSIX read fails at EOF when the line has no newline. provision.sh
        # treats that failure as an unreadable secret.
        os.write(fd, secret.encode("utf-8") + b"\n")
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _command(
    *,
    kind: str,
    image: str,
    script: str,
    package_dir: Path,
    out_dir: Path,
    consumer: str,
    admin_names: Sequence[str],
    endpoint: str | None,
    host: str | None,
    port: int | None,
    database: str | None,
    extensions: Sequence[str],
) -> list[str]:
    args = ["docker", "run", "--rm", "--network", RESOURCE_NETWORK]
    for name in admin_names:
        args.extend(["-e", name])
    if kind == "s3":
        args.extend(["-e", "MC_CONFIG_DIR=/tmp/mc"])
    args.extend(
        [
            "-v",
            f"{package_dir}:/pkg:ro",
            "-v",
            f"{out_dir}:/out",
            "--entrypoint",
            "/bin/sh",
            image,
            f"/pkg/{script}",
            "--consumer",
            consumer,
            "--secret-file",
            f"/out/{consumer}.secret",
        ]
    )
    if kind == "s3":
        args.extend(["--endpoint", endpoint or ""])
    else:
        joined = ",".join(extensions)
        args.extend(
            [
                "--host",
                host or "",
                "--port",
                str(port if port is not None else ""),
                "--database",
                database or "",
                "--extensions",
                joined,
            ]
        )
    return args


def _parse_stdout(kind: str, stdout: str) -> tuple[dict[str, Any] | None, str]:
    line = ""
    for candidate in reversed(stdout.splitlines()):
        if candidate.strip():
            line = candidate.strip()
            break
    if not line:
        return None, "The provision script printed no JSON."
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None, "The provision script did not print JSON."
    if not isinstance(payload, dict):
        return None, "The provision script did not print a JSON object."
    expected = f"{kind}/v1"
    if payload.get("interface") != expected:
        return None, f"The provision script did not return {expected}."
    required = (
        ("endpoint", "region", "bucket", "access_key", "path_style", "secret_key_file")
        if kind == "s3"
        else ("host", "port", "database", "user", "sslmode", "password_file")
    )
    missing = [name for name in required if name not in payload]
    if missing:
        return None, f"The provision result is missing {', '.join(missing)}."
    return payload, ""


def run_provision(
    runner: CommandRunner,
    *,
    root: Path,
    kind: str,
    image: str,
    script: str,
    package_dir: Path,
    consumer: str,
    admin: Mapping[str, str],
    secret: str | None,
    secrets: SecretRegistry,
    endpoint: str | None = None,
    host: str | None = None,
    port: int | None = None,
    database: str | None = None,
    extensions: Sequence[str] = (),
) -> ProvisionResult:
    """Run provision.sh. The consumer secret is read back from the temp file."""
    if not _network_present(runner):
        return ProvisionResult(
            ok=False,
            error_id="docker_unavailable",
            message=(
                "The keystone-resources network does not exist yet. "
                "Deploy a service so Docker creates it, then run the command again."
            ),
            retryable=True,
        )
    out_dir = Path(root) / ".keystonectl" / "tmp" / uuid.uuid4().hex
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    chosen = secret if secret else token_hex(32)
    secrets.register(chosen)
    try:
        _write_secret(out_dir, consumer, chosen)
        args = _command(
            kind=kind,
            image=image,
            script=script,
            package_dir=Path(package_dir),
            out_dir=out_dir,
            consumer=consumer,
            admin_names=tuple(admin),
            endpoint=endpoint,
            host=host,
            port=port,
            database=database,
            extensions=extensions,
        )
        result = runner.run(args, env=dict(admin))
        if result.returncode == 3:
            return _fail(_tail(result.stderr, secrets), retryable=True)
        if result.returncode != 0:
            return _fail(_tail(result.stderr, secrets), retryable=False)
        payload, problem = _parse_stdout(kind, result.stdout)
        if payload is None:
            return _fail(problem)
        secret_path = out_dir / f"{consumer}.secret"
        written = _read_secret(secret_path, chosen)
        secrets.register(written)
        values = {key: str(value) for key, value in payload.items() if key not in _STDOUT_DROPPED}
        if kind == "s3":
            values["secret_key"] = written
            if endpoint is not None:
                values["endpoint"] = endpoint
        else:
            values["password"] = written
            if host is not None:
                values["host"] = host
            if port is not None:
                values["port"] = str(port)
            if database is not None:
                # A different name in stdout would be a different principal.
                values["database"] = database
                values["user"] = database
            # The resource network is not TLS. The script must not store another mode.
            values["sslmode"] = "disable"
        raw_extensions = payload.get("extensions") or list(extensions)
        if not isinstance(raw_extensions, list):
            raw_extensions = list(extensions)
        return ProvisionResult(
            ok=True,
            values=values,
            extensions=[str(item) for item in raw_extensions],
        )
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
