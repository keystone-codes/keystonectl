"""Upstream registry and catalog snapshot documents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import yaml

REGISTRY_HEADER = "# Managed by keystonectl: entries are preserved, comments are not.\n"


def bearer_env_name(namespace: str) -> str:
    return f"KEYSTONE_MCP_UPSTREAM_{namespace.upper().replace('-', '_')}_BEARER"


def identity_env_name(namespace: str) -> str:
    return f"KEYSTONE_MCP_UPSTREAM_{namespace.upper().replace('-', '_')}_IDENTITY_SECRET"


def registry_url(manifest: Mapping[str, Any]) -> str:
    mcp = manifest["mcp"]
    role = str(mcp["container"])
    containers = manifest["containers"]
    port = int(containers[role]["port"])
    path = str(mcp.get("path") or "/mcp")
    if not path.startswith("/"):
        path = "/" + path
    return f"http://{manifest['name']}:{port}{path}"


def tool_names(namespace: str, tools: Sequence[str]) -> list[str]:
    return [f"{namespace}_{tool}" for tool in tools]


def render_registry(entries: Sequence[Mapping[str, Any]]) -> str:
    document = {"schema_version": 1, "entries": [dict(item) for item in entries]}
    body = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    return REGISTRY_HEADER + body


def merge_registry_entries(
    current: Sequence[Mapping[str, Any]],
    entry: Mapping[str, Any],
) -> list[dict[str, Any]]:
    name = str(entry["name"])
    merged: list[dict[str, Any]] = []
    replaced = False
    for item in current:
        if str(item.get("name")) == name:
            merged.append(dict(entry))
            replaced = True
        else:
            merged.append(dict(item))
    if not replaced:
        merged.append(dict(entry))
    return merged


def registry_entry(
    manifest: Mapping[str, Any], *, namespace: str, registry_name: str
) -> dict[str, Any]:
    return {
        "name": registry_name,
        "url": registry_url(manifest),
        "namespace": namespace,
        "enabled": True,
        "auth": {
            "bearer_env": bearer_env_name(namespace),
            "identity_secret_env": identity_env_name(namespace),
        },
    }


def _names(section: Any) -> list[str]:
    found: list[str] = []
    if not isinstance(section, list):
        return found
    for item in section:
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            found.append(str(item["name"]))
    return found


def owned_tool_names(services: Sequence[Mapping[str, Any]]) -> set[str]:
    owned: set[str] = set()
    for service in services:
        namespace = service.get("namespace")
        tools = service.get("tools")
        if not isinstance(namespace, str) or not namespace or not isinstance(tools, list):
            continue
        for tool in tools:
            if isinstance(tool, str):
                owned.add(f"{namespace}_{tool}")
    return owned


def merge_snapshot(
    current: Mapping[str, Any] | None,
    *,
    services: Sequence[Mapping[str, Any]],
    previous_owned: set[str],
) -> dict[str, Any]:
    """Keep foreign active names. Replace names this lock used to publish."""

    document = dict(current or {})
    active = set(_names(document.get("active")))
    active -= set(previous_owned)
    active |= owned_tool_names(services)
    merged = {
        "active": sorted(active),
        "reserved": list(document.get("reserved") or []),
        "wip": list(document.get("wip") or []),
    }
    return merged


def render_snapshot(document: Mapping[str, Any]) -> str:
    payload = {
        "active": list(document.get("active") or []),
        "reserved": list(document.get("reserved") or []),
        "wip": list(document.get("wip") or []),
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
