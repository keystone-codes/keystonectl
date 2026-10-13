"""Namespace and bearer-env checks copied from keystone-mcp 0.5.0."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from keystonectl.generate.registry import bearer_env_name, identity_env_name
from keystonectl.keystone_mcp_rules import (
    _NAMESPACE_RE,
    BUILTIN_TOOL_NAMES,
    RESERVED_BEARER_ENV_NAMES,
)


def namespace_reason(namespace: str, existing: Sequence[str]) -> str | None:
    if not _NAMESPACE_RE.match(namespace):
        return (
            "it must be 1-64 characters of letters, digits, '_' or '-', "
            "not starting or ending with '_' or '-'"
        )
    prefix = f"{namespace}_"
    for tool in sorted(BUILTIN_TOOL_NAMES):
        if tool.startswith(prefix):
            return f"published names {namespace}_* can collide with built-in tool {tool}"
    for other in existing:
        if other == namespace:
            return f"namespace {other} is already used"
        if other.startswith(prefix) or namespace.startswith(f"{other}_"):
            return f"it overlaps namespace {other}"
    return None


def env_name_reason(namespace: str, entries: Sequence[Mapping[str, Any]]) -> str | None:
    bearer = bearer_env_name(namespace)
    identity = identity_env_name(namespace)
    for name in (bearer, identity):
        if name in RESERVED_BEARER_ENV_NAMES:
            return f"{name} is reserved by keystone-mcp"
    used: set[str] = set()
    for entry in entries:
        auth = entry.get("auth") if isinstance(entry.get("auth"), dict) else {}
        for key in ("bearer_env", "identity_secret_env"):
            value = auth.get(key)
            if isinstance(value, str):
                used.add(value)
        other = entry.get("namespace")
        if isinstance(other, str) and other and other != namespace:
            used.add(bearer_env_name(other))
            used.add(identity_env_name(other))
    for name in (bearer, identity):
        if name in used:
            return f"{name} is already used by another registry entry"
    return None
