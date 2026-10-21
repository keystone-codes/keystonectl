"""Fixtures for resource binding tests. No Docker and no network."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from keystonectl.catalog import catalog_errors
from keystonectl.manifest import manifest_errors
from keystonectl.output import SecretRegistry
from keystonectl.plan import run_service
from keystonectl.resources.postgres_probe import ProbeError as PgError
from keystonectl.resources.postgres_probe import ProbeReport as PgReport
from keystonectl.resources.s3_probe import ProbeError as S3Error
from keystonectl.resources.s3_probe import ProbeReport as S3Report
from keystonectl.runner import CommandResult, FakeCommandRunner
from tests.support import EXAMPLES, SHA, private_file, write_empty_lock
from tests.support.deploylab import DockerBox

ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE01"
SECRET_KEY = "wJalrXUtnFEMI7SECRETKEY32charsxx"
PG_PASSWORD = "pg-password-value-0123456789abcdef"
BASE_ENV = {"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"}
_CLOCK = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))


def s3_consumer(name: str = "file-mcp", *, namespace: str = "files") -> dict[str, Any]:
    document = _load("file-mcp.keystone-service.yaml")
    document["name"] = name
    document["summary"] = f"{name} keeps files in S3."
    document["mcp"]["namespace"] = namespace
    return document


def pg_consumer(
    name: str = "notes-mcp",
    *,
    form: str = "fields",
    extensions: list[str] | None = None,
    namespace: str = "notes",
) -> dict[str, Any]:
    document = _load("file-mcp.keystone-service.yaml")
    document["name"] = name
    document["summary"] = f"{name} keeps rows in PostgreSQL."
    document["mcp"]["namespace"] = namespace
    if form == "url":
        bind = {"url": "APP_DATABASE_URL"}
    else:
        bind = {
            "host": "NOTES_PG_HOST",
            "port": "NOTES_PG_PORT",
            "database": "NOTES_PG_DATABASE",
            "user": "NOTES_PG_USER",
            "password": "NOTES_PG_PASSWORD",
            "sslmode": "NOTES_PG_SSLMODE",
        }
    need: dict[str, Any] = {"kind": "postgres", "version": 1, "bind_env": bind}
    if extensions:
        need["extensions"] = list(extensions)
    document["needs"] = [need]
    return document


def both_consumer() -> dict[str, Any]:
    return _load("docs-service.keystone-service.yaml")


def s3_provider(name: str = "minio") -> dict[str, Any]:
    document = _load("minio.keystone-service.yaml")
    document["name"] = name
    document["summary"] = f"{name} provides s3/v1."
    return document


def pg_provider(name: str = "postgres") -> dict[str, Any]:
    document = _load("postgres.keystone-service.yaml")
    document["name"] = name
    document["summary"] = f"{name} provides postgres/v1."
    return document


def standard_manifests() -> list[dict[str, Any]]:
    return [
        s3_consumer("file-mcp", namespace="files"),
        s3_consumer("pages-mcp", namespace="pages"),
        pg_consumer("notes-mcp", extensions=["pgcrypto"]),
        pg_consumer("url-mcp", form="url", namespace="library"),
        both_consumer(),
        s3_provider("minio"),
        pg_provider("postgres"),
    ]


def set_need_version(document: dict[str, Any], kind: str, version: int) -> None:
    for need in document["needs"]:
        if need["kind"] == kind:
            need["version"] = version


class FakeDeployer:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def deploy(self, ctx: object, package: object, *, role: str) -> None:
        self.events.append(("deploy", str(getattr(package, "name", ""))))

    def wait_healthy(self, ctx: object, name: str, *, timeout: float) -> None:
        self.events.append(("wait", name))

    def container_ip(self, ctx: object, service: str, network: str) -> str:
        return "10.0.0.8"


class Lab:
    """A deployment directory, fake git, and fake docker."""

    def __init__(
        self,
        root: Path,
        manifests: list[dict[str, Any]],
        *,
        extra: list[dict[str, Any]] | None = None,
        validate_manifests: bool = True,
    ) -> None:
        self.root = root
        self.secrets = SecretRegistry()
        self.deployer = FakeDeployer()
        self.provision_code = 0
        self.provision_stderr = "provision failed"
        self.containers: dict[str, dict[str, Any]] = {}
        self.by_url: dict[str, str] = {}
        self.texts: dict[str, str] = {}
        self.docker_box = DockerBox()
        write_empty_lock(root)
        groups = [("local", manifests, "https://example.com/catalog.git", 100)]
        if extra:
            groups.append(("extra", extra, "https://example.com/extra-catalog.git", 50))
        sources: list[dict[str, Any]] = []
        for source, documents, url, priority in groups:
            sources.append({"name": source, "url": url, "ref": "v1.0.0", "priority": priority})
            packages: dict[str, Any] = {}
            for document in documents:
                text = yaml.safe_dump(document, sort_keys=False)
                loaded = yaml.safe_load(text)
                if validate_manifests:
                    problems = manifest_errors(loaded)
                    if problems:
                        raise AssertionError(f"{document['name']}: {problems}")
                name = str(loaded["name"])
                self.texts[name] = text
                entry = _catalog_entry(loaded)
                packages[name] = entry
                self.by_url[str(entry["source"])] = text
                _seed_package(root, source, loaded, text)
            catalog = {
                "schema_version": 1,
                "name": source,
                "description": "Test catalog",
                "packages": packages,
            }
            problems = catalog_errors(catalog)
            if problems:
                raise AssertionError("\n".join(problems))
            catalog_text = yaml.safe_dump(catalog, sort_keys=False)
            self.by_url[url] = catalog_text
            cache = root / ".keystonectl" / "catalogs" / source
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "catalog.yaml").write_text(catalog_text, encoding="utf-8")
        (root / "sources.yaml").write_text(
            yaml.safe_dump({"schema_version": 1, "sources": sources}, sort_keys=False),
            encoding="utf-8",
        )
        self.runner = self._runner()

    def _runner(self) -> FakeCommandRunner:
        lab = self

        def ls_remote(args, **_kwargs):
            last = str(args[-1])
            if last.startswith("refs/tags/"):
                tag = last.removeprefix("refs/tags/")
                stdout = f"{SHA}\trefs/tags/{tag}\n"
            else:
                stdout = f"{SHA}\trefs/tags/v1.0.0\n"
            return CommandResult(tuple(args), 0, stdout, "")

        def clone(args, **_kwargs):
            dest = Path(args[-1])
            url = str(args[-2])
            dest.mkdir(parents=True, exist_ok=True)
            text = lab.by_url[url]
            if (
                "packages:" in text.split("\n", 8)[-1]
                or text.startswith("schema_version:")
                and ("\npackages:" in text)
            ):
                (dest / "catalog.yaml").write_text(text, encoding="utf-8")
            else:
                (dest / "keystone-service.yaml").write_text(text, encoding="utf-8")
                if "\nkind: resource\n" in text:
                    (dest / "provision.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (dest / ".git").mkdir()
            return CommandResult(tuple(args), 0, "", "")

        def docker(args, env=None, **_kwargs):
            if tuple(args[:3]) == ("docker", "network", "inspect"):
                network = args[3]
                if "--format" in args:
                    payload = lab.containers.get(network, {})
                    return CommandResult(tuple(args), 0, json.dumps(payload), "")
                code = 0 if network == "keystone-resources" else 1
                return CommandResult(tuple(args), code, "", "")
            if tuple(args[:2]) == ("docker", "run"):
                if lab.provision_code != 0:
                    return CommandResult(tuple(args), lab.provision_code, "", lab.provision_stderr)
                return CommandResult(tuple(args), 0, _provision_json(list(args)) + "\n", "")
            # Everything else (compose up/config/version, image inspect, container
            # health) goes through the same fake Docker the plain-service deploy
            # tests use, so a real `ComposeDeployer.deploy()` (for example from
            # `service upgrade` on a resource provider) works here too.
            return lab.docker_box(args, **_kwargs)

        ok = CommandResult((), 0, "", "")
        return FakeCommandRunner(
            [
                (["git", "ls-remote"], ls_remote),
                (["git", "clone"], clone),
                (["git", "init"], ok),
                (["git", "fetch"], ok),
                (["git", "checkout"], ok),
                (["git", "rev-parse", "HEAD"], CommandResult((), 0, SHA + "\n", "")),
                (["docker"], docker),
            ]
        )

    def run(
        self,
        verb: str,
        ref: str,
        *,
        environ: Mapping[str, str] | None = None,
        deployer: bool = True,
    ):
        return run_service(
            self.root,
            verb,
            ref,
            runner=self.runner,
            secrets=self.secrets,
            sets={},
            environ={**BASE_ENV, **dict(environ or {})},
            interactive=False,
            deployer=self.deployer if deployer else None,
            clock=lambda: _CLOCK,
            defer_public=True,
        )

    def plan(self, ref: str, *, environ: Mapping[str, str] | None = None):
        return self.run("plan", ref, environ=environ)

    def apply(self, ref: str, *, environ: Mapping[str, str] | None = None, deployer: bool = True):
        return self.run("apply", ref, environ=environ, deployer=deployer)

    def s3_answers(self, **overrides: str) -> dict[str, str]:
        access = self.root / "s3-access"
        secret = self.root / "s3-secret"
        private_file(access, overrides.pop("access_key", ACCESS_KEY) + "\n")
        private_file(secret, overrides.pop("secret_key", SECRET_KEY) + "\n")
        values = {
            "KEYSTONE_SETUP_RESOURCE_S3": "external",
            "KEYSTONE_SETUP_S3_ENDPOINT": "https://s3.example",
            "KEYSTONE_SETUP_S3_BUCKET": "file-mcp",
            "KEYSTONE_SETUP_S3_ACCESS_KEY_FILE": str(access),
            "KEYSTONE_SETUP_S3_SECRET_KEY_FILE": str(secret),
        }
        values.update(overrides)
        return values

    def pg_answers(self, **overrides: str) -> dict[str, str]:
        password = self.root / "pg-password"
        private_file(password, overrides.pop("password", PG_PASSWORD) + "\n")
        values = {
            "KEYSTONE_SETUP_RESOURCE_POSTGRES": "external",
            "KEYSTONE_SETUP_POSTGRES_HOST": "db.example",
            "KEYSTONE_SETUP_POSTGRES_DATABASE": "notesdb",
            "KEYSTONE_SETUP_POSTGRES_USER": "notes",
            "KEYSTONE_SETUP_POSTGRES_PASSWORD_FILE": str(password),
        }
        values.update(overrides)
        return values

    def url_file(self, url: str) -> str:
        path = self.root / "pg-url"
        private_file(path, url + "\n")
        return str(path)


def _catalog_entry(manifest: Mapping[str, Any]) -> dict[str, Any]:
    version = {
        "version": manifest["version"],
        "ref": f"v{manifest['version']}",
    }
    entry: dict[str, Any] = {
        "kind": manifest["kind"],
        "summary": manifest["summary"],
        "source": f"https://example.com/{manifest['name']}.git",
        "versions": [version],
    }
    if manifest["kind"] == "service":
        version["keystone_mcp"] = manifest["compat"]["keystone_mcp"]
    else:
        entry["provides"] = {
            "kind": manifest["provides"]["kind"],
            "version": manifest["provides"]["version"],
        }
    return entry


def _seed_package(root: Path, source: str, manifest: Mapping[str, Any], text: str) -> None:
    dest = (
        root
        / ".keystonectl"
        / "packages"
        / source
        / str(manifest["name"])
        / str(manifest["version"])
    )
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "keystone-service.yaml").write_text(text, encoding="utf-8")
    if manifest.get("kind") == "resource":
        (dest / "provision.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")


def _provision_json(args: list[str]) -> str:
    def option(flag: str) -> str:
        return args[args.index(flag) + 1]

    consumer = option("--consumer")
    if "--endpoint" in args:
        payload: dict[str, Any] = {
            "interface": "s3/v1",
            "endpoint": option("--endpoint"),
            "region": "us-east-1",
            "bucket": consumer,
            "access_key": consumer,
            "path_style": True,
            "secret_key_file": option("--secret-file"),
        }
    else:
        raw = option("--extensions")
        payload = {
            "interface": "postgres/v1",
            "host": option("--host"),
            "port": int(option("--port")),
            "database": option("--database"),
            "user": option("--database"),
            "sslmode": "disable",
            "password_file": option("--secret-file"),
            "extensions": [item for item in raw.split(",") if item],
        }
    return json.dumps(payload)


def s3_report(
    *,
    exit_code: int = 0,
    code: str | None = None,
    message: str = "probe failed",
    missing: list[str] | None = None,
) -> S3Report:
    error = None if code is None else S3Error(code, message, retryable=exit_code == 3)
    return S3Report(
        ok=exit_code == 0,
        exit_code=exit_code,
        endpoint="https://s3.example",
        bucket="file-mcp",
        region="us-east-1",
        path_style=True,
        checks={},
        warnings=[],
        error=error,
        missing=missing or [],
    )


def pg_report(
    *,
    exit_code: int = 0,
    code: str | None = None,
    message: str = "probe failed",
    missing: list[str] | None = None,
    missing_extensions: list[str] | None = None,
) -> PgReport:
    error = None
    if code is not None:
        error = PgError(
            code, message, retryable=exit_code == 3, missing_extensions=missing_extensions
        )
    return PgReport(
        ok=exit_code == 0,
        exit_code=exit_code,
        host="db.example",
        port=5432,
        database="notesdb",
        user="notes",
        sslmode="require",
        server_version="16.0",
        checks={},
        warnings=[],
        error=error,
        missing=missing or [],
    )


def install_ok_probes(monkeypatch) -> list[tuple[str, Any, dict[str, Any]]]:
    calls: list[tuple[str, Any, dict[str, Any]]] = []

    def s3(params, **kwargs):
        calls.append(("s3", params, kwargs))
        report = s3_report()
        if kwargs.get("connect_endpoint"):
            report.checked_via = kwargs["connect_endpoint"]
        return report

    def postgres(params, **kwargs):
        calls.append(("postgres", params, kwargs))
        report = pg_report()
        if kwargs.get("connect_host"):
            report.checked_via = kwargs["connect_host"]
        return report

    monkeypatch.setattr("keystonectl.resources.binding.probe_s3", s3)
    monkeypatch.setattr("keystonectl.resources.binding.probe_postgres", postgres)
    return calls


def install_scripted_probe(monkeypatch, kind: str, reports: list[Any]) -> list[dict[str, Any]]:
    queue = list(reports)
    calls: list[dict[str, Any]] = []

    def fake(params, **kwargs):
        calls.append(kwargs)
        report = queue.pop(0)
        endpoint = kwargs.get("connect_endpoint") or kwargs.get("connect_host")
        if endpoint:
            report = copy.copy(report)
            report.checked_via = endpoint
        return report

    name = "probe_s3" if kind == "s3" else "probe_postgres"
    monkeypatch.setattr(f"keystonectl.resources.binding.{name}", fake)
    return calls


def consumer_name(kind: str) -> str:
    return "file-mcp" if kind == "s3" else "notes-mcp"


def provider_name(kind: str) -> str:
    return "minio" if kind == "s3" else "postgres"


def choice_var(kind: str) -> str:
    return f"KEYSTONE_SETUP_RESOURCE_{kind.upper()}"
