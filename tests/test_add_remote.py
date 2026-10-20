"""service add-remote, remote removal, and check warnings."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from keystonectl.check import run_check
from keystonectl.lifecycle import remove_service, upgrade_service
from keystonectl.output import SecretRegistry
from keystonectl.remote import add_remote
from keystonectl.runner import CommandResult, FakeCommandRunner
from tests.support import mode, write_empty_lock
from tests.support.deploylab import CONFIG, LAYOUT

URL = "http://analytics-mcp-http:8081/mcp"


class RemoteDocker:
    def __init__(self) -> None:
        self.probe_code = 0
        self.validate_code = 0
        self.running = ""
        self.calls: list[list[str]] = []

    def __call__(self, args, **_kwargs) -> CommandResult:
        self.calls.append(list(args))
        if args[1] == "compose" and "version" in args:
            return CommandResult(tuple(args), 0, "2.29.1\n", "")
        if args[1] == "compose" and "config" in args:
            return CommandResult(tuple(args), 0, json.dumps(CONFIG), "")
        if args[:3] == ["docker", "image", "inspect"] or (args[1] == "pull"):
            return CommandResult(tuple(args), 0, "[]", "")
        if args[1] == "run" and "validate" in args:
            err = "registry rejected" if self.validate_code else ""
            return CommandResult(tuple(args), self.validate_code, "", err)
        if args[1] == "run":
            return CommandResult(tuple(args), self.probe_code, "status:401\n", "")
        if args[1] == "compose" and "ps" in args:
            return CommandResult(tuple(args), 0, self.running, "")
        return CommandResult(tuple(args), 0, "", "")


def _root(tmp_path: Path) -> tuple[Path, RemoteDocker, FakeCommandRunner]:
    write_empty_lock(tmp_path)
    lock = yaml.safe_load((tmp_path / "services.lock.yaml").read_text(encoding="utf-8"))
    lock["layout"] = LAYOUT
    (tmp_path / "services.lock.yaml").write_text(
        yaml.safe_dump(lock, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "docker-compose.packages.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "packages").mkdir()
    (tmp_path / "keystonectl").mkdir()
    (tmp_path / "keystonectl" / "upstreams.yaml").write_text(
        "schema_version: 1\nentries: []\n", encoding="utf-8"
    )
    (tmp_path / "keystonectl" / "catalog-snapshot.yaml").write_text(
        "active: []\nreserved: []\nwip: []\n", encoding="utf-8"
    )
    box = RemoteDocker()
    return tmp_path, box, FakeCommandRunner([(["docker"], box)])


def _add(root, runner, name="analytics-mcp", url=URL, **kwargs):
    return add_remote(
        root,
        name,
        url,
        runner=runner,
        secrets=SecretRegistry(),
        sets={},
        environ={},
        interactive=False,
        **kwargs,
    )


def _entry(root: Path, name: str = "analytics-mcp") -> dict:
    document = yaml.safe_load((root / "keystonectl" / "upstreams.yaml").read_text(encoding="utf-8"))
    return next(item for item in document["entries"] if item["name"] == name)


def _lock_service(root: Path, name: str = "analytics-mcp") -> dict:
    lock = yaml.safe_load((root / "services.lock.yaml").read_text(encoding="utf-8"))
    return next(item for item in lock["services"] if item["name"] == name)


def test_add_remote_writes_registry_lock_and_handoff_file(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    result = _add(root, runner)
    assert result.code == 0, result.error
    handoff = root / "packages" / "analytics-mcp.env"
    assert mode(handoff) == 0o600
    text = handoff.read_text(encoding="utf-8")
    assert "# audience (aud) = analytics-mcp" in text
    assert "KEYSTONE_MCP_UPSTREAM_ANALYTICS_BEARER=" in text
    assert "KEYSTONE_MCP_UPSTREAM_ANALYTICS_IDENTITY_SECRET=" in text
    rendered = json.dumps(result.to_dict())
    secret = text.split("BEARER=", 1)[1].splitlines()[0]
    assert secret not in rendered
    assert result.result["handoff_file"] == "packages/analytics-mcp.env"
    assert "give this file" in result.summary
    entry = _lock_service(root)
    assert entry["managed"] == "remote"
    assert entry["registry_name"] == "analytics-mcp"
    assert entry["env"]["files"] == ["packages/analytics-mcp.env"]


def test_add_remote_secrets_generated_once(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    assert _add(root, runner).code == 0
    first = (root / ".env").read_text(encoding="utf-8")
    assert _add(root, runner).changes == 0
    assert (root / ".env").read_text(encoding="utf-8") == first


def test_add_remote_no_auth_warns_and_has_no_auth_block(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    result = _add(root, runner, no_auth=True)
    assert result.code == 0, result.error
    assert any(item["id"] == "remote_without_identity" for item in result.warnings)
    assert "auth" not in _entry(root)
    assert not (root / "packages" / "analytics-mcp.env").exists()


def test_add_remote_no_auth_summary_does_not_ask_to_check_bearer(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    result = _add(root, runner, no_auth=True)
    assert result.code == 0, result.error
    assert "must check" not in result.summary
    assert (
        "No bearer or identity secret was issued. Any container on the services "
        "network can call this service as anyone."
    ) in result.summary


def test_add_remote_with_auth_summary_still_asks_to_check_bearer(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    result = _add(root, runner)
    assert result.code == 0, result.error
    assert "The service must check the bearer and X-Keystone-Identity. aud = analytics-mcp." in (
        result.summary
    )


def test_add_remote_namespace_default_strips_mcp(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    result = _add(root, runner)
    assert result.result["namespace"] == "analytics"
    assert _entry(root)["namespace"] == "analytics"


def test_add_remote_namespace_collision_asks(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    result = _add(root, runner, name="users-mcp", url="http://users-mcp:8081/mcp")
    assert result.code == 2
    assert result.missing[0]["var"] == "KEYSTONE_SETUP_SERVICE_USERS_MCP_NAMESPACE"


def test_add_remote_http_only_for_docker_names(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    result = _add(root, runner, url="http://example.com/mcp")
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "registry_invalid"


def test_add_remote_allow_insecure_http_written(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    assert _add(root, runner).code == 0
    assert "KEYSTONE_MCP_UPSTREAM_ALLOW_INSECURE_HTTP=true" in (root / ".env").read_text(
        encoding="utf-8"
    )


def test_add_remote_snapshot_untouched_without_tools(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    before = (root / "keystonectl" / "catalog-snapshot.yaml").read_text(encoding="utf-8")
    assert _add(root, runner).code == 0
    assert (root / "keystonectl" / "catalog-snapshot.yaml").read_text(encoding="utf-8") == before
    assert _lock_service(root)["tools"] == []


def test_add_remote_tools_into_snapshot(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    result = _add(root, runner, tools="create,read")
    assert result.code == 0, result.error
    snapshot = yaml.safe_load(
        (root / "keystonectl" / "catalog-snapshot.yaml").read_text(encoding="utf-8")
    )
    assert snapshot["active"] == ["analytics_create", "analytics_read"]
    assert _lock_service(root)["tools"] == ["create", "read"]


def test_check_warns_remote_tools_not_in_snapshot(tmp_path: Path, monkeypatch) -> None:
    root, _box, runner = _root(tmp_path)
    assert _add(root, runner).code == 0
    monkeypatch.setattr(
        "keystonectl.remote.fetch_live_names",
        lambda *_args, **_kwargs: ["analytics_create", "analytics_read"],
    )
    result = run_check(root, environ={})
    assert any(item["id"] == "remote_tools_not_in_snapshot" for item in result.warnings)
    message = next(
        item["message"] for item in result.warnings if item["id"] == "remote_tools_not_in_snapshot"
    )
    assert "keystonectl service add-remote analytics-mcp --url" in message
    assert "--tools create,read" in message


def test_check_warns_remote_tools_unknown_without_live_list(tmp_path: Path, monkeypatch) -> None:
    root, _box, runner = _root(tmp_path)
    assert _add(root, runner).code == 0
    monkeypatch.setattr("keystonectl.remote.fetch_live_names", lambda *_a, **_k: None)
    result = run_check(root, environ={})
    assert any(item["id"] == "remote_tools_unknown" for item in result.warnings)
    message = next(
        item["message"] for item in result.warnings if item["id"] == "remote_tools_unknown"
    )
    assert "analytics_*" in message


def test_add_remote_adopt_existing_keeps_auth_and_secrets(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    _seed_foreign(root)
    before = (root / ".env").read_text(encoding="utf-8")
    result = _add(root, runner, adopt_existing=True)
    assert result.code == 0, result.error
    assert (root / ".env").read_text(encoding="utf-8") == before
    assert not (root / "packages" / "analytics-mcp.env").exists()
    assert _lock_service(root)["env"]["files"] == []
    assert "KEYSTONE_MCP_UPSTREAM_ANALYTICS_BEARER" in _lock_service(root)["env"]["keystone"]


def test_add_remote_adopt_existing_url_mismatch(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    _seed_foreign(root)
    result = _add(root, runner, url="http://other:8081/mcp", adopt_existing=True)
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "remote_url_mismatch"


def test_add_remote_existing_foreign_entry_without_flag_refused(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    _seed_foreign(root)
    result = _add(root, runner)
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "registry_name_taken"


def test_add_remote_prefix_filters_written_to_registry_and_lock(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    result = _add(
        root,
        runner,
        tool_prefix="pub_",
        include_prefixes="pub_,ext_",
        exclude_prefixes="ext_secret",
        strip_prefix="pub_",
    )
    assert result.code == 0, result.error
    entry = _entry(root)
    assert entry["tool_prefix"] == "pub_"
    assert entry["include_prefixes"] == ["pub_", "ext_"]
    assert entry["exclude_prefixes"] == ["ext_secret"]
    assert entry["strip_prefix"] == "pub_"
    filters = _lock_service(root)["filters"]
    assert filters["tool_prefix"] == "pub_"
    assert filters["include_prefixes"] == ["pub_", "ext_"]
    assert filters["exclude_prefixes"] == ["ext_secret"]
    assert filters["strip_prefix"] == "pub_"


def test_add_remote_adopt_existing_keeps_filters(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    _seed_foreign(
        root,
        extra={
            "tool_prefix": "bill_",
            "include_prefixes": ["bill_"],
            "exclude_prefixes": ["bill_hidden"],
            "strip_prefix": "bill_",
        },
    )
    result = _add(root, runner, adopt_existing=True)
    assert result.code == 0, result.error
    filters = _lock_service(root)["filters"]
    assert filters["tool_prefix"] == "bill_"
    assert filters["strip_prefix"] == "bill_"
    assert _entry(root)["include_prefixes"] == ["bill_"]


def test_add_remote_unreachable_is_warning(tmp_path: Path) -> None:
    root, box, runner = _root(tmp_path)
    box.probe_code = 1
    result = _add(root, runner)
    assert result.code == 0, result.error
    assert result.result["reachable"] is False
    assert any(item["id"] == "remote_unreachable" for item in result.warnings)


def test_add_remote_validation_runs_and_keystone_restarted_when_running(tmp_path: Path) -> None:
    root, box, runner = _root(tmp_path)
    box.running = "container-id\n"
    result = _add(root, runner)
    assert result.code == 0, result.error
    assert any("validate" in call for call in box.calls)
    assert any("restart" in call for call in box.calls)


def test_add_remote_rerun_same_changes_zero(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    assert _add(root, runner, tools="create").code == 0
    again = _add(root, runner, tools="create")
    assert again.code == 0
    assert again.changes == 0


def test_add_remote_url_change_updates_entry(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    assert _add(root, runner).code == 0
    updated = _add(root, runner, url="http://analytics-mcp-http:9090/mcp")
    assert updated.code == 0, updated.error
    assert updated.changes > 0
    assert _entry(root)["url"] == "http://analytics-mcp-http:9090/mcp"
    assert _lock_service(root)["namespace"] == "analytics"


def test_remove_remote_entry(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    assert _add(root, runner).code == 0
    result = remove_service(
        root,
        "analytics-mcp",
        runner=runner,
        secrets=SecretRegistry(),
        sets={},
        environ={"KEYSTONE_SETUP_SERVICE_ANALYTICS_MCP_CONFIRM_REMOVE": "yes"},
        interactive=False,
        clock=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    assert result.code == 0, result.error
    document = yaml.safe_load((root / "keystonectl" / "upstreams.yaml").read_text(encoding="utf-8"))
    assert document["entries"] == []
    assert not (root / "packages" / "analytics-mcp.env").exists()
    env = (root / ".env").read_text(encoding="utf-8")
    assert "KEYSTONE_MCP_UPSTREAM_ANALYTICS_BEARER" not in env
    lock = yaml.safe_load((root / "services.lock.yaml").read_text(encoding="utf-8"))
    assert lock["services"] == []
    assert all("network" not in call.args or "rm" not in call.args for call in runner.calls)


def test_upgrade_remote_not_package(tmp_path: Path) -> None:
    root, _box, runner = _root(tmp_path)
    assert _add(root, runner).code == 0
    result = upgrade_service(
        root,
        "analytics-mcp",
        runner=runner,
        secrets=SecretRegistry(),
        sets={},
        environ={},
        interactive=False,
        clock=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "not_package"
    assert "add-remote" in result.error["message"]


def test_no_secret_in_output_lock_or_args(tmp_path: Path) -> None:
    root, box, runner = _root(tmp_path)
    secrets = SecretRegistry()
    result = add_remote(
        root,
        "analytics-mcp",
        URL,
        runner=runner,
        secrets=secrets,
        sets={},
        environ={},
        interactive=False,
    )
    assert result.code == 0, result.error
    handoff = (root / "packages" / "analytics-mcp.env").read_text(encoding="utf-8")
    secret = handoff.split("BEARER=", 1)[1].splitlines()[0]
    assert secret
    blob = json.dumps(result.to_dict()) + (root / "services.lock.yaml").read_text(encoding="utf-8")
    argv = " ".join(" ".join(call) for call in box.calls)
    assert secret not in blob
    assert secret not in argv
    assert secret not in result.summary
    os.chmod(root / "packages" / "analytics-mcp.env", 0o600)


def _seed_foreign(root: Path, *, extra: dict | None = None) -> None:
    entry = {
        "name": "analytics-mcp",
        "url": URL,
        "namespace": "analytics",
        "enabled": True,
        "auth": {
            "bearer_env": "KEYSTONE_MCP_UPSTREAM_ANALYTICS_BEARER",
            "identity_secret_env": "KEYSTONE_MCP_UPSTREAM_ANALYTICS_IDENTITY_SECRET",
        },
    }
    if extra:
        entry.update(extra)
    text = "schema_version: 1\nentries:\n"
    (root / "keystonectl" / "upstreams.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "entries": [entry]}, sort_keys=False),
        encoding="utf-8",
    )
    (root / ".env").write_text(
        "# >>> keystonectl: managed block, do not edit >>>\n"
        "KEYSTONE_MCP_UPSTREAM_ANALYTICS_BEARER=kept-bearer\n"
        "KEYSTONE_MCP_UPSTREAM_ANALYTICS_IDENTITY_SECRET=kept-identity\n"
        "# <<< keystonectl <<<\n",
        encoding="utf-8",
    )
    os.chmod(root / ".env", 0o600)
    del text
