"""keystonectl command line."""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path

from keystonectl import __version__
from keystonectl.answers import AnswerError, parse_sets
from keystonectl.bundles import add_bundle_parser
from keystonectl.check import run_check
from keystonectl.errors import ERRORS, explain
from keystonectl.lint import render_text, run_lint
from keystonectl.lock import LockError, hold_lock, load_lock
from keystonectl.output import Result, SecretRegistry, emit
from keystonectl.plan import run_service
from keystonectl.runner import build_runner
from keystonectl.sources import add_source, list_catalogs, set_source_ref
from keystonectl.wrap_stdio import STDIO_BOX_IMAGE, run_wrap_stdio

ROOT_HINT = "Run `keystonectl init --adopt <dir>`."


class KeystoneParser(argparse.ArgumentParser):
    json_mode = False
    secrets: SecretRegistry | None = None

    def error(self, message: str) -> None:
        secrets = self.secrets or SecretRegistry()
        result = Result(
            code=1,
            command="",
            summary="The command was not understood.",
            error={"id": "usage", "message": message, "hint": "Run keystonectl --help."},
        )
        if self.json_mode:
            emit(result, json_mode=True, secrets=secrets)
        else:
            self.print_usage(sys.stderr)
            sys.stderr.write(f"usage: {message}\n")
        raise SystemExit(1)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root", default=None)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--set", action="append", default=None)
    parser.add_argument("--version", action="store_true")


def build_parser() -> KeystoneParser:
    parser = KeystoneParser(prog="keystonectl")
    _add_common(parser)
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="Create or adopt a deployment directory")
    _add_common(init)
    init.add_argument("root_arg")
    init.add_argument("--adopt", action="store_true")
    init.add_argument("--keystone-service", default=None)

    lint = sub.add_parser("lint", help="Check a package manifest or catalog.yaml")
    _add_common(lint)
    lint.add_argument("target")
    lint.add_argument("--keystone-mcp-version", default=None)

    package = sub.add_parser("package")
    _add_common(package)
    package_sub = package.add_subparsers(dest="package_command")
    package_lint = package_sub.add_parser("lint")
    _add_common(package_lint)
    package_lint.add_argument("target")
    package_lint.add_argument("--keystone-mcp-version", default=None)
    wrap = package_sub.add_parser(
        "wrap-stdio",
        help="Write a box package from a stdio provider directory",
    )
    _add_common(wrap)
    wrap.add_argument("provider_dir")
    wrap.add_argument("--image", default=STDIO_BOX_IMAGE)
    discovery = wrap.add_mutually_exclusive_group()
    discovery.add_argument("--tools-snapshot", metavar="FILE", default=None)
    discovery.add_argument("--no-discovery", action="store_true")
    wrap.add_argument("--name", default=None)
    wrap.add_argument("--namespace", default=None)
    wrap.add_argument("--derived-image", action="store_true")
    wrap.add_argument("--force", action="store_true")

    check = sub.add_parser("check", help="Check a deployment directory")
    _add_common(check)

    catalog = sub.add_parser("catalog")
    _add_common(catalog)
    catalog_sub = catalog.add_subparsers(dest="catalog_command")
    add = catalog_sub.add_parser("add")
    _add_common(add)
    add.add_argument("url")
    add.add_argument("--name", default=None)
    add.add_argument("--ref", default=None)
    add.add_argument("--priority", type=int, default=None)
    set_ref = catalog_sub.add_parser("set-ref")
    _add_common(set_ref)
    set_ref.add_argument("name")
    set_ref.add_argument("ref")
    catalog_list = catalog_sub.add_parser("list")
    _add_common(catalog_list)

    service = sub.add_parser("service")
    _add_common(service)
    service_sub = service.add_subparsers(dest="service_command")
    for name in ("list",):
        command = service_sub.add_parser(name)
        _add_common(command)
    for name in ("plan", "apply", "install"):
        command = service_sub.add_parser(name)
        _add_common(command)
        command.add_argument("ref")
    explain_service = service_sub.add_parser("explain")
    _add_common(explain_service)
    explain_service.add_argument("error_id")
    register(service_sub)

    explain_cmd = sub.add_parser("explain", help="Explain an error or warning id")
    _add_common(explain_cmd)
    explain_cmd.add_argument("error_id")
    add_bundle_parser(sub)
    return parser


def register(subparsers: argparse._SubParsersAction) -> None:
    """Later cards call ``register`` on this service subparser from their modules."""
    for module_name in (
        "keystonectl.resources",
        "keystonectl.compose_gen",
        "keystonectl.tls",
        "keystonectl.bundles",
        "keystonectl.remote",
    ):
        try:
            module = __import__(module_name, fromlist=["register"])
        except ImportError:
            continue
        hook = getattr(module, "register", None)
        if hook is not None:
            hook(subparsers)


def find_root(
    explicit: str | None, environ: Mapping[str, str], cwd: Path, *, required: bool
) -> Path | None:
    if explicit:
        return Path(explicit)
    if not required:
        if environ.get("KEYSTONECTL_ROOT"):
            return Path(environ["KEYSTONECTL_ROOT"])
        return None
    if environ.get("KEYSTONECTL_ROOT"):
        return Path(environ["KEYSTONECTL_ROOT"])
    current = cwd.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "services.lock.yaml").is_file():
            return candidate
    return None


def _version_result() -> Result:
    return Result(
        code=0,
        command="version",
        summary=f"keystonectl {__version__}",
        result={"version": __version__},
        changes=0,
    )


def _error(
    error_id: str, command: str, message: str | None = None, *, target: str | None = None
) -> Result:
    spec = ERRORS[error_id]
    return Result(
        code=spec.code,
        command=command,
        summary=message or spec.title,
        target=target,
        error={"id": spec.id, "message": message or spec.title, "hint": spec.action},
        retryable=spec.retryable,
    )


def _internal_error(command: str, exc: Exception) -> Result:
    """Any exception a command did not handle itself. Never covers ``SystemExit``

    or ``KeyboardInterrupt``; those propagate so ``Ctrl-C`` and ``--help`` keep working.
    """
    if os.environ.get("KEYSTONECTL_DEBUG") == "1":
        traceback.print_exc(file=sys.stderr)
    return _error("internal_error", command, f"{type(exc).__name__}: {exc}")


def _explain(error_id: str) -> Result:
    spec = explain(error_id)
    if spec is None:
        return _error("usage", "explain", f"Unknown error id '{error_id}'.", target=error_id)
    code = getattr(spec, "code", None)
    retryable = getattr(spec, "retryable", False)
    return Result(
        code=0,
        command="explain",
        summary=spec.title,
        target=error_id,
        result={
            "id": spec.id,
            "code": code,
            "retryable": retryable,
            "title": spec.title,
            "action": spec.action,
        },
    )


def _service_list(root: Path, environ: Mapping[str, str]) -> Result:
    try:
        lock = load_lock(root / "services.lock.yaml")
    except LockError as exc:
        return Result(
            code=exc.code,
            command="service list",
            summary=exc.message,
            error={"id": exc.error_id, "message": exc.message, "hint": exc.hint},
            retryable=exc.retryable,
        )
    from keystonectl.compat import detect_keystone_mcp_version

    detection = detect_keystone_mcp_version(root, lock=lock, environ=environ)
    return Result(
        code=0,
        command="service list",
        summary=f"{len(lock.get('services') or [])} services.",
        result={
            "services": list(lock.get("services") or []),
            "resources": list(lock.get("resources") or []),
            "keystone_mcp_version": detection.version,
        },
    )


def _init(
    root: Path, *, adopt: bool, keystone_service: str | None, environ: Mapping[str, str]
) -> Result:
    try:
        from keystonectl.layout import LayoutError, apply_init, plan_init
    except ImportError:
        return _error(
            "usage",
            "init",
            "Deployment layout support is not available yet.",
        )
    from dataclasses import replace
    from datetime import UTC, datetime

    try:
        plan = plan_init(root, adopt=adopt, keystone_service=keystone_service, environ=environ)
    except LayoutError as exc:
        code = getattr(exc, "code", "usage")
        spec = ERRORS.get(code, ERRORS["usage"])
        missing = []
        if code == "keystone_service_ambiguous" or getattr(exc, "exit_code", spec.code) == 2:
            missing.append(
                {
                    "var": "--keystone-service",
                    "question": getattr(exc, "message", spec.title),
                    "kind": "argument",
                    "choices": list(getattr(exc, "choices", []) or []),
                    "default": None,
                    "secret": False,
                }
            )
            return Result(
                code=2,
                command="init",
                summary=missing_summary_one(),
                target=str(root),
                missing=missing,
                error={
                    "id": code,
                    "message": getattr(exc, "message", spec.title),
                    "hint": getattr(exc, "hint", spec.action),
                },
            )
        return Result(
            code=getattr(exc, "exit_code", spec.code),
            command="init",
            summary=getattr(exc, "message", spec.title),
            target=str(root),
            error={
                "id": code,
                "message": getattr(exc, "message", spec.title),
                "hint": getattr(exc, "hint", spec.action),
            },
            retryable=False,
        )
    kept = tuple(item for item in plan.writes if not (root / item.path).exists())
    plan = replace(plan, writes=kept)
    written = apply_init(root, plan, now=datetime.now(UTC))
    done = {f"write:{path}" for path in written} | {f"create:{path}" for path in written}
    actions = []
    for action in getattr(plan, "actions", []) or []:
        title = str(action)
        actions.append(
            {
                "id": "init",
                "title": title,
                "status": "done" if title in done else "unchanged",
            }
        )
    if not actions and written:
        for path in written:
            actions.append({"id": "init", "title": str(path), "status": "done"})
    warnings = []
    for item in getattr(plan, "warnings", []) or []:
        text = str(item)
        warning_id = text.split(":", 1)[0]
        if warning_id in ERRORS or warning_id in _warning_ids():
            from keystonectl.errors import WARNINGS

            spec = WARNINGS.get(warning_id)
            message = (
                text.split(":", 1)[1].strip()
                if ":" in text and spec
                else (spec.title if spec else text)
            )
            warnings.append({"id": warning_id if spec else "layout", "message": message or text})
        else:
            warnings.append({"id": text if text in _warning_ids() else "layout", "message": text})
    warnings.extend(_unmanaged_upstreams(root))
    deployment = getattr(plan, "deployment", None)
    layout = (
        deployment.to_dict() if deployment is not None and hasattr(deployment, "to_dict") else None
    )
    compose = getattr(plan, "compose_command", None)
    if isinstance(compose, (list, tuple)):
        compose_text = " ".join(str(part) for part in compose)
    else:
        compose_text = str(compose or "")
    changes = len(written)
    return Result(
        code=0,
        command="init",
        summary="Deployment directory is ready."
        if getattr(plan, "ready", False)
        else "Created keystonectl files.",
        target=str(root),
        actions=actions,
        warnings=warnings,
        changes=changes,
        result={
            "ready": bool(getattr(plan, "ready", False)),
            "layout": layout,
            "compose_command": compose_text,
        },
    )


def _unmanaged_upstreams(root: Path) -> list[dict[str, str]]:
    import yaml

    path = root / "keystonectl" / "upstreams.yaml"
    if not path.is_file():
        return []
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        lock = load_lock(root / "services.lock.yaml")
    except (OSError, LockError, yaml.YAMLError):
        return []
    if not isinstance(document, dict):
        return []
    managed = {
        item.get("registry_name") for item in (lock.get("services") or []) if isinstance(item, dict)
    }
    found: list[dict[str, str]] = []
    for entry in document.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if name in managed:
            continue
        found.append(
            {
                "id": "unmanaged_upstream",
                "message": f"Upstream {name} is not in the lock and was left unchanged.",
            }
        )
    return found


def _warning_ids() -> set[str]:
    from keystonectl.errors import WARNINGS

    return set(WARNINGS)


def missing_summary_one() -> str:
    return "1 answer is missing"


def _flag(args: argparse.Namespace, name: str, default: object = None) -> object:
    return getattr(args, name, default)


def _command_name(args: argparse.Namespace) -> str:
    command = args.command or ""
    if command == "service" and args.service_command:
        verb = "apply" if args.service_command == "install" else args.service_command
        return f"service {verb}"
    if command == "package" and args.package_command:
        return f"package {args.package_command}"
    if command == "catalog" and args.catalog_command:
        return f"catalog {args.catalog_command}"
    if command == "bundle" and getattr(args, "bundle_command", None):
        return f"bundle {args.bundle_command}"
    return command


def _needs_root(args: argparse.Namespace) -> bool:
    return args.command not in {None, "init", "lint", "explain", "package"}


_STATE_CHANGING_SERVICE_COMMANDS = {
    "apply",
    "install",
    "remove",
    "upgrade",
    "rollback",
    "eject",
    "adopt",
    "add-remote",
}


def _lock_target(args: argparse.Namespace, root: Path | None) -> tuple[bool, Path | None]:
    """Return whether the command changes state and, if so, the directory holding its lock.

    State-changing commands hold ``services.lock.yaml.lck`` for the whole run so a second
    process gets ``busy`` instead of racing the registry, ``.env`` files, and Docker.
    """
    if args.command == "init":
        if bool(getattr(args, "adopt", False)):
            return True, Path(args.root_arg)
        return False, None
    if args.command == "service" and args.service_command in _STATE_CHANGING_SERVICE_COMMANDS:
        return True, root
    if args.command == "catalog" and args.catalog_command in {"add", "set-ref"}:
        return True, root
    if args.command == "bundle" and args.bundle_command == "install":
        return True, root
    return False, None


def _scan_options(argv: Sequence[str]) -> tuple[str | None, list[str], bool]:
    """Read global flags from argv. Subparsers must not drop a flag placed before the command."""
    root: str | None = None
    sets: list[str] = []
    non_interactive = False
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--root" and index + 1 < len(argv):
            root = argv[index + 1]
            index += 2
            continue
        if token.startswith("--root="):
            root = token.split("=", 1)[1]
            index += 1
            continue
        if token == "--set" and index + 1 < len(argv):
            sets.append(argv[index + 1])
            index += 2
            continue
        if token.startswith("--set="):
            sets.append(token.split("=", 1)[1])
            index += 1
            continue
        if token == "--non-interactive":
            non_interactive = True
        index += 1
    return root, sets, non_interactive


def _dispatch(
    args: argparse.Namespace,
    root: Path | None,
    command: str,
    *,
    interactive: bool,
    json_mode: bool,
    sets: Mapping[str, str],
    secrets: SecretRegistry,
) -> Result:
    """Build the ``Result`` for one command. Callers hold the lock for state-changing commands."""
    if args.command == "explain" or (
        args.command == "service" and args.service_command == "explain"
    ):
        result = _explain(args.error_id)
    elif args.command == "init":
        result = _init(
            Path(args.root_arg),
            adopt=bool(args.adopt),
            keystone_service=args.keystone_service,
            environ=os.environ,
        )
    elif args.command == "lint" or (args.command == "package" and args.package_command == "lint"):
        version = args.keystone_mcp_version
        if version is None and args.root:
            from keystonectl.compat import detect_keystone_mcp_version

            detection = detect_keystone_mcp_version(
                Path(args.root),
                lock=load_lock(Path(args.root) / "services.lock.yaml"),
                environ=os.environ,
            )
            version = detection.version
        result = run_lint(Path(args.target), keystone_version=version)
        if args.command == "package":
            result.command = "package lint"
    elif args.command == "package" and args.package_command == "wrap-stdio":
        snapshot = getattr(args, "tools_snapshot", None)
        result = run_wrap_stdio(
            Path(args.provider_dir),
            image=args.image,
            tools_snapshot=Path(snapshot) if snapshot else None,
            no_discovery=bool(args.no_discovery),
            name=args.name,
            namespace=args.namespace,
            derived_image=bool(args.derived_image),
            force=bool(args.force),
            runner=build_runner(),
        )
    elif args.command == "check":
        assert root is not None
        result = run_check(root, environ=os.environ)
    elif args.command == "catalog" and args.catalog_command == "add":
        assert root is not None
        result = add_source(
            root,
            args.url,
            build_runner(),
            name=args.name,
            ref=args.ref,
            priority=args.priority,
        )
    elif args.command == "catalog" and args.catalog_command == "set-ref":
        assert root is not None
        result = set_source_ref(root, args.name, args.ref, build_runner())
    elif args.command == "catalog" and args.catalog_command == "list":
        assert root is not None
        from keystonectl.compat import detect_keystone_mcp_version

        lock = load_lock(root / "services.lock.yaml")
        detection = detect_keystone_mcp_version(root, lock=lock, environ=os.environ, answers=sets)
        result = list_catalogs(
            root,
            build_runner(),
            keystone_version=detection.version,
            lock=lock,
        )
    elif args.command == "service" and args.service_command == "status":
        assert root is not None
        from keystonectl.generate.status import run_status

        result = run_status(root, build_runner(), name=args.name, secrets=secrets)
    elif args.command == "service" and args.service_command == "list":
        assert root is not None
        result = _service_list(root, os.environ)
    elif args.command == "service" and args.service_command in {"plan", "apply", "install"}:
        assert root is not None
        verb = "apply" if args.service_command == "install" else args.service_command
        from keystonectl.resources.binding import register_resources

        register_resources()
        from keystonectl.generate.install import ComposeDeployer

        result = run_service(
            root,
            verb,
            args.ref,
            runner=build_runner(),
            secrets=secrets,
            sets=sets,
            environ=os.environ,
            interactive=interactive,
            deployer=ComposeDeployer(),
            defer_public=bool(getattr(args, "defer_public", False)),
            certbot_staging=bool(getattr(args, "certbot_staging", False)),
        )
        if args.service_command == "install":
            result.command = "service install"
    elif args.command == "service" and args.service_command in {
        "remove",
        "upgrade",
        "rollback",
        "eject",
        "adopt",
    }:
        assert root is not None
        from datetime import UTC, datetime

        from keystonectl.lifecycle import (
            adopt_service,
            eject_service,
            remove_service,
            rollback_service,
            upgrade_service,
        )

        clock = lambda: datetime.now(UTC)
        if args.service_command == "remove":
            result = remove_service(
                root,
                args.name,
                runner=build_runner(),
                secrets=secrets,
                sets=sets,
                environ=os.environ,
                interactive=interactive,
                clock=clock,
            )
        elif args.service_command == "upgrade":
            result = upgrade_service(
                root,
                args.ref,
                runner=build_runner(),
                secrets=secrets,
                sets=sets,
                environ=os.environ,
                interactive=interactive,
                clock=clock,
            )
        elif args.service_command == "rollback":
            result = rollback_service(root, args.name, runner=build_runner(), clock=clock)
        elif args.service_command == "eject":
            result = eject_service(root, args.name, clock=clock)
        else:
            result = adopt_service(
                root,
                args.name,
                runner=build_runner(),
                sets=sets,
                environ=os.environ,
                interactive=interactive,
                clock=clock,
                json_mode=json_mode,
            )
    elif args.command == "service" and args.service_command == "add-remote":
        assert root is not None
        from datetime import UTC, datetime

        from keystonectl.remote import add_remote

        result = add_remote(
            root,
            args.name,
            args.url,
            runner=build_runner(),
            secrets=secrets,
            sets=sets,
            environ=os.environ,
            interactive=interactive,
            clock=lambda: datetime.now(UTC),
            namespace=args.namespace,
            tools=args.tools,
            tool_prefix=args.tool_prefix,
            include_prefixes=args.include_prefixes,
            exclude_prefixes=args.exclude_prefixes,
            strip_prefix=args.strip_prefix,
            no_auth=bool(args.no_auth),
            adopt_existing=bool(args.adopt_existing),
        )
    elif args.command == "bundle" and args.bundle_command in {"plan", "install"}:
        assert root is not None
        from datetime import UTC, datetime

        from keystonectl.bundles import install_bundle
        from keystonectl.compose_gen import ensure_registered
        from keystonectl.generate.install import ComposeDeployer

        ensure_registered()
        result = install_bundle(
            root,
            args.ref,
            plan_only=args.bundle_command == "plan",
            runner=build_runner(),
            secrets=secrets,
            sets=sets,
            environ=os.environ,
            interactive=interactive,
            clock=lambda: datetime.now(UTC),
            deployer=ComposeDeployer(),
            defer_public=bool(args.defer_public),
            certbot_staging=bool(args.certbot_staging),
        )
    else:
        result = _error("usage", command or "", "Unknown command.")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in args_list
    secrets = SecretRegistry()
    parser = build_parser()
    parser.json_mode = json_mode
    parser.secrets = secrets
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 1
    json_mode = json_mode or bool(args.json)
    root_arg, set_pairs, scanned_non_interactive = _scan_options(args_list)
    if root_arg:
        args.root = root_arg
    if not json_mode and not args.non_interactive and not scanned_non_interactive:
        interactive = sys.stdin.isatty()
    else:
        interactive = False
    try:
        sets = parse_sets(set_pairs)
    except AnswerError as exc:
        result = Result(
            code=exc.code,
            command=_command_name(args) if args.command else "",
            summary=exc.message,
            error={"id": exc.error_id, "message": exc.message, "hint": exc.hint},
            retryable=exc.retryable,
        )
        emit(result, json_mode=json_mode, secrets=secrets)
        return result.code
    if args.version and not args.command:
        result = _version_result()
        emit(result, json_mode=json_mode, secrets=secrets)
        return 0
    if not args.command:
        parser.json_mode = json_mode
        parser.error("a command is required")
    command = _command_name(args)
    if _needs_root(args):
        root = find_root(args.root, os.environ, Path.cwd(), required=True)
        if root is None:
            result = _error(
                "root_not_found", command, f"{ERRORS['root_not_found'].title} {ROOT_HINT}"
            )
            emit(result, json_mode=json_mode, secrets=secrets)
            return result.code
    else:
        root = find_root(args.root, os.environ, Path.cwd(), required=False)

    state_changing, lock_root = _lock_target(args, root)
    try:
        if state_changing and lock_root is not None:
            try:
                with hold_lock(lock_root / "services.lock.yaml"):
                    result = _dispatch(
                        args,
                        root,
                        command,
                        interactive=interactive,
                        json_mode=json_mode,
                        sets=sets,
                        secrets=secrets,
                    )
            except LockError as exc:
                result = Result(
                    code=exc.code,
                    command=command,
                    summary=exc.message,
                    error={"id": exc.error_id, "message": exc.message, "hint": exc.hint},
                    retryable=exc.retryable,
                )
        else:
            result = _dispatch(
                args,
                root,
                command,
                interactive=interactive,
                json_mode=json_mode,
                sets=sets,
                secrets=secrets,
            )
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort catch-all, reported as JSON
        result = _internal_error(command, exc)
    if (
        not json_mode
        and result.command == "lint"
        or (not json_mode and result.command == "package lint")
    ):
        text = result.result.get("text")
        if isinstance(text, str):
            sys.stdout.write(
                secrets.redact(render_text(result.result.get("problems") or []) + "\n")
            )
            return result.code
    emit(result, json_mode=json_mode, secrets=secrets)
    return result.code


if __name__ == "__main__":
    sys.exit(main())
