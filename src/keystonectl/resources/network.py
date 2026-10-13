"""Resolve a compose service to the address Docker published on a network."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def ip_for_service(containers: object, service: str) -> str:
    """Return the IPv4 address of ``service`` inside a network-inspect payload.

    ``docker network inspect`` names the container ``<project>-<service>-<n>``.
    Aliases, when present, use the service name itself.
    """

    if not isinstance(containers, dict):
        return ""
    for item in containers.values():
        if not isinstance(item, Mapping):
            continue
        if not any(name_matches_service(name, service) for name in endpoint_names(item)):
            continue
        ip = str(item.get("IPv4Address") or "").split("/", 1)[0]
        if ip:
            return ip
    return ""


def endpoint_names(item: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    raw_name = item.get("Name")
    if isinstance(raw_name, str) and raw_name.strip():
        names.append(raw_name.strip().lstrip("/"))
    for key in ("Aliases", "DNSNames"):
        raw = item.get(key) or []
        if isinstance(raw, list):
            names.extend(str(name).strip().lstrip("/") for name in raw if str(name).strip())
    return names


def name_matches_service(candidate: str, service: str) -> bool:
    if candidate == service:
        return True
    for separator in ("-", "_"):
        if candidate.endswith(f"{separator}{service}"):
            return True
        marker = f"{separator}{service}{separator}"
        index = candidate.find(marker)
        if index == -1:
            continue
        replica = candidate[index + len(marker) :]
        if replica.isdigit():
            return True
    return False
