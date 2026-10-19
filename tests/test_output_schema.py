"""Code-0 ``result`` is typed for the six commands. Failures stay a free object."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
import yaml
from jsonschema import Draft202012Validator

from keystonectl.bundles import install_bundle
from keystonectl.cli import _service_list
from keystonectl.generate.status import run_status
from keystonectl.lifecycle import remove_service
from keystonectl.lock import load_lock
from keystonectl.output import Result, SecretRegistry
from keystonectl.resources import schema_text
from keystonectl.runner import CommandResult, FakeCommandRunner
from keystonectl.sources import list_catalogs
from tests.support import write_empty_lock
from tests.support.deploylab import apply_package
from tests.support.resources import (
    Lab,
    install_ok_probes,
    pg_consumer,
    s3_consumer,
    standard_manifests,
)
from tests.support.served_catalog import catalog_runner
from tests.test_bundle import _put_bundle

PACKAGES = Path(__file__).resolve().parent / "data" / "packages"
SCHEMA = json.loads(schema_text("output.schema.json"))
VALIDATOR = Draft202012Validator(SCHEMA)
LOCK_COPIED = (
    "timestamp",
    "sha256",
    "commit",
    "env_name",
    "container",
    "filters",
    "env",
    "public",
    "package_dirs",
    "previous",
    "service",
    "resource",
)
ENV = {"KEYSTONE_SETUP_KEYSTONE_MCP_VERSION": "0.5.0"}


def _validate(result: Result) -> None:
    assert result.code == 0, result.error
    VALIDATOR.validate(result.to_dict())


def _facade() -> str:
    document = yaml.safe_load((PACKAGES / "file-mcp" / "keystone-service.yaml").read_text())
    document.pop("needs", None)
    document["containers"]["mcp"].pop("env", None)
    return yaml.safe_dump(document)


def _ok() -> FakeCommandRunner:
    return FakeCommandRunner([([], lambda args, **_kwargs: CommandResult(tuple(args), 0, "", ""))])


def _status_runner(rows: list[dict]) -> FakeCommandRunner:
    def docker(args, **_kwargs):
        if args[0] == "docker" and args[1] == "info":
            return CommandResult(tuple(args), 0, "ok\n", "")
        if "config" in args:
            return CommandResult(tuple(args), 0, json.dumps({"name": "demo", "services": {}}), "")
        if "ps" in args:
            text = "".join(json.dumps(row) + "\n" for row in rows)
            return CommandResult(tuple(args), 0, text, "")
        if "exec" in args or "logs" in args or (len(args) > 1 and args[1] == "run"):
            return CommandResult(tuple(args), 0, "status:200\n", "")
        return CommandResult(tuple(args), 0, "", "")

    return FakeCommandRunner([(["docker"], docker)])


def test_service_and_resource_defs_match_lock_schema() -> None:
    """Sibling ``$ref`` is not resolved by the test validator, so the defs are copied."""

    lock = json.loads(schema_text("lock-v1.json"))
    for name in LOCK_COPIED:
        assert SCHEMA["$defs"][name] == lock["$defs"][name]


def test_service_apply_and_install_match_schema(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    _validate(result)
    assert result.result["resources"] == []
    installed = result.to_dict()
    installed["command"] = "service install"
    VALIDATOR.validate(installed)


def test_service_apply_reports_consumed_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, [s3_consumer(), *standard_manifests()[5:]])
    result = lab.apply(
        "file-mcp",
        environ={
            **lab.s3_answers(),
            "KEYSTONE_SETUP_SERVICE_FILE_MCP_ENV_FILE_MCP_MAX_BYTES": "10",
            "KEYSTONE_SETUP_SERVICE_FILE_MCP_ENV_FILE_MCP_LINK_TTL_SECONDS": "60",
        },
    )
    _validate(result)
    assert result.result["resources"] == [{"kind": "s3", "origin": "external", "provider": None}]
    assert "s3" in result.result["probes"]

    postgres = Lab(tmp_path / "pg", [pg_consumer(), *standard_manifests()[6:]])
    pg = postgres.apply(
        "notes-mcp",
        environ={
            **postgres.pg_answers(),
            "KEYSTONE_SETUP_SERVICE_NOTES_MCP_ENV_FILE_MCP_MAX_BYTES": "10",
            "KEYSTONE_SETUP_SERVICE_NOTES_MCP_ENV_FILE_MCP_LINK_TTL_SECONDS": "60",
        },
    )
    _validate(pg)
    assert pg.result["resources"] == [{"kind": "postgres", "origin": "external", "provider": None}]
    assert "postgres" in pg.result["probes"]


def test_service_list_status_and_remove_match_schema(tmp_path: Path) -> None:
    root = tmp_path / "root"
    applied, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    assert applied.code == 0, applied.error
    listed = _service_list(root, ENV)
    _validate(listed)
    assert listed.result["services"]
    assert isinstance(listed.result["services"][0], dict)

    rows = [
        {"Service": "file-mcp-mcp", "State": "running", "ExitCode": 0, "Health": "healthy"},
    ]
    status = run_status(root, _status_runner(rows))
    _validate(status)

    removed = remove_service(
        root,
        "file-mcp",
        runner=_ok(),
        secrets=SecretRegistry(),
        sets={"KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE": "yes"},
        environ=ENV,
        interactive=False,
        clock=lambda: datetime.now(UTC),
    )
    _validate(removed)
    assert isinstance(removed.result["kept"], list)


def test_catalog_list_matches_schema(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_empty_lock(root)
    (root / "sources.yaml").write_text(
        "schema_version: 1\n"
        "sources:\n"
        "  - name: keystone-codes\n"
        "    url: https://example.com/keystone-codes.git\n"
        "    ref: v1.0.0\n"
        "    priority: 100\n",
        encoding="utf-8",
    )
    result = list_catalogs(
        root,
        catalog_runner(),
        keystone_version="0.5.0",
        lock=load_lock(root / "services.lock.yaml"),
    )
    _validate(result)
    assert result.result["sources"]
    assert result.result["packages"]
    assert "bundles" in result.result


def test_bundle_plan_and_install_match_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_ok_probes(monkeypatch)
    lab = Lab(tmp_path, [s3_consumer(), *standard_manifests()[5:]])
    _put_bundle(lab, "minimal", ["file-mcp"])
    answers = {
        "KEYSTONE_SETUP_RESOURCE_S3": "catalog",
        "KEYSTONE_SETUP_SERVICE_FILE_MCP_ENV_FILE_MCP_MAX_BYTES": "10",
        "KEYSTONE_SETUP_SERVICE_FILE_MCP_ENV_FILE_MCP_LINK_TTL_SECONDS": "60",
    }
    common = {
        "runner": lab.runner,
        "secrets": lab.secrets,
        "sets": {},
        "environ": {**ENV, **answers},
        "interactive": False,
        "deployer": lab.deployer,
        "defer_public": True,
    }
    planned = install_bundle(lab.root, "local/minimal", plan_only=True, **common)
    _validate(planned)
    installed = install_bundle(lab.root, "local/minimal", plan_only=False, **common)
    _validate(installed)


def test_extra_apply_field_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    document = result.to_dict()
    document["result"]["extra"] = True
    with pytest.raises(jsonschema.ValidationError):
        VALIDATOR.validate(document)


def test_service_list_of_names_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apply_package(root, "file-mcp", {"file-mcp": _facade()})
    document = _service_list(root, ENV).to_dict()
    document["result"]["services"] = ["file-mcp"]
    with pytest.raises(jsonschema.ValidationError):
        VALIDATOR.validate(document)


def test_nonzero_code_keeps_a_free_result(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result, _runner, _box = apply_package(root, "file-mcp", {"file-mcp": _facade()})
    document = result.to_dict()
    document["ok"] = False
    document["code"] = 3
    document["error"] = {"id": "service_unhealthy", "message": "down", "hint": "see the logs"}
    document["retryable"] = True
    document["result"] = {
        "rollback": {"containers": ["file-mcp-mcp"], "lock_entry": "dropped"},
        "unexpected": {"nested": True},
    }
    VALIDATOR.validate(document)
