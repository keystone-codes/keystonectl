"""Install a package: fragment, secrets, registry, snapshot, then start it."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from keystonectl import __version__
from keystonectl.answers import Question, pkg_var
from keystonectl.envfiles import read_env, upsert_env
from keystonectl.errors import ERRORS, fragment_modified_message
from keystonectl.generate.compose import (
    fragment_digest,
    fragment_guard,
    render_fragment,
    service_names,
)
from keystonectl.generate.docker import (
    METRICS_CODE,
    PROBE_CODE,
    RESOURCES_NETWORK,
    UPSTREAMS_NETWORK,
    compose_config,
    compose_prefix,
    daemon_down,
    ensure_network,
    facade_probe_url,
    image_ready,
    layout_problems,
    parse_config,
    policy_host_path,
    run,
)
from keystonectl.generate.files import atomic_write, write_backup
from keystonectl.generate.namespace import env_name_reason, namespace_reason
from keystonectl.generate.packages_file import (
    compose_is_old,
    override_is_present,
    render_packages_file,
)
from keystonectl.generate.placement import mcp_role, package_roles, placement
from keystonectl.generate.registry import (
    bearer_env_name,
    identity_env_name,
    merge_registry_entries,
    merge_snapshot,
    owned_tool_names,
    registry_entry,
    registry_url,
    render_registry,
    render_snapshot,
)
from keystonectl.generate.tokens import token_urlsafe
from keystonectl.generate.validators import (
    discovery_unavailable,
    is_namespace_error,
    metric_available,
    unmatched_patterns,
    unreachable_tools,
)
from keystonectl.keystone_mcp_rules import MAX_REGISTRY_ENTRIES
from keystonectl.layout import (
    LayoutError,
    env_assigned_outside_managed_block,
    managed_env_values,
    replace_managed_env_block,
)
from keystonectl.lock import save_lock, utc_now
from keystonectl.plan import Context, StepPlan
from keystonectl.resources.network import ip_for_service
from keystonectl.sources import Resolution, package_cache, warning

REGISTRY_SIGHUP_MIN_VERSION = None
_POLICY_LIMIT = 64 * 1024


def pause(seconds: float) -> None:
    time.sleep(seconds)


def _monotonic() -> float:
    return time.monotonic()


class _Undo:
    def __init__(self) -> None:
        self._saved: dict[Path, bytes | None] = {}

    def remember(self, path: Path) -> None:
        if path not in self._saved:
            self._saved[path] = path.read_bytes() if path.is_file() else None

    def restore(self) -> None:
        for path, data in self._saved.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)


def _fail(
    ctx: Context,
    error_id: str,
    message: str,
    *,
    hint: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    spec = ERRORS[error_id]
    ctx.failure = Resolution(
        error_id=error_id,
        message=ctx.secrets.redact(message),
        hint=hint or spec.action,
        result=result or {},
    )


def _layout(ctx: Context) -> dict[str, Any]:
    layout = ctx.lock.get("layout")
    return layout if isinstance(layout, dict) else {}


def _service(lock: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    for item in lock.get("services") or []:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def _load_yaml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def _entries(document: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not document:
        return []
    raw = document.get("entries")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _manifest_of(ctx: Context) -> dict[str, Any] | None:
    if isinstance(ctx.manifest, dict):
        return ctx.manifest
    package = ctx.resolved
    if package is None:
        return None
    dest = package_cache(ctx.root, package.source_name, package.name, package.version)
    path = (
        dest / "keystone-service.yaml"
        if package.path == "."
        else dest / package.path / "keystone-service.yaml"
    )
    if not path.is_file():
        return None
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        ctx.manifest = loaded
        return loaded
    return None


def _lookup_text(ctx: Context, package: str, var: str) -> str | None:
    if var in ctx.sets:
        return ctx.sets[var]
    if ctx.environ.get(var):
        return str(ctx.environ[var])
    current = _service(ctx.lock, package)
    answers = current.get("answers") if isinstance(current, dict) else None
    if isinstance(answers, dict) and answers.get(var) not in {None, ""}:
        return str(answers[var])
    return None


def _lookup_secret(ctx: Context, package: str, var: str) -> str | None:
    from keystonectl.resources.binding import _load_secret

    raw = _lookup_text(ctx, package, var)
    if raw is None:
        return None
    return _load_secret(Path(raw), ctx.secrets)


def _remember(ctx: Context, package: str, var: str, value: str) -> None:
    current = _service(ctx.lock, package)
    if current is None:
        return
    answers = current.setdefault("answers", {})
    if isinstance(answers, dict):
        answers[var] = value


def _env_questions(ctx: Context, manifest: Mapping[str, Any]) -> list[Question]:
    package = str(manifest["name"])
    questions: list[Question] = []
    containers = manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
    seen: set[str] = set()
    for spec in containers.values():
        if not isinstance(spec, dict):
            continue
        for item in spec.get("env") or []:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            if "default" in item and item.get("default") is not None:
                continue
            var = pkg_var(package, f"ENV_{item['name']}")
            if var in seen:
                continue
            seen.add(var)
            if _lookup_text(ctx, package, var) is None:
                questions.append(Question(var, f"Value of {item['name']} for {package}.", "text"))
        for item in spec.get("secrets") or []:
            if not isinstance(item, dict) or not item.get("required"):
                continue
            if not isinstance(item.get("name"), str):
                continue
            var = pkg_var(package, f"SECRET_{item['name']}_FILE")
            if var in seen:
                continue
            seen.add(var)
            if _lookup_text(ctx, package, var) is None:
                questions.append(
                    Question(
                        var,
                        f"Path to a mode 600 file holding secret {item['name']} for {package}.",
                        "file",
                        secret=True,
                    )
                )
    return questions


def _namespace_choice(
    ctx: Context, manifest: Mapping[str, Any], entries: list[dict[str, Any]]
) -> str | None:
    if mcp_role(manifest) is None:
        return None
    package = str(manifest["name"])
    current = _service(ctx.lock, package)
    locked = current.get("namespace") if isinstance(current, dict) else None
    var = pkg_var(package, "NAMESPACE")
    answered = _lookup_text(ctx, package, var)
    if isinstance(locked, str) and locked:
        if answered and answered != locked:
            _fail(
                ctx,
                "namespace_change_unsupported",
                ERRORS["namespace_change_unsupported"].title,
                hint="Run `keystonectl service remove`, then install the service again.",
            )
            return None
        return locked
    candidate = answered or str((manifest.get("mcp") or {}).get("namespace") or "")
    others = [
        str(item["namespace"])
        for item in entries
        if isinstance(item.get("namespace"), str) and item.get("name") != package
    ]
    reason = namespace_reason(candidate, others) or env_name_reason(
        candidate, [item for item in entries if item.get("name") != package]
    )
    if reason is None and candidate:
        return candidate
    ctx.halt_questions.append(
        Question(
            var,
            (
                f"The tool prefix '{candidate}' cannot be used: {reason}. "
                f"Choose another prefix for {package}'s tools (letters, digits, '-' or '_')."
            ),
            "text",
        )
    )
    return None


def _env_values(ctx: Context, manifest: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    package = str(manifest["name"])
    containers = manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
    found: dict[str, dict[str, str]] = {}
    for role, spec in containers.items():
        if not isinstance(spec, dict):
            continue
        values: dict[str, str] = {}
        for item in spec.get("env") or []:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            key = str(item["name"])
            if "default" in item and item.get("default") is not None:
                values[key] = str(item["default"])
            answered = _lookup_text(ctx, package, pkg_var(package, f"ENV_{key}"))
            if answered is not None:
                values[key] = answered
                _remember(ctx, package, pkg_var(package, f"ENV_{key}"), answered)
        if values:
            found[str(role)] = values
    return found


def _seconds(value: object, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.endswith("s") and value[:-1].isdigit():
        return int(value[:-1])
    return default


def _budget(manifest: Mapping[str, Any]) -> float:
    best = 0
    containers = manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
    for spec in containers.values():
        if not isinstance(spec, dict) or spec.get("init"):
            continue
        health = spec.get("healthcheck") if isinstance(spec.get("healthcheck"), dict) else {}
        start = _seconds(health.get("start_period"), 20)
        interval = _seconds(health.get("interval"), 10)
        retries = int(health.get("retries") or 5)
        best = max(best, start + interval * retries)
    return float(best + 60)


def _write_env_values(path: Path, values: Mapping[str, str], undo: _Undo) -> None:
    undo.remember(path)
    if values:
        upsert_env(path, values)
    elif not path.exists():
        upsert_env(path, {})


def _env_state_path(ctx: Context) -> Path:
    return ctx.root / ".keystonectl" / "env-applied.json"


def _env_digests(ctx: Context, manifest: Mapping[str, Any]) -> dict[str, str]:
    plan = placement(manifest)
    found: dict[str, str] = {}
    for relative in plan.files:
        path = ctx.root / relative
        data = path.read_bytes() if path.is_file() else b""
        found[relative] = hashlib.sha256(data).hexdigest()
    return found


def _load_env_state(ctx: Context) -> dict[str, dict[str, str]]:
    path = _env_state_path(ctx)
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    clean: dict[str, dict[str, str]] = {}
    for name, item in loaded.items():
        if not isinstance(name, str) or not isinstance(item, dict):
            continue
        clean[name] = {
            str(key): str(value)
            for key, value in item.items()
            if isinstance(key, str) and isinstance(value, str)
        }
    return clean


def _save_env_state(ctx: Context, manifest: Mapping[str, Any]) -> None:
    state = _load_env_state(ctx)
    state[str(manifest["name"])] = _env_digests(ctx, manifest)
    atomic_write(
        _env_state_path(ctx),
        (json.dumps(state, sort_keys=True) + "\n").encode("utf-8"),
        mode=0o600,
    )


def _generated_in_lock(ctx: Context, name: str) -> list[str]:
    entry = _service(ctx.lock, name)
    if not isinstance(entry, dict) or not isinstance(entry.get("env"), dict):
        return []
    return [str(item) for item in entry["env"].get("generated") or [] if isinstance(item, str)]


def _shared_secret_drops(ctx: Context, manifest: Mapping[str, Any]) -> list[str]:
    """Keys this version no longer places in the shared env file."""

    plan = placement(manifest)
    current = read_env(ctx.root / plan.shared)
    if not current:
        return []
    placed_shared = {item.name for item in plan.secrets if item.file == plan.shared}
    drop: list[str] = []
    for item in plan.secrets:
        if item.file != plan.shared and item.name in current:
            drop.append(item.name)
    for name in _generated_in_lock(ctx, str(manifest["name"])):
        if name not in placed_shared and name in current and name not in drop:
            drop.append(name)
    return drop


def _env_matches(ctx: Context, manifest: Mapping[str, Any]) -> bool:
    if _shared_secret_drops(ctx, manifest):
        return False
    saved = _load_env_state(ctx).get(str(manifest["name"]))
    if not isinstance(saved, dict) or not saved:
        return False
    return saved == _env_digests(ctx, manifest)


def _retire_shared_secrets(ctx: Context, manifest: Mapping[str, Any], undo: _Undo) -> None:
    """Move a narrowed secret out of the shared file, then delete the old key.

    Every container receives the shared file. A name this version does not
    place there must not stay, or a container that never declared it still
    sees the value. The existing value is kept for the roles that still
    declare the name.
    """

    plan = placement(manifest)
    shared_path = ctx.root / plan.shared
    current = read_env(shared_path)
    if not current:
        return
    for item in plan.secrets:
        if item.file == plan.shared or item.name not in current:
            continue
        target = ctx.root / item.file
        if item.name not in read_env(target):
            ctx.secrets.register(current[item.name])
            _write_env_values(target, {item.name: current[item.name]}, undo)
    drop = _shared_secret_drops(ctx, manifest)
    if not drop:
        return
    for name in drop:
        if name in current:
            ctx.secrets.register(current[name])
    undo.remember(shared_path)
    upsert_env(shared_path, {}, remove=drop)


def _fill_secrets(
    ctx: Context,
    manifest: Mapping[str, Any],
    undo: _Undo,
) -> None:
    package = str(manifest["name"])
    plan = placement(manifest)
    for secret in plan.secrets:
        path = ctx.root / secret.file
        current = read_env(path)
        if current.get(secret.name):
            ctx.secrets.register(current[secret.name])
            continue
        if secret.generate_bytes:
            value = token_urlsafe(secret.generate_bytes)
        elif secret.required:
            var = pkg_var(package, f"SECRET_{secret.name}_FILE")
            loaded = _lookup_secret(ctx, package, var)
            if loaded is None:
                continue
            value = loaded
            raw = _lookup_text(ctx, package, var)
            if raw:
                _remember(ctx, package, var, raw)
        else:
            continue
        ctx.secrets.register(value)
        _write_env_values(path, {secret.name: value}, undo)


def _gateway_secrets(
    ctx: Context,
    manifest: Mapping[str, Any],
    namespace: str,
    registry_name: str,
    undo: _Undo,
) -> dict[str, str]:
    """Write the facade file and return the keystone-mcp ``.env`` names."""

    plan = placement(manifest)
    if plan.mcp is None:
        return {}
    mcp = manifest["mcp"]
    identity = manifest.get("identity") if isinstance(manifest.get("identity"), dict) else {}
    bearer_key = str(mcp["bearer_env"])
    secret_key = str(identity.get("secret_env") or "")
    audience_key = str(identity.get("audience_env") or "")
    mcp_path = ctx.root / plan.mcp
    root_env = ctx.root / ".env"
    bearer_name = bearer_env_name(namespace)
    identity_name = identity_env_name(namespace)
    bearer = _shared_secret(ctx, [(mcp_path, bearer_key), (root_env, bearer_name)])
    identity_value = _shared_secret(ctx, [(mcp_path, secret_key), (root_env, identity_name)])
    facade: dict[str, str] = {}
    if bearer_key:
        facade[bearer_key] = bearer
    if secret_key:
        facade[secret_key] = identity_value
    if audience_key:
        facade[audience_key] = registry_name
    _write_env_values(mcp_path, facade, undo)
    return {bearer_name: bearer, identity_name: identity_value}


def _shared_secret(ctx: Context, slots: list[tuple[Path, str]]) -> str:
    for path, key in slots:
        if not key:
            continue
        current = read_env(path).get(key, "")
        if current:
            ctx.secrets.register(current)
            return current
    value = token_urlsafe(32)
    ctx.secrets.register(value)
    return value


def _write_root_env(ctx: Context, values: Mapping[str, str], undo: _Undo) -> None:
    path = ctx.root / ".env"
    undo.remember(path)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    merged = dict(managed_env_values(existing))
    merged.update(values)
    try:
        rendered = replace_managed_env_block(existing, merged)
    except (ValueError, OSError, LayoutError) as exc:
        code = getattr(exc, "code", "env_name_conflict")
        _fail(ctx, code if code in ERRORS else "env_name_conflict", str(exc))
        return
    atomic_write(path, rendered.encode("utf-8"), mode=0o600)


def _allow_insecure(ctx: Context, entries: list[dict[str, Any]]) -> str:
    urls = [str(item.get("url") or "") for item in entries]
    needed = any(url.startswith("http://") for url in urls)
    current = read_env(ctx.root / ".env").get("KEYSTONE_MCP_UPSTREAM_ALLOW_INSECURE_HTTP", "")
    if current.lower() == "true" or needed:
        return "true"
    return current or "false"


def _egress(ctx: Context) -> str:
    current = read_env(ctx.root / ".env").get("KEYSTONE_MCP_UPSTREAM_EGRESS_ALLOW_PRIVATE", "")
    return current or "false"


def _image(ctx: Context) -> str:
    return str(_layout(ctx).get("keystone_image") or "")


def _local_build(ctx: Context) -> bool:
    return bool(_layout(ctx).get("keystone_build"))


def _staging(ctx: Context) -> Path:
    stamp = ctx.clock().strftime("%Y%m%dT%H%M%SZ")
    path = ctx.root / ".keystonectl" / "staging" / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_staging(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    parent = path.parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()


def _validate(
    ctx: Context, manifest: Mapping[str, Any], namespace: str, registry_name: str
) -> bool:
    layout = _layout(ctx)
    if not layout.get("keystone_service") or not layout.get("compose_files"):
        _fail(ctx, "layout_invalid", ERRORS["layout_invalid"].title)
        return False
    version = run(ctx.runner, ["docker", "compose", "version", "--short"], cwd=ctx.root)
    if daemon_down(version):
        _fail(ctx, "docker_unavailable", ERRORS["docker_unavailable"].title)
        return False
    if version.returncode != 0 or compose_is_old(version.stdout):
        _fail(ctx, "compose_too_old", ERRORS["compose_too_old"].title)
        return False
    config_result = compose_config(ctx.runner, ctx.root, layout)
    if daemon_down(config_result):
        _fail(ctx, "docker_unavailable", ERRORS["docker_unavailable"].title)
        return False
    config = parse_config(config_result)
    if config is None:
        _fail(
            ctx,
            "layout_invalid",
            "docker compose config failed.\n" + ERRORS["layout_invalid"].action,
        )
        return False
    problems = layout_problems(config, layout)
    if problems:
        _fail(ctx, "layout_invalid", "; ".join(problems))
        return False
    image = _image(ctx)
    if not image:
        if _local_build(ctx):
            ctx.extra_warnings.append(warning("validation_skipped_local", WARNINGS_LOCAL))
            return True
        _fail(ctx, "keystone_mcp_image_unavailable", ERRORS["keystone_mcp_image_unavailable"].title)
        return False
    available, down = image_ready(ctx.runner, image)
    if down:
        _fail(ctx, "docker_unavailable", ERRORS["docker_unavailable"].title)
        return False
    if not available:
        if _local_build(ctx):
            ctx.extra_warnings.append(warning("validation_skipped_local", WARNINGS_LOCAL))
            return True
        _fail(ctx, "keystone_mcp_image_unavailable", ERRORS["keystone_mcp_image_unavailable"].title)
        return False
    if mcp_role(manifest) is None:
        return True
    return _validate_mcp(ctx, manifest, namespace, registry_name, image, config)


WARNINGS_LOCAL = "Upstream and policy checks were skipped because the local image is not built."


def _validate_mcp(
    ctx: Context,
    manifest: Mapping[str, Any],
    namespace: str,
    registry_name: str,
    image: str,
    config: Mapping[str, Any],
) -> bool:
    staging = _staging(ctx)
    try:
        current = _load_yaml(ctx.root / "keystonectl" / "upstreams.yaml")
        entries = _entries(current)
        entry = registry_entry(manifest, namespace=namespace, registry_name=registry_name)
        merged = merge_registry_entries(entries, entry)
        (staging / "upstreams.yaml").write_text(render_registry(merged), encoding="utf-8")
        allow = _allow_insecure(ctx, merged)
        egress = _egress(ctx)
        validated = run(
            ctx.runner,
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-e",
                f"KEYSTONE_MCP_UPSTREAM_ALLOW_INSECURE_HTTP={allow}",
                "-e",
                f"KEYSTONE_MCP_UPSTREAM_EGRESS_ALLOW_PRIVATE={egress}",
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
        if validated.returncode == 1:
            text = f"{validated.stderr}\n{validated.stdout}"
            if is_namespace_error(text):
                ctx.halt_questions.append(
                    Question(
                        pkg_var(str(manifest["name"]), "NAMESPACE"),
                        (
                            f"The tool prefix '{namespace}' cannot be used: {text.strip()}. "
                            f"Choose another prefix for {manifest['name']}'s tools "
                            "(letters, digits, '-' or '_')."
                        ),
                        "text",
                    )
                )
                return False
            _fail(
                ctx,
                "registry_invalid",
                ERRORS["registry_invalid"].title,
                result={"problems": [line for line in text.splitlines() if line.strip()]},
            )
            return False
        if validated.returncode != 0:
            if daemon_down(validated):
                _fail(ctx, "docker_unavailable", ERRORS["docker_unavailable"].title)
            else:
                _fail(ctx, "registry_invalid", validated.stderr.strip() or validated.stdout.strip())
            return False
        return _validate_policy(ctx, manifest, namespace, image, config, staging)
    finally:
        _cleanup_staging(staging)


def _live_names(ctx: Context, namespace: str) -> tuple[list[str], str | None]:
    layout = _layout(ctx)
    service = str(layout.get("keystone_service") or "")
    result = run(
        ctx.runner,
        [
            *compose_prefix(ctx.root, layout),
            "exec",
            "-T",
            service,
            "python",
            "-m",
            "keystone_mcp.rbac.catalog",
        ],
        cwd=ctx.root,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        problem = ctx.secrets.redact(detail[0] if detail else "command failed")
        if len(problem) > 200:
            problem = problem[:200]
        return [], problem
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    prefix = f"{namespace}_"
    return [name for name in names if not name.startswith(prefix)], None


def _validate_policy(
    ctx: Context,
    manifest: Mapping[str, Any],
    namespace: str,
    image: str,
    config: Mapping[str, Any],
    staging: Path,
) -> bool:
    mode = read_env(ctx.root / ".env").get("KEYSTONE_MCP_RBAC_MODE", "")
    policy = policy_host_path(config, _layout(ctx), ctx.root)
    if mode == "off" or policy is None or not policy.is_file():
        ctx.extra_warnings.append(warning("policy_check_skipped", "The policy check was skipped."))
        return True
    layout = _layout(ctx)
    project = str(layout.get("project") or "")
    live, problem = _live_names(ctx, namespace)
    if problem is not None and _keystone_running(ctx, project):
        ctx.extra_warnings.append(
            warning(
                "builtin_tools_unavailable",
                f"Could not read the tools keystone-mcp publishes now ({problem}). "
                "The policy check ran without them.",
            )
        )
    (staging / "live.txt").write_text("".join(f"{name}\n" for name in live), encoding="utf-8")
    current = _load_yaml(ctx.root / "keystonectl" / "catalog-snapshot.yaml")
    services = [item for item in (ctx.lock.get("services") or []) if isinstance(item, dict)]
    previous = owned_tool_names(services)
    tools = manifest.get("mcp", {}).get("tools") or []
    projected = [dict(item) for item in services if item.get("name") != manifest["name"]]
    projected.append(
        {
            "name": manifest["name"],
            "namespace": namespace,
            "tools": list(tools),
        }
    )
    candidate = merge_snapshot(current, services=projected, previous_owned=previous)
    base = current or {"active": [], "reserved": [], "wip": []}
    (staging / "catalog-snapshot.yaml").write_text(render_snapshot(base), encoding="utf-8")
    base_run = _catalog_validate(ctx, image, policy, staging)
    (staging / "catalog-snapshot.yaml").write_text(render_snapshot(candidate), encoding="utf-8")
    next_run = _catalog_validate(ctx, image, policy, staging)
    if next_run is None or base_run is None:
        return False
    _note_unreachable(ctx, next_run.stderr + "\n" + next_run.stdout)
    if next_run.returncode == 0:
        return True
    new_patterns = unmatched_patterns(next_run.stderr + "\n" + next_run.stdout)
    old_patterns = unmatched_patterns(base_run.stderr + "\n" + base_run.stdout)
    if (
        next_run.returncode == 1
        and base_run.returncode == 1
        and set(new_patterns) == set(old_patterns)
        and new_patterns
    ):
        ctx.extra_warnings.append(
            warning(
                "policy_already_inconsistent",
                "The policy already has the same unmatched allow patterns.",
            )
        )
        return True
    if new_patterns:
        _fail(
            ctx,
            "policy_breaks",
            "New unmatched allow patterns: " + ", ".join(new_patterns),
            result={"problems": new_patterns},
        )
        return False
    text = (next_run.stderr or next_run.stdout).strip()
    _fail(ctx, "policy_breaks", text or ERRORS["policy_breaks"].title)
    return False


def _catalog_validate(ctx: Context, image: str, policy: Path, staging: Path) -> Any:
    result = run(
        ctx.runner,
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{policy}:/ksctl-policy/policy.yaml:ro",
            "-v",
            f"{staging}:/ksctl:ro",
            "--entrypoint",
            "keystone-mcp",
            image,
            "catalog",
            "validate",
            "--policy",
            "/ksctl-policy/policy.yaml",
            "--snapshot",
            "/ksctl/catalog-snapshot.yaml",
            "--live-file",
            "/ksctl/live.txt",
        ],
    )
    if daemon_down(result):
        _fail(ctx, "docker_unavailable", ERRORS["docker_unavailable"].title)
        return None
    return result


def _note_unreachable(ctx: Context, text: str) -> None:
    if unreachable_tools(text) or "rbac policy leaves" in text:
        ctx.extra_warnings.append(
            warning(
                "tools_unreachable",
                "No role can call these tools yet. Ask an administrator to add policy rows "
                "(see the recommended policy below).",
            )
        )


def _packages_text(ctx: Context, name: str) -> str:
    layout = _layout(ctx)
    names: dict[str, bool] = {}
    for item in ctx.lock.get("services") or []:
        if not isinstance(item, dict) or item.get("managed") == "remote":
            continue
        if item.get("fragment") or item.get("name") == name:
            names[str(item["name"])] = override_is_present(
                ctx.root / "packages" / f"{item['name']}.override.yml"
            )
    names[name] = override_is_present(ctx.root / "packages" / f"{name}.override.yml")
    network = (
        layout.get("upstreams_network") if isinstance(layout.get("upstreams_network"), dict) else {}
    )
    return render_packages_file(
        keystone_service=str(layout.get("keystone_service") or "keystone"),
        packages=[(item, flag) for item, flag in names.items()],
        attach_network=network.get("attached_by") != "operator",
        include_default_network=bool(network.get("include_default_network")),
    )


def _unchanged(
    ctx: Context, manifest: Mapping[str, Any], fragment: str, namespace: str | None
) -> bool:
    name = str(manifest["name"])
    path = ctx.root / "packages" / f"{name}.yml"
    if not path.is_file() or path.read_text(encoding="utf-8") != fragment:
        return False
    if not _env_matches(ctx, manifest):
        return False
    if (
        (ctx.root / "docker-compose.packages.yml").read_text(encoding="utf-8")
        != _packages_text(ctx, name)
        if (ctx.root / "docker-compose.packages.yml").is_file()
        else True
    ):
        if not (ctx.root / "docker-compose.packages.yml").is_file():
            return False
        if (ctx.root / "docker-compose.packages.yml").read_text(encoding="utf-8") != _packages_text(
            ctx, name
        ):
            return False
    if namespace is None:
        return True
    document = _load_yaml(ctx.root / "keystonectl" / "upstreams.yaml")
    wanted = registry_entry(
        manifest, namespace=namespace, registry_name=_registry_name(ctx, manifest)
    )
    current = next(
        (item for item in _entries(document) if item.get("name") == wanted["name"]), None
    )
    if current is None or any(current.get(key) != wanted[key] for key in wanted):
        return False
    snapshot = _load_yaml(ctx.root / "keystonectl" / "catalog-snapshot.yaml")
    services = [item for item in (ctx.lock.get("services") or []) if isinstance(item, dict)]
    merged = merge_snapshot(
        snapshot,
        services=_project_services(services, manifest, namespace),
        previous_owned=owned_tool_names(services),
    )
    if snapshot is None:
        return False
    return (
        list(snapshot.get("active") or []) == merged["active"]
        and list(snapshot.get("reserved") or []) == merged["reserved"]
        and list(snapshot.get("wip") or []) == merged["wip"]
    )


def _registry_name(ctx: Context, manifest: Mapping[str, Any]) -> str:
    current = _service(ctx.lock, str(manifest["name"]))
    if (
        isinstance(current, dict)
        and isinstance(current.get("registry_name"), str)
        and current["registry_name"]
    ):
        return str(current["registry_name"])
    return str(manifest["name"])


def _project_services(
    services: list[dict[str, Any]], manifest: Mapping[str, Any], namespace: str
) -> list[dict[str, Any]]:
    tools = list((manifest.get("mcp") or {}).get("tools") or [])
    projected = [dict(item) for item in services if item.get("name") != manifest["name"]]
    projected.append({"name": manifest["name"], "namespace": namespace, "tools": tools})
    return projected


def _taken(ctx: Context, registry_name: str, entries: list[dict[str, Any]]) -> bool:
    package = str(ctx.manifest["name"]) if isinstance(ctx.manifest, dict) else ""
    for item in ctx.lock.get("services") or []:
        if (
            isinstance(item, dict)
            and item.get("name") != package
            and item.get("registry_name") == registry_name
        ):
            return True
    ours = _service(ctx.lock, package)
    owned = isinstance(ours, dict) and ours.get("registry_name") == registry_name
    in_file = any(item.get("name") == registry_name for item in entries)
    return in_file and not owned


def _ps(ctx: Context, project: str) -> list[dict[str, Any]]:
    result = run(
        ctx.runner,
        [*compose_prefix(ctx.root, _layout(ctx), project=project), "ps", "-a", "--format", "json"],
        cwd=ctx.root,
    )
    found: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            found.append(loaded)
        elif isinstance(loaded, list):
            found.extend(item for item in loaded if isinstance(item, dict))
    return found


def _inspect(ctx: Context, container_id: str) -> dict[str, Any]:
    result = run(ctx.runner, ["docker", "inspect", container_id])
    try:
        loaded = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    if isinstance(loaded, list) and loaded and isinstance(loaded[0], dict):
        return loaded[0]
    if isinstance(loaded, dict):
        return loaded
    return {}


def _wait(ctx: Context, manifest: Mapping[str, Any], project: str) -> bool:
    name = str(manifest["name"])
    containers = manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
    inits = {
        role for role, spec in containers.items() if isinstance(spec, dict) and spec.get("init")
    }
    deadline = _monotonic() + _budget(manifest)
    pending = set(service_names(manifest))
    last_logs: dict[str, str] = {}
    while pending and _monotonic() <= deadline:
        rows = {str(row.get("Service")): row for row in _ps(ctx, project)}
        pending = set()
        for role, spec in containers.items():
            if not isinstance(spec, dict):
                continue
            service = f"{name}-{role}"
            row = rows.get(service)
            if row is None:
                pending.add(service)
                continue
            inspected = _inspect(ctx, str(row.get("ID") or service))
            state = inspected.get("State") if isinstance(inspected.get("State"), dict) else {}
            status = str(state.get("Status") or row.get("State") or "")
            code = state.get("ExitCode", row.get("ExitCode"))
            health = ""
            if isinstance(state.get("Health"), dict):
                health = str(state["Health"].get("Status") or "")
            elif row.get("Health"):
                health = str(row.get("Health"))
            if role in inits:
                if status == "exited" and code == 0:
                    continue
                if status == "exited":
                    last_logs[service] = _logs(ctx, project, service)
                    _fail(ctx, "init_failed", _log_message(ctx, service, last_logs[service]))
                    return False
                pending.add(service)
                continue
            if health == "healthy" or (status == "running" and not spec.get("healthcheck")):
                continue
            pending.add(service)
        if pending and _monotonic() < deadline:
            pause(3)
    if pending:
        service = min(pending)
        text = _logs(ctx, project, service)
        init_service = any(service.endswith(f"-{role}") for role in inits)
        _fail(
            ctx,
            "init_failed" if init_service else "service_unhealthy",
            _log_message(ctx, service, text),
        )
        return False
    return True


def _logs(ctx: Context, project: str, service: str) -> str:
    result = run(
        ctx.runner,
        [*compose_prefix(ctx.root, _layout(ctx), project=project), "logs", "--tail", "50", service],
        cwd=ctx.root,
    )
    lines = (result.stdout + result.stderr).splitlines()
    return "\n".join(lines[-50:])


def _log_message(ctx: Context, service: str, text: str) -> str:
    body = ctx.secrets.redact(text).strip()
    return (
        f"{service} did not become ready.\n{body}" if body else f"{service} did not become ready."
    )


def _facade(ctx: Context, manifest: Mapping[str, Any], image: str) -> bool:
    role = mcp_role(manifest)
    if role is None:
        return True
    containers = manifest["containers"]
    port = int(containers[role]["port"])
    healthz = str((manifest.get("mcp") or {}).get("healthz") or "/healthz")
    url = facade_probe_url(manifest, port, healthz)
    result = run(
        ctx.runner,
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
            PROBE_CODE,
            url,
        ],
    )
    if result.returncode != 0:
        _fail(
            ctx,
            "service_unhealthy",
            ctx.secrets.redact(result.stderr or result.stdout or "facade health check failed"),
        )
        return False
    return True


def _keystone_running(ctx: Context, project: str) -> bool:
    service = str(_layout(ctx).get("keystone_service") or "")
    result = run(
        ctx.runner,
        [
            *compose_prefix(ctx.root, _layout(ctx), project=project),
            "ps",
            "--status",
            "running",
            "-q",
            service,
        ],
        cwd=ctx.root,
    )
    return bool(result.stdout.strip())


def _signal_keystone(
    ctx: Context,
    project: str,
    *,
    wiring: bool,
    registry: bool,
    snapshot: bool,
    root_env_changed: bool = False,
) -> str:
    if not _keystone_running(ctx, project):
        return "keystone-mcp is not running; it will read the new registry when you start it"
    service = str(_layout(ctx).get("keystone_service") or "")
    prefix = compose_prefix(ctx.root, _layout(ctx), project=project)
    if wiring or root_env_changed:
        command = [*prefix, "up", "-d", "--no-deps"]
        if root_env_changed:
            command.append("--force-recreate")
        command.append(service)
        run(ctx.runner, command, cwd=ctx.root)
        return "recreated"
    if registry or REGISTRY_SIGHUP_MIN_VERSION is None and registry:
        run(ctx.runner, [*prefix, "restart", service], cwd=ctx.root)
        return "restarted"
    if snapshot:
        run(ctx.runner, [*prefix, "kill", "-s", "HUP", service], cwd=ctx.root)
        return "sighup"
    return "unchanged"


def _discover(ctx: Context, project: str, namespace: str) -> str:
    if read_env(ctx.root / ".env").get("KEYSTONE_MCP_METRICS_ENABLED", "").lower() == "true":
        service = str(_layout(ctx).get("keystone_service") or "")
        result = run(
            ctx.runner,
            [
                *compose_prefix(ctx.root, _layout(ctx), project=project),
                "exec",
                "-T",
                service,
                "python",
                "-c",
                METRICS_CODE,
            ],
            cwd=ctx.root,
        )
        metric = metric_available(result.stdout, namespace)
        if metric is True:
            return "available"
        if metric is False:
            ctx.extra_warnings.append(
                warning("discovery_pending", "keystone-mcp has not discovered this service yet.")
            )
            return "unavailable"
    live = run(
        ctx.runner,
        [
            *compose_prefix(ctx.root, _layout(ctx), project=project),
            "exec",
            "-T",
            str(_layout(ctx).get("keystone_service") or ""),
            "python",
            "-m",
            "keystone_mcp.rbac.catalog",
        ],
        cwd=ctx.root,
    )
    if live.returncode == 0 and any(
        line.startswith(f"{namespace}_") for line in live.stdout.splitlines()
    ):
        return "available"
    since = ctx.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
    logs = run(
        ctx.runner,
        [
            *compose_prefix(ctx.root, _layout(ctx), project=project),
            "logs",
            "--since",
            since,
            str(_layout(ctx).get("keystone_service") or ""),
        ],
        cwd=ctx.root,
    )
    unavailable = discovery_unavailable(logs.stdout + "\n" + logs.stderr, namespace)
    if unavailable is True:
        ctx.extra_warnings.append(
            warning("discovery_pending", "keystone-mcp has not discovered this service yet.")
        )
        return "unavailable"
    if unavailable is False:
        return "available"
    return "unknown"


def _policy_blob(ctx: Context, manifest: Mapping[str, Any]) -> dict[str, str] | None:
    relative = manifest.get("recommended_policy")
    if not isinstance(relative, str) or ctx.resolved is None:
        return None
    base = package_cache(
        ctx.root, ctx.resolved.source_name, ctx.resolved.name, ctx.resolved.version
    )
    if ctx.resolved.path != ".":
        base = base / ctx.resolved.path
    path = base / relative
    payload = {"path": relative}
    if path.is_file():
        data = path.read_bytes()
        if len(data) <= _POLICY_LIMIT:
            payload["content"] = data.decode("utf-8")
    return payload


def _result_body(
    ctx: Context,
    manifest: Mapping[str, Any],
    *,
    namespace: str | None,
    registry_name: str | None,
    discovery: str | None,
) -> dict[str, Any]:
    containers = manifest.get("containers") if isinstance(manifest.get("containers"), dict) else {}
    body: dict[str, Any] = {
        "containers": [
            {
                "role": role,
                "service": f"{manifest['name']}-{role}",
                "image": str(spec.get("image") or ""),
                "init": bool(spec.get("init")),
            }
            for role, spec in containers.items()
            if isinstance(spec, dict)
        ],
        "check": f"keystonectl service status {manifest['name']}",
    }
    if namespace is not None:
        tools = [str(item) for item in (manifest.get("mcp") or {}).get("tools") or []]
        body["namespace"] = namespace
        body["registry_name"] = registry_name
        body["tools"] = tools
        body["discovery"] = discovery or "unknown"
        critical = (manifest.get("mcp") or {}).get("critical") or []
        body["critical_tools"] = [f"{namespace}_{item}" for item in critical]
        body["critical_tools_hint"] = "add these to critical_tools in the policy file"
    policy = _policy_blob(ctx, manifest)
    if policy:
        body["recommended_policy"] = policy
    return body


def _save_service(
    ctx: Context,
    manifest: Mapping[str, Any],
    *,
    namespace: str | None,
    registry_name: str | None,
    fragment: str,
) -> None:
    from keystonectl.resources.binding import _ensure_service, _generated_names

    package = ctx.resolved
    assert package is not None
    name = str(manifest["name"])
    plan = placement(manifest)
    role = "resource" if manifest.get("kind") == "resource" else "service"
    entry = _ensure_service(
        ctx.lock,
        package=package,
        manifest=manifest,
        role=role,
        now=utc_now(ctx.clock()),
        env_files=list(plan.files),
        resources=list(entry_resources(ctx, name)),
        generated=_generated_names(manifest)
        or [item.name for item in plan.secrets if item.generate_bytes],
        root=ctx.root,
    )
    digest = fragment_digest(fragment)
    entry["fragment_sha256"] = digest
    entry["fragment"] = f"packages/{name}.yml"
    entry["managed"] = entry.get("managed") or "generated"
    entry["containers"] = {
        role_name: {
            "service": f"{name}-{role_name}",
            "image": str(spec.get("image") or ""),
            "init": bool(spec.get("init")),
        }
        for role_name, spec in (manifest.get("containers") or {}).items()
        if isinstance(spec, dict)
    }
    env = entry.setdefault("env", {"keystone": [], "files": [], "generated": []})
    if isinstance(env, dict):
        env["files"] = list(plan.files)
        if namespace:
            env["keystone"] = [bearer_env_name(namespace), identity_env_name(namespace)]
        env["generated"] = [item.name for item in plan.secrets if item.generate_bytes]
    entry["registry_name"] = registry_name
    entry["namespace"] = namespace
    entry["url"] = registry_url(manifest) if namespace else None
    tools = (manifest.get("mcp") or {}).get("tools") if namespace else []
    entry["tools"] = [str(item) for item in tools or []]
    entry["updated_at"] = utc_now(ctx.clock())
    from keystonectl.public import stamp_lock_public

    stamp_lock_public(ctx, manifest, entry)
    ctx.lock["updated_by"] = f"keystonectl {__version__}"
    save_lock(ctx.root / "services.lock.yaml", ctx.lock, now=ctx.clock())


def entry_resources(ctx: Context, name: str) -> list[str]:
    current = _service(ctx.lock, name)
    if isinstance(current, dict) and isinstance(current.get("resources"), list):
        return [str(item) for item in current["resources"]]
    return []


def _install_registry_files(
    ctx: Context,
    manifest: Mapping[str, Any],
    namespace: str,
    registry_name: str,
) -> tuple[bool, bool]:
    registry_path = ctx.root / "keystonectl" / "upstreams.yaml"
    snapshot_path = ctx.root / "keystonectl" / "catalog-snapshot.yaml"
    current = _load_yaml(registry_path)
    entries = _entries(current)
    entry = registry_entry(manifest, namespace=namespace, registry_name=registry_name)
    merged_entries = merge_registry_entries(entries, entry)
    registry_changed = not any(
        item.get("name") == entry["name"] and all(item.get(key) == entry[key] for key in entry)
        for item in entries
    )
    services = [item for item in (ctx.lock.get("services") or []) if isinstance(item, dict)]
    snapshot = _load_yaml(snapshot_path)
    merged = merge_snapshot(
        snapshot,
        services=_project_services(services, manifest, namespace),
        previous_owned=owned_tool_names(services),
    )
    snapshot_changed = snapshot is None or (
        list(snapshot.get("active") or []) != merged["active"]
        or list(snapshot.get("reserved") or []) != merged["reserved"]
        or list(snapshot.get("wip") or []) != merged["wip"]
    )
    if registry_changed:
        write_backup(registry_path)
        atomic_write(registry_path, render_registry(merged_entries).encode("utf-8"), mode=0o644)
    if snapshot_changed:
        write_backup(snapshot_path)
        atomic_write(snapshot_path, render_snapshot(merged).encode("utf-8"), mode=0o644)
    return registry_changed, snapshot_changed


def _start(ctx: Context, manifest: Mapping[str, Any], *, recreate: bool) -> str | None:
    layout = _layout(ctx)
    config = parse_config(compose_config(ctx.runner, ctx.root, layout))
    project = str((config or {}).get("name") or layout.get("project") or "")
    if not project:
        _fail(ctx, "layout_invalid", "docker compose config did not include a project name")
        return None
    names = service_names(manifest)
    prefix = compose_prefix(ctx.root, layout, project=project)
    run(ctx.runner, [*prefix, "pull", *names], cwd=ctx.root)
    up = [*prefix, "up", "-d"]
    if recreate:
        up.append("--force-recreate")
    up.extend(names)
    ctx.started_project = project
    started = run(ctx.runner, up, cwd=ctx.root)
    if started.returncode != 0:
        if daemon_down(started):
            _fail(ctx, "docker_unavailable", ERRORS["docker_unavailable"].title)
        else:
            _fail(ctx, "service_unhealthy", ctx.secrets.redact(started.stderr or started.stdout))
        return None
    return project


def service_install_would_rerun(ctx: Context) -> bool:
    """True when apply would leave the generated fragment and registry untouched."""

    manifest = _manifest_of(ctx)
    if manifest is None or ctx.resolved is None or manifest.get("kind") == "resource":
        return False
    name = str(manifest["name"])
    from keystonectl.public import fragment_ports, install_questions

    if _service(ctx.lock, name) and _service(ctx.lock, name).get("managed") == "ejected":
        return False
    if _env_questions(ctx, manifest) or install_questions(ctx, manifest):
        return False
    entries = _entries(_load_yaml(ctx.root / "keystonectl" / "upstreams.yaml"))
    namespace = _namespace_choice(ctx, manifest, entries) if mcp_role(manifest) else None
    if ctx.failure is not None:
        return False
    registry_name = _registry_name(ctx, manifest) if namespace else None
    if namespace and registry_name and _taken(ctx, registry_name, entries):
        return False
    if namespace and registry_name:
        names = {str(item.get("name")) for item in entries}
        if registry_name not in names and len(entries) >= MAX_REGISTRY_ENTRIES:
            return False
    env_values = _env_values(ctx, manifest)
    host_port, extra_hosts = fragment_ports(ctx, manifest)
    if ctx.failure is not None:
        return False
    fragment = render_fragment(
        manifest,
        {"env_values": env_values},
        host_port=host_port,
        extra_hosts=extra_hosts,
    )
    return _unchanged(ctx, manifest, fragment, namespace)


def _stop_started(ctx: Context, manifest: Mapping[str, Any], project: str | None) -> list[str]:
    """Remove the Compose services ``up -d`` created. Needs the fragment still on disk."""

    if not project:
        return []
    name = str(manifest["name"])
    names = service_names(manifest)
    prefix = compose_prefix(ctx.root, _layout(ctx), project=project)
    removed = run(ctx.runner, [*prefix, "rm", "-s", "-f", *names], cwd=ctx.root)
    run(ctx.runner, ["docker", "network", "rm", f"ks-pkg-{name}"], cwd=ctx.root)
    return names if removed.returncode == 0 else []


def _drop_new_entry(ctx: Context, name: str) -> None:
    """Remove the entry a failed first install left, and its consumer records.

    A consumer record in ``resources[]`` cannot outlive its service entry, so it goes
    the way ``service remove`` drops it. The resource itself, its env file and secrets stay.
    """

    from keystonectl.resources.binding import unbind_consumer

    before = copy.deepcopy(ctx.lock)
    unbind_consumer(ctx.lock, name)
    services = ctx.lock.get("services")
    if isinstance(services, list):
        ctx.lock["services"] = [
            item for item in services if not (isinstance(item, dict) and item.get("name") == name)
        ]
    if ctx.lock == before:
        return
    ctx.lock["updated_by"] = f"keystonectl {__version__}"
    save_lock(ctx.root / "services.lock.yaml", ctx.lock, now=ctx.clock())


def _roll_back(ctx: Context, manifest: Mapping[str, Any], undo: _Undo) -> None:
    """Undo a failed apply: stop what it started, restore files, drop a first-install lock entry.

    A service that was already recorded before this run keeps its containers and its entry.
    """

    name = str(manifest["name"])
    first_install = name not in ctx.lock_services_before
    started = ctx.started_project
    ctx.started_project = None
    containers = _stop_started(ctx, manifest, started) if first_install else []
    undo.restore()
    if first_install:
        _drop_new_entry(ctx, name)
    failure = ctx.failure
    if failure is None:
        return
    if first_install:
        if containers:
            note = f"Containers {', '.join(containers)} were stopped and removed; "
        elif started:
            note = f"Docker could not remove containers {', '.join(service_names(manifest))}; "
        else:
            note = ""
        note += f"{name} is not recorded in services.lock.yaml."
    else:
        note = f"{name} is still recorded in services.lock.yaml."
    failure.message = f"{failure.message} {note}".strip()
    failure.result = {
        **failure.result,
        "rollback": {
            "containers": containers,
            "lock_entry": "dropped" if first_install else "kept",
        },
    }


def install_service(ctx: Context) -> None:
    manifest = _manifest_of(ctx)
    if manifest is None or ctx.resolved is None:
        return
    if manifest.get("kind") == "resource":
        return
    name = str(manifest["name"])
    from keystonectl.lifecycle import prune_retired_names
    from keystonectl.public import install_questions

    if isinstance(ctx.lock, dict):
        prune_retired_names(ctx.root, ctx.lock)

    if _service(ctx.lock, name) and _service(ctx.lock, name).get("managed") == "ejected":
        ctx.skip_actions.add("deploy.fragment")
        questions = install_questions(ctx, manifest)
        if questions:
            ctx.halt_questions.extend(questions)
        return
    questions = _env_questions(ctx, manifest)
    questions.extend(install_questions(ctx, manifest))
    if questions:
        ctx.halt_questions.extend(questions)
        return
    entries = _entries(_load_yaml(ctx.root / "keystonectl" / "upstreams.yaml"))
    namespace = _namespace_choice(ctx, manifest, entries) if mcp_role(manifest) else None
    if ctx.failure is not None or ctx.halt_questions:
        return
    registry_name = _registry_name(ctx, manifest) if namespace else None
    if namespace and registry_name and _taken(ctx, registry_name, entries):
        _fail(ctx, "registry_name_taken", ERRORS["registry_name_taken"].title)
        return
    if namespace and registry_name:
        names = {str(item.get("name")) for item in entries}
        if registry_name not in names and len(entries) >= MAX_REGISTRY_ENTRIES:
            _fail(ctx, "registry_full", ERRORS["registry_full"].title)
            return
    if service_install_would_rerun(ctx):
        ctx.rerun = True
        entry = _service(ctx.lock, name)
        if isinstance(entry, dict):
            from keystonectl.public import stamp_lock_public

            stamp_lock_public(ctx, manifest, entry)
            save_lock(ctx.root / "services.lock.yaml", ctx.lock, now=ctx.clock())
        ctx.output_result = _result_body(
            ctx, manifest, namespace=namespace, registry_name=registry_name, discovery="unknown"
        )
        return
    env_values = _env_values(ctx, manifest)
    from keystonectl.public import fragment_ports

    host_port, extra_hosts = fragment_ports(ctx, manifest)
    if ctx.failure is not None:
        return
    fragment = render_fragment(
        manifest,
        {"env_values": env_values},
        host_port=host_port,
        extra_hosts=extra_hosts,
    )
    if namespace and not _validate(ctx, manifest, namespace, registry_name or name):
        return
    if mcp_role(manifest) is None and not _validate_compose_only(ctx):
        return
    fragment_path = ctx.root / "packages" / f"{name}.yml"
    current = _service(ctx.lock, name) or {"name": name, "managed": "generated"}
    guarded = fragment_guard(current, fragment_path)
    if guarded == "ejected":
        return
    if guarded:
        _fail(ctx, "fragment_modified", guarded, hint=fragment_modified_message(name))
        return
    undo = _Undo()
    ctx.started_project = None
    try:
        _materialise(ctx, manifest, fragment, namespace, registry_name, undo)
    except (OSError, ValueError, RuntimeError) as exc:
        _fail(ctx, "service_unhealthy", ctx.secrets.redact(str(exc)))
        _roll_back(ctx, manifest, undo)
        return
    if ctx.failure is not None:
        _roll_back(ctx, manifest, undo)


def _validate_compose_only(ctx: Context) -> bool:
    """Packages without an MCP facade still need a current Compose and a valid layout."""

    version = run(ctx.runner, ["docker", "compose", "version", "--short"], cwd=ctx.root)
    if daemon_down(version):
        _fail(ctx, "docker_unavailable", ERRORS["docker_unavailable"].title)
        return False
    if version.returncode != 0 or compose_is_old(version.stdout):
        _fail(ctx, "compose_too_old", ERRORS["compose_too_old"].title)
        return False
    return True


def _materialise(
    ctx: Context,
    manifest: Mapping[str, Any],
    fragment: str,
    namespace: str | None,
    registry_name: str | None,
    undo: _Undo,
    *,
    commit: bool = True,
) -> None:
    name = str(manifest["name"])
    plan = placement(manifest)
    saved_env = _load_env_state(ctx).get(name)
    for relative in plan.files:
        path = ctx.root / relative
        if not path.exists():
            _write_env_values(path, {}, undo)
    _retire_shared_secrets(ctx, manifest, undo)
    _fill_secrets(ctx, manifest, undo)
    from keystonectl.public import materialise_public_base_env

    public_base = materialise_public_base_env(ctx, manifest)
    if public_base:
        _write_env_values(ctx.root / plan.shared, public_base, undo)
    root_values: dict[str, str] = {}
    if namespace and registry_name:
        root_values = _gateway_secrets(ctx, manifest, namespace, registry_name, undo)
        if ctx.failure is not None:
            return
        allow = _allow_insecure(
            ctx,
            merge_registry_entries(
                _entries(_load_yaml(ctx.root / "keystonectl" / "upstreams.yaml")),
                registry_entry(manifest, namespace=namespace, registry_name=registry_name),
            ),
        )
        if allow == "true":
            insecure = "KEYSTONE_MCP_UPSTREAM_ALLOW_INSECURE_HTTP"
            env_path = ctx.root / ".env"
            env_text = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
            if not env_assigned_outside_managed_block(env_text, insecure):
                root_values[insecure] = "true"
    root_env_changed = False
    if root_values:
        env_path = ctx.root / ".env"
        env_before = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
        _write_root_env(ctx, root_values, undo)
        if ctx.failure is not None:
            return
        env_after = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
        root_env_changed = env_before != env_after
    fragment_path = ctx.root / "packages" / f"{name}.yml"
    previous_fragment = (
        fragment_path.read_text(encoding="utf-8") if fragment_path.is_file() else None
    )
    undo.remember(fragment_path)
    fragment_path.parent.mkdir(parents=True, exist_ok=True)
    fragment_path.write_text(fragment, encoding="utf-8")
    os.chmod(fragment_path, 0o644)
    packages_path = ctx.root / "docker-compose.packages.yml"
    undo.remember(packages_path)
    previous_packages = packages_path.read_text(encoding="utf-8") if packages_path.is_file() else ""
    packages_text = _packages_text(ctx, name)
    packages_path.write_text(packages_text, encoding="utf-8")
    for network in (UPSTREAMS_NETWORK, RESOURCES_NETWORK):
        created = ensure_network(ctx.runner, network)
        if daemon_down(created) or created.returncode != 0:
            _fail(ctx, "docker_unavailable", ERRORS["docker_unavailable"].title)
            return
    env_changed = saved_env != _env_digests(ctx, manifest)
    recreate = previous_fragment != fragment or previous_packages != packages_text or env_changed
    project = _start(ctx, manifest, recreate=recreate)
    if project is None or ctx.failure is not None:
        return
    if not _wait(ctx, manifest, project):
        return
    image = _image(ctx)
    if namespace and image and not _facade(ctx, manifest, image):
        return
    registry_changed = False
    snapshot_changed = False
    if namespace and registry_name:
        registry_changed, snapshot_changed = _install_registry_files(
            ctx, manifest, namespace, registry_name
        )
    wiring = "keystonectl:/etc/keystone-mcp/keystonectl" not in previous_packages
    if namespace:
        info = _signal_keystone(
            ctx,
            project,
            wiring=wiring,
            registry=registry_changed,
            snapshot=snapshot_changed and not registry_changed and not wiring,
            root_env_changed=root_env_changed,
        )
        if info.startswith("keystone-mcp is not running"):
            ctx.output_result["info"] = info
            discovery: str | None = "unknown"
        else:
            discovery = _discover(ctx, project, namespace)
    else:
        discovery = None
    _save_env_state(ctx, manifest)
    if not commit:
        return
    _save_service(
        ctx, manifest, namespace=namespace, registry_name=registry_name, fragment=fragment
    )
    ctx.output_result.update(
        _result_body(
            ctx,
            manifest,
            namespace=namespace,
            registry_name=registry_name,
            discovery=discovery,
        )
    )


def _reject_manifest(ctx: Context, document: Mapping[str, Any]) -> bool:
    """Same manifest parse the service command runs before it writes anything."""

    from keystonectl.manifest import manifest_errors

    lines = manifest_errors(document)
    if not lines:
        return False
    problems = [
        {"path": line.split(": ", 1)[0], "message": line, "severity": "error"} for line in lines
    ]
    _fail(ctx, "manifest_invalid", problems[0]["message"], result={"problems": problems})
    return True


def fragment_write_blocked(ctx: Context, name: str) -> str | None:
    """Stop a provider write the same way a service write stops.

    ``ejected`` skips the write without an error. A hand edit is
    ``modified`` and sets ``fragment_modified``.
    """

    current = _service(ctx.lock, name) or {"name": name, "managed": "generated"}
    path = ctx.root / "packages" / f"{name}.yml"
    guarded = fragment_guard(current, path)
    if guarded == "ejected":
        return "ejected"
    if guarded:
        _fail(ctx, "fragment_modified", guarded, hint=fragment_modified_message(name))
        return "modified"
    return None


class ComposeDeployer:
    """Deployer from I02. ``install_service`` is the service path; ``deploy`` is the provider path."""

    def plan(self, ctx: Context) -> StepPlan:
        manifest = _manifest_of(ctx)
        if manifest is None or manifest.get("kind") == "resource":
            return StepPlan()
        actions = [
            {
                "id": "deploy.fragment",
                "title": f"Write packages/{manifest['name']}.yml",
                "status": "planned",
            },
            {"id": "deploy.up", "title": f"Start {manifest['name']}", "status": "planned"},
        ]
        if mcp_role(manifest):
            actions.append(
                {
                    "id": "deploy.registry",
                    "title": f"Publish {manifest['name']} in the upstream registry",
                    "status": "planned",
                }
            )
        return StepPlan(actions=actions, questions=_env_questions(ctx, manifest))

    def install_service(self, ctx: Context) -> None:
        install_service(ctx)

    def deploy(self, ctx: Context, package: Any, *, role: str) -> None:
        manifest = _load_package_manifest(ctx, package)
        if manifest is None:
            _fail(ctx, "manifest_invalid", f"{package.name} has no keystone-service.yaml.")
            return
        if _reject_manifest(ctx, manifest):
            return
        name = str(manifest["name"])
        blocked = fragment_write_blocked(ctx, name)
        if blocked:
            return
        if not _validate_compose_only(ctx):
            return
        fragment = render_fragment(manifest, {})
        undo = _Undo()
        ctx.started_project = None
        _materialise(ctx, manifest, fragment, None, None, undo, commit=False)
        if ctx.failure is not None:
            _roll_back(ctx, manifest, undo)

    def wait_healthy(self, ctx: Context, name: str, *, timeout: float) -> None:
        if ctx.failure is not None:
            return
        manifest = (
            ctx.manifest
            if isinstance(ctx.manifest, dict) and ctx.manifest.get("name") == name
            else None
        )
        if manifest is None:
            return
        layout = _layout(ctx)
        config = parse_config(compose_config(ctx.runner, ctx.root, layout))
        project = str((config or {}).get("name") or layout.get("project") or "keystonectl")
        deadline = _monotonic() + timeout
        while _monotonic() <= deadline:
            if _wait_once(ctx, manifest, project):
                return
            pause(3)
        _fail(ctx, "service_unhealthy", ERRORS["service_unhealthy"].title)

    def container_ip(self, ctx: Context, service: str, network: str) -> str:
        result = run(
            ctx.runner,
            ["docker", "network", "inspect", network, "--format", "{{json .Containers}}"],
        )
        if result.returncode != 0 or not result.stdout.strip():
            return ""
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return ""
        return ip_for_service(payload, service)


def _wait_once(ctx: Context, manifest: Mapping[str, Any], project: str) -> bool:
    rows = {str(row.get("Service")): row for row in _ps(ctx, project)}
    for role in package_roles(manifest):
        service = f"{manifest['name']}-{role}"
        if service not in rows:
            return False
    return True


def _load_package_manifest(ctx: Context, package: Any) -> dict[str, Any] | None:
    dest = package_cache(ctx.root, package.source_name, package.name, package.version)
    path = (
        dest / "keystone-service.yaml"
        if package.path == "."
        else dest / package.path / "keystone-service.yaml"
    )
    if not path.is_file():
        return None
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


class DeployStep:
    """Service deploy step. A catalog provider still needs a Deployer passed in."""

    id = "deploy"
    is_service_deploy = True

    def plan(self, ctx: Context) -> StepPlan:
        deployer = ctx.deployer
        if isinstance(deployer, ComposeDeployer):
            return deployer.plan(ctx)
        return StepPlan()

    def apply(self, ctx: Context) -> None:
        deployer = ctx.deployer
        manifest = _manifest_of(ctx)
        if isinstance(deployer, ComposeDeployer):
            if manifest is not None and manifest.get("kind") == "resource":
                return
            deployer.install_service(ctx)
            return
        if deployer is None and manifest is not None and manifest.get("kind") != "resource":
            _fail(ctx, "not_implemented", ERRORS["not_implemented"].title)
