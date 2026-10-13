"""Parse keystone-mcp 0.5.0 validate output. Messages are a contract of that release."""

from __future__ import annotations

import re

_UNMATCHED = "unmatched allow patterns (typo or missing catalog/WIP entry): "
_UNREACHABLE = re.compile(r"rbac policy leaves (\d+) tool\(s\) unreachable by any role: (.*)")
_DISCOVERY = re.compile(
    r"upstream discovery: \d+ of \d+ upstream\(s\) unavailable \((.*)\); proxying the rest"
)
_DISCOVERY_ALL = "upstream discovery failed for every configured upstream"


def unmatched_patterns(text: str) -> list[str]:
    found: list[str] = []
    for line in text.splitlines():
        if _UNMATCHED not in line:
            continue
        tail = line.split(_UNMATCHED, 1)[1].strip()
        if tail:
            found.extend(part.strip() for part in tail.split(",") if part.strip())
    return found


def is_namespace_error(text: str) -> bool:
    return any("namespace" in line for line in text.splitlines())


def unreachable_tools(text: str) -> list[str]:
    for line in text.splitlines():
        match = _UNREACHABLE.search(line)
        if match is None:
            continue
        tail = match.group(2).strip()
        if not tail:
            return []
        return [part.strip() for part in tail.split(",") if part.strip()]
    return []


def discovery_unavailable(log_text: str, namespace: str) -> bool | None:
    """True when logs say the namespace is down, False when they do not, None if no discovery line."""

    saw = False
    for line in log_text.splitlines():
        if _DISCOVERY_ALL in line:
            return True
        match = _DISCOVERY.search(line)
        if match is None:
            continue
        saw = True
        detail = match.group(1)
        if namespace in detail:
            return True
    if saw:
        return False
    return None


def metric_available(text: str, namespace: str) -> bool | None:
    needle = f'keystone_mcp_upstream_available{{namespace="{namespace}"}}'
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(needle):
            value = stripped.split()[-1]
            return value == "1"
    if "keystone_mcp_upstream_available" in text:
        return False
    return None
