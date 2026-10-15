"""Fixed queries over one keystonectl JSON document. No expressions."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any


def load_document(text: str) -> dict[str, Any]:
    document = json.loads(text)
    if not isinstance(document, dict):
        raise TypeError("JSON root must be an object")
    return document


def _missing(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = document.get("missing")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, Mapping)]


def _warnings(document: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    for item in document.get("warnings") or []:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            found.append(item["id"])
    result = document.get("result")
    if isinstance(result, Mapping):
        for service in result.get("services") or []:
            if not isinstance(service, Mapping):
                continue
            for item in service.get("warnings") or []:
                if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                    found.append(item["id"])
    return found


def _problems(document: Mapping[str, Any]) -> list[str]:
    found: list[str] = []

    def take(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                found.append(item["id"])
            elif isinstance(item, str):
                found.append(item)

    result = document.get("result")
    if isinstance(result, Mapping):
        take(result.get("problems"))
        for service in result.get("services") or []:
            if isinstance(service, Mapping):
                take(service.get("problems"))
        for check in result.get("checks") or []:
            if (
                isinstance(check, Mapping)
                and check.get("status") == "error"
                and isinstance(check.get("id"), str)
            ):
                found.append(check["id"])
    return found


def _service(document: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    result = document.get("result")
    if not isinstance(result, Mapping):
        return None
    for service in result.get("services") or []:
        if isinstance(service, Mapping) and service.get("name") == name:
            return service
    return None


def query_lines(document: Mapping[str, Any], query: str, arg: str | None) -> list[str]:
    """Return one value per line for a named query."""

    if query == "code":
        return [str(document.get("code"))]
    if query == "missing-vars":
        return [str(item.get("var")) for item in _missing(document) if item.get("var")]
    if query == "missing-kind":
        return [str(item.get("kind") or "") for item in _missing(document)]
    if query == "missing-choices":
        lines: list[str] = []
        for item in _missing(document):
            choices = item.get("choices")
            if isinstance(choices, list):
                lines.extend(str(choice) for choice in choices)
        return lines
    if query == "confirm-vars":
        return [
            str(item.get("var"))
            for item in _missing(document)
            if item.get("kind") == "confirm" and item.get("var")
        ]
    if query == "error-id":
        error = document.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("id"), str):
            return [error["id"]]
        return []
    if query == "retryable":
        return ["true" if document.get("retryable") else "false"]
    if query == "changes":
        return [str(document.get("changes"))]
    if query == "kept":
        result = document.get("result")
        kept = result.get("kept") if isinstance(result, Mapping) else None
        if not isinstance(kept, list):
            return []
        return [str(item) for item in kept]
    if query == "warning-ids":
        return _warnings(document)
    if query == "problems":
        return _problems(document)
    if query == "compose-command":
        result = document.get("result")
        if isinstance(result, Mapping) and isinstance(result.get("compose_command"), str):
            return [result["compose_command"]]
        return []
    if query == "public-state":
        return _public_field(document, "state")
    if query == "public-tls":
        return _public_field(document, "tls")
    if query == "packages":
        result = document.get("result")
        packages = result.get("packages") if isinstance(result, Mapping) else None
        if not isinstance(packages, list):
            return []
        lines = []
        for item in packages:
            if isinstance(item, Mapping):
                lines.append(f"{item.get('name')} {item.get('status')}")
        return lines
    if query == "has-recommended-policy":
        result = document.get("result")
        if isinstance(result, Mapping) and result.get("recommended_policy"):
            return ["yes"]
        return ["no"]
    if query == "package-status":
        if not arg:
            raise ValueError("package-status needs a package name")
        return _package_status(document, arg)
    if query == "box-service":
        if not arg:
            raise ValueError("box-service needs a package name")
        return _box_service(document, arg)
    raise ValueError(f"unknown query {query}")


def _public_field(document: Mapping[str, Any], field: str) -> list[str]:
    result = document.get("result")
    if not isinstance(result, Mapping):
        return []
    public = result.get("public")
    if isinstance(public, Mapping) and public.get(field) is not None:
        return [str(public[field])]
    return []


def _box_service(document: Mapping[str, Any], name: str) -> list[str]:
    service = _service(document, name)
    if service is None:
        return []
    for container in service.get("containers") or []:
        if not isinstance(container, Mapping) or container.get("role") != "mcp":
            continue
        compose_service = container.get("service")
        if isinstance(compose_service, str) and compose_service:
            return [compose_service]
    return []


def _package_status(document: Mapping[str, Any], name: str) -> list[str]:
    service = _service(document, name)
    if service is None:
        return ["missing"]
    lines = [f"managed {service.get('managed')}"]
    for container in service.get("containers") or []:
        if not isinstance(container, Mapping):
            continue
        lines.append(
            "container {role} state {state} health {health} exit {code}".format(
                role=container.get("role"),
                state=container.get("state"),
                health=container.get("health"),
                code=container.get("exit_code"),
            )
        )
    for item in service.get("problems") or []:
        if isinstance(item, Mapping):
            lines.append(f"problem {item.get('id')}")
    for item in service.get("warnings") or []:
        if isinstance(item, Mapping):
            lines.append(f"warning {item.get('id')}")
    keystone = service.get("keystone")
    if isinstance(keystone, Mapping):
        lines.append(f"keystone {keystone.get('ready')}")
        lines.append(f"discovery {keystone.get('discovery')}")
    public = service.get("public")
    if isinstance(public, Mapping):
        for field in ("state", "tls", "inside"):
            if public.get(field) is not None:
                lines.append(f"public_{field} {public.get(field)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(
            "usage: jsonq.py QUERY [ARG]\n"
            "queries: code missing-vars missing-kind missing-choices confirm-vars "
            "error-id retryable changes kept warning-ids problems compose-command "
            "public-state public-tls packages has-recommended-policy package-status "
            "box-service",
            file=sys.stderr,
        )
        return 2
    query = args[0]
    arg = args[1] if len(args) > 1 else None
    if len(args) > 2:
        print("jsonq.py takes one query and one optional argument", file=sys.stderr)
        return 2
    try:
        document = load_document(sys.stdin.read())
        lines = query_lines(document, query, arg)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.write("".join(f"{line}\n" for line in lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
