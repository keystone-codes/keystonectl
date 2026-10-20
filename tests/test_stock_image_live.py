"""A box on the stock stdio-box image starts from the files ``wrap-stdio --stock-image`` writes.

The container gets what the generated compose fragment gives it: the package directory mounted
read-only at ``package_dir.path``, the manifest's environment, named volumes for ``/data`` and
``/cache``, and ``no-new-privileges``. The secrets are values made up for this test.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from keystonectl.generate.compose import render_fragment
from keystonectl.runner import RealCommandRunner
from keystonectl.wrap_stdio import STDIO_BOX_IMAGE, run_wrap_stdio, stock_provider_dir

_NOTION = Path(__file__).parent / "data" / "stdio" / "notion"
_LOG_TAIL = 80


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _docker("info", timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _registry_reachable() -> bool:
    try:
        with socket.create_connection(("registry.npmjs.org", 443), timeout=5):
            return True
    except OSError:
        return False


def _seconds(value: object) -> int:
    text = str(value)
    return int(text[:-1]) if text.endswith("s") else int(text)


def _fragment_service(manifest: dict[str, Any]) -> dict[str, Any]:
    name = str(manifest["name"])
    copy = f".keystonectl/package-dirs/{name}/{manifest['version']}-{'0' * 12}"
    entry = {"name": name, "package_dirs": {"mcp": {"copy": copy}}}
    fragment = yaml.safe_load(render_fragment(manifest, entry))
    return fragment["services"][f"{name}-mcp"]


def _test_secrets(manifest: dict[str, Any]) -> dict[str, str]:
    """Generated throwaway values for every secret the manifest names; none is a real secret."""
    container = manifest["containers"]["mcp"]
    values = {item["name"]: secrets.token_hex(32) for item in container.get("secrets") or []}
    values[manifest["mcp"]["bearer_env"]] = secrets.token_hex(32)
    values[manifest["identity"]["secret_env"]] = secrets.token_hex(32)
    values[manifest["identity"]["audience_env"]] = "keystone-test"
    values[manifest["public_base_url_env"]] = "https://box.example.com"
    return values


def _run_args(
    service: dict[str, Any], directory: Path, label: str, values: dict[str, str]
) -> tuple[list[str], list[str]]:
    """``docker run`` arguments for the fragment's service, and the volumes they create."""
    args = ["run", "-d", "--name", label]
    for option in service.get("security_opt") or []:
        args += ["--security-opt", option]
    environment = {
        key: str(value).replace("$$", "$") for key, value in service["environment"].items()
    }
    environment.update(values)
    for key, value in environment.items():
        args += ["-e", f"{key}={value}"]
    volumes: list[str] = []
    for item in service["volumes"]:
        source, target, *mode = item.split(":")
        if source.startswith("../"):
            # The package_dir copy, read-only, exactly as the fragment mounts it.
            args += ["-v", ":".join([str(directory.resolve()), target, *mode])]
            continue
        volume = f"{label}-{source}"
        volumes.append(volume)
        args += ["-v", f"{volume}:{target}"]
    args.append(str(service["image"]))
    return args, volumes


def _health(label: str, port: int, path: str) -> str:
    probe = _docker(
        "exec",
        label,
        "curl",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        f"http://127.0.0.1:{port}{path}",
        timeout=30,
    )
    return probe.stdout.strip()


def _running(label: str) -> bool:
    state = _docker("inspect", "-f", "{{.State.Running}}", label, timeout=30)
    return state.stdout.strip() == "true"


def _logs(label: str) -> str:
    logs = _docker("logs", "--tail", str(_LOG_TAIL), label, timeout=60)
    state = _docker("inspect", "-f", "exit={{.State.ExitCode}} {{.State.Status}}", label)
    return f"{state.stdout.strip()}\n{logs.stdout}{logs.stderr}"


@pytest.mark.integration
def test_stock_image_box_starts(tmp_path: Path) -> None:
    if not _docker_ready():
        pytest.skip("Docker is not available")
    if not _registry_reachable():
        pytest.skip("no network to the npm registry")
    directory = tmp_path / "notion"
    shutil.copytree(_NOTION, directory)

    # The provider-named discovery mount is G12; an empty snapshot is enough for the box to start.
    result = run_wrap_stdio(
        directory,
        image=STDIO_BOX_IMAGE,
        tools_snapshot=None,
        no_discovery=True,
        name=None,
        namespace=None,
        derived_image=False,
        force=False,
        runner=RealCommandRunner(),
        stock_image=True,
        resolve_lock=True,
        environ=dict(os.environ),
    )
    assert result.code == 0, (result.error, result.result)
    manifest = yaml.safe_load((directory / "keystone-service.yaml").read_text(encoding="utf-8"))
    container = manifest["containers"]["mcp"]
    target = stock_provider_dir("notion")
    assert container["package_dir"] == {"from": ".", "path": target}

    service = _fragment_service(manifest)
    assert f"../.keystonectl/package-dirs/notion/1.0.0-{'0' * 12}:{target}:ro" in service["volumes"]
    assert service["environment"]["BOX_PROVIDER_DIR"] == target

    label = f"ks-stock-image-test-{secrets.token_hex(4)}"
    args, volumes = _run_args(service, directory, label, _test_secrets(manifest))
    begun = time.monotonic()
    deadline = begun + _seconds(container["healthcheck"]["start_period"])
    status = ""
    try:
        started = _docker(*args, timeout=600)
        assert started.returncode == 0, started.stderr
        while time.monotonic() < deadline:
            if not _running(label):
                break
            status = _health(label, int(container["port"]), container["healthcheck"]["path"])
            if status == "200":
                break
            time.sleep(3)
        assert status == "200", f"/healthz answered {status!r}\n{_logs(label)}"
        # Shown with ``-s``: how long the start took and what the box logged.
        print(f"/healthz 200 after {time.monotonic() - begun:.0f}s\n{_logs(label)}")
    finally:
        _docker("rm", "-f", "-v", label, timeout=60)
        for volume in volumes:
            _docker("volume", "rm", "-f", volume, timeout=60)
