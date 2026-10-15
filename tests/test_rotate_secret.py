"""service rotate-secret replaces a catalog consumer secret and restarts its containers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from keystonectl.answers import pkg_var
from keystonectl.envfiles import read_env
from keystonectl.resources.provision import consumer_secret_path
from keystonectl.resources.rotate import rotate_secret
from keystonectl.runner import CommandResult
from tests.support import mode
from tests.support.resources import (
    SECRET_KEY,
    Lab,
    install_ok_probes,
    pg_report,
    s3_report,
    standard_manifests,
)

_CLOCK = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def _prepare(lab: Lab) -> dict:
    """Record provision secrets and answer compose health checks."""
    record: dict = {"up": [], "secrets": [], "runs": 0, "fail_run": None, "restarted": []}
    original = None
    for prefix, outcome in lab.runner.rules:
        if tuple(prefix) == ("docker",):
            original = outcome
    assert original is not None

    def docker(args, env=None, **kwargs):
        argv = [str(item) for item in args]
        if len(argv) >= 2 and argv[1] == "compose" and "up" in argv:
            record["up"].append(tuple(argv))
            if "--force-recreate" in argv:
                record["restarted"] = argv[argv.index("--force-recreate") + 1 :]
            return CommandResult(tuple(args), 0, "", "")
        if len(argv) >= 2 and argv[1] == "compose" and "ps" in argv:
            lines = [
                json.dumps({"Service": service, "ID": service, "State": "running"})
                for service in record["restarted"]
            ]
            text = ("\n".join(lines) + "\n") if lines else ""
            return CommandResult(tuple(args), 0, text, "")
        if len(argv) >= 2 and argv[1] == "inspect":
            body = json.dumps(
                [
                    {
                        "State": {
                            "Status": "running",
                            "ExitCode": 0,
                            "Health": {"Status": "healthy"},
                        }
                    }
                ]
            )
            return CommandResult(tuple(args), 0, body, "")
        if tuple(argv[:2]) == ("docker", "run"):
            out = next(item[: -len(":/out")] for item in argv if item.endswith(":/out"))
            consumer = argv[argv.index("--consumer") + 1]
            text = (Path(out) / f"{consumer}.secret").read_text(encoding="utf-8")
            assert all(text not in arg for arg in argv)
            record["secrets"].append(text.rstrip("\n"))
            record["runs"] += 1
            if record["fail_run"] == record["runs"]:
                return CommandResult(tuple(args), 1, "", "provision failed")
        return original(args, env=env, **kwargs)

    lab.runner.rules = [
        (prefix, docker if tuple(prefix) == ("docker",) else outcome)
        for prefix, outcome in lab.runner.rules
    ]
    return record


def _confirm(name: str) -> dict[str, str]:
    return {pkg_var(name, "CONFIRM_ROTATE"): "yes"}


def _rotate(lab: Lab, name: str, kind: str, *, sets: dict[str, str] | None = None):
    return rotate_secret(
        lab.root,
        name,
        kind,
        runner=lab.runner,
        secrets=lab.secrets,
        sets=_confirm(name) if sets is None else sets,
        environ={},
        interactive=False,
        clock=lambda: _CLOCK,
    )


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, environ: dict[str, str]):
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    record = _prepare(lab)
    result = lab.apply(name, environ=environ)
    assert result.code == 0, result.error
    return lab, record


def _secret_key(kind: str) -> str:
    if kind == "s3":
        return "FILE_MCP_S3_SECRET_KEY"
    return "NOTES_PG_PASSWORD"


def _env_path(lab: Lab, name: str) -> Path:
    return lab.root / "packages" / f"{name}.env"


def test_rotate_s3_replaces_secret_and_recreates_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab, record = _install(
        tmp_path, monkeypatch, "file-mcp", {"KEYSTONE_SETUP_RESOURCE_S3": "catalog"}
    )
    key = _secret_key("s3")
    old = read_env(_env_path(lab, "file-mcp"))[key]
    before = len(record["secrets"])
    result = _rotate(lab, "file-mcp", "s3")
    assert result.code == 0, result.error
    new = read_env(_env_path(lab, "file-mcp"))[key]
    secret_path = consumer_secret_path(lab.root, "file-mcp", "s3")
    assert new != old
    assert record["secrets"][before] == new
    assert read_env(secret_path)[key] == new
    assert mode(secret_path) == 0o600
    assert record["restarted"] == ["file-mcp-mcp"]
    up = record["up"][-1]
    assert up[up.index("up") : up.index("--force-recreate") + 1] == ("up", "-d", "--force-recreate")
    payload = result.to_dict()
    assert payload["result"] == {
        "rotated": {"kind": "s3", "provider": "minio"},
        "restarted": ["file-mcp-mcp"],
    }
    encoded = json.dumps(payload)
    assert old not in encoded
    assert new not in encoded
    assert old in lab.secrets.values
    assert new in lab.secrets.values


def test_rotate_postgres_same_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lab, record = _install(
        tmp_path, monkeypatch, "notes-mcp", {"KEYSTONE_SETUP_RESOURCE_POSTGRES": "catalog"}
    )
    key = _secret_key("postgres")
    old = read_env(_env_path(lab, "notes-mcp"))[key]
    before = len(record["secrets"])
    result = _rotate(lab, "notes-mcp", "postgres")
    assert result.code == 0, result.error
    new = read_env(_env_path(lab, "notes-mcp"))[key]
    secret_path = consumer_secret_path(lab.root, "notes-mcp", "postgres")
    assert new != old
    assert record["secrets"][before] == new
    assert read_env(secret_path)[key] == new
    assert record["restarted"] == ["notes-mcp-mcp"]
    payload = result.to_dict()
    assert payload["result"]["rotated"] == {"kind": "postgres", "provider": "postgres"}
    assert payload["result"]["restarted"] == ["notes-mcp-mcp"]
    encoded = json.dumps(payload)
    assert old not in encoded
    assert new not in encoded


def test_rotate_s3_restarts_only_containers_with_the_kind_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab, record = _install(
        tmp_path,
        monkeypatch,
        "docs-service",
        {
            "KEYSTONE_SETUP_RESOURCE_S3": "catalog",
            "KEYSTONE_SETUP_RESOURCE_POSTGRES": "catalog",
        },
    )
    env_path = lab.root / "packages" / "docs-service.s3.env"
    key = "DOCS_S3_SECRET_KEY"
    old = read_env(env_path)[key]
    before = len(record["secrets"])
    result = _rotate(lab, "docs-service", "s3")
    assert result.code == 0, result.error
    new = read_env(env_path)[key]
    assert new != old
    assert record["secrets"][before] == new
    assert read_env(consumer_secret_path(lab.root, "docs-service", "s3"))[key] == new
    assert result.result["restarted"] == ["docs-service-api"]
    assert "docs-service-mcp" not in result.result["restarted"]
    assert "docs-service-migrate" not in result.result["restarted"]


def _fail_probe_after_apply(
    monkeypatch: pytest.MonkeyPatch, kind: str, phase: dict[str, bool]
) -> None:
    def fake(params, **kwargs):
        del params, kwargs
        if phase["rotate"]:
            if kind == "s3":
                return s3_report(exit_code=1, code="auth_failed")
            return pg_report(exit_code=1, code="auth_failed")
        if kind == "s3":
            return s3_report()
        return pg_report()

    probe = "probe_s3" if kind == "s3" else "probe_postgres"
    monkeypatch.setattr(f"keystonectl.resources.binding.{probe}", fake)


def test_rotate_probe_failure_restores_old_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    phase = {"rotate": False}
    _fail_probe_after_apply(monkeypatch, "s3", phase)
    lab = Lab(tmp_path, standard_manifests())
    record = _prepare(lab)
    installed = lab.apply("file-mcp", environ={"KEYSTONE_SETUP_RESOURCE_S3": "catalog"})
    assert installed.code == 0, installed.error
    key = _secret_key("s3")
    old = read_env(_env_path(lab, "file-mcp"))[key]
    before = len(record["secrets"])
    phase["rotate"] = True
    result = _rotate(lab, "file-mcp", "s3")
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "rotate_failed"
    assert result.retryable is True
    assert record["secrets"][before] != old
    assert record["secrets"][before + 1] == old
    assert read_env(_env_path(lab, "file-mcp"))[key] == old
    assert read_env(consumer_secret_path(lab.root, "file-mcp", "s3"))[key] == old
    encoded = json.dumps(result.to_dict())
    assert old not in encoded
    assert record["secrets"][before] not in encoded


def test_rotate_restore_failure_keeps_new_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    phase = {"rotate": False}
    _fail_probe_after_apply(monkeypatch, "s3", phase)
    lab = Lab(tmp_path, standard_manifests())
    record = _prepare(lab)
    installed = lab.apply("file-mcp", environ={"KEYSTONE_SETUP_RESOURCE_S3": "catalog"})
    assert installed.code == 0, installed.error
    key = _secret_key("s3")
    old = read_env(_env_path(lab, "file-mcp"))[key]
    record["fail_run"] = record["runs"] + 2
    phase["rotate"] = True
    result = _rotate(lab, "file-mcp", "s3")
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "rotate_failed"
    assert "unknown state" in result.error["message"]
    assert "service apply file-mcp" in result.error["hint"]
    new = read_env(_env_path(lab, "file-mcp"))[key]
    assert new != old
    assert read_env(consumer_secret_path(lab.root, "file-mcp", "s3"))[key] == new
    assert new not in json.dumps(result.to_dict())
    assert old not in json.dumps(result.to_dict())


def test_rotate_external_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lab, _record = _install_external(tmp_path, monkeypatch)
    result = _rotate(lab, "file-mcp", "s3")
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "rotate_external_unsupported"
    hint = result.error["hint"].lower()
    assert "remove" in hint
    assert "install" in hint


def _install_external(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Lab, dict]:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    record = _prepare(lab)
    result = lab.apply("file-mcp", environ=lab.s3_answers())
    assert result.code == 0, result.error
    return lab, record


def test_external_reapply_does_not_replace_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, standard_manifests())
    first = lab.apply("file-mcp", environ=lab.s3_answers())
    assert first.code == 0, first.error
    env_path = _env_path(lab, "file-mcp")
    assert read_env(env_path)["FILE_MCP_S3_SECRET_KEY"] == SECRET_KEY
    again = lab.apply(
        "file-mcp",
        environ=lab.s3_answers(secret_key="second-secret-value-0123456789abcd"),
    )
    assert again.code == 0, again.error
    assert read_env(env_path)["FILE_MCP_S3_SECRET_KEY"] == SECRET_KEY


def test_rotate_kind_not_bound_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lab, _record = _install(
        tmp_path, monkeypatch, "file-mcp", {"KEYSTONE_SETUP_RESOURCE_S3": "catalog"}
    )
    result = _rotate(lab, "file-mcp", "postgres")
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "resource_not_bound"


def test_rotate_requires_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lab, _record = _install(
        tmp_path, monkeypatch, "file-mcp", {"KEYSTONE_SETUP_RESOURCE_S3": "catalog"}
    )
    result = _rotate(lab, "file-mcp", "s3", sets={})
    assert result.code == 2
    assert any(item["var"] == pkg_var("file-mcp", "CONFIRM_ROTATE") for item in result.missing)
