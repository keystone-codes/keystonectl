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

from keystonectl.cli import main
from keystonectl.lint import lint_manifest
from keystonectl.runner import CommandResult, FakeCommandRunner, RealCommandRunner
from keystonectl.wrap_stdio import (
    CHANGING_CREDS,
    CREDS_TOOLS,
    PROVIDER_SCHEMA_SHA256,
    STDIO_BOX_IMAGE,
    run_wrap_stdio,
)

_DATA = Path(__file__).parent / "data" / "stdio"
_ATLASSIAN = _DATA / "atlassian"
_NOTION = _DATA / "notion"
_TAVILY = _DATA / "tavily"
_REMOTE = _DATA / "example-remote"
_POLICY = _DATA / "policy.example.atlassian.yaml"
_BOX_TOOLS = ["docker", "run", "--rm", "--entrypoint", "stdio-box", STDIO_BOX_IMAGE, "box-tools"]
_IMAGE_TOOLS = [*CREDS_TOOLS[:9], "creds_snapshot_tools", CREDS_TOOLS[9]]


def _no_box_tools() -> FakeCommandRunner:
    """stdio-box 0.1.0 has no ``box-tools`` subcommand: it prints usage and exits 2."""
    return FakeCommandRunner([(_BOX_TOOLS, CommandResult((), 2, "", "usage: stdio-box"))])


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
        "runner": _no_box_tools(),
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
    runner = FakeCommandRunner(
        [
            (_BOX_TOOLS, CommandResult((), 2, "", "usage: stdio-box")),
            (["docker"], CommandResult((), 0, stdout, "")),
        ]
    )
    result = _wrap(directory, tools_snapshot=None, runner=runner)
    assert result.code == 0, result.result
    assert result.result["discovery"] == "docker"
    assert len(runner.calls) == 2
    assert list(runner.calls[1].args) == _BOX_TOOLS
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
    assert "tools.snapshot.json" in result.result["files"]
    assert (
        json.loads((directory / "tools.snapshot.json").read_text(encoding="utf-8"))["tools"] == []
    )


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


def _lint_errors(directory: Path) -> list[dict]:
    problems, _warnings = lint_manifest(
        directory / "keystone-service.yaml", directory, keystone_version=None, check_files=True
    )
    return [item for item in problems if item["severity"] == "error"]


def _env(document: dict) -> dict[str, dict]:
    return {item["name"]: item for item in document["containers"]["mcp"]["env"]}


def test_stock_image_mounts_package_dir(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    result = _wrap(directory, stock_image=True)
    assert result.code == 0, result.result
    document = _manifest(directory)
    container = document["containers"]["mcp"]
    assert container["image"] == STDIO_BOX_IMAGE
    # stdio-box checks that provider.yaml's name equals the name of its folder.
    assert container["package_dir"] == {"from": ".", "path": "/opt/box/packages/atlassian"}
    env = _env(document)
    assert env["BOX_PROVIDER_DIR"]["default"] == "/opt/box/packages/atlassian"
    assert env["BOX_PROVIDER"]["default"] == "atlassian"
    assert not (directory / "Dockerfile").exists()
    assert _lint_errors(directory) == []


def test_stock_image_and_derived_image_are_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    both = _wrap(directory, stock_image=True, derived_image=True)
    assert both.code == 1
    assert both.error is not None and both.error["id"] == "usage"
    assert not (directory / "keystone-service.yaml").exists()

    monkeypatch.setattr("keystonectl.wrap_stdio.shutil.which", lambda _name: None)
    argv = ["package", "wrap-stdio", str(directory), "--stock-image", "--derived-image", "--json"]
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert "not allowed with argument" in captured.out + captured.err


def test_stock_image_update_adds_missing_keys(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    assert _wrap(directory).code == 0
    manifest = directory / "keystone-service.yaml"
    before = manifest.read_text(encoding="utf-8")
    assert "package_dir" not in before

    result = _wrap(directory, stock_image=True)
    assert result.code == 0, result.result
    after = manifest.read_text(encoding="utf-8")
    document = yaml.safe_load(after)
    assert document["containers"]["mcp"]["package_dir"] == {
        "from": ".",
        "path": "/opt/box/packages/atlassian",
    }
    assert _env(document)["BOX_PROVIDER_DIR"]["default"] == "/opt/box/packages/atlassian"
    assert [item["name"] for item in document["containers"]["mcp"]["env"]] == [
        "BOX_PROVIDER",
        "BOX_COMMAND",
        "BOX_PROVIDER_DIR",
    ]
    added = [line for line in after.splitlines() if line not in before.splitlines()]
    assert len(added) == len(after.splitlines()) - len(before.splitlines())
    assert _lint_errors(directory) == []

    again = _wrap(directory, stock_image=True)
    assert again.code == 0, again.result
    assert manifest.read_text(encoding="utf-8") == after


def _old_stock_manifest(directory: Path, *, flow: bool = False) -> str:
    """A manifest as --stock-image wrote it before the path was named after the provider."""
    manifest = directory / "keystone-service.yaml"
    text = manifest.read_text(encoding="utf-8")
    new_dir = '"/opt/box/packages/atlassian"'
    old = text.replace("path: /opt/box/packages/atlassian\n", "path: /opt/box/provider  # mount\n")
    old = old.replace(f"default: {new_dir}\n", 'default: "/opt/box/provider"  # box\n')
    if flow:
        old = old.replace(
            '    package_dir:\n      from: "."\n      path: /opt/box/provider  # mount\n',
            '    package_dir: {from: ".", path: /opt/box/provider}  # mount\n',
        )
    assert old != text
    manifest.write_text(old, encoding="utf-8")
    return old


@pytest.mark.parametrize("flow", [False, True])
def test_stock_image_update_moves_old_path(tmp_path: Path, flow: bool) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    assert _wrap(directory, stock_image=True).code == 0
    old = _old_stock_manifest(directory, flow=flow)
    assert yaml.safe_load(old)["containers"]["mcp"]["package_dir"]["path"] == "/opt/box/provider"

    result = _wrap(directory, stock_image=True)
    assert result.code == 0, result.result
    after = (directory / "keystone-service.yaml").read_text(encoding="utf-8")
    document = yaml.safe_load(after)
    assert document["containers"]["mcp"]["package_dir"] == {
        "from": ".",
        "path": "/opt/box/packages/atlassian",
    }
    assert _env(document)["BOX_PROVIDER_DIR"]["default"] == "/opt/box/packages/atlassian"
    assert "/opt/box/provider" not in after
    # Only the two paths change; comments and every other line stay.
    changed = [
        (before, now)
        for before, now in zip(old.splitlines(), after.splitlines(), strict=True)
        if before != now
    ]
    assert len(changed) == 2
    assert all("# mount" in now or "# box" in now for _before, now in changed)
    assert _lint_errors(directory) == []

    again = _wrap(directory, stock_image=True)
    assert again.code == 0, again.result
    assert (directory / "keystone-service.yaml").read_text(encoding="utf-8") == after


def test_stock_image_update_keeps_author_path(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    assert _wrap(directory, stock_image=True).code == 0
    manifest = directory / "keystone-service.yaml"
    custom = manifest.read_text(encoding="utf-8").replace(
        "/opt/box/packages/atlassian", "/opt/box/packages/atlassian-custom"
    )
    manifest.write_text(custom, encoding="utf-8")

    result = _wrap(directory, stock_image=True)
    assert result.code == 0, result.result
    assert manifest.read_text(encoding="utf-8") == custom


def test_without_stock_image_old_path_stays(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    assert _wrap(directory, stock_image=True).code == 0
    old = _old_stock_manifest(directory)

    result = _wrap(directory)
    assert result.code == 0, result.result
    assert (directory / "keystone-service.yaml").read_text(encoding="utf-8") == old


def test_no_discovery_writes_empty_snapshot(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    (directory / "tools.snapshot.json").unlink()
    result = _wrap(directory, tools_snapshot=None, no_discovery=True, stock_image=True)
    assert result.code == 0, result.result
    assert [item["id"] for item in result.warnings] == ["tools_snapshot_missing"]
    text = (directory / "tools.snapshot.json").read_text(encoding="utf-8")
    snapshot = json.loads(text)
    # The form of stdio-box snapshot.build_snapshot and render_snapshot.
    assert set(snapshot) == {"schema_version", "server", "taken_at", "tools"}
    assert snapshot["schema_version"] == 1
    assert snapshot["server"] == {"command": "uvx mcp-atlassian==0.23.1"}
    assert snapshot["tools"] == []
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", snapshot["taken_at"])
    assert text == json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    assert _lint_errors(directory) == []

    # The box's own shape check (catalog._check_snapshot_shape) is mirrored by _parse_snapshot.
    reread = _wrap(directory, tools_snapshot=directory / "tools.snapshot.json")
    assert reread.code == 0, reread.result
    assert _manifest(directory)["mcp"]["tools"] == list(CREDS_TOOLS)


def test_no_discovery_keeps_existing_snapshot(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    original = (directory / "tools.snapshot.json").read_text(encoding="utf-8")
    result = _wrap(directory, tools_snapshot=None, no_discovery=True)
    assert result.code == 0, result.result
    assert "tools.snapshot.json" not in result.result["files"]
    assert (directory / "tools.snapshot.json").read_text(encoding="utf-8") == original


def test_metadata_flags_on_new_manifest(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    result = _wrap(
        directory,
        publisher="example",
        homepage="https://example.com/atlassian",
        version="2.3.4",
    )
    assert result.code == 0, result.result
    document = _manifest(directory)
    assert document["publisher"] == "example"
    assert document["homepage"] == "https://example.com/atlassian"
    assert document["version"] == "2.3.4"


def test_metadata_flags_update_in_place(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    assert _wrap(directory).code == 0
    manifest = directory / "keystone-service.yaml"
    before = manifest.read_text(encoding="utf-8")

    unchanged = _wrap(directory, publisher="example")
    assert unchanged.code == 0, unchanged.result
    middle = manifest.read_text(encoding="utf-8")
    assert yaml.safe_load(middle)["version"] == "1.0.0"
    assert middle == before.replace("publisher: keystone-codes\n", "publisher: example\n")

    result = _wrap(directory, version="1.1.0", homepage="https://example.com/box")
    assert result.code == 0, result.result
    after = manifest.read_text(encoding="utf-8")
    document = yaml.safe_load(after)
    assert document["version"] == "1.1.0"
    assert document["publisher"] == "example"
    assert document["homepage"] == "https://example.com/box"
    assert len(after.splitlines()) == len(before.splitlines())


@pytest.mark.parametrize(
    "flags",
    [{"version": "1.0"}, {"version": "v1.0.0"}, {"version": "1.0.0-rc.1"}, {"publisher": "Ex"}],
)
def test_bad_metadata_flag_is_usage(tmp_path: Path, flags: dict) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    assert _wrap(directory).code == 0
    before = (directory / "keystone-service.yaml").read_text(encoding="utf-8")
    result = _wrap(directory, **flags)
    assert result.code == 1
    assert result.error is not None and result.error["id"] == "usage"
    assert (directory / "keystone-service.yaml").read_text(encoding="utf-8") == before


def test_box_tools_from_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("keystonectl.wrap_stdio.shutil.which", lambda _name: "/usr/bin/docker")
    directory = _copy(_ATLASSIAN, tmp_path)
    runner = FakeCommandRunner([(_BOX_TOOLS, CommandResult((), 0, json.dumps(_IMAGE_TOOLS), ""))])
    result = _wrap(directory, runner=runner, stock_image=True)
    assert result.code == 0, result.result
    assert result.result["box_tools_source"] == "image"
    assert [list(call.args) for call in runner.calls] == [_BOX_TOOLS]
    document = _manifest(directory)
    assert document["mcp"]["tools"][3:] == _IMAGE_TOOLS
    assert "creds_snapshot_tools" not in document["mcp"]["critical"]
    assert document["mcp"]["critical"] == list(CHANGING_CREDS)
    policy = yaml.safe_load((directory / "policy.example.yaml").read_text(encoding="utf-8"))
    assert "atlassian_creds_snapshot_tools" not in policy["critical_tools"]
    assert "atlassian_creds_snapshot_tools" not in policy["requires_human_identity"]
    assert _lint_errors(directory) == []


@pytest.mark.parametrize(
    "outcome",
    [
        CommandResult((), 2, "", "usage: stdio-box"),
        CommandResult((), 0, "not json", ""),
        CommandResult((), 0, json.dumps({"tools": _IMAGE_TOOLS}), ""),
        CommandResult((), 0, json.dumps([*_IMAGE_TOOLS, "jira_search"]), ""),
        CommandResult((), 0, json.dumps([*_IMAGE_TOOLS, 7]), ""),
        CommandResult((), 0, json.dumps([]), ""),
        CommandResult((), 0, json.dumps(["creds_whoami"]), ""),
    ],
)
def test_box_tools_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: CommandResult
) -> None:
    monkeypatch.setattr("keystonectl.wrap_stdio.shutil.which", lambda _name: "/usr/bin/docker")
    directory = _copy(_ATLASSIAN, tmp_path)
    runner = FakeCommandRunner([(_BOX_TOOLS, outcome)])
    result = _wrap(directory, runner=runner)
    assert result.code == 0, result.result
    assert result.result["box_tools_source"] == "fallback"
    assert _manifest(directory)["mcp"]["tools"][3:] == list(CREDS_TOOLS)


def test_box_tools_without_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("keystonectl.wrap_stdio.shutil.which", lambda _name: None)
    directory = _copy(_ATLASSIAN, tmp_path)
    runner = FakeCommandRunner()
    result = _wrap(directory, runner=runner)
    assert result.code == 0, result.result
    assert result.result["box_tools_source"] == "fallback"
    assert runner.calls == []


def test_read_only_and_destructive_override_hints(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    result = _wrap(directory, read_only=["jira_search"], destructive=["confluence_get_page"])
    assert result.code == 0, result.result
    document = _manifest(directory)
    assert document["mcp"]["critical"] == ["confluence_get_page", *CHANGING_CREDS]
    policy = yaml.safe_load((directory / "policy.example.yaml").read_text(encoding="utf-8"))
    roles = policy["roles"]
    assert roles["atlassian_reader"]["allow"] == [
        "atlassian_jira_search",
        "atlassian_creds_whoami",
        "atlassian_creds_providers",
    ]
    assert roles["atlassian_writer"]["allow"][:3] == [
        "atlassian_jira_get_issue",
        "atlassian_jira_search",
        "atlassian_confluence_get_page",
    ]
    assert policy["critical_tools"][0] == "atlassian_confluence_get_page"
    assert _lint_errors(directory) == []

    only_destructive = _wrap(directory, destructive=["jira_get_issue"])
    assert only_destructive.code == 0, only_destructive.result
    policy = yaml.safe_load((directory / "policy.example.yaml").read_text(encoding="utf-8"))
    # Without --read-only the snapshot's readOnlyHint still decides the reader role.
    assert len(policy["roles"]["atlassian_reader"]["allow"]) == 5
    assert _manifest(directory)["mcp"]["critical"][0] == "jira_get_issue"


def test_read_only_and_destructive_errors(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    unknown = _wrap(directory, read_only=["jira_search", "atlassian_jira_search"])
    assert unknown.code == 3
    assert unknown.error is not None and unknown.error["id"] == "provider_invalid"
    assert [item["path"] for item in unknown.result["problems"]] == ["--read-only"]
    assert "atlassian_jira_search" in unknown.result["problems"][0]["message"]

    both = _wrap(directory, read_only=["jira_search"], destructive=["jira_search"])
    assert both.code == 3
    assert both.error is not None and both.error["id"] == "provider_invalid"
    assert any("both" in item["message"] for item in both.result["problems"])

    nothing = _wrap(directory, tools_snapshot=None, no_discovery=True, destructive=["jira_search"])
    assert nothing.code == 3
    assert nothing.error is not None and nothing.error["id"] == "provider_invalid"


def test_read_only_flag_from_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("keystonectl.wrap_stdio.shutil.which", lambda _name: None)
    directory = _copy(_ATLASSIAN, tmp_path)
    argv = [
        "package",
        "wrap-stdio",
        str(directory),
        "--tools-snapshot",
        str(directory / "tools.snapshot.json"),
        "--read-only",
        "jira_search, jira_get_issue",
        "--version",
        "1.2.3",
        "--json",
    ]
    assert main(argv) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["result"]["box_tools_source"] == "fallback"
    assert _manifest(directory)["version"] == "1.2.3"
    policy = yaml.safe_load((directory / "policy.example.yaml").read_text(encoding="utf-8"))
    assert policy["roles"]["atlassian_reader"]["allow"][:2] == [
        "atlassian_jira_get_issue",
        "atlassian_jira_search",
    ]


def test_remote_bridge_fixture(tmp_path: Path) -> None:
    directory = _copy(_REMOTE, tmp_path)
    result = _wrap(directory, tools_snapshot=None, no_discovery=True, stock_image=True)
    assert result.code == 0, result.result
    assert result.result["namespace"] == "example_remote"
    document = _manifest(directory)
    command = (
        "npx -y mcp-remote@0.14.3 https://mcp.example.com/mcp "
        '--header "Authorization:Bearer ${EXAMPLE_TOKEN}"'
    )
    assert _env(document)["BOX_COMMAND"]["default"] == command
    assert document["containers"]["mcp"]["package_dir"]["from"] == "."
    snapshot = json.loads((directory / "tools.snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["server"]["command"] == command
    assert snapshot["tools"] == []
    assert _lint_errors(directory) == []
