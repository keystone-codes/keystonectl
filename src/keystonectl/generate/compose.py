"""Deterministic compose fragment for one package."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from keystonectl.errors import fragment_modified_message
from keystonectl.generate.placement import (
    joins_resources,
    joins_upstreams,
    mcp_role,
    package_roles,
    placement,
    public_role,
)

RESOURCES_NETWORK = "keystone-resources"
UPSTREAMS_NETWORK = "keystone-mcp-upstreams"
_DEFAULT_HEALTH = {"interval": "10s", "timeout": "5s", "start_period": "20s", "retries": 5}
_YAML11 = {
    "y",
    "n",
    "yes",
    "no",
    "true",
    "false",
    "on",
    "off",
    "Y",
    "N",
    "Yes",
    "No",
    "True",
    "False",
    "On",
    "Off",
}
_PLAIN = re.compile(r"[A-Za-z0-9_./:@+=,%$~-]+")
_DANGEROUS = ("privileged", "cap_add", "network_mode", "pid", "profiles")


def yaml_scalar(value: str) -> str:
    """Quote a scalar so a newline cannot open another YAML key."""

    if value in _YAML11 or value == "" or value[:1] in "-?:,[]{}#&*!|>%@`'\"":
        return json.dumps(value)
    if _PLAIN.fullmatch(value):
        return value
    return json.dumps(value)


def _yaml_token(value: object) -> str:
    if isinstance(value, bool):
        return yaml_scalar("true" if value else "false")
    if isinstance(value, int):
        return str(value)
    return yaml_scalar(str(value))


def _one_line(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ")


def yaml_flow(items: Sequence[str]) -> str:
    return "[" + ", ".join(json.dumps(item) for item in items) + "]"


def escape_compose(value: str) -> str:
    """Compose interpolates ``$``. A literal dollar is written ``$$``."""
    return value.replace("$", "$$")


def fragment_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fragment_guard(entry: Mapping[str, Any], path: Path) -> str | None:
    """Return an error message when a generated fragment was edited by hand.

    A missing file may be written. A matching hash may be rewritten. ``ejected``
    is reported as the string ``ejected`` so the caller skips the write.
    """

    if entry.get("managed") == "ejected":
        return "ejected"
    if not path.is_file():
        return None
    expected = entry.get("fragment_sha256")
    if not isinstance(expected, str) or not expected:
        return None
    if hashlib.sha256(path.read_bytes()).hexdigest() == expected:
        return None
    name = str(entry.get("name") or path.stem)
    return fragment_modified_message(name)


def _env_values(manifest: Mapping[str, Any], entry: Mapping[str, Any], role: str) -> dict[str, str]:
    containers = manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
    spec = containers.get(role) if isinstance(containers.get(role), dict) else {}
    overrides = entry.get("env_values") if isinstance(entry.get("env_values"), dict) else {}
    role_overrides = overrides.get(role) if isinstance(overrides.get(role), dict) else {}
    values: dict[str, str] = {}
    for item in spec.get("env") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        key = str(item["name"])
        if key in role_overrides:
            values[key] = str(role_overrides[key])
        elif "default" in item and item["default"] is not None:
            values[key] = str(item["default"])
    return values


def _path_health_shell(url: str) -> str:
    py = f"python -c \"import urllib.request; urllib.request.urlopen('{url}')\""
    return f"wget -q -O /dev/null {url} || curl -fsS -o /dev/null {url} || {py}"


def _health(spec: Mapping[str, Any], *, port: int | None) -> list[str] | None:
    health = spec.get("healthcheck")
    if not isinstance(health, dict):
        return None
    lines = ["    healthcheck:"]
    command = health.get("command")
    path = health.get("path")
    if isinstance(command, list) and command:
        lines.append(f"      test: {yaml_flow(['CMD', *map(str, command)])}")
    elif isinstance(path, str) and path and port:
        url = f"http://127.0.0.1:{port}{path}"
        shell = _path_health_shell(url)
        lines.append(f"      test: {yaml_flow(['CMD-SHELL', shell])}")
    else:
        return None
    interval = health.get("interval") or _DEFAULT_HEALTH["interval"]
    timeout = health.get("timeout") or _DEFAULT_HEALTH["timeout"]
    start = health.get("start_period") or _DEFAULT_HEALTH["start_period"]
    retries = health.get("retries", _DEFAULT_HEALTH["retries"])
    lines.append(f"      interval: {_yaml_token(interval)}")
    lines.append(f"      timeout: {_yaml_token(timeout)}")
    lines.append(f"      start_period: {_yaml_token(start)}")
    lines.append(f"      retries: {_yaml_token(retries)}")
    return lines


def _named_volumes(spec: Mapping[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for item in spec.get("volumes") or []:
        if not isinstance(item, dict):
            continue
        volume = item.get("name")
        target = item.get("path")
        if isinstance(volume, str) and isinstance(target, str) and "/" not in volume:
            found.append((volume, target))
    return found


def render_fragment(
    manifest: Mapping[str, Any],
    entry: Mapping[str, Any] | None = None,
    *,
    host_port: int | None = None,
    extra_hosts: Sequence[str] = (),
) -> str:
    """Render ``packages/<name>.yml``. Key order is fixed; roles follow the manifest."""

    document = entry if isinstance(entry, Mapping) else {}
    name = str(manifest["name"])
    version = str(manifest.get("version") or "")
    publisher = str(manifest.get("publisher") or "")
    roles = package_roles(manifest)
    containers = manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
    plan = placement(manifest)
    inits = [
        role
        for role in roles
        if isinstance(containers.get(role), dict) and containers[role].get("init")
    ]
    published = public_role(manifest)
    shown = _one_line(name)
    lines = [
        f"# Generated by keystonectl from {shown} {_one_line(version)} ({_one_line(publisher)}).",
        f"# Do not edit: put your changes in packages/{shown}.override.yml,",
        f"# or run `keystonectl service eject {shown}` to take this file over.",
        "services:",
    ]
    volume_names: list[str] = []
    uses_resources = False
    uses_upstreams = False
    for role in roles:
        spec = containers[role]
        if not isinstance(spec, dict):
            continue
        init = bool(spec.get("init"))
        lines.append(f"  {yaml_scalar(f'{name}-{role}')}:")
        lines.append(f"    image: {yaml_scalar(str(spec.get('image') or ''))}")
        if init:
            lines.append('    restart: "no"')
        else:
            lines.append("    restart: unless-stopped")
        lines.append(f"    security_opt: {yaml_flow(['no-new-privileges:true'])}")
        command = spec.get("command")
        if isinstance(command, list) and command:
            lines.append(f"    command: {yaml_flow([str(item) for item in command])}")
        if isinstance(spec.get("user"), str):
            lines.append(f"    user: {yaml_scalar(str(spec['user']))}")
        env_files = plan.fragment_env_files(role)
        if env_files:
            lines.append(f"    env_file: {yaml_flow(env_files)}")
        environment = _env_values(manifest, document, role)
        if environment:
            lines.append("    environment:")
            for key, value in environment.items():
                lines.append(f"      {yaml_scalar(key)}: {yaml_scalar(escape_compose(value))}")
        port = spec.get("port")
        port_number = int(port) if isinstance(port, int) else None
        if host_port is not None and published == role and port_number is not None and not init:
            lines.append(f"    ports: {yaml_flow([f'127.0.0.1:{host_port}:{port_number}'])}")
        if extra_hosts and not init:
            hosts = [f"{domain}:host-gateway" for domain in extra_hosts]
            lines.append(f"    extra_hosts: {yaml_flow(hosts)}")
        volumes = _named_volumes(spec)
        if volumes:
            lines.append("    volumes:")
            for volume, target in volumes:
                key = f"{name}-{volume}"
                if key not in volume_names:
                    volume_names.append(key)
                lines.append(f"      - {yaml_scalar(f'{key}:{target}')}")
        lines.append("    networks:")
        lines.append(f"      {yaml_scalar(f'{name}-internal')}: {{aliases: [{yaml_scalar(role)}]}}")
        if joins_resources(manifest, role):
            uses_resources = True
            lines.append(f"      {yaml_scalar(f'{name}-resources')}: {{}}")
        if joins_upstreams(manifest, role):
            uses_upstreams = True
            alias = yaml_scalar(name)
            lines.append(f"      {yaml_scalar(f'{name}-upstreams')}: {{aliases: [{alias}]}}")
        if not init and inits:
            lines.append("    depends_on:")
            for init_role in inits:
                lines.append(
                    f"      {yaml_scalar(f'{name}-{init_role}')}: "
                    "{condition: service_completed_successfully}"
                )
        if not init:
            health = _health(spec, port=port_number)
            if health:
                lines.extend(health)
    lines.append("networks:")
    lines.append(f"  {yaml_scalar(f'{name}-internal')}:")
    lines.append(f"    name: {yaml_scalar(f'ks-pkg-{name}')}")
    if uses_resources:
        lines.append(f"  {yaml_scalar(f'{name}-resources')}:")
        lines.append(f"    name: {yaml_scalar(RESOURCES_NETWORK)}")
        lines.append("    external: true")
    if uses_upstreams:
        lines.append(f"  {yaml_scalar(f'{name}-upstreams')}:")
        lines.append(f"    name: {yaml_scalar(UPSTREAMS_NETWORK)}")
        lines.append("    external: true")
    if volume_names:
        lines.append("volumes:")
        for key in volume_names:
            suffix = key[len(name) + 1 :]
            lines.append(f"  {yaml_scalar(key)}:")
            lines.append(f"    name: {yaml_scalar(f'ks-pkg-{name}-{suffix}')}")
    text = "\n".join(lines) + "\n"
    for forbidden in _DANGEROUS:
        if re.search(rf"^\s*{forbidden}:", text, re.MULTILINE):
            raise RuntimeError(f"fragment emitted {forbidden}")
    return text


def service_names(manifest: Mapping[str, Any]) -> list[str]:
    name = str(manifest["name"])
    return [f"{name}-{role}" for role in package_roles(manifest)]


def mcp_service(manifest: Mapping[str, Any]) -> str | None:
    role = mcp_role(manifest)
    if role is None:
        return None
    return f"{manifest['name']}-{role}"
