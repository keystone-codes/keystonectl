"""``keystonectl package import-snapshot`` brings a box's tool snapshot into ``local/<name>/``."""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import pytest
import yaml

from keystonectl.cli import main
from keystonectl.import_snapshot import import_snapshot, snapshot_sha256
from keystonectl.lock import load_lock, validate_lock
from keystonectl.runner import CommandResult, FakeCommandRunner
from keystonectl.wrap_stdio import CHANGING_CREDS, CREDS_TOOLS, STDIO_BOX_IMAGE, run_wrap_stdio
from tests.support.deploylab import prepare_root

_REMOTE = Path(__file__).parent / "data" / "stdio" / "example-remote"
NAME = "example-remote"
SERVICE = f"{NAME}-mcp"
COMMAND = yaml.safe_load((_REMOTE / "provider.yaml").read_text(encoding="utf-8"))["server"][
    "command"
]
TOOLS = [
    {"name": "get-issue", "annotations": {"readOnlyHint": True}},
    {"name": "search-issues", "annotations": {"readOnlyHint": True}},
    {"name": "delete-issue", "annotations": {"destructiveHint": True}},
]


def _snapshot_text(*, command: str = COMMAND, tools: list | None = None) -> str:
    document = {
        "schema_version": 1,
        "server": {"command": command},
        "taken_at": "2026-10-15T00:00:00Z",
        "tools": TOOLS if tools is None else tools,
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _hash(text: str) -> str:
    return snapshot_sha256(json.loads(text))


def _no_box_tools(args, **_kwargs) -> CommandResult:
    return CommandResult(tuple(args), 2, "", "usage: stdio-box")


def _deployment(tmp_path: Path, *, source: str = "local") -> Path:
    """A deployment with ``local/example-remote`` wrapped with --no-discovery and installed."""

    root = tmp_path / "root"
    prepare_root(root, {}, source="lab")
    folder = root / "local" / NAME
    folder.parent.mkdir()
    shutil.copytree(_REMOTE, folder)
    wrapped = run_wrap_stdio(
        folder,
        image=STDIO_BOX_IMAGE,
        tools_snapshot=None,
        no_discovery=True,
        name=None,
        namespace=None,
        derived_image=False,
        force=False,
        runner=FakeCommandRunner([(["docker", "run"], _no_box_tools)]),
        stock_image=True,
    )
    assert wrapped.code == 0, wrapped.to_dict()
    lock = yaml.safe_load((root / "services.lock.yaml").read_text(encoding="utf-8"))
    entry = {
        "name": NAME,
        "role": "service",
        "managed": "generated",
        "source": source,
        "package": NAME,
        "version": "1.0.0",
        "ref": "local" if source == "local" else "v1.0.0",
        "commit": None if source == "local" else "a" * 40,
        "manifest_sha256": "c" * 64,
        "fragment": f"packages/{NAME}.yml",
        "fragment_sha256": "d" * 64,
        "containers": {
            "mcp": {
                "service": SERVICE,
                "image": STDIO_BOX_IMAGE,
                "init": False,
            }
        },
    }
    if source == "local":
        entry["content_sha256"] = "b" * 64
    lock["services"].append(entry)
    (root / "services.lock.yaml").write_text(yaml.safe_dump(lock, sort_keys=False), "utf-8")
    assert validate_lock(load_lock(root / "services.lock.yaml")) == []
    return root


def _box(
    text: str | None = None,
    *,
    reported: str | None = None,
    returncode: int = 0,
    stdout: str | None = None,
    stderr: str = "",
) -> FakeCommandRunner:
    body = _snapshot_text() if text is None else text

    def export(args, **_kwargs) -> CommandResult:
        if stdout is not None:
            return CommandResult(tuple(args), returncode, stdout, stderr)
        envelope = {
            "ok": True,
            "code": 0,
            "error": None,
            "result": {
                "id": 7,
                "sha256": _hash(body) if reported is None else reported,
                "snapshot": body,
            },
        }
        return CommandResult(tuple(args), returncode, json.dumps(envelope), stderr)

    return FakeCommandRunner([(["docker", "compose"], export), (["docker", "run"], _no_box_tools)])


def _files(root: Path) -> dict[str, bytes]:
    folder = root / "local" / NAME
    return {item.name: item.read_bytes() for item in sorted(folder.iterdir()) if item.is_file()}


def _run(root: Path, runner: FakeCommandRunner, **kwargs):
    values = {
        "snapshot_id": "7",
        "sha256": _hash(_snapshot_text()),
        "version": "1.1.0",
        "runner": runner,
    }
    values.update(kwargs)
    return import_snapshot(root, NAME, **values)


def _manifest(root: Path) -> dict:
    path = root / "local" / NAME / "keystone-service.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_import_writes_snapshot_and_updates_manifest(tmp_path: Path) -> None:
    root = _deployment(tmp_path)
    runner = _box()
    result = _run(root, runner)
    assert result.code == 0, result.to_dict()
    assert result.command == "package import-snapshot"
    assert result.result == {
        "package": NAME,
        "version": "1.1.0",
        "tools_added": ["delete_issue", "get_issue", "search_issues"],
        "tools_removed": [],
        "next": f"keystonectl service upgrade {NAME}",
    }
    folder = root / "local" / NAME
    snapshot = folder / "tools.snapshot.json"
    assert snapshot.read_bytes() == _snapshot_text().encode("utf-8")
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o644
    manifest = _manifest(root)
    assert manifest["version"] == "1.1.0"
    assert manifest["mcp"]["tools"] == [
        "get_issue",
        "search_issues",
        "delete_issue",
        *CREDS_TOOLS,
    ]
    assert manifest["mcp"]["critical"] == ["delete_issue", *CHANGING_CREDS]
    policy = (folder / "policy.example.yaml").read_text(encoding="utf-8")
    assert "example_remote_get_issue" in policy
    export = runner.calls[0].args
    assert export[:2] == ("docker", "compose")
    assert "-f" in export and str(root / "docker-compose.packages.yml") in export
    assert export[-8:] == (
        "exec",
        "-T",
        SERVICE,
        "credsctl",
        "--json",
        "snapshot",
        "export",
        "7",
    )
    assert runner.calls[0].cwd == root
    # upgrade is left to the operator
    assert not any("up" in call.args for call in runner.calls)
    assert load_lock(root / "services.lock.yaml")["services"][0]["version"] == "1.0.0"


def test_read_only_and_destructive_flags_reach_the_manifest(tmp_path: Path) -> None:
    root = _deployment(tmp_path)
    result = _run(root, _box(), read_only=["get_issue"], destructive=["search_issues"])
    assert result.code == 0, result.to_dict()
    assert _manifest(root)["mcp"]["critical"] == ["search_issues", *CHANGING_CREDS]


def test_second_import_reports_removed_tools(tmp_path: Path) -> None:
    root = _deployment(tmp_path)
    assert _run(root, _box()).code == 0
    text = _snapshot_text(tools=TOOLS[:1])
    result = _run(root, _box(text), sha256=_hash(text), version="1.2.0")
    assert result.code == 0, result.to_dict()
    assert result.result["tools_added"] == []
    assert result.result["tools_removed"] == ["delete_issue", "search_issues"]


def test_package_from_a_git_source_is_not_local(tmp_path: Path) -> None:
    root = _deployment(tmp_path, source="lab")
    before = _files(root)
    runner = _box()
    result = _run(root, runner)
    assert result.code == 3
    assert result.error["id"] == "not_local_package"
    assert runner.calls == []
    assert _files(root) == before


def test_package_that_is_not_installed_is_not_local(tmp_path: Path) -> None:
    root = _deployment(tmp_path)
    result = import_snapshot(
        root,
        "other",
        snapshot_id="7",
        sha256="a" * 64,
        version="1.1.0",
        runner=_box(),
    )
    assert result.code == 3
    assert result.error["id"] == "not_local_package"


def test_unknown_snapshot_id_is_snapshot_not_found(tmp_path: Path) -> None:
    root = _deployment(tmp_path)
    before = _files(root)
    envelope = {
        "ok": False,
        "code": 3,
        "error": {"id": "snapshot_not_found", "message": "snapshot not found"},
        "result": None,
    }
    runner = _box(returncode=3, stdout=json.dumps(envelope), stderr="snapshot not found")
    result = _run(root, runner)
    assert result.code == 3
    assert result.error["id"] == "snapshot_not_found"
    assert result.retryable is False
    assert _files(root) == before


def test_argument_hash_mismatch_writes_nothing(tmp_path: Path) -> None:
    root = _deployment(tmp_path)
    before = _files(root)
    result = _run(root, _box(), sha256="0" * 64)
    assert result.code == 3
    assert result.error["id"] == "snapshot_hash_mismatch"
    assert _files(root) == before


def test_box_hash_mismatch_writes_nothing(tmp_path: Path) -> None:
    root = _deployment(tmp_path)
    before = _files(root)
    result = _run(root, _box(reported="1" * 64))
    assert result.code == 3
    assert result.error["id"] == "snapshot_hash_mismatch"
    assert _files(root) == before


def test_hash_ignores_taken_at() -> None:
    text = _snapshot_text()
    later = text.replace("2026-10-15T00:00:00Z", "2026-10-16T12:00:00Z")
    assert _hash(text) == _hash(later)


def test_snapshot_from_another_server_restores_files(tmp_path: Path) -> None:
    root = _deployment(tmp_path)
    before = _files(root)
    text = _snapshot_text(command="npx -y mcp-remote@0.15.0 https://mcp.example.com/mcp")
    result = _run(root, _box(text), sha256=_hash(text))
    assert result.code == 3
    assert result.error["id"] == "provider_invalid"
    assert result.command == "package import-snapshot"
    messages = [item["message"] for item in result.result["problems"]]
    assert "snapshot was taken from another server version" in messages
    assert _files(root) == before


@pytest.mark.parametrize("version", ["1.0.0", "0.9.0"])
def test_version_not_higher_is_usage(tmp_path: Path, version: str) -> None:
    root = _deployment(tmp_path)
    before = _files(root)
    runner = _box()
    result = _run(root, runner, version=version)
    assert result.code == 1
    assert result.error["id"] == "usage"
    assert runner.calls == []
    assert _files(root) == before


@pytest.mark.parametrize(
    ("kwargs", "flag"),
    [
        ({"sha256": "abc"}, "--sha256"),
        ({"sha256": "g" * 64}, "--sha256"),
        ({"version": "1.1"}, "--version"),
        ({"snapshot_id": "x7"}, "--id"),
    ],
)
def test_bad_arguments_are_usage(tmp_path: Path, kwargs: dict, flag: str) -> None:
    root = _deployment(tmp_path)
    runner = _box()
    result = _run(root, runner, **kwargs)
    assert result.error["id"] == "usage"
    assert flag in result.error["message"]
    assert runner.calls == []


def test_docker_missing_is_docker_unavailable(tmp_path: Path) -> None:
    root = _deployment(tmp_path)

    def missing(args, **_kwargs) -> CommandResult:
        raise FileNotFoundError("docker")

    result = _run(root, FakeCommandRunner([(["docker"], missing)]))
    assert result.error["id"] == "docker_unavailable"


def test_docker_daemon_down_is_docker_unavailable(tmp_path: Path) -> None:
    root = _deployment(tmp_path)
    runner = _box(
        returncode=1,
        stdout="",
        stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
    )
    result = _run(root, runner)
    assert result.error["id"] == "docker_unavailable"


def test_box_not_running_is_service_unhealthy(tmp_path: Path) -> None:
    root = _deployment(tmp_path)
    before = _files(root)
    runner = _box(returncode=1, stdout="", stderr=f'service "{SERVICE}" is not running')
    result = _run(root, runner)
    assert result.error["id"] == "service_unhealthy"
    assert "is not running" in result.result["stderr"]
    assert _files(root) == before


def test_cli_json(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    root = _deployment(tmp_path)
    runner = _box()
    monkeypatch.setattr("keystonectl.cli.build_runner", lambda: runner)
    code = main(
        [
            "package",
            "import-snapshot",
            NAME,
            "--id",
            "7",
            "--sha256",
            _hash(_snapshot_text()),
            "--version",
            "1.1.0",
            "--root",
            str(root),
            "--json",
        ]
    )
    document = json.loads(capsys.readouterr().out)
    assert code == 0, document
    assert document["command"] == "package import-snapshot"
    assert document["result"]["next"] == f"keystonectl service upgrade {NAME}"


def test_cli_requires_id_sha256_and_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _deployment(tmp_path)
    code = main(["package", "import-snapshot", NAME, "--root", str(root)])
    assert code == 1
    assert "--id, --sha256, --version" in capsys.readouterr().err
