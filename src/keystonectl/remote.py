"""service add-remote: register a service the operator runs, without compose."""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from keystonectl import __version__
from keystonectl.answers import Question, pkg_var
from keystonectl.envfiles import quote_env, read_env
from keystonectl.errors import ERRORS, WARNINGS
from keystonectl.generate.docker import (
    UPSTREAMS_NETWORK,
    compose_config,
    compose_prefix,
    daemon_down,
    image_ready,
    layout_problems,
    parse_config,
    run,
)
from keystonectl.generate.files import atomic_write, write_backup
from keystonectl.generate.namespace import env_name_reason, namespace_reason
from keystonectl.generate.packages_file import compose_is_old
from keystonectl.generate.registry import (
    bearer_env_name,
    identity_env_name,
    merge_registry_entries,
    merge_snapshot,
    render_registry,
    render_snapshot,
    tool_names,
)
from keystonectl.generate.tokens import token_urlsafe
from keystonectl.keystone_mcp_rules import _NAMESPACE_RE
from keystonectl.layout import _ENV_BLOCK_END, _ENV_BLOCK_START, replace_managed_env_block
from keystonectl.lock import LockError, load_lock, save_lock, utc_now
from keystonectl.output import Result, SecretRegistry, missing_summary
from keystonectl.runner import CommandRunner

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_LOCK_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_TOOL = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HANDOFF = (
    "give this file to the team that runs the service; it must check the bearer "
    "and X-Keystone-Identity (see docs/connecting-services.md in keystone-mcp)"
)
_PROBE = """
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
url = sys.argv[1]
try:
    with urlopen(url, timeout=4) as response:
        print("status:%s" % response.status)
except HTTPError as exc:
    print("status:%s" % exc.code)
except URLError:
    sys.exit(1)
except Exception:
    sys.exit(1)
""".strip()


def register(subparsers: argparse._SubParsersAction) -> None:
    from keystonectl.check import register_check

    parser = subparsers.add_parser(
        "add-remote",
        help="Connect a service you run yourself. No compose is generated.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root", default=None)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--set", action="append", default=None)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("name")
    parser.add_argument("--url", required=True)
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--tools", default=None)
    parser.add_argument("--tool-prefix", default=None)
    parser.add_argument("--include-prefixes", default=None)
    parser.add_argument("--exclude-prefixes", default=None)
    parser.add_argument("--strip-prefix", default=None)
    parser.add_argument("--no-auth", action="store_true")
    parser.add_argument("--adopt-existing", action="store_true")
    ensure_remote_checks(register_check)


def ensure_remote_checks(register_check: Callable[..., None] | None = None) -> None:
    from keystonectl.check import _EXTRA
    from keystonectl.check import register_check as default_register

    hook = register_check or default_register
    if any(getattr(fn, "check_id", None) == "remote_tools" for fn in _EXTRA):
        return
    check_remote_tools.check_id = "remote_tools"  # type: ignore[attr-defined]
    hook(check_remote_tools)


def add_remote(
    root: Path,
    name: str,
    url: str,
    *,
    runner: CommandRunner,
    secrets: SecretRegistry,
    sets: Mapping[str, str],
    environ: Mapping[str, str],
    interactive: bool,
    clock: Callable[[], datetime] | None = None,
    namespace: str | None = None,
    tools: str | None = None,
    tool_prefix: str | None = None,
    include_prefixes: str | None = None,
    exclude_prefixes: str | None = None,
    strip_prefix: str | None = None,
    no_auth: bool = False,
    adopt_existing: bool = False,
) -> Result:
    command = "service add-remote"
    moment = clock or (lambda: datetime.now(UTC))
    refused = _name_error(command, name)
    if refused is not None:
        return refused
    url_error = _url_error(command, name, url)
    if url_error is not None:
        return url_error
    try:
        lock = load_lock(root / "services.lock.yaml")
    except LockError as exc:
        return _fail(command, name, "lock_corrupt", exc.message)
    current = _lock_entry(lock, name)
    if current is not None and current.get("managed") != "remote":
        return _fail(
            command,
            name,
            "registry_name_taken",
            f"{name} is already an installed package.",
        )
    entries = _registry_entries(root)
    foreign = _registry_entry(entries, name)
    if foreign is not None and current is None and not adopt_existing:
        return _fail(
            command,
            name,
            "registry_name_taken",
            f"Registry entry {name} is not in the lock. Pass --adopt-existing to take it.",
        )
    if adopt_existing and current is None:
        if foreign is None:
            return _fail(
                command,
                name,
                "registry_name_taken",
                f"Registry entry {name} was not found.",
            )
        mismatch = _adopt_mismatch(
            foreign,
            url,
            tool_prefix=tool_prefix,
            include_prefixes=include_prefixes,
            exclude_prefixes=exclude_prefixes,
            strip_prefix=strip_prefix,
        )
        if mismatch is not None:
            return _fail(command, name, "remote_url_mismatch", mismatch)
        return _write_adopted(root, lock, name, foreign, runner, secrets, moment)
    chosen_namespace = namespace or _default_namespace(name)
    if current is not None and str(current.get("namespace") or "") != chosen_namespace:
        return _fail(
            command,
            name,
            "namespace_change_unsupported",
            ERRORS["namespace_change_unsupported"].title,
            hint="Run keystonectl service remove, then add-remote again.",
        )
    asked = _namespace_question(name, chosen_namespace, entries, sets, environ)
    if isinstance(asked, Result):
        return asked
    chosen_namespace = asked
    tool_list = _split_tools(tools) if tools is not None else None
    if tool_list is not None:
        bad = [item for item in tool_list if not _TOOL.fullmatch(item)]
        if bad:
            return _fail(command, name, "registry_invalid", f"Invalid tool name: {bad[0]}.")
    filters = _filters_from_flags(
        tool_prefix=tool_prefix,
        include_prefixes=include_prefixes,
        exclude_prefixes=exclude_prefixes,
        strip_prefix=strip_prefix,
    )
    kept_filters = current.get("filters") if isinstance(current, dict) else None
    if not filters and isinstance(kept_filters, dict):
        filters = dict(kept_filters)
    if current is not None and _unchanged_remote(
        current, url, chosen_namespace, tool_list, filters, no_auth=no_auth
    ):
        return Result(
            code=0,
            command=command,
            summary=f"{name} is unchanged.",
            target=name,
            changes=0,
            result=_result_body(
                name,
                str(current.get("namespace") or chosen_namespace),
                url,
                _handoff_rel(name) if _handoff_listed(current) else None,
                True,
            ),
        )
    warnings: list[dict[str, str]] = []
    if no_auth:
        warnings.append(
            {
                "id": "remote_without_identity",
                "message": WARNINGS["remote_without_identity"].title,
            }
        )
    reachable, reach_warning = _probe(root, lock, runner, url)
    if reach_warning:
        warnings.append(reach_warning)
    auth_names = _auth_names(chosen_namespace) if not no_auth else None
    entry = _registry_record(name, url, chosen_namespace, auth_names, filters)
    merged = merge_registry_entries(entries, entry)
    invalid = _validate_candidates(root, lock, runner, merged)
    if invalid is not None:
        invalid.warnings = [*warnings, *invalid.warnings]
        return invalid
    bearer_value, identity_value = "", ""
    if auth_names and not adopt_existing:
        bearer_value, identity_value = _ensure_secrets(root, auth_names, secrets)
    tool_names_for_lock = tool_list
    if tool_names_for_lock is None and current is not None:
        tool_names_for_lock = [str(item) for item in current.get("tools") or []]
    if tool_list is not None:
        _write_snapshot(root, chosen_namespace, tool_list)
    changes = _write_registry(root, merged)
    if auth_names:
        changes += _write_env(root, auth_names, bearer_value, identity_value, url)
        changes += _write_handoff(root, name, auth_names, bearer_value, identity_value)
    _save_remote_lock(
        root,
        lock,
        name=name,
        namespace=chosen_namespace,
        url=url,
        filters=filters or None,
        tools=tool_names_for_lock or [],
        auth_names=list(auth_names) if auth_names else [],
        handoff=not no_auth,
        now=moment(),
    )
    changes += 1
    if _keystone_running(root, lock, runner):
        _restart(root, lock, runner)
    note = _HANDOFF if auth_names else ""
    summary = (
        f"Connected {name}. Network {UPSTREAMS_NETWORK}; the url must answer from inside "
        f"that network. "
    )
    if no_auth:
        summary += (
            "No bearer or identity secret was issued. Any container on the services "
            "network can call this service as anyone."
        )
    else:
        summary += f"The service must check the bearer and X-Keystone-Identity. aud = {name}."
    if note:
        summary = f"{summary} {note}"
    return Result(
        code=0,
        command=command,
        summary=summary,
        target=name,
        warnings=warnings,
        changes=changes,
        result=_result_body(
            name,
            chosen_namespace,
            url,
            _handoff_rel(name) if auth_names else None,
            reachable,
            note=note,
        ),
    )


def _write_adopted(
    root: Path,
    lock: dict[str, Any],
    name: str,
    foreign: Mapping[str, Any],
    runner: CommandRunner,
    secrets: SecretRegistry,
    clock: Callable[[], datetime],
) -> Result:
    command = "service add-remote"
    url = str(foreign.get("url") or "")
    namespace = str(foreign.get("namespace") or _default_namespace(name))
    filters = _filters_from_entry(foreign)
    auth = foreign.get("auth") if isinstance(foreign.get("auth"), dict) else {}
    keystone = [
        str(auth[key])
        for key in ("bearer_env", "identity_secret_env")
        if isinstance(auth.get(key), str)
    ]
    warnings: list[dict[str, str]] = []
    reachable, reach_warning = _probe(root, lock, runner, url)
    if reach_warning:
        warnings.append(reach_warning)
    invalid = _validate_candidates(root, lock, runner, _registry_entries(root))
    if invalid is not None:
        return invalid
    for value in read_env(root / ".env").values():
        if value:
            secrets.register(value)
    _save_remote_lock(
        root,
        lock,
        name=name,
        namespace=namespace,
        url=url,
        filters=filters,
        tools=[],
        auth_names=keystone,
        handoff=False,
        now=clock(),
    )
    if _keystone_running(root, lock, runner):
        _restart(root, lock, runner)
    return Result(
        code=0,
        command=command,
        summary=(
            f"Adopted {name}. Network {UPSTREAMS_NETWORK}; the url must answer from inside "
            f"that network. The service must check the bearer and X-Keystone-Identity. "
            f"aud = {name}."
        ),
        target=name,
        warnings=warnings,
        changes=1,
        result=_result_body(name, namespace, url, None, reachable),
    )


def check_remote_tools(context: Any) -> list[dict[str, str]]:
    lock = context.lock if isinstance(getattr(context, "lock", None), dict) else {}
    root = context.root
    snapshot = _load_yaml(root / "keystonectl" / "catalog-snapshot.yaml")
    active = set()
    if isinstance(snapshot, dict):
        active = {str(item) for item in snapshot.get("active") or [] if isinstance(item, str)}
    live = fetch_live_names(root, lock)
    found: list[dict[str, str]] = []
    for service in lock.get("services") or []:
        if not isinstance(service, dict) or service.get("managed") != "remote":
            continue
        tools = service.get("tools")
        if isinstance(tools, list) and tools:
            continue
        namespace = str(service.get("namespace") or "")
        name = str(service.get("name") or "")
        url = str(service.get("url") or "")
        if live is None:
            found.append(
                {
                    "id": "remote_tools_unknown",
                    "status": "warning",
                    "message": f"Policy rows for {namespace}_* are checked only while the service is up.",
                    "hint": WARNINGS["remote_tools_unknown"].action,
                }
            )
            continue
        prefix = f"{namespace}_"
        missing = sorted(item for item in live if item.startswith(prefix) and item not in active)
        if not missing:
            continue
        bare = ",".join(item.removeprefix(prefix) for item in missing)
        command = f"keystonectl service add-remote {name} --url {url} --tools {bare}"
        found.append(
            {
                "id": "remote_tools_not_in_snapshot",
                "status": "warning",
                "message": (
                    f"Live tools are missing from the snapshot for {name}. Run `{command}`."
                ),
                "hint": command,
            }
        )
    if not found:
        return [
            {
                "id": "remote_tools",
                "status": "ok",
                "message": "Remote snapshots are set.",
                "hint": "",
            }
        ]
    return found


def fetch_live_names(root: Path, lock: Mapping[str, Any]) -> list[str] | None:
    layout = lock.get("layout") if isinstance(lock.get("layout"), dict) else None
    if not layout or not layout.get("keystone_service"):
        return None
    service = str(layout.get("keystone_service"))
    try:
        completed = subprocess.run(
            [
                *compose_prefix(root, layout),
                "exec",
                "-T",
                service,
                "python",
                "-m",
                "keystone_mcp.rbac.catalog",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _name_error(command: str, name: str) -> Result | None:
    if not _NAME.fullmatch(name) or not _LOCK_NAME.fullmatch(name):
        return _fail(command, name, "registry_invalid", f"Invalid service name '{name}'.")
    return None


def _url_error(command: str, name: str, url: str) -> Result | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return _fail(
            command, name, "registry_invalid", "url must be http:// or https:// with a path."
        )
    if not parsed.path or parsed.path == "/":
        return _fail(command, name, "registry_invalid", "url must include a path such as /mcp.")
    host = parsed.hostname or ""
    if parsed.scheme == "http" and "." in host:
        return _fail(
            command,
            name,
            "registry_invalid",
            "http:// is allowed only for a Docker DNS name with no dot.",
        )
    return None


def _default_namespace(name: str) -> str:
    if name.endswith("-mcp") and len(name) > 4:
        return name[: -len("-mcp")]
    return name


def _namespace_question(
    name: str,
    namespace: str,
    entries: list[dict[str, Any]],
    sets: Mapping[str, str],
    environ: Mapping[str, str],
) -> str | Result:
    var = pkg_var(name, "NAMESPACE")
    answered = sets.get(var) or environ.get(var)
    candidate = str(answered) if answered else namespace
    others_entries = [item for item in entries if item.get("name") != name]
    others = [
        str(item.get("namespace"))
        for item in others_entries
        if isinstance(item.get("namespace"), str)
    ]
    reason = None
    if not _NAMESPACE_RE.match(candidate):
        reason = "it does not match the namespace pattern"
    else:
        reason = namespace_reason(candidate, others) or env_name_reason(candidate, others_entries)
    if reason is None:
        return candidate
    question = Question(
        var,
        (
            f"The tool prefix '{candidate}' cannot be used: {reason}. "
            f"Choose another prefix for {name}'s tools (letters, digits, '-' or '_')."
        ),
        "text",
    )
    return Result(
        code=2,
        command="service add-remote",
        summary=missing_summary(1),
        target=name,
        missing=[question.as_missing()],
    )


def _unchanged_remote(
    current: Mapping[str, Any],
    url: str,
    namespace: str,
    tool_list: list[str] | None,
    filters: Mapping[str, Any],
    *,
    no_auth: bool,
) -> bool:
    if str(current.get("url") or "") != url:
        return False
    if str(current.get("namespace") or "") != namespace:
        return False
    existing_tools = [str(item) for item in current.get("tools") or []]
    if tool_list is not None and tool_list != existing_tools:
        return False
    existing_filters = current.get("filters") if isinstance(current.get("filters"), dict) else {}
    if dict(filters) != dict(existing_filters):
        return False
    has_auth = bool((current.get("env") or {}).get("keystone"))
    return has_auth == (not no_auth)


def _split_tools(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def _filters_from_flags(
    *,
    tool_prefix: str | None,
    include_prefixes: str | None,
    exclude_prefixes: str | None,
    strip_prefix: str | None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if tool_prefix:
        filters["tool_prefix"] = tool_prefix
    if include_prefixes:
        filters["include_prefixes"] = _split_tools(include_prefixes)
    if exclude_prefixes:
        filters["exclude_prefixes"] = _split_tools(exclude_prefixes)
    if strip_prefix:
        filters["strip_prefix"] = strip_prefix
    return filters


def _filters_from_entry(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    filters: dict[str, Any] = {}
    for key in ("tool_prefix", "strip_prefix"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            filters[key] = value
    for key in ("include_prefixes", "exclude_prefixes"):
        value = entry.get(key)
        if isinstance(value, list) and value:
            filters[key] = [str(item) for item in value]
        elif isinstance(value, str) and value:
            filters[key] = _split_tools(value)
    return filters or None


def _adopt_mismatch(
    entry: Mapping[str, Any],
    url: str,
    *,
    tool_prefix: str | None,
    include_prefixes: str | None,
    exclude_prefixes: str | None,
    strip_prefix: str | None,
) -> str | None:
    if str(entry.get("url") or "") != url:
        return "The --url flag does not match the registry entry."
    given = _filters_from_flags(
        tool_prefix=tool_prefix,
        include_prefixes=include_prefixes,
        exclude_prefixes=exclude_prefixes,
        strip_prefix=strip_prefix,
    )
    if not given:
        return None
    current = _filters_from_entry(entry) or {}
    if given != current:
        return "The filter flags do not match the registry entry."
    return None


def _auth_names(namespace: str) -> tuple[str, str]:
    return bearer_env_name(namespace), identity_env_name(namespace)


def _registry_record(
    name: str,
    url: str,
    namespace: str,
    auth_names: tuple[str, str] | None,
    filters: Mapping[str, Any],
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "url": url,
        "namespace": namespace,
        "enabled": True,
    }
    if auth_names:
        entry["auth"] = {"bearer_env": auth_names[0], "identity_secret_env": auth_names[1]}
    entry.update(dict(filters))
    return entry


def _ensure_secrets(
    root: Path, auth_names: tuple[str, str], secrets: SecretRegistry
) -> tuple[str, str]:
    current = _managed_values(root)
    values: list[str] = []
    for name in auth_names:
        existing = current.get(name, "")
        if existing:
            secrets.register(existing)
            values.append(existing)
        else:
            generated = token_urlsafe(32)
            secrets.register(generated)
            values.append(generated)
    return values[0], values[1]


def _managed_values(root: Path) -> dict[str, str]:
    path = root / ".env"
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    start = text.find(_ENV_BLOCK_START)
    end = text.find(_ENV_BLOCK_END, start + 1) if start >= 0 else -1
    if start < 0 or end < 0:
        return {}
    found: dict[str, str] = {}
    block = text[start + len(_ENV_BLOCK_START) : end]
    for line in block.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key] = value
    return found


def _write_env(
    root: Path,
    auth_names: tuple[str, str],
    bearer: str,
    identity: str,
    url: str,
) -> int:
    path = root / ".env"
    existing = _managed_values(root)
    before = path.read_text(encoding="utf-8") if path.is_file() else ""
    values = dict(existing)
    values[auth_names[0]] = bearer
    values[auth_names[1]] = identity
    if url.startswith("http://"):
        values["KEYSTONE_MCP_UPSTREAM_ALLOW_INSECURE_HTTP"] = "true"
    rendered = replace_managed_env_block(before, values)
    if rendered == before:
        return 0
    atomic_write(path, rendered.encode("utf-8"), mode=0o600)
    return 1


def _write_handoff(
    root: Path,
    name: str,
    auth_names: tuple[str, str],
    bearer: str,
    identity: str,
) -> int:
    path = root / "packages" / f"{name}.env"
    text = (
        f"# audience (aud) = {name}\n"
        f"{auth_names[0]}={quote_env(bearer)}\n"
        f"{auth_names[1]}={quote_env(identity)}\n"
    )
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return 0
    atomic_write(path, text.encode("utf-8"), mode=0o600)
    return 1


def _write_registry(root: Path, entries: Sequence[Mapping[str, Any]]) -> int:
    path = root / "keystonectl" / "upstreams.yaml"
    rendered = render_registry(entries)
    if path.is_file() and path.read_text(encoding="utf-8") == rendered:
        return 0
    write_backup(path)
    atomic_write(path, rendered.encode("utf-8"), mode=0o644)
    return 1


def _write_snapshot(root: Path, namespace: str, tools: list[str]) -> bool:
    path = root / "keystonectl" / "catalog-snapshot.yaml"
    current = _load_yaml(path)
    document = current if isinstance(current, dict) else {}
    published = tool_names(namespace, tools)
    merged = merge_snapshot(
        document,
        services=[{"namespace": namespace, "tools": tools}],
        previous_owned=set(),
    )
    # merge_snapshot adds namespace_tool from tools. Also keep names already active.
    rendered = render_snapshot(merged)
    if path.is_file() and path.read_text(encoding="utf-8") == rendered:
        return False
    write_backup(path)
    atomic_write(path, rendered.encode("utf-8"), mode=0o644)
    del published
    return True


def _save_remote_lock(
    root: Path,
    lock: dict[str, Any],
    *,
    name: str,
    namespace: str,
    url: str,
    filters: dict[str, Any] | None,
    tools: list[str],
    auth_names: list[str],
    handoff: bool,
    now: datetime,
) -> None:
    services = [item for item in lock.get("services") or [] if isinstance(item, dict)]
    previous = next((item for item in services if item.get("name") == name), None)
    installed = previous.get("installed_at") if isinstance(previous, dict) else None
    entry = {
        "name": name,
        "role": "service",
        "managed": "remote",
        "registry_name": name,
        "namespace": namespace,
        "url": url,
        "filters": filters,
        "tools": tools,
        "env": {
            "keystone": auth_names,
            "files": [_handoff_rel(name)] if handoff else [],
            "generated": [],
        },
        "installed_at": installed or utc_now(now),
    }
    replaced = False
    updated: list[dict[str, Any]] = []
    for item in services:
        if item.get("name") == name:
            updated.append(entry)
            replaced = True
        else:
            updated.append(item)
    if not replaced:
        updated.append(entry)
    lock["services"] = updated
    lock["updated_by"] = f"keystonectl {__version__}"
    save_lock(root / "services.lock.yaml", lock, now=now)


def _validate_candidates(
    root: Path,
    lock: Mapping[str, Any],
    runner: CommandRunner,
    entries: Sequence[Mapping[str, Any]],
) -> Result | None:
    layout = lock.get("layout") if isinstance(lock.get("layout"), dict) else {}
    if not layout or not layout.get("keystone_image"):
        return None
    version = run(runner, ["docker", "compose", "version", "--short"], cwd=root)
    if daemon_down(version):
        return _fail(
            "service add-remote", "", "docker_unavailable", ERRORS["docker_unavailable"].title
        )
    if version.returncode != 0 or compose_is_old(version.stdout):
        return _fail("service add-remote", "", "compose_too_old", ERRORS["compose_too_old"].title)
    config_result = compose_config(runner, root, layout)
    if daemon_down(config_result):
        return _fail(
            "service add-remote", "", "docker_unavailable", ERRORS["docker_unavailable"].title
        )
    config = parse_config(config_result)
    if config is None:
        return _fail("service add-remote", "", "layout_invalid", ERRORS["layout_invalid"].title)
    problems = layout_problems(config, layout)
    if problems:
        return _fail("service add-remote", "", "layout_invalid", "; ".join(problems))
    image = str(layout.get("keystone_image") or "")
    available, down = image_ready(runner, image)
    if down:
        return _fail(
            "service add-remote", "", "docker_unavailable", ERRORS["docker_unavailable"].title
        )
    if not available:
        return _fail(
            "service add-remote",
            "",
            "keystone_mcp_image_unavailable",
            ERRORS["keystone_mcp_image_unavailable"].title,
        )
    staging = root / ".keystonectl" / "staging" / "remote"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "upstreams.yaml").write_text(render_registry(entries), encoding="utf-8")
    allow = (
        "true"
        if any(str(item.get("url") or "").startswith("http://") for item in entries)
        else "false"
    )
    validated = run(
        runner,
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-e",
            f"KEYSTONE_MCP_UPSTREAM_ALLOW_INSECURE_HTTP={allow}",
            "-v",
            f"{staging}:/ksctl:ro",
            "--entrypoint",
            "keystone-mcp",
            image,
            "upstream",
            "validate",
            "--path",
            "/ksctl/upstreams.yaml",
        ],
    )
    if validated.returncode != 0:
        if daemon_down(validated):
            return _fail(
                "service add-remote", "", "docker_unavailable", ERRORS["docker_unavailable"].title
            )
        text = (validated.stderr or validated.stdout).strip()
        return _fail(
            "service add-remote", "", "registry_invalid", text or ERRORS["registry_invalid"].title
        )
    return None


def _probe(
    root: Path, lock: Mapping[str, Any], runner: CommandRunner, url: str
) -> tuple[bool, dict[str, str] | None]:
    layout = lock.get("layout") if isinstance(lock.get("layout"), dict) else {}
    image = str((layout or {}).get("keystone_image") or "")
    warning = {
        "id": "remote_unreachable",
        "message": WARNINGS["remote_unreachable"].action,
    }
    if not image:
        return False, warning
    probed = run(
        runner,
        [
            "docker",
            "run",
            "--rm",
            "--network",
            UPSTREAMS_NETWORK,
            "--entrypoint",
            "python",
            image,
            "-c",
            _PROBE,
            url,
        ],
    )
    if probed.returncode == 0:
        return True, None
    return False, warning


def _keystone_running(root: Path, lock: Mapping[str, Any], runner: CommandRunner) -> bool:
    layout = lock.get("layout") if isinstance(lock.get("layout"), dict) else None
    if not layout:
        return False
    service = str(layout.get("keystone_service") or "")
    project = str(layout.get("project") or "")
    result = run(
        runner,
        [
            *compose_prefix(root, layout, project=project or None),
            "ps",
            "--status",
            "running",
            "-q",
            service,
        ],
        cwd=root,
    )
    return bool(result.stdout.strip())


def _restart(root: Path, lock: Mapping[str, Any], runner: CommandRunner) -> None:
    layout = lock.get("layout")
    if not isinstance(layout, dict):
        return
    service = str(layout.get("keystone_service") or "")
    project = str(layout.get("project") or "")
    run(
        runner,
        [*compose_prefix(root, layout, project=project or None), "restart", service],
        cwd=root,
    )


def _lock_entry(lock: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    for item in lock.get("services") or []:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def _registry_entries(root: Path) -> list[dict[str, Any]]:
    document = _load_yaml(root / "keystonectl" / "upstreams.yaml")
    if not isinstance(document, dict):
        return []
    return [item for item in document.get("entries") or [] if isinstance(item, dict)]


def _registry_entry(entries: Sequence[Mapping[str, Any]], name: str) -> dict[str, Any] | None:
    for item in entries:
        if item.get("name") == name:
            return dict(item)
    return None


def _load_yaml(path: Path) -> Any:
    if not path.is_file():
        return None
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def _handoff_rel(name: str) -> str:
    return f"packages/{name}.env"


def _handoff_listed(entry: Mapping[str, Any]) -> bool:
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    return bool(env.get("files"))


def _result_body(
    name: str,
    namespace: str,
    url: str,
    handoff: str | None,
    reachable: bool,
    *,
    note: str = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "registry_name": name,
        "namespace": namespace,
        "url": url,
        "reachable": reachable,
    }
    if handoff:
        body["handoff_file"] = handoff
    if note:
        body["note"] = note
    return body


def _fail(
    command: str, target: str, error_id: str, message: str, *, hint: str | None = None
) -> Result:
    spec = ERRORS[error_id]
    return Result(
        code=spec.code,
        command=command,
        summary=message or spec.title,
        target=target or None,
        error={"id": spec.id, "message": message or spec.title, "hint": hint or spec.action},
        retryable=spec.retryable,
    )
