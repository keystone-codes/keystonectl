"""``package wrap-stdio --resolve-lock`` builds the lock in the image and installs it there."""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from keystonectl.cli import main
from keystonectl.runner import CommandResult, RealCommandRunner
from keystonectl.wrap_stdio import STDIO_BOX_IMAGE, run_wrap_stdio

_DATA = Path(__file__).parent / "data" / "stdio"
_ATLASSIAN = _DATA / "atlassian"
_REMOTE = _DATA / "example-remote"
_USER = f"{os.getuid()}:{os.getgid()}"
_CACHE_ENV = [
    "-e",
    "HOME=/tmp/h",
    "-e",
    "npm_config_cache=/tmp/npm",
    "-e",
    "UV_CACHE_DIR=/tmp/uv",
]
_NPM_ARGS = ["install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund"]
_UV_ARGS = [
    "pip",
    "compile",
    "--universal",
    "--generate-hashes",
    "-o",
    "requirements.txt",
    "requirements.in",
]
_LOCK = {
    "name": "example-remote",
    "lockfileVersion": 3,
    "requires": True,
    "packages": {
        "": {"dependencies": {"mcp-remote": "0.14.3"}},
        "node_modules/mcp-remote": {
            "version": "0.14.3",
            "resolved": "https://registry.npmjs.org/mcp-remote/-/mcp-remote-0.14.3.tgz",
            "integrity": "sha512-example",
        },
    },
}
_REQUIREMENTS = (
    "mcp-atlassian==0.23.1 \\\n    --hash=sha256:" + "0" * 64 + "\n    # via -r requirements.in\n"
)

Step = Callable[[Path], CommandResult]


def _ok(_copy: Path) -> CommandResult:
    return CommandResult((), 0, "", "")


def _writes_npm_lock(copy: Path) -> CommandResult:
    (copy / "package-lock.json").write_text(json.dumps(_LOCK), encoding="utf-8")
    return CommandResult((), 0, "", "added 1 package\n")


def _writes_requirements(copy: Path) -> CommandResult:
    (copy / "requirements.txt").write_text(_REQUIREMENTS, encoding="utf-8")
    return CommandResult((), 0, "", "Resolved 1 package\n")


class _Runner:
    """Plays docker: the build step writes into the mounted copy, the install step answers."""

    def __init__(self, resolve: Step, install: Step = _ok) -> None:
        self.resolve = resolve
        self.install = install
        self.calls: list[tuple[list[str], Mapping[str, str] | None, float]] = []
        self.copies: list[Path] = []

    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        input: str | None = None,
        timeout: float = 300,
    ) -> CommandResult:
        argv = list(args)
        self.calls.append((argv, env, timeout))
        if argv[-1] == "box-tools":
            return CommandResult((), 2, "", "usage: stdio-box")
        mount = argv[argv.index("-v") + 1]
        copy = Path(mount.split(":", 1)[0])
        self.copies.append(copy)
        if "install" in argv and argv[argv.index("--entrypoint") + 1] == "stdio-box":
            return self.install(copy)
        return self.resolve(copy)


def _copy(source: Path, root: Path) -> Path:
    destination = root / "packages" / source.name
    shutil.copytree(source, destination)
    return destination


def _state(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in sorted(directory.iterdir())
    }


def _wrap(directory: Path, runner: _Runner, **kwargs):
    values = {
        "image": STDIO_BOX_IMAGE,
        "tools_snapshot": None,
        "no_discovery": True,
        "name": None,
        "namespace": None,
        "derived_image": False,
        "force": False,
        "runner": runner,
        "stock_image": True,
        "resolve_lock": True,
        "environ": {},
    }
    values.update(kwargs)
    return run_wrap_stdio(directory, **values)


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("keystonectl.wrap_stdio.shutil.which", lambda _name: "/usr/bin/docker")


@pytest.mark.usefixtures("docker")
def test_npm_lock_is_built_checked_and_moved(tmp_path: Path) -> None:
    directory = _copy(_REMOTE, tmp_path)
    (directory / "README.md").write_text("keep me\n", encoding="utf-8")
    root = tmp_path / "deploy"
    root.mkdir()
    runner = _Runner(_writes_npm_lock)
    result = _wrap(directory, runner, work_root=root)
    assert result.code == 0, result.error
    assert result.result["lock"] == {"resolved": True, "file": "package-lock.json"}
    assert result.result["files"][:2] == ["package.json", "package-lock.json"]

    resolve, install = runner.calls[0], runner.calls[1]
    copy = runner.copies[0]
    assert copy.parent.parent == root / ".keystonectl" / "tmp"
    assert copy.name == "example-remote"
    assert resolve[0] == [
        "docker",
        "run",
        "--rm",
        "--user",
        _USER,
        *_CACHE_ENV,
        "-v",
        f"{copy}:/work",
        "-w",
        "/work",
        "--entrypoint",
        "npm",
        STDIO_BOX_IMAGE,
        *_NPM_ARGS,
    ]
    assert install[0] == [
        "docker",
        "run",
        "--rm",
        "--user",
        _USER,
        *_CACHE_ENV,
        "-v",
        f"{copy}:/work/example-remote",
        "-w",
        "/work/example-remote",
        "--entrypoint",
        "stdio-box",
        STDIO_BOX_IMAGE,
        "install",
        "--provider-dir",
        "/work/example-remote",
        "--cache",
        "/tmp/cache",
    ]
    assert resolve[1] is None and install[1] is None
    assert resolve[2] == 600

    package = json.loads((directory / "package.json").read_text(encoding="utf-8"))
    assert package == {"private": True, "dependencies": {"mcp-remote": "0.14.3"}}
    lock = json.loads((directory / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["packages"]["node_modules/mcp-remote"]["version"] == "0.14.3"
    for name in ("package.json", "package-lock.json"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o644
    assert (directory / "README.md").read_text(encoding="utf-8") == "keep me\n"
    assert list((root / ".keystonectl" / "tmp").iterdir()) == []


@pytest.mark.usefixtures("docker")
def test_uv_lock_without_existing_lock(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    (directory / "requirements.txt").unlink()
    runner = _Runner(_writes_requirements)
    result = _wrap(directory, runner)
    assert result.code == 0, result.error
    assert result.result["lock"] == {"resolved": True, "file": "requirements.txt"}
    copy = runner.copies[0]
    assert runner.calls[0][0] == [
        "docker",
        "run",
        "--rm",
        "--user",
        _USER,
        *_CACHE_ENV,
        "-v",
        f"{copy}:/work",
        "-w",
        "/work",
        "--entrypoint",
        "uv",
        STDIO_BOX_IMAGE,
        *_UV_ARGS,
    ]
    assert runner.calls[1][0][-5:] == [
        "install",
        "--provider-dir",
        "/work/atlassian",
        "--cache",
        "/tmp/cache",
    ]
    assert (directory / "requirements.txt").read_text(encoding="utf-8") == _REQUIREMENTS
    assert (directory / "requirements.in").read_text(encoding="utf-8") == "mcp-atlassian==0.23.1\n"
    for name in ("requirements.txt", "requirements.in"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o644
    assert not copy.exists()


@pytest.mark.usefixtures("docker")
def test_existing_lock_is_rebuilt_from_scratch(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    seen: list[bool] = []

    def resolve(copy: Path) -> CommandResult:
        seen.append((copy / "requirements.txt").exists())
        return _writes_requirements(copy)

    result = _wrap(directory, _Runner(resolve))
    assert result.code == 0, result.error
    assert seen == [False]
    assert (directory / "requirements.txt").read_text(encoding="utf-8") == _REQUIREMENTS


@pytest.mark.usefixtures("docker")
def test_https_mirrors_are_passed_by_name(tmp_path: Path) -> None:
    directory = _copy(_REMOTE, tmp_path)
    runner = _Runner(_writes_npm_lock)
    environ = {
        "BOX_NPM_REGISTRY": "https://npm.example.com/",
        "BOX_PIP_INDEX_URL": "https://pypi.example.com/simple",
    }
    result = _wrap(directory, runner, environ=environ)
    assert result.code == 0, result.error
    resolve_args, resolve_env, _ = runner.calls[0]
    assert resolve_args[11:13] == ["-e", "npm_config_registry"]
    assert resolve_env == {"npm_config_registry": "https://npm.example.com/"}
    assert "https://npm.example.com/" not in " ".join(resolve_args)
    install_args, install_env, _ = runner.calls[1]
    assert install_args[11:15] == ["-e", "BOX_NPM_REGISTRY", "-e", "BOX_PIP_INDEX_URL"]
    assert install_env == environ


@pytest.mark.usefixtures("docker")
def test_uv_mirror_is_uv_index_url(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    runner = _Runner(_writes_requirements)
    environ = {"BOX_PIP_INDEX_URL": "https://pypi.example.com/simple"}
    assert _wrap(directory, runner, environ=environ).code == 0
    assert runner.calls[0][1] == {"UV_INDEX_URL": "https://pypi.example.com/simple"}
    assert runner.calls[0][0][11:13] == ["-e", "UV_INDEX_URL"]


@pytest.mark.usefixtures("docker")
@pytest.mark.parametrize("name", ["BOX_NPM_REGISTRY", "BOX_PIP_INDEX_URL"])
def test_http_mirror_is_usage(tmp_path: Path, name: str) -> None:
    directory = _copy(_REMOTE, tmp_path)
    before = _state(directory)
    runner = _Runner(_writes_npm_lock)
    result = _wrap(directory, runner, environ={name: "http://mirror.example.com/"})
    assert result.code == 1
    assert result.error is not None
    assert result.error["id"] == "usage"
    assert name in result.error["message"]
    assert runner.calls == []
    assert _state(directory) == before


@pytest.mark.usefixtures("docker")
def test_tool_missing_in_image(tmp_path: Path) -> None:
    directory = _copy(_REMOTE, tmp_path)
    before = _state(directory)
    stderr = 'docker: Error response from daemon: exec: "npm": executable file not found in $PATH\n'
    runner = _Runner(lambda _copy: CommandResult((), 127, "", stderr))
    result = _wrap(directory, runner)
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "lock_tool_missing"
    assert "npm" in result.error["message"]
    assert result.retryable is False
    assert len(runner.calls) == 1
    assert _state(directory) == before


@pytest.mark.usefixtures("docker")
def test_resolve_failure_leaves_directory(tmp_path: Path) -> None:
    directory = _copy(_REMOTE, tmp_path)
    before = _state(directory)
    lines = "".join(f"line {index}\n" for index in range(30))
    stderr = lines + "npm error 404 No match found for version 0.14.3\n"
    runner = _Runner(lambda _copy: CommandResult((), 1, "", stderr))
    result = _wrap(directory, runner)
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "lock_resolve_failed"
    assert result.retryable is True
    tail = result.result["stderr"].splitlines()
    assert len(tail) == 20
    assert tail[-1] == "npm error 404 No match found for version 0.14.3"
    assert len(runner.calls) == 1
    assert _state(directory) == before
    assert not runner.copies[0].parent.exists()


@pytest.mark.usefixtures("docker")
def test_resolve_without_output_is_failure(tmp_path: Path) -> None:
    directory = _copy(_REMOTE, tmp_path)
    before = _state(directory)
    result = _wrap(directory, _Runner(_ok))
    assert result.error is not None
    assert result.error["id"] == "lock_resolve_failed"
    assert _state(directory) == before


@pytest.mark.usefixtures("docker")
def test_install_failure_leaves_directory(tmp_path: Path) -> None:
    directory = _copy(_REMOTE, tmp_path)
    before = _state(directory)
    stderr = '{"code": "install_failed", "event": "provider_install_failed"}\n'
    runner = _Runner(_writes_npm_lock, lambda _copy: CommandResult((), 3, "", stderr))
    result = _wrap(directory, runner)
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "lock_install_failed"
    assert result.retryable is False
    assert "provider_install_failed" in result.result["stderr"]
    assert len(runner.calls) == 2
    assert _state(directory) == before
    assert not runner.copies[0].parent.exists()


def test_docker_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("keystonectl.wrap_stdio.shutil.which", lambda _name: None)
    directory = _copy(_REMOTE, tmp_path)
    runner = _Runner(_writes_npm_lock)
    result = _wrap(directory, runner)
    assert result.code == 3
    assert result.error is not None
    assert result.error["id"] == "docker_unavailable"
    assert "lock" in result.error["message"]
    assert runner.calls == []


def test_without_flag_the_lock_must_exist(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    (directory / "requirements.txt").unlink()
    result = _wrap(directory, _Runner(_ok), resolve_lock=False)
    assert result.error is not None
    assert result.error["id"] == "provider_invalid"
    assert result.result["problems"][0]["path"] == "server.lock"


@pytest.mark.usefixtures("docker")
def test_provider_is_checked_before_the_lock(tmp_path: Path) -> None:
    directory = _copy(_REMOTE, tmp_path)
    text = (directory / "provider.yaml").read_text(encoding="utf-8")
    (directory / "provider.yaml").write_text(
        text.replace("name: example-remote", "name: other"), encoding="utf-8"
    )
    runner = _Runner(_writes_npm_lock)
    result = _wrap(directory, runner)
    assert result.error is not None
    assert result.error["id"] == "provider_invalid"
    assert runner.calls == []


def test_without_resolve_lock_reports_unresolved(tmp_path: Path) -> None:
    directory = _copy(_ATLASSIAN, tmp_path)
    result = _wrap(directory, _Runner(_ok), resolve_lock=False)
    assert result.code == 0, result.error
    assert result.result["lock"] == {"resolved": False, "file": "requirements.txt"}


def test_cli_flag_reads_mirrors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = _copy(_REMOTE, tmp_path)
    monkeypatch.setenv("BOX_NPM_REGISTRY", "http://npm.example.com/")
    code = main(["package", "wrap-stdio", str(directory), "--resolve-lock", "--json"])
    document = json.loads(capsys.readouterr().out)
    assert code == 1
    assert document["error"]["id"] == "usage"
    assert "BOX_NPM_REGISTRY" in document["error"]["message"]


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        completed = subprocess.run(["docker", "info"], capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _registry_reachable() -> bool:
    try:
        with socket.create_connection(("registry.npmjs.org", 443), timeout=5):
            return True
    except OSError:
        return False


@pytest.mark.integration
def test_live_mcp_remote_lock(tmp_path: Path) -> None:
    if not _docker_ready():
        pytest.skip("Docker is not available")
    if not _registry_reachable():
        pytest.skip("no network to the npm registry")
    directory = _copy(_REMOTE, tmp_path)
    for name in ("package.json", "package-lock.json"):
        (directory / name).unlink()
    result = _wrap(directory, RealCommandRunner(), environ=dict(os.environ))
    assert result.code == 0, (result.error, result.result)
    assert result.result["lock"] == {"resolved": True, "file": "package-lock.json"}
    lock = json.loads((directory / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["lockfileVersion"] == 3
    assert lock["packages"]["node_modules/mcp-remote"]["version"] == "0.14.3"
    package = json.loads((directory / "package.json").read_text(encoding="utf-8"))
    assert package["dependencies"] == {"mcp-remote": "0.14.3"}
