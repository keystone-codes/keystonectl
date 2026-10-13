"""Docker Compose commands used by install and status. Secrets never go in argv."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from keystonectl.runner import CommandResult, CommandRunner

WIRED_DIR = "/etc/keystone-mcp/keystonectl"
UPSTREAMS_NETWORK = "keystone-mcp-upstreams"
RESOURCES_NETWORK = "keystone-resources"
PROBE_CODE = "import sys,urllib.request; urllib.request.urlopen(sys.argv[1], timeout=4)"
METRICS_CODE = (
    "import urllib.request; "
    "print(urllib.request.urlopen('http://127.0.0.1:8080/metrics', timeout=4).read().decode())"
)
PUBLIC_PROBE_CODE = """
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
url = sys.argv[1]
try:
    with urlopen(url, timeout=4) as response:
        print(f"status:{response.status}")
except HTTPError as exc:
    print(f"status:{exc.code}")
except URLError as exc:
    print(f"error:{type(exc.reason).__name__}: {exc.reason}")
except Exception as exc:
    print(f"error:{type(exc).__name__}: {exc}")
""".strip()


def daemon_down(result: CommandResult) -> bool:
    text = f"{result.stderr}\n{result.stdout}".lower()
    return "cannot connect to the docker daemon" in text or "is the docker daemon running" in text


def compose_files(root: Path, layout: Mapping[str, Any]) -> list[str]:
    names = layout.get("compose_files") if isinstance(layout.get("compose_files"), list) else []
    if names:
        return [str(Path(root) / str(name)) for name in names]
    return [
        str(Path(root) / "docker-compose.yml"),
        str(Path(root) / "docker-compose.packages.yml"),
    ]


def compose_prefix(
    root: Path, layout: Mapping[str, Any], *, project: str | None = None
) -> list[str]:
    command = ["docker", "compose"]
    if project:
        command.extend(["-p", project, "--project-directory", str(root)])
    for path in compose_files(root, layout):
        command.extend(["-f", path])
    return command


def run(runner: CommandRunner, args: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
    return runner.run(list(args), cwd=cwd)


def compose_config(runner: CommandRunner, root: Path, layout: Mapping[str, Any]) -> CommandResult:
    return run(runner, [*compose_prefix(root, layout), "config", "--format", "json"], cwd=root)


def parse_config(result: CommandResult) -> dict[str, Any] | None:
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        loaded = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def service_env(service: Mapping[str, Any]) -> dict[str, str]:
    raw = service.get("environment")
    found: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            found[str(key)] = "" if value is None else str(value)
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, str) or "=" not in item:
                continue
            key, value = item.split("=", 1)
            found[key] = value
    return found


def service_networks(service: Mapping[str, Any]) -> set[str]:
    raw = service.get("networks")
    if isinstance(raw, dict):
        return {str(name) for name in raw}
    if isinstance(raw, list):
        return {str(name) for name in raw}
    return set()


def layout_problems(config: Mapping[str, Any], layout: Mapping[str, Any]) -> list[str]:
    service_name = str(layout.get("keystone_service") or "")
    services = config.get("services") if isinstance(config.get("services"), dict) else {}
    problems: list[str] = []
    service = services.get(service_name) if service_name else None
    if not isinstance(service, dict):
        return [f"compose service {service_name or '(unset)'} was not found"]
    env = service_env(service)
    upstreams = f"{WIRED_DIR}/upstreams.yaml"
    snapshot = f"{WIRED_DIR}/catalog-snapshot.yaml"
    if env.get("KEYSTONE_MCP_UPSTREAMS_PATH") != upstreams:
        problems.append(f"KEYSTONE_MCP_UPSTREAMS_PATH is not {upstreams}")
    if env.get("KEYSTONE_MCP_RBAC_CATALOG_SNAPSHOT") != snapshot:
        problems.append(f"KEYSTONE_MCP_RBAC_CATALOG_SNAPSHOT is not {snapshot}")
    if not _has_keystonectl_volume(service):
        problems.append(f"no volume mounts the deployment keystonectl directory at {WIRED_DIR}")
    if UPSTREAMS_NETWORK not in service_networks(service):
        problems.append(f"service is not on the {UPSTREAMS_NETWORK} network")
    return problems


def _has_keystonectl_volume(service: Mapping[str, Any]) -> bool:
    for item in service.get("volumes") or []:
        if isinstance(item, str):
            parts = item.split(":")
            if len(parts) >= 2:
                source, target = parts[0], parts[1]
                if target.rstrip("/") == WIRED_DIR and source.rstrip("/").endswith("keystonectl"):
                    return True
            continue
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "")
        source = str(item.get("source") or "")
        if target.rstrip("/") == WIRED_DIR and source.rstrip("/").endswith("keystonectl"):
            return True
    return False


def policy_host_path(
    config: Mapping[str, Any], layout: Mapping[str, Any], root: Path
) -> Path | None:
    service_name = str(layout.get("keystone_service") or "")
    services = config.get("services") if isinstance(config.get("services"), dict) else {}
    service = services.get(service_name)
    container_path = ""
    if isinstance(service, dict):
        container_path = service_env(service).get("KEYSTONE_MCP_RBAC_POLICY_PATH", "")
        if container_path and isinstance(service.get("volumes"), list):
            for item in service["volumes"]:
                if isinstance(item, dict) and str(item.get("target") or "") == container_path:
                    source = item.get("source")
                    if isinstance(source, str) and source:
                        return Path(source)
                if isinstance(item, str) and ":" in item:
                    parts = item.split(":")
                    if len(parts) >= 2 and parts[1] == container_path:
                        source = parts[0]
                        return Path(source) if source.startswith("/") else root / source
    policy = layout.get("policy") if isinstance(layout.get("policy"), dict) else {}
    relative = policy.get("file") if isinstance(policy, dict) else None
    if isinstance(relative, str) and relative:
        return root / relative
    return None


def ensure_network(runner: CommandRunner, name: str) -> CommandResult:
    inspected = run(runner, ["docker", "network", "inspect", name])
    if inspected.returncode == 0:
        return inspected
    if daemon_down(inspected):
        return inspected
    return run(runner, ["docker", "network", "create", name])


def image_ready(runner: CommandRunner, image: str) -> tuple[bool, bool]:
    """Return ``(available, daemon_down)``."""

    inspected = run(runner, ["docker", "image", "inspect", image])
    if inspected.returncode == 0:
        return True, False
    if daemon_down(inspected):
        return False, True
    pulled = run(runner, ["docker", "pull", image])
    if daemon_down(pulled):
        return False, True
    return pulled.returncode == 0, False
