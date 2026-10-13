"""``keystonectl service status`` reads containers and the gateway. It does not change files."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from keystonectl.errors import ERRORS
from keystonectl.generate.docker import (
    PUBLIC_PROBE_CODE,
    UPSTREAMS_NETWORK,
    compose_prefix,
    daemon_down,
    parse_config,
    run,
)
from keystonectl.generate.install import _discover, _ps
from keystonectl.lock import LockError, load_lock
from keystonectl.output import Result
from keystonectl.plan import Context
from keystonectl.runner import CommandRunner


def run_status(
    root: Path,
    runner: CommandRunner,
    *,
    name: str | None = None,
    secrets: Any = None,
) -> Result:
    try:
        lock = load_lock(root / "services.lock.yaml")
    except LockError as exc:
        return Result(
            code=exc.code,
            command="service status",
            summary=exc.message,
            error={"id": exc.error_id, "message": exc.message, "hint": exc.hint or ""},
        )
    layout = lock.get("layout") if isinstance(lock.get("layout"), dict) else {}
    project = str(layout.get("project") or "")
    config = parse_config(
        run(runner, [*compose_prefix(root, layout), "config", "--format", "json"], cwd=root)
    )
    if config and config.get("name"):
        project = str(config["name"])
    probe = run(runner, ["docker", "info"], cwd=root)
    if daemon_down(probe):
        spec = ERRORS["docker_unavailable"]
        return Result(
            code=spec.code,
            command="service status",
            summary=spec.title,
            error={"id": spec.id, "message": spec.title, "hint": spec.action},
            retryable=True,
        )
    rows = _ps(_status_ctx(root, runner, lock), project) if project else []
    if not rows:
        ps = run(
            runner,
            [
                *compose_prefix(root, layout, project=project or None),
                "ps",
                "--all",
                "--format",
                "json",
            ],
            cwd=root,
        )
        if daemon_down(ps):
            spec = ERRORS["docker_unavailable"]
            return Result(
                code=spec.code,
                command="service status",
                summary=spec.title,
                error={"id": spec.id, "message": spec.title, "hint": spec.action},
                retryable=True,
            )
    by_service = {str(row.get("Service")): row for row in rows}
    services = []
    for entry in lock.get("services") or []:
        if not isinstance(entry, dict):
            continue
        if name and entry.get("name") != name:
            continue
        services.append(_one(root, runner, lock, layout, project, entry, by_service))
    return Result(
        code=0,
        command="service status",
        summary="Service status.",
        result={"services": services},
    )


def _status_ctx(root: Path, runner: CommandRunner, lock: dict[str, Any]) -> Context:
    from datetime import UTC, datetime

    from keystonectl.output import SecretRegistry

    return Context(
        root=root,
        runner=runner,
        secrets=SecretRegistry(),
        sets={},
        environ={},
        interactive=False,
        lock=lock,
        clock=lambda: datetime.now(UTC),
    )


def _one(
    root: Path,
    runner: CommandRunner,
    lock: Mapping[str, Any],
    layout: Mapping[str, Any],
    project: str,
    entry: Mapping[str, Any],
    rows: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    managed = str(entry.get("managed") or "")
    body: dict[str, Any] = {
        "name": entry.get("name"),
        "managed": managed,
        "version": entry.get("version"),
        "namespace": entry.get("namespace"),
        "containers": [],
        "keystone": _keystone(root, runner, layout, project, entry),
        "public": None,
        "problems": [],
        "warnings": [],
    }
    if managed == "remote":
        return body
    containers = entry.get("containers") if isinstance(entry.get("containers"), dict) else {}
    for role, spec in containers.items():
        if not isinstance(spec, dict):
            continue
        service = str(spec.get("service") or f"{entry.get('name')}-{role}")
        row = rows.get(service)
        state, health, code = _row_state(row)
        item = {
            "role": role,
            "service": service,
            "state": state,
            "health": health,
            "exit_code": code,
        }
        body["containers"].append(item)
        if row is None:
            body["problems"].append(
                {
                    "id": "container_missing",
                    "message": (
                        f"{service} is not in the compose project. "
                        "If you renamed it, put the lock name back or eject the package."
                    ),
                }
            )
            continue
        if spec.get("init"):
            if state not in {"exited", "dead"}:
                body["problems"].append(
                    {"id": "init_pending", "message": f"{service} has not finished."}
                )
            elif code not in {0, "0", None}:
                body["problems"].append(
                    {"id": "init_failed", "message": f"{service} exited {code}."}
                )
        elif health == "unhealthy" or state in {"exited", "dead", "restarting"}:
            body["problems"].append(
                {"id": "container_unhealthy", "message": f"{service} is {state or health}."}
            )
    public = entry.get("public") if isinstance(entry.get("public"), dict) else {}
    if public.get("state") == "ready":
        inside, warning, problem = _public_inside(root, runner, layout, entry, public)
        body["public"] = {"inside": inside}
        if warning:
            body["warnings"].append(warning)
        if problem:
            body["problems"].append(problem)
    return body


def _row_state(row: Mapping[str, Any] | None) -> tuple[str, str, Any]:
    if row is None:
        return "missing", "", None
    return str(row.get("State") or ""), str(row.get("Health") or ""), row.get("ExitCode")


def _keystone(
    root: Path,
    runner: CommandRunner,
    layout: Mapping[str, Any],
    project: str,
    entry: Mapping[str, Any],
) -> dict[str, str]:
    service = str(layout.get("keystone_service") or "keystone")
    prefix = compose_prefix(root, layout, project=project or None)
    ready = run(
        runner,
        [*prefix, "exec", "-T", service, "python", "-m", "keystone_mcp.healthcheck", "/readyz"],
        cwd=root,
    )
    state = "ready" if ready.returncode == 0 else "down"
    namespace = entry.get("namespace")
    discovery = "unknown"
    if isinstance(namespace, str) and namespace and state == "ready":
        ctx = _status_ctx(root, runner, {"layout": dict(layout), "services": []})
        discovery = _discover(ctx, project, namespace)
        if discovery is None:
            discovery = "unknown"
    return {"ready": state, "discovery": discovery}


def _public_inside(
    root: Path,
    runner: CommandRunner,
    layout: Mapping[str, Any],
    entry: Mapping[str, Any],
    public: Mapping[str, Any],
) -> tuple[str, dict[str, str] | None, dict[str, str] | None]:
    domain = str(public.get("domain") or "")
    healthz = str(public.get("healthz") or "/healthz")
    image = str(layout.get("keystone_image") or "")
    url = f"https://{domain}{healthz}"
    result = run(
        runner,
        [
            "docker",
            "run",
            "--rm",
            "--network",
            UPSTREAMS_NETWORK,
            "--add-host",
            f"{domain}:host-gateway",
            "--entrypoint",
            "python",
            image,
            "-c",
            PUBLIC_PROBE_CODE,
            url,
        ],
        cwd=root,
    )
    text = f"{result.stdout}\n{result.stderr}"
    return classify_public(text)


def classify_public(text: str) -> tuple[str, dict[str, str] | None, dict[str, str] | None]:
    """Map a public probe transcript to inside/warning/problem."""

    for line in text.splitlines():
        if line.startswith("status:"):
            code = line.split(":", 1)[1].strip()
            if code == "200":
                return "ok", None, None
            if code in {"502", "503", "504"}:
                return "down", None, {"id": "public_upstream_down", "message": f"HTTP {code}"}
        if "CERTIFICATE_VERIFY_FAILED" in line or "SSLCertVerificationError" in line:
            return "ok", {"id": "tls_untrusted", "message": WARNINGS_TLS}, None
    return "down", None, {"id": "hairpin_failed", "message": "No connection or a TLS error."}


WARNINGS_TLS = "The public certificate is not trusted by the keystone-mcp image."
