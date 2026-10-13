"""Public paths: loopback port, nginx vhost, and the certificate steps around deploy."""

from __future__ import annotations

import argparse
import re
import socket
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx

from keystonectl.answers import Question, pkg_var
from keystonectl.envfiles import read_env, upsert_env
from keystonectl.errors import ERRORS, WARNINGS
from keystonectl.plan import Context, StepPlan
from keystonectl.sources import warning
from keystonectl.tls import Tls, TlsError

PLACEHOLDER_URL = "https://public-domain-pending.invalid"
PORT_MIN = 47600
PORT_MAX = 47699
_DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_TOKEN = re.compile(r"__[A-Z_]+__")
_SERVER_NAME = re.compile(r"^\s*server_name\s+(.+);")


def public_paths(manifest: Mapping[str, Any]) -> list[str]:
    raw = manifest.get("public_paths")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item]


def healthz_path(manifest: Mapping[str, Any]) -> str:
    mcp = manifest.get("mcp") if isinstance(manifest.get("mcp"), dict) else {}
    value = mcp.get("healthz") if isinstance(mcp, dict) else None
    if isinstance(value, str) and value.startswith("/"):
        return value
    return "/healthz"


def upstream_name(package: str) -> str:
    return "keystone_svc_" + package.replace("-", "_")


def site_name(package: str) -> str:
    return f"keystone-svc-{package}"


def domain_error(value: str) -> str | None:
    if value != value.lower() or not _DOMAIN.fullmatch(value):
        return "domain_invalid"
    return None


def location_block(path: str) -> str:
    return "\n".join(
        [
            f"    location ^~ {path} {{",
            "        proxy_pass http://__UPSTREAM__;",
            "        proxy_http_version 1.1;",
            "        proxy_set_header Host              $host;",
            "        proxy_set_header X-Real-IP         $remote_addr;",
            "        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;",
            "        proxy_set_header X-Forwarded-Proto $scheme;",
            "        proxy_set_header X-Forwarded-Host  $host;",
            "        client_max_body_size 100m;",
            "        proxy_request_buffering off;",
            "        proxy_buffering off;",
            "        proxy_read_timeout 300s;",
            "        proxy_send_timeout 300s;",
            "        access_log off;          # the token in the path is a secret",
            "    }",
        ]
    )


def render_vhost(kind: str, substitutions: Mapping[str, str]) -> str:
    """Load the package template and replace tokens. ``kind`` is ``http`` or ``https``."""

    filename = "service.conf.template" if kind == "http" else "service-https.conf.template"
    template = files("keystonectl.nginx").joinpath(filename).read_text(encoding="utf-8")
    text = template.replace("__LOCATIONS__", substitutions.get("LOCATIONS", ""))
    for key, value in substitutions.items():
        if key == "LOCATIONS":
            continue
        text = text.replace(f"__{key}__", value)
    leftover = _TOKEN.findall(text)
    if leftover:
        raise RuntimeError(f"unreplaced template tokens: {', '.join(leftover)}")
    if not text.endswith("\n"):
        text += "\n"
    return text


def vhost_substitutions(
    manifest: Mapping[str, Any],
    *,
    domain: str,
    host_port: int,
    cert_path: str,
    key_path: str,
) -> dict[str, str]:
    paths = public_paths(manifest)
    locations = "\n".join(location_block(path) for path in paths)
    if locations:
        locations += "\n"
    return {
        "DOMAIN": domain,
        "UPSTREAM": upstream_name(str(manifest["name"])),
        "HOST_PORT": str(host_port),
        "LOCATIONS": locations,
        "HEALTHZ_PATH": healthz_path(manifest),
        "CERT_PATH": cert_path,
        "KEY_PATH": key_path,
    }


def _lookup(ctx: Context, package: str, suffix: str) -> str | None:
    var = pkg_var(package, suffix) if suffix != "TLS_EMAIL" else "KEYSTONE_SETUP_TLS_EMAIL"
    if suffix == "TLS_EMAIL":
        var = "KEYSTONE_SETUP_TLS_EMAIL"
    if var in ctx.sets and ctx.sets[var] != "":
        return ctx.sets[var]
    if ctx.environ.get(var):
        return str(ctx.environ[var])
    from keystonectl.resources.binding import _service

    current = _service(ctx.lock, package)
    answers = current.get("answers") if isinstance(current, dict) else None
    if isinstance(answers, dict) and answers.get(var) not in {None, ""}:
        return str(answers[var])
    public = current.get("public") if isinstance(current, dict) else None
    if isinstance(public, dict):
        field = {
            "DOMAIN": "domain",
            "DNS": "dns",
            "TLS": "tls",
        }.get(suffix)
        if field and public.get(field) not in {None, ""}:
            value = str(public[field])
            if suffix == "DNS" and value == "checked":
                return "check"
            if suffix == "DNS" and value == "skipped":
                return "skip"
            return value
    return None


def _remember(ctx: Context, package: str, suffix: str, value: str) -> None:
    from keystonectl.resources.binding import _service

    var = "KEYSTONE_SETUP_TLS_EMAIL" if suffix == "TLS_EMAIL" else pkg_var(package, suffix)
    current = _service(ctx.lock, package)
    if not isinstance(current, dict):
        return
    answers = current.setdefault("answers", {})
    if isinstance(answers, dict):
        answers[var] = value


def _tls_choices(ctx: Context, package: str) -> tuple[str, ...]:
    """``keep`` is offered only when a certificate is already on disk and not expired."""

    choices = ["certbot", "custom", "manual"]
    domain = _lookup(ctx, package, "DOMAIN") or ""
    if domain and domain_error(domain) is None:
        info = _tls(ctx).detect(domain)
        if info.kind != "none" and info.days_left is not None and info.days_left >= 0:
            choices.insert(0, "keep")
    return tuple(choices)


def install_questions(ctx: Context, manifest: Mapping[str, Any]) -> list[Question]:
    paths = public_paths(manifest)
    if not paths or ctx.defer_public:
        return []
    name = str(manifest["name"])
    if not _lookup(ctx, name, "DOMAIN"):
        return [
            Question(
                pkg_var(name, "DOMAIN"),
                f"Public domain for {name} (lowercase, at least two labels).",
                "text",
            )
        ]
    questions: list[Question] = []
    if not _lookup(ctx, name, "TLS"):
        questions.append(
            Question(
                pkg_var(name, "TLS"),
                f"How should {name} get a certificate?",
                "choice",
                choices=_tls_choices(ctx, name),
            )
        )
        return questions
    tls_mode = _lookup(ctx, name, "TLS")
    if tls_mode == "certbot" and not _lookup(ctx, name, "TLS_EMAIL"):
        questions.append(
            Question(
                "KEYSTONE_SETUP_TLS_EMAIL",
                "Email address for Let's Encrypt.",
                "text",
            )
        )
    if tls_mode == "custom":
        if not _lookup(ctx, name, "CERT_PATH"):
            questions.append(
                Question(
                    pkg_var(name, "CERT_PATH"),
                    f"Path to the PEM certificate for {name}.",
                    "text",
                )
            )
        if not _lookup(ctx, name, "KEY_PATH"):
            questions.append(
                Question(
                    pkg_var(name, "KEY_PATH"),
                    f"Path to the PEM private key for {name}.",
                    "text",
                )
            )
    return questions


def deferred_env(ctx: Context, manifest: Mapping[str, Any]) -> dict[str, str]:
    if not ctx.defer_public or not public_paths(manifest):
        return {}
    from keystonectl.resources.binding import _service

    current = _service(ctx.lock, str(manifest["name"]))
    public = current.get("public") if isinstance(current, dict) else None
    if isinstance(public, dict) and public.get("state") == "ready" and public.get("domain"):
        return {}
    name = manifest.get("public_base_url_env")
    if not isinstance(name, str) or not name:
        return {}
    return {name: PLACEHOLDER_URL}


def ready_domains(ctx: Context, *, extra: str | None = None) -> list[str]:
    found: list[str] = []
    for item in ctx.lock.get("services") or []:
        if not isinstance(item, dict):
            continue
        public = item.get("public") if isinstance(item.get("public"), dict) else {}
        if public.get("state") == "ready" and isinstance(public.get("domain"), str):
            domain = str(public["domain"])
            if domain not in found:
                found.append(domain)
    if extra and extra not in found:
        found.append(extra)
    return found


def choose_port(ctx: Context, package: str) -> int:
    from keystonectl.resources.binding import _service

    current = _service(ctx.lock, package)
    public = current.get("public") if isinstance(current, dict) else None
    if isinstance(public, dict) and isinstance(public.get("host_port"), int):
        return int(public["host_port"])
    used: set[int] = set()
    for item in ctx.lock.get("services") or []:
        if not isinstance(item, dict) or item.get("name") == package:
            continue
        other = item.get("public") if isinstance(item.get("public"), dict) else {}
        if isinstance(other.get("host_port"), int):
            used.add(int(other["host_port"]))
    for port in range(PORT_MIN, PORT_MAX + 1):
        if port in used:
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", port))
        except OSError:
            continue
        return port
    raise TlsError("port_range_exhausted", ERRORS["port_range_exhausted"].title)


def fragment_ports(ctx: Context, manifest: Mapping[str, Any]) -> tuple[int | None, tuple[str, ...]]:
    if not public_paths(manifest) or ctx.defer_public:
        return None, tuple(ready_domains(ctx))
    name = str(manifest["name"])
    domain = _lookup(ctx, name, "DOMAIN")
    if not domain:
        return None, tuple(ready_domains(ctx))
    try:
        port = choose_port(ctx, name)
    except TlsError as exc:
        _fail(ctx, exc.error_id, exc.message, hint=exc.hint)
        return None, ()
    ctx.chosen_port = port  # type: ignore[attr-defined]
    return port, tuple(ready_domains(ctx))


def stamp_lock_public(ctx: Context, manifest: Mapping[str, Any], entry: dict[str, Any]) -> None:
    if not public_paths(manifest):
        return
    public = entry.get("public") if isinstance(entry.get("public"), dict) else {}
    public = dict(public)
    if ctx.defer_public and public.get("state") != "ready":
        public["state"] = "pending"
        public.setdefault("last_error", None)
        entry["public"] = public
        return
    port = getattr(ctx, "chosen_port", None)
    if isinstance(port, int):
        public["host_port"] = port
    domain = _lookup(ctx, str(manifest["name"]), "DOMAIN")
    if domain:
        problem = domain_error(domain)
        if problem is None:
            public["domain"] = domain
    if public.get("state") not in {"ready", "pending"}:
        public["state"] = "pending"
        public.setdefault("last_error", None)
    if public:
        entry["public"] = public


def _fail(
    ctx: Context,
    error_id: str,
    message: str,
    *,
    hint: str | None = None,
    result: dict | None = None,
) -> None:
    from keystonectl.generate.install import _fail as install_fail

    install_fail(ctx, error_id, message, hint=hint, result=result)


def _service(ctx: Context, name: str) -> dict[str, Any] | None:
    from keystonectl.resources.binding import _service as lookup

    return lookup(ctx.lock, name)


def _tls(ctx: Context) -> Tls:
    kwargs: dict[str, Any] = {"clock": ctx.clock, "redact": ctx.secrets.redact}
    http = getattr(ctx, "http", None)
    if http is not None:
        kwargs["http"] = http
    if getattr(ctx, "le_live", None) is not None:
        kwargs["le_live"] = Path(ctx.le_live)
    if getattr(ctx, "custom_dir", None) is not None:
        kwargs["custom_dir"] = Path(ctx.custom_dir)
    if getattr(ctx, "cron_file", None) is not None:
        kwargs["cron_file"] = Path(ctx.cron_file)
    handshake = getattr(ctx, "handshake", None)
    if handshake is not None:
        kwargs["handshake"] = handshake
    return Tls(ctx.runner, **kwargs)


def _nginx_dirs(ctx: Context) -> tuple[Path, Path, Path]:
    root = getattr(ctx, "nginx_root", None)
    base = Path(root) if root else Path("/etc/nginx")
    return base / "sites-available", base / "sites-enabled", base / "conf.d"


def _cache_vhost(ctx: Context, name: str) -> Path:
    return ctx.root / ".keystonectl" / "nginx" / f"{site_name(name)}.conf"


def _action(ctx: Context, action_id: str, title: str, status: str) -> None:
    ctx.action_log.append({"id": action_id, "title": title, "status": status})


def _write_public(ctx: Context, name: str, **fields: Any) -> None:
    from keystonectl.lock import save_lock

    current = _service(ctx, name)
    if not isinstance(current, dict):
        return
    public = current.get("public") if isinstance(current.get("public"), dict) else {}
    merged = dict(public)
    for key, value in fields.items():
        if value is None and key not in {"took_over", "last_error"}:
            merged.pop(key, None)
        else:
            merged[key] = value
    merged.setdefault("state", "pending")
    current["public"] = merged
    ctx.lock["updated_by"] = ctx.lock.get("updated_by") or "keystonectl"
    save_lock(ctx.root / "services.lock.yaml", ctx.lock, now=ctx.clock())


def _domain_in_use(ctx: Context, package: str, domain: str) -> bool:
    for item in ctx.lock.get("services") or []:
        if not isinstance(item, dict) or item.get("name") == package:
            continue
        public = item.get("public") if isinstance(item.get("public"), dict) else {}
        if public.get("domain") == domain:
            return True
    return False


def _line_has_domain(text: str, domain: str) -> bool:
    for line in text.splitlines():
        match = _SERVER_NAME.match(line)
        if not match:
            continue
        names = match.group(1).split()
        if domain in names:
            return True
    return False


def _foreign_sites(ctx: Context, package: str, domain: str) -> list[tuple[Path, str]]:
    _available, enabled, conf_d = _nginx_dirs(ctx)
    ours = site_name(package)
    found: list[tuple[Path, str]] = []
    for directory in (enabled, conf_d):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.name.startswith(ours):
                continue
            if path.name.startswith("."):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not _line_has_domain(text, domain):
                continue
            if directory == conf_d or not path.is_symlink():
                kind = "file"
            else:
                kind = "link"
            found.append((path, kind))
    return found


def _tee(tls: Tls, path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    result = tls._run(["tee", str(path)], input=content)
    return result.returncode == 0


def _nginx_test(tls: Tls) -> tuple[bool, str]:
    result = tls._run(["nginx", "-t"])
    text = (result.stderr or result.stdout).strip()
    return result.returncode == 0, text


def _reload(tls: Tls) -> None:
    result = tls._run(["systemctl", "reload", "nginx"])
    if result.returncode != 0:
        tls._run(["nginx", "-s", "reload"])


def _write_site(ctx: Context, tls: Tls, package: str, content: str) -> str | None:
    available, enabled, _conf = _nginx_dirs(ctx)
    path = available / site_name(package)
    previous = path.read_text(encoding="utf-8") if path.is_file() else None
    if not _tee(tls, path, content):
        return "could not write the nginx site"
    link = enabled / site_name(package)
    if not link.exists() and not link.is_symlink():
        linked = tls._run(["ln", "-s", str(path), str(link)])
        if linked.returncode != 0:
            return linked.stderr or "could not enable the nginx site"
    ok, detail = _nginx_test(tls)
    if ok:
        _reload(tls)
        return None
    if previous is None:
        tls._run(["rm", "-f", str(path)])
        if link.is_symlink():
            tls._run(["rm", "-f", str(link)])
    else:
        _tee(tls, path, previous)
    _fail(
        ctx,
        "nginx_config_invalid",
        detail or ERRORS["nginx_config_invalid"].title,
        hint=ERRORS["nginx_config_invalid"].action,
    )
    return detail


def _privileges(ctx: Context, tls: Tls, package: str, manifest: Mapping[str, Any]) -> bool:
    if tls._root():
        nginx = tls._run(["nginx", "-v"])
        if nginx.returncode != 0:
            _fail(
                ctx,
                "nginx_missing",
                ERRORS["nginx_missing"].title,
                hint="install nginx or answer TLS=manual",
            )
            return False
        return True
    probe = tls._run(["true"])
    if probe.returncode != 0:
        _manual_steps(ctx, package, manifest, tls_mode=_lookup(ctx, package, "TLS") or "certbot")
        _fail(
            ctx,
            "sudo_required",
            ERRORS["sudo_required"].title,
            hint="or answer TLS=manual",
            result={"manual_steps": ctx.output_result.get("manual_steps", [])},
        )
        return False
    nginx = tls._run(["nginx", "-v"])
    if nginx.returncode != 0:
        _fail(
            ctx,
            "nginx_missing",
            ERRORS["nginx_missing"].title,
            hint="install nginx or answer TLS=manual",
        )
        return False
    return True


def _manual_steps(
    ctx: Context, package: str, manifest: Mapping[str, Any], *, tls_mode: str
) -> None:
    sample = _cache_vhost(ctx, package)
    available, enabled, _conf = _nginx_dirs(ctx)
    site = site_name(package)
    steps = [
        f"install {sample} {available / site}",
        f"ln -s {available / site} {enabled / site}",
        "nginx -t",
        "systemctl reload nginx",
    ]
    if tls_mode == "certbot":
        steps.append("certbot --nginx -d <domain> --redirect --non-interactive --agree-tos")
    else:
        steps.append("install your certificate and key, then reload nginx")
    steps.append("or answer TLS=manual")
    ctx.output_result["manual_steps"] = steps


def _sample(
    ctx: Context, manifest: Mapping[str, Any], domain: str, port: int, cert: str, key: str
) -> Path:
    path = _cache_vhost(ctx, str(manifest["name"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render_vhost(
        "https",
        vhost_substitutions(manifest, domain=domain, host_port=port, cert_path=cert, key_path=key),
    )
    path.write_text(text, encoding="utf-8")
    return path


def _takeover(ctx: Context, package: str, domain: str) -> bool:
    foreign = _foreign_sites(ctx, package, domain)
    if not foreign:
        return True
    path, kind = foreign[0]
    answer = _lookup(ctx, package, "TAKEOVER")
    if answer is None:
        ctx.halt_questions.append(
            Question(
                pkg_var(package, "TAKEOVER"),
                f"{domain} is already served by {path.name}. Take it over?",
                "choice",
                choices=("yes", "no"),
            )
        )
        return False
    if answer != "yes":
        _fail(ctx, "domain_in_use", f"{domain} is already served by {path.name}")
        return False
    if kind != "link":
        _fail(
            ctx,
            "domain_in_use",
            f"{path} is a regular file. Remove it by hand before keystonectl can use {domain}.",
        )
        return False
    target = str(path.resolve()) if path.is_symlink() else ""
    removed = _tls(ctx)._run(["rm", str(path)])
    if removed.returncode != 0:
        _fail(ctx, "domain_in_use", removed.stderr or f"could not disable {path.name}")
        return False
    _remember(ctx, package, "TAKEOVER", "yes")
    if target:
        _remember(ctx, package, "TOOK_OVER_TARGET", target)
    _write_public(ctx, package, took_over=path.name)
    return True


def _cert_paths(
    ctx: Context, tls: Tls, package: str, domain: str, mode: str
) -> tuple[str, str] | None:
    if mode == "custom":
        info = tls.install_custom(
            domain,
            Path(_lookup(ctx, package, "CERT_PATH") or ""),
            Path(_lookup(ctx, package, "KEY_PATH") or ""),
        )
    elif mode == "keep":
        info = tls.detect(domain)
        if info.kind == "none" or not info.cert_path or not info.key_path:
            _fail(ctx, "cert_unreadable", "no certificate was found to keep")
            return None
    else:
        email = _lookup(ctx, package, "TLS_EMAIL") or ""
        current = tls.detect(domain)
        if (
            mode == "certbot"
            and current.kind == "letsencrypt"
            and current.days_left is not None
            and current.days_left > 30
        ):
            info = current
        else:
            info = tls.issue_certbot(domain, email, staging=bool(ctx.certbot_staging))
    if not info.cert_path or not info.key_path:
        _fail(ctx, "cert_unreadable", "certificate paths are missing")
        return None
    ctx.cert_info = info  # type: ignore[attr-defined]
    return info.cert_path, info.key_path


def _base_url(ctx: Context, manifest: Mapping[str, Any], domain: str) -> bool:
    from keystonectl.generate.install import _start

    env_name = manifest.get("public_base_url_env")
    if not isinstance(env_name, str):
        return True
    from keystonectl.generate.placement import placement

    path = ctx.root / placement(manifest).shared
    before = read_env(path).get(env_name)
    url = f"https://{domain}"
    upsert_env(path, {env_name: url})
    if before == url:
        return True
    project = _project(ctx)
    if project is None:
        return False
    _start(ctx, manifest, recreate=True)
    return ctx.failure is None


def _project(ctx: Context) -> str | None:
    from keystonectl.generate.docker import compose_config, parse_config
    from keystonectl.generate.install import _layout

    layout = _layout(ctx)
    config = parse_config(compose_config(ctx.runner, ctx.root, layout))
    project = str((config or {}).get("name") or layout.get("project") or "")
    return project or None


def _rewrite_extra_hosts(ctx: Context, manifest: Mapping[str, Any], domain: str) -> None:
    from keystonectl.generate.compose import fragment_digest, fragment_guard, render_fragment
    from keystonectl.generate.install import _env_values, _start

    domains = ready_domains(ctx, extra=domain)
    line = ", ".join(f"{item}:host-gateway" for item in domains)
    hint = f'extra_hosts: ["{line}"]' if len(domains) == 1 else f"extra_hosts: [{line}]"
    for item in ctx.lock.get("services") or []:
        if not isinstance(item, dict) or item.get("managed") == "remote":
            continue
        name = str(item.get("name") or "")
        path = ctx.root / "packages" / f"{name}.yml"
        document = manifest if name == manifest.get("name") else None
        if document is None and ctx.resolved is not None and name == ctx.resolved.name:
            document = manifest
        if (
            document is None
            and isinstance(item.get("source"), str)
            and isinstance(item.get("version"), str)
        ):
            from keystonectl.sources import package_cache

            cached = (
                package_cache(ctx.root, str(item["source"]), name, str(item["version"]))
                / "keystone-service.yaml"
            )
            if cached.is_file():
                import yaml

                loaded = yaml.safe_load(cached.read_text(encoding="utf-8"))
                document = loaded if isinstance(loaded, dict) else None
        if not isinstance(document, dict):
            ctx.extra_warnings.append(warning("extra_hosts_manual", f"add this to {name}: {hint}"))
            continue
        guarded = fragment_guard(item, path)
        if guarded:
            ctx.extra_warnings.append(
                warning("extra_hosts_manual", f"add this to packages/{name}.yml: {hint}")
            )
            continue
        port = None
        public = item.get("public") if isinstance(item.get("public"), dict) else {}
        if public_paths(document) and isinstance(public.get("host_port"), int):
            port = int(public["host_port"])
        if name == manifest.get("name") and isinstance(getattr(ctx, "chosen_port", None), int):
            port = int(ctx.chosen_port)
        text = render_fragment(
            document,
            {"env_values": _env_values(ctx, document)},
            host_port=port,
            extra_hosts=domains,
        )
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        item["fragment_sha256"] = fragment_digest(text)
        if name == manifest.get("name"):
            _start(ctx, document, recreate=True)


def _external_check(ctx: Context, tls: Tls, domain: str, healthz: str, *, manual: bool) -> bool:
    url = f"https://{domain}{healthz}"
    try:
        response = tls.http.get(url, timeout=5.0)
        status = response.status_code
    except (httpx.HTTPError, OSError) as exc:
        status = 0
        detail = str(exc)
    else:
        detail = f"HTTP {status}"
    if status == 200:
        return True
    if manual:
        _fail(ctx, "public_unreachable", detail or ERRORS["public_unreachable"].title)
        return False
    ctx.extra_warnings.append(
        warning("public_check_failed", f"{url} did not return 200 ({detail})")
    )
    return True


def publish(ctx: Context) -> None:
    manifest = ctx.manifest if isinstance(ctx.manifest, dict) else None
    if manifest is None or not public_paths(manifest) or ctx.failure is not None:
        return
    name = str(manifest["name"])
    if ctx.defer_public:
        _action(ctx, "public.defer", f"Leave the public address of {name} pending", "done")
        return
    questions = install_questions(ctx, manifest)
    if questions:
        ctx.halt_questions.extend(questions)
        return
    domain = _lookup(ctx, name, "DOMAIN") or ""
    if domain_error(domain):
        _fail(ctx, "domain_invalid", f"{domain!r} is not a lowercase hostname with two labels")
        return
    if _domain_in_use(ctx, name, domain):
        _fail(ctx, "domain_in_use", f"{domain} is already used by another service")
        return
    for suffix in ("DOMAIN", "TLS", "DNS", "TLS_EMAIL", "CERT_PATH", "KEY_PATH"):
        value = _lookup(ctx, name, suffix)
        if value:
            _remember(ctx, name, suffix, value)
    mode = _lookup(ctx, name, "TLS") or "certbot"
    dns_mode = _lookup(ctx, name, "DNS") or "check"
    tls = _tls(ctx)
    port = getattr(ctx, "chosen_port", None)
    if not isinstance(port, int):
        try:
            port = choose_port(ctx, name)
        except TlsError as exc:
            _fail(ctx, exc.error_id, exc.message, hint=exc.hint)
            return
        ctx.chosen_port = port
    _action(ctx, "service.deploy", f"Start {name}", "done")
    if mode != "manual" and dns_mode != "skip":
        _action(ctx, "public.dns", f"Check that {domain} points to this host", "planned")
        try:
            tls.check_dns(domain)
        except TlsError as exc:
            _action(ctx, "public.dns", f"Check that {domain} points to this host", "failed")
            _action(ctx, "public.tls", f"Get a certificate for {domain} ({mode})", "skipped")
            _write_public(
                ctx,
                name,
                state="pending",
                domain=domain,
                host_port=port,
                dns="checked",
                tls=mode,
                last_error=exc.error_id,
            )
            _fail(
                ctx, exc.error_id, exc.message, hint=exc.hint, result={"public": _view(ctx, name)}
            )
            return
        _action(ctx, "public.dns", f"Check that {domain} points to this host", "done")
    if mode != "manual":
        if not _privileges(ctx, tls, name, manifest):
            _write_public(
                ctx,
                name,
                state="pending",
                domain=domain,
                host_port=port,
                dns="skipped" if dns_mode == "skip" else "checked",
                tls=mode,
                last_error=(ctx.failure.error_id if ctx.failure else "sudo_required"),
            )
            if ctx.failure is not None:
                ctx.failure.result = {**ctx.failure.result, "public": _view(ctx, name)}
            return
        if not _takeover(ctx, name, domain):
            if ctx.halt_questions:
                _write_public(
                    ctx, name, state="pending", domain=domain, host_port=port, last_error=None
                )
            return
        http = render_vhost(
            "http",
            vhost_substitutions(
                manifest,
                domain=domain,
                host_port=port,
                cert_path="",
                key_path="",
            ),
        )
        if _write_site(ctx, tls, name, http):
            _write_public(
                ctx,
                name,
                state="pending",
                domain=domain,
                host_port=port,
                last_error="nginx_config_invalid",
            )
            return
        _action(ctx, "public.tls", f"Get a certificate for {domain} ({mode})", "planned")
        try:
            paths = _cert_paths(ctx, tls, name, domain, mode)
        except TlsError as exc:
            _action(ctx, "public.tls", f"Get a certificate for {domain} ({mode})", "failed")
            for item in tls.warnings:
                ctx.extra_warnings.append(item)
            _write_public(
                ctx,
                name,
                state="pending",
                domain=domain,
                host_port=port,
                dns="skipped" if dns_mode == "skip" else "checked",
                tls=mode,
                vhost=str(_nginx_dirs(ctx)[0] / site_name(name)),
                last_error=exc.error_id,
            )
            _fail(
                ctx, exc.error_id, exc.message, hint=exc.hint, result={"public": _view(ctx, name)}
            )
            return
        if paths is None or ctx.failure is not None:
            return
        cert_path, key_path = paths
        https = render_vhost(
            "https",
            vhost_substitutions(
                manifest,
                domain=domain,
                host_port=port,
                cert_path=cert_path,
                key_path=key_path,
            ),
        )
        if _write_site(ctx, tls, name, https):
            _write_public(
                ctx, name, state="pending", domain=domain, last_error="nginx_config_invalid"
            )
            return
        renewal = tls.renewal_check(domain)
        for item in tls.warnings:
            ctx.extra_warnings.append(item)
        info = getattr(ctx, "cert_info", None)
        not_after = None
        if info is not None and info.not_after is not None:
            not_after = info.not_after.astimezone().strftime("%Y-%m-%dT%H:%M:%SZ")
            from datetime import UTC

            not_after = info.not_after.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _action(ctx, "public.tls", f"Get a certificate for {domain} ({mode})", "done")
        if not _base_url(ctx, manifest, domain):
            return
        _rewrite_extra_hosts(ctx, manifest, domain)
        if not _external_check(ctx, tls, domain, healthz_path(manifest), manual=False):
            return
        _write_public(
            ctx,
            name,
            state="ready",
            domain=domain,
            host_port=port,
            dns="skipped" if dns_mode == "skip" else "checked",
            tls=mode,
            cert_kind=info.kind if info is not None else None,
            cert_path=cert_path,
            key_path=key_path,
            not_after=not_after,
            vhost=str(_nginx_dirs(ctx)[0] / site_name(name)),
            last_error=None,
        )
        ctx.output_result["public"] = _view(ctx, name)
        if renewal.mode == "missing":
            ctx.extra_warnings.append(
                warning("renewal_timer_missing", WARNINGS["renewal_timer_missing"].title)
            )
        return
    cert_guess = f"/etc/ssl/keystone/{domain}/fullchain.pem"
    key_guess = f"/etc/ssl/keystone/{domain}/privkey.pem"
    sample = _sample(ctx, manifest, domain, port, cert_guess, key_guess)
    ctx.output_result["manual_steps"] = [str(sample)]
    _action(ctx, "public.tls", f"Operator proxy for {domain}", "done")
    if not _base_url(ctx, manifest, domain):
        return
    _rewrite_extra_hosts(ctx, manifest, domain)
    if not _external_check(ctx, tls, domain, healthz_path(manifest), manual=True):
        _write_public(
            ctx,
            name,
            state="pending",
            domain=domain,
            host_port=port,
            dns="skipped" if dns_mode == "skip" else "checked",
            tls="manual",
            vhost=str(sample),
            last_error="public_unreachable",
        )
        if ctx.failure is not None:
            ctx.failure.result = {**ctx.failure.result, "public": _view(ctx, name)}
        return
    remote = tls.remote_cert(domain)
    not_after = None
    if remote.not_after is not None:
        from datetime import UTC

        not_after = remote.not_after.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_public(
        ctx,
        name,
        state="ready",
        domain=domain,
        host_port=port,
        dns="skipped" if dns_mode == "skip" else "checked",
        tls="manual",
        cert_kind="custom",
        not_after=not_after,
        vhost=str(sample),
        last_error=None,
    )
    ctx.output_result["public"] = _view(ctx, name)


def _view(ctx: Context, name: str) -> dict[str, Any]:
    current = _service(ctx, name)
    public = current.get("public") if isinstance(current, dict) else {}
    if not isinstance(public, dict):
        return {}
    return {
        "state": public.get("state"),
        "domain": public.get("domain"),
        "last_error": public.get("last_error"),
    }


class PublicStep:
    """Runs after deploy. A missing domain is a question, not a failed install."""

    id = "public"
    is_service_deploy = False

    def plan(self, ctx: Context) -> StepPlan:
        manifest = ctx.manifest if isinstance(ctx.manifest, dict) else None
        if manifest is None or not public_paths(manifest):
            return StepPlan()
        name = str(manifest["name"])
        if ctx.defer_public:
            return StepPlan(
                actions=[
                    {
                        "id": "public.defer",
                        "title": f"Leave the public address of {name} pending",
                        "status": "planned",
                    }
                ]
            )
        return StepPlan(
            questions=install_questions(ctx, manifest),
            actions=[
                {
                    "id": "public.publish",
                    "title": f"Publish {name}",
                    "status": "planned",
                }
            ],
        )

    def apply(self, ctx: Context) -> None:
        publish(ctx)


def ensure_public() -> None:
    from keystonectl.plan import _EXTRAS, register_step

    if any(getattr(step, "id", None) == "public" for step, _after in _EXTRAS):
        return
    register_step(PublicStep(), after="deploy")


def register_commands(subparsers: argparse._SubParsersAction) -> None:
    ensure_public()
    for name in ("apply", "install"):
        parser = subparsers.choices.get(name)
        if parser is None:
            continue
        parser.add_argument("--defer-public", action="store_true")
        parser.add_argument("--certbot-staging", action="store_true")


def status_public(ctx: Context, entry: Mapping[str, Any], body: dict[str, Any]) -> None:
    """Add certificate warnings. The inside-network probe stays in status.py."""

    public = entry.get("public") if isinstance(entry.get("public"), dict) else None
    if not public or public.get("state") in {None, "none"}:
        return
    state = str(public.get("state") or "")
    shown: dict[str, Any] = {
        "state": state,
        "domain": public.get("domain"),
        "tls": public.get("tls"),
    }
    if isinstance(body.get("public"), dict):
        shown.update(body["public"])
    body["public"] = shown
    if state == "pending":
        last = public.get("last_error")
        message = WARNINGS["public_pending"].title
        if isinstance(last, str) and last:
            message = f"{message} Last error: {last}."
        body["warnings"].append({"id": "public_pending", "message": message})
        return
    if state != "ready":
        return
    domain = str(public.get("domain") or "")
    if not domain:
        return
    tls = _tls(ctx)
    mode = str(public.get("tls") or "")
    try:
        if mode == "manual":
            info = tls.remote_cert(domain)
            renewal_mode = "operator"
        else:
            info = tls.detect(domain)
            renewal_mode = tls.renewal_check(domain).mode
    except TlsError as exc:
        body["warnings"].append({"id": "public_pending", "message": exc.message})
        return
    if info.not_after is not None:
        from datetime import UTC

        shown["not_after"] = info.not_after.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if info.days_left is not None:
        letsencrypt = info.kind == "letsencrypt" or mode == "certbot"
        limit = 20 if letsencrypt else 30
        if info.days_left <= limit:
            body["warnings"].append(
                {
                    "id": "cert_expiring",
                    "message": f"{domain} expires in {info.days_left} days",
                }
            )
    if renewal_mode == "missing":
        body["warnings"].append(
            {"id": "renewal_timer_missing", "message": WARNINGS["renewal_timer_missing"].title}
        )
    if renewal_mode == "operator":
        body["warnings"].append(
            {"id": "renewal_manual", "message": WARNINGS["renewal_manual"].action}
        )
    for item in tls.warnings:
        if item["id"] not in {warning["id"] for warning in body["warnings"]}:
            body["warnings"].append(item)
