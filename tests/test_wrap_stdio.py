"""``keystonectl package wrap-stdio`` writes a box package from a provider directory."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from keystonectl.lint import lint_manifest
from keystonectl.runner import CommandResult, FakeCommandRunner, RealCommandRunner
from keystonectl.wrap_stdio import (
    CREDS_TOOLS,
    PROVIDER_SCHEMA_SHA256,
    STDIO_BOX_IMAGE,
    run_wrap_stdio,
)

_DATA = Path(__file__).parent / "data" / "stdio"
_ATLASSIAN = _DATA / "atlassian"
_NOTION = _DATA / "notion"
_TAVILY = _DATA / "tavily"
_POLICY = _DATA / "policy.example.atlassian.yaml"


def _copy(source: Path, root: Path) -> Path:
    destination = root / source.name
    shutil.copytree(source, destination)
    return destination


def _wrap(directory: Path, **kwargs):
    values = {
        "image": STDIO_BOX_IMAGE,
        "tools_snapshot": directory / "tools.snapshot.json",
        "no_discovery": False,
        "name": None,
        "namespace": None,
        "derived_image": False,
        "force": False,
        "runner": FakeCommandRunner(),
    }
    values.update(kwargs)
    return run_wrap_stdio(directory, **values)


def _manifest(directory: Path) -> dict:
    document = yaml.safe_load((directory / "keystone-service.yaml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_atlassian_package_lints_clean(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    result = _wrap(directory)
    assert result.code == 0, result.result
    assert result.result["namespace"] == "atlassian"
    assert result.result["package"] == "atlassian"
    assert result.result["discovery"] == "file"
    document = _manifest(directory)
    assert document["mcp"]["tools"] == [
        "jira_get_issue",
        "jira_search",
        "confluence_get_page",
        *CREDS_TOOLS,
    ]
    assert document["mcp"]["namespace"] == "atlassian"
    env = {item["name"]: item for item in document["containers"]["mcp"]["env"]}
    assert env["BOX_PROVIDER"]["default"] == "atlassian"
    assert env["BOX_COMMAND"]["default"] == "uvx mcp-atlassian==0.23.1"
    volumes = {item["name"] for item in document["containers"]["mcp"]["volumes"]}
    assert volumes == {"store", "cache"}
    assert document["public_paths"] == ["/creds/"]
    secret = document["containers"]["mcp"]["secrets"][0]
    assert secret["name"] == "BOX_STORE_KEY"
    assert secret["generate"]["bytes"] == 32
    problems, warnings = lint_manifest(
        directory / "keystone-service.yaml",
        directory,
        keystone_version=None,
    )
    assert [item for item in problems if item["severity"] == "error"] == []
    assert warnings == []


def test_discovery_runs_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("keystonectl.wrap_stdio.shutil.which", lambda _name: "/usr/bin/docker")
    directory = _copy(_ATLASSIAN, tmp_path)
    stdout = (directory / "tools.snapshot.json").read_text(encoding="utf-8")
    runner = FakeCommandRunner([(["docker"], CommandResult((), 0, stdout, ""))])
    result = _wrap(directory, tools_snapshot=None, runner=runner)
    assert result.code == 0, result.result
    assert result.result["discovery"] == "docker"
    assert len(runner.calls) == 1
    call = runner.calls[0]
    mounted = directory.resolve()
    assert list(call.args) == [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "stdio-box",
        "-v",
        f"{mounted}:/opt/box/packages/atlassian:ro",
        "-e",
        "BOX_PROVIDER_DIR=/opt/box/packages/atlassian",
        STDIO_BOX_IMAGE,
        "snapshot-tools",
        "--out",
        "-",
    ]
    assert call.timeout == 600
    assert (directory / "tools.snapshot.json").read_text(encoding="utf-8") == stdout


def test_discovery_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    monkeypatch.setattr("keystonectl.wrap_stdio.shutil.which", lambda _name: None)
    missing = _wrap(directory, tools_snapshot=None)
    assert missing.code == 3
    assert missing.error is not None
    assert missing.error["id"] == "docker_unavailable"
    assert missing.retryable is False
    assert "--tools-snapshot" in missing.error["hint"]

    monkeypatch.setattr("keystonectl.wrap_stdio.shutil.which", lambda _name: "/usr/bin/docker")
    runner = FakeCommandRunner([(["docker"], CommandResult((), 1, "", "install failed\n"))])
    failed = _wrap(directory, tools_snapshot=None, runner=runner)
    assert failed.code == 3
    assert failed.error is not None
    assert failed.error["id"] == "discovery_failed"
    assert failed.retryable is True
    assert "install failed" in failed.result["stderr"]


@pytest.mark.integration
def test_discovery_mounts_provider_named_directory(tmp_path: Path) -> None:
    """``wrap-stdio`` discovery must work for a provider not named ``provider``.

    Regression test for G12: stdio-box requires the mounted directory's name to
    equal ``name`` in ``provider.yaml``. The fixture uses tavily with its
    ``discovery: placeholder`` key, since ``tools/list`` does not need a real one.
    """
    if shutil.which("docker") is None:
        pytest.skip("Docker is not installed")
    directory = _copy(_TAVILY, tmp_path)
    result = run_wrap_stdio(
        directory,
        image=STDIO_BOX_IMAGE,
        tools_snapshot=None,
        no_discovery=False,
        name=None,
        namespace=None,
        derived_image=False,
        force=False,
        runner=RealCommandRunner(),
    )
    if result.code != 0:
        error_id = (result.error or {}).get("id")
        if error_id in {"docker_unavailable", "discovery_failed"}:
            pytest.skip(f"Docker or network is unavailable for live discovery: {result.result}")
        pytest.fail(f"wrap-stdio failed: {result.result}")
    assert result.result["discovery"] == "docker"
    snapshot = json.loads((directory / "tools.snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["tools"]


def test_update_keeps_author_fields(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    assert _wrap(directory).code == 0
    manifest = directory / "keystone-service.yaml"
    original = manifest.read_text(encoding="utf-8")
    edited = original.replace("version: 1.0.0\n", "version: 9.9.9\n# author-kept\n", 1)
    edited = edited.replace(
        'summary: "Atlassian (Jira and Confluence) tools (mcp-atlassian 0.23.1) '
        'behind Keystone; each person, service or role uses its own account."',
        'summary: "Author summary that must stay."',
        1,
    )
    manifest.write_text(edited, encoding="utf-8")
    provider = directory / "provider.yaml"
    provider.write_text(
        provider.read_text(encoding="utf-8").replace("0.23.1", "0.23.2"),
        encoding="utf-8",
    )
    snapshot = json.loads((directory / "tools.snapshot.json").read_text(encoding="utf-8"))
    snapshot["server"]["command"] = "uvx mcp-atlassian==0.23.2"
    snapshot["tools"].append(
        {
            "name": "jira_delete_issue",
            "inputSchema": {"type": "object"},
            "annotations": {"destructiveHint": True},
        }
    )
    updated_snapshot = directory / "next.snapshot.json"
    updated_snapshot.write_text(json.dumps(snapshot), encoding="utf-8")
    result = _wrap(directory, tools_snapshot=updated_snapshot)
    assert result.code == 0, result.result
    after = manifest.read_text(encoding="utf-8")
    document = yaml.safe_load(after)
    assert document["version"] == "9.9.9"
    assert document["summary"] == "Author summary that must stay."
    assert "# author-kept" in after
    assert "jira_delete_issue" in document["mcp"]["tools"]
    assert document["mcp"]["critical"][0] == "jira_delete_issue"
    env = {item["name"]: item for item in document["containers"]["mcp"]["env"]}
    assert env["BOX_COMMAND"]["default"] == "uvx mcp-atlassian==0.23.2"

    def _block(text: str, key: str) -> str:
        match = re.search(rf"(?m)^  {key}:\n(?:    - .*\n)+", text)
        assert match is not None, key
        return match.group(0)

    restored = after
    restored = restored.replace(_block(after, "tools"), _block(edited, "tools"))
    restored = restored.replace(_block(after, "critical"), _block(edited, "critical"))
    restored = restored.replace(
        'default: "uvx mcp-atlassian==0.23.2"',
        'default: "uvx mcp-atlassian==0.23.1"',
    )
    assert restored == edited


def test_without_snapshot_warns(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    (directory / "tools.snapshot.json").unlink()
    result = _wrap(directory, tools_snapshot=None, no_discovery=True)
    assert result.code == 0, result.result
    assert result.result["discovery"] == "skipped"
    assert result.result["tools"] == len(CREDS_TOOLS)
    assert [item["id"] for item in result.warnings] == ["tools_snapshot_missing"]
    assert _manifest(directory)["mcp"]["tools"] == list(CREDS_TOOLS)
    assert not (directory / "tools.snapshot.json").exists()


def test_hyphen_provider_namespace(tmp_path: Path) -> None:
    directory = tmp_path / "yandex-tracker"
    directory.mkdir()
    (directory / "requirements.txt").write_text("ya-tracker==5.1.0\n", encoding="utf-8")
    (directory / "provider.yaml").write_text(
        """
schema_version: 1
name: yandex-tracker
title: Yandex Tracker
server:
  command: "uvx ya-tracker==5.1.0"
  bin: ya-tracker
  lock: requirements.txt
fields:
  - {name: TOKEN, type: string, secret: true, hint: "Tracker token from the account page"}
variants:
  - name: org
    fields: [TOKEN]
    checks:
      - http:
          method: GET
          url: "https://api.tracker.yandex.net/v2/myself"
          auth: {bearer: TOKEN}
          expect_status: [200]
          identity: login
discovery:
  mode: placeholder
  env: {TOKEN: discovery-placeholder}
""".lstrip(),
        encoding="utf-8",
    )
    result = _wrap(directory, tools_snapshot=None, no_discovery=True)
    assert result.code == 0, result.result
    assert result.result["package"] == "yandex-tracker"
    assert result.result["namespace"] == "yandex_tracker"
    document = _manifest(directory)
    assert document["name"] == "yandex-tracker"
    assert document["mcp"]["namespace"] == "yandex_tracker"


def test_box_command_grammar(tmp_path: Path) -> None:
    uvx = _copy(_ATLASSIAN, tmp_path)
    provider = uvx / "provider.yaml"
    provider.write_text(
        provider.read_text(encoding="utf-8").replace(
            "uvx mcp-atlassian==0.23.1", "uvx mcp-atlassian"
        ),
        encoding="utf-8",
    )
    bare = _wrap(uvx, tools_snapshot=None, no_discovery=True)
    assert bare.code == 3
    assert bare.error is not None and bare.error["id"] == "provider_invalid"
    assert any("name==version" in item["message"] for item in bare.result["problems"])

    parent = tmp_path / "more"
    parent.mkdir()
    npx = _copy(_NOTION, parent)
    text = (npx / "provider.yaml").read_text(encoding="utf-8")
    (npx / "provider.yaml").write_text(
        text.replace(
            "npx -y @notionhq/notion-mcp-server@2.5.2",
            "npx pkg@^1",
        ),
        encoding="utf-8",
    )
    ranged = _wrap(npx, tools_snapshot=None, no_discovery=True)
    assert ranged.code == 3
    assert ranged.error is not None and ranged.error["id"] == "provider_invalid"
    assert any("exact" in item["message"] for item in ranged.result["problems"])


def test_secret_arg_rejected(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    provider = directory / "provider.yaml"
    provider.write_text(
        provider.read_text(encoding="utf-8").replace(
            '    hint: "API token from the Atlassian account security page"\n',
            '    hint: "API token from the Atlassian account security page"\n'
            '    to: {arg: "--token"}\n',
        ),
        encoding="utf-8",
    )
    result = _wrap(directory, tools_snapshot=None, no_discovery=True)
    assert result.code == 3
    assert result.error is not None and result.error["id"] == "provider_invalid"
    assert any(
        "cannot be passed as an argument" in item["message"] for item in result.result["problems"]
    )


def test_snake_names_and_conflicts(tmp_path: Path) -> None:
    directory = _copy(_NOTION, tmp_path)
    snapshot = {
        "schema_version": 1,
        "server": {"command": "npx -y @notionhq/notion-mcp-server@2.5.2"},
        "taken_at": "2026-01-01T00:00:00Z",
        "tools": [
            {"name": "API-post-search", "inputSchema": {"type": "object"}},
        ],
    }
    path = directory / "tools.snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    result = _wrap(directory, tools_snapshot=path)
    assert result.code == 0, result.result
    assert _manifest(directory)["mcp"]["tools"][0] == "post_search"

    snapshot["tools"].append({"name": "post_search", "inputSchema": {"type": "object"}})
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    conflict = _wrap(directory, tools_snapshot=path, force=True)
    assert conflict.code == 3
    assert conflict.error is not None and conflict.error["id"] == "provider_invalid"
    assert any(
        "duplicate published name" in item["message"] for item in conflict.result["problems"]
    )


def test_derived_image_dockerfile(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    result = _wrap(directory, derived_image=True)
    assert result.code == 0, result.result
    dockerfile = (directory / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile == f"FROM {STDIO_BOX_IMAGE}\nCOPY . /opt/box/packages/atlassian/\n"
    ignored = (directory / ".dockerignore").read_text(encoding="utf-8")
    assert ignored == "keystone-service.yaml\npolicy.example.yaml\n"
    text = (directory / "keystone-service.yaml").read_text(encoding="utf-8")
    assert "placeholder until the release workflow pins the digest" in text
    image = _manifest(directory)["containers"]["mcp"]["image"]
    assert image == "ghcr.io/keystone-codes/atlassian@sha256:" + ("0" * 64)


def test_policy_example_matches_box(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    assert _wrap(directory).code == 0
    assert (directory / "policy.example.yaml").read_text(encoding="utf-8") == _POLICY.read_text(
        encoding="utf-8"
    )


def test_vendored_schema_hash() -> None:
    data = files("keystonectl").joinpath("schemas/stdio-provider-v1.json").read_bytes()
    assert hashlib.sha256(data).hexdigest() == PROVIDER_SCHEMA_SHA256
