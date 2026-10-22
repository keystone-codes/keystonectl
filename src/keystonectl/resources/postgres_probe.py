"""Postgres/v1 resource probe using psycopg."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import psycopg
import yaml
from psycopg import sql

PARAM_NAMES = ("host", "port", "database", "user", "password", "sslmode")
ENV_PREFIX = "KEYSTONE_POSTGRES_"
MIN_SERVER_VERSION_NUM = 140000
PROBE_TABLE_PREFIX = "keystone_probe_"

_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_IPV6_HOST_RE = re.compile(r"^\[[0-9a-fA-F:]+\]$")
_PORT_RE = re.compile(r"^[0-9]{1,5}$")
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_PASSWORD_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,128}$")
_SSLMODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"},
)
_INSECURE_SSLMODES = frozenset({"disable", "allow", "prefer"})
_PROBE_ID_RE = re.compile(r"^[a-z0-9]+$")

logger = logging.getLogger(__name__)

ConnectFactory = Callable[..., Any]


class ParamsError(ValueError):
    """Missing or invalid parameters before probe."""

    def __init__(self, *, missing: list[str] | None = None, message: str = "") -> None:
        self.missing = missing or []
        super().__init__(message or f"missing parameters: {', '.join(self.missing)}")


@dataclass(frozen=True)
class PgParams:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    sslmode: str = "prefer"


@dataclass
class ProbeError:
    code: str
    message: str
    retryable: bool = False
    missing_extensions: list[str] | None = None


@dataclass
class ProbeReport:
    ok: bool
    exit_code: int
    host: str
    port: int
    database: str
    user: str
    sslmode: str
    server_version: str
    checks: dict[str, str]
    warnings: list[str]
    error: ProbeError | None
    missing: list[str]
    checked_via: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
            "sslmode": self.sslmode,
            "server_version": self.server_version,
            "checks": self.checks,
            "warnings": self.warnings,
            "error": None if self.error is None else _error_to_dict(self.error),
            "missing": self.missing,
        }
        if self.checked_via is not None:
            data["checked_via"] = self.checked_via
        return data


def _error_to_dict(error: ProbeError) -> dict[str, Any]:
    out: dict[str, Any] = {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
    }
    if error.missing_extensions is not None:
        out["missing_extensions"] = error.missing_extensions
    return out


def _valid_host(host: str) -> bool:
    if _IPV6_HOST_RE.match(host):
        return True
    return bool(_HOST_RE.match(host))


def param_errors(raw: Mapping[str, str]) -> dict[str, str]:
    """Return {param_name: problem} for invalid or missing required fields."""
    errors: dict[str, str] = {}
    for name in PARAM_NAMES:
        value = raw.get(name)
        if value is None or (isinstance(value, str) and value.strip() == ""):
            errors[name] = "required"
            continue
        if not isinstance(value, str):
            errors[name] = "must be a string"
            continue
        value = value.strip()
        if name == "host":
            if not _valid_host(value):
                errors[name] = "invalid host"
        elif name == "port":
            if not _PORT_RE.match(value):
                errors[name] = "invalid port"
            else:
                port_num = int(value)
                if port_num < 1 or port_num > 65535:
                    errors[name] = "invalid port"
        elif name in ("database", "user"):
            if not _IDENT_RE.match(value):
                errors[name] = f"invalid {name}"
        elif name == "password":
            if not _PASSWORD_RE.match(value):
                errors[name] = "invalid password"
        elif name == "sslmode" and value not in _SSLMODES:
            errors[name] = "invalid sslmode"
    return errors


def _format_host_for_url(host: str) -> str:
    if host.startswith("[") and host.endswith("]"):
        return host
    if ":" in host:
        return f"[{host}]"
    return host


def build_url(params: PgParams) -> str:
    user = quote(params.user, safe="")
    password = quote(params.password, safe="")
    host = _format_host_for_url(params.host)
    return (
        f"postgresql://{user}:{password}@{host}:{params.port}/"
        f"{params.database}?sslmode={params.sslmode}"
    )


def params_from_url(url: str) -> PgParams:
    parsed = urlparse(url)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise ParamsError(message="invalid postgres url scheme")
    if not parsed.hostname:
        raise ParamsError(message="invalid postgres url: missing host")
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    database = (parsed.path or "").lstrip("/")
    if not database:
        raise ParamsError(message="invalid postgres url: missing database")
    port = parsed.port or 5432
    qs = parse_qs(parsed.query)
    ssl_vals = qs.get("sslmode", ["prefer"])
    sslmode = ssl_vals[0] if ssl_vals else "prefer"
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    raw = {
        "host": host,
        "port": str(port),
        "database": database,
        "user": user,
        "password": password,
        "sslmode": sslmode,
    }
    errors = param_errors(raw)
    if errors:
        invalid = sorted(errors)
        raise ParamsError(message=f"invalid url parameters: {', '.join(invalid)}")
    return PgParams(
        host=raw["host"],
        port=int(raw["port"]),
        database=raw["database"],
        user=raw["user"],
        password=raw["password"],
        sslmode=raw["sslmode"],
    )


def _params_from_raw(raw: Mapping[str, str]) -> PgParams:
    errors = param_errors(raw)
    if errors:
        missing = sorted(k for k, v in errors.items() if v == "required")
        if missing:
            raise ParamsError(missing=missing)
        invalid = sorted(k for k in errors if errors[k] != "required")
        raise ParamsError(message=f"invalid parameters: {', '.join(invalid)}")
    return PgParams(
        host=raw["host"].strip(),
        port=int(raw["port"].strip()),
        database=raw["database"].strip(),
        user=raw["user"].strip(),
        password=raw["password"].strip(),
        sslmode=raw["sslmode"].strip(),
    )


def params_from_env(environ: Mapping[str, str]) -> PgParams:
    url = environ.get(f"{ENV_PREFIX}URL", "").strip()
    if url:
        return params_from_url(url)
    raw = {
        "host": environ.get(f"{ENV_PREFIX}HOST", ""),
        "port": environ.get(f"{ENV_PREFIX}PORT", ""),
        "database": environ.get(f"{ENV_PREFIX}DATABASE", ""),
        "user": environ.get(f"{ENV_PREFIX}USER", ""),
        "password": environ.get(f"{ENV_PREFIX}PASSWORD", ""),
        "sslmode": environ.get(f"{ENV_PREFIX}SSLMODE", "prefer"),
    }
    return _params_from_raw(raw)


def params_from_file(path: Path) -> PgParams:
    st = path.stat()
    if st.st_mode & 0o077:
        raise ParamsError(
            message=f"params file {path} must not be readable by group or others (chmod 600)"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ParamsError(message=f"params file {path} must be a YAML mapping")
    if data.get("url"):
        return params_from_url(str(data["url"]))
    raw = {
        name: str(data.get(name, "")) if data.get(name) is not None else "" for name in PARAM_NAMES
    }
    if raw.get("sslmode") == "":
        raw["sslmode"] = "prefer"
    return _params_from_raw(raw)


def _is_private_ip(host: str) -> bool:
    bare = host.strip("[]")
    if re.match(r"^10\.", bare):
        return True
    if re.match(r"^192\.168\.", bare):
        return True
    m = re.match(r"^172\.(\d+)\.", bare)
    if m and 16 <= int(m.group(1)) <= 31:
        return True
    return bare in ("127.0.0.1", "localhost")


def _internal_host(host: str) -> bool:
    bare = host.strip("[]")
    if "." not in bare:
        return True
    return _is_private_ip(bare)


def _insecure_sslmode_warnings(host: str, sslmode: str) -> list[str]:
    if sslmode not in _INSECURE_SSLMODES:
        return []
    if _internal_host(host):
        return []
    return ["insecure_sslmode"]


def _redact(text: str, secret: str) -> str:
    if not secret or secret not in text:
        return text
    return text.replace(secret, "***")


def _server_message(exc: BaseException, password: str) -> str:
    sqlstate = getattr(exc, "sqlstate", None) or ""
    diag = getattr(exc, "diag", None)
    primary = ""
    if diag is not None:
        primary = getattr(diag, "message_primary", None) or ""
    if not primary:
        primary = str(exc).split("\n", maxsplit=1)[0]
    primary = _redact(primary, password)
    if sqlstate:
        return f"{sqlstate}: {primary}"
    return primary


def _classify_connection_error(exc: BaseException, password: str) -> ProbeError:
    sqlstate = getattr(exc, "sqlstate", None) or ""
    msg = _server_message(exc, password).lower()
    if sqlstate in ("28P01", "28000") or "password authentication failed" in msg:
        return ProbeError(
            code="auth_failed", message=_server_message(exc, password), retryable=False
        )
    if sqlstate == "3D000" or "does not exist" in msg and "database" in msg:
        return ProbeError(
            code="database_not_found", message=_server_message(exc, password), retryable=False
        )
    if sqlstate.startswith(("08", "57P")):
        return ProbeError(
            code="provider_error", message=_server_message(exc, password), retryable=True
        )
    if not sqlstate and isinstance(exc, psycopg.OperationalError):
        # No SQLSTATE means the server never answered: a real Postgres server
        # always tags its own rejections (28P01/28000 wrong password, 3D000
        # missing database, 08xxx/57Pxx admin issues) with one, all handled
        # above. What is left here is DNS, "connection refused", a timeout, an
        # unreachable network, or a TLS failure -- host resolution included,
        # whatever libpq's exact wording for it is. Matching message text was
        # unreliable: an unresolvable host name that did not happen to match
        # one of the specific "connection refused"/"could not translate host
        # name"/etc. phrases fell through to provider_error below, i.e.
        # postgres_access_denied, retryable false, "check the password" --
        # wrong advice for something DNS or the network could still fix.
        return ProbeError(
            code="endpoint_unreachable",
            message=_server_message(exc, password),
            retryable=True,
        )
    return ProbeError(
        code="provider_error", message=_server_message(exc, password), retryable=False
    )


def _default_connect(**kwargs: Any):
    return psycopg.connect(**kwargs)


def _fail_report(
    params: PgParams,
    *,
    exit_code: int,
    checks: dict[str, str],
    error: ProbeError,
    warnings: list[str] | None = None,
    missing: list[str] | None = None,
    server_version: str = "",
    checked_via: str | None = None,
) -> ProbeReport:
    return ProbeReport(
        ok=False,
        exit_code=exit_code,
        host=params.host,
        port=params.port,
        database=params.database,
        user=params.user,
        sslmode=params.sslmode,
        server_version=server_version,
        checks=checks,
        warnings=warnings or [],
        error=error,
        missing=missing or [],
        checked_via=checked_via,
    )


def _ok_report(
    params: PgParams,
    checks: dict[str, str],
    warnings: list[str],
    *,
    server_version: str,
    exit_code: int = 0,
    checked_via: str | None = None,
) -> ProbeReport:
    return ProbeReport(
        ok=exit_code == 0,
        exit_code=exit_code,
        host=params.host,
        port=params.port,
        database=params.database,
        user=params.user,
        sslmode=params.sslmode,
        server_version=server_version,
        checks=checks,
        warnings=warnings,
        error=None,
        missing=[],
        checked_via=checked_via,
    )


def probe_postgres(
    params: PgParams,
    *,
    extensions: Sequence[str] = (),
    connect: ConnectFactory | None = None,
    connect_host: str | None = None,
    connect_port: int | None = None,
    require_isolation: bool = False,
    probe_id: str | None = None,
) -> ProbeReport:
    raw = {name: str(getattr(params, name)) for name in PARAM_NAMES}
    errors = param_errors(raw)
    if errors:
        missing = sorted(k for k, v in errors.items() if v == "required")
        if missing:
            return _fail_report(
                params,
                exit_code=2,
                checks={},
                error=ProbeError(
                    code="missing_parameters",
                    message=f"missing: {', '.join(missing)}",
                    retryable=False,
                ),
                missing=missing,
            )
        invalid = sorted(k for k in errors if errors[k] != "required")
        return _fail_report(
            params,
            exit_code=2,
            checks={},
            error=ProbeError(
                code="invalid_parameters",
                message=f"invalid: {', '.join(invalid)}",
                retryable=False,
            ),
        )

    warnings = _insecure_sslmode_warnings(params.host, params.sslmode)
    host = connect_host if connect_host is not None else params.host
    port = connect_port if connect_port is not None else params.port
    checked_via = None
    if connect_host is not None or connect_port is not None:
        checked_via = f"{host}:{port}"

    connect_fn = connect or _default_connect
    checks: dict[str, str] = {}
    server_version = ""
    pid = probe_id or uuid.uuid4().hex[:12]
    if not _PROBE_ID_RE.match(pid):
        pid = re.sub(r"[^a-z0-9]", "", pid.lower()) or "probe"
    table_name = f"{PROBE_TABLE_PREFIX}{pid}"

    conn = None
    try:
        conn = connect_fn(
            host=host,
            port=port,
            dbname=params.database,
            user=params.user,
            password=params.password,
            sslmode=params.sslmode,
            connect_timeout=5,
            application_name="keystonectl-probe",
        )
        checks["connect"] = "ok"
    except Exception as exc:
        err = _classify_connection_error(exc, params.password)
        if err.code == "endpoint_unreachable":
            code = 3
        elif err.code in ("auth_failed", "database_not_found"):
            code = 1
        elif err.code == "provider_error":
            code = 3 if err.retryable else 1
        else:
            code = 1
        return _fail_report(
            params,
            exit_code=code,
            checks=checks,
            error=err,
            warnings=warnings,
            checked_via=checked_via,
        )

    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT version()")
                version_row = cur.fetchone()
                server_version = (version_row[0] if version_row else "") or ""
                cur.execute("SHOW server_version_num")
                num_row = cur.fetchone()
                version_num = int(num_row[0]) if num_row and num_row[0] is not None else 0
                checks["version"] = "ok"
                if version_num < MIN_SERVER_VERSION_NUM:
                    return _fail_report(
                        params,
                        exit_code=1,
                        checks={**checks, "version": "failed"},
                        error=ProbeError(
                            code="version_too_old",
                            message=f"server_version_num {version_num} < {MIN_SERVER_VERSION_NUM}",
                            retryable=False,
                        ),
                        warnings=warnings,
                        server_version=server_version,
                        checked_via=checked_via,
                    )
            except Exception as exc:
                err = _classify_connection_error(exc, params.password)
                code = 3 if err.retryable else 1
                return _fail_report(
                    params,
                    exit_code=code,
                    checks=checks,
                    error=err,
                    warnings=warnings,
                    server_version=server_version,
                    checked_via=checked_via,
                )

            try:
                cur.execute("SELECT current_schema()")
                schema_row = cur.fetchone()
                if not schema_row or not schema_row[0]:
                    return _fail_report(
                        params,
                        exit_code=1,
                        checks={**checks, "create_table": "failed"},
                        error=ProbeError(
                            code="create_table_failed",
                            message="no current schema",
                            retryable=False,
                        ),
                        warnings=warnings,
                        server_version=server_version,
                        checked_via=checked_via,
                    )
                cur.execute(sql.SQL("CREATE TABLE {} (id int)").format(sql.Identifier(table_name)))
                cur.execute(
                    sql.SQL("INSERT INTO {} (id) VALUES (1)").format(sql.Identifier(table_name))
                )
                cur.execute(sql.SQL("SELECT id FROM {}").format(sql.Identifier(table_name)))
                cur.fetchone()
                conn.rollback()
                checks["create_table"] = "ok"
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                sqlstate = getattr(exc, "sqlstate", None) or ""
                if sqlstate == "42501":
                    return _fail_report(
                        params,
                        exit_code=1,
                        checks={**checks, "create_table": "failed"},
                        error=ProbeError(
                            code="create_table_failed",
                            message=_server_message(exc, params.password),
                            retryable=False,
                        ),
                        warnings=warnings,
                        server_version=server_version,
                        checked_via=checked_via,
                    )
                err = _classify_connection_error(exc, params.password)
                code = 3 if err.retryable else 1
                return _fail_report(
                    params,
                    exit_code=code,
                    checks=checks,
                    error=err,
                    warnings=warnings,
                    server_version=server_version,
                    checked_via=checked_via,
                )

            ext_list = [e.strip() for e in extensions if e.strip()]
            if not ext_list:
                checks["extensions"] = "skipped"
            else:
                try:
                    cur.execute("SELECT extname FROM pg_extension")
                    rows = cur.fetchall()
                    installed = {row[0] for row in rows if row and row[0]}
                    missing_ext = sorted(e for e in ext_list if e not in installed)
                    if missing_ext:
                        return _fail_report(
                            params,
                            exit_code=1,
                            checks={**checks, "extensions": "failed"},
                            error=ProbeError(
                                code="extension_missing",
                                message=f"missing extensions: {', '.join(missing_ext)}",
                                retryable=False,
                                missing_extensions=missing_ext,
                            ),
                            warnings=warnings,
                            server_version=server_version,
                            checked_via=checked_via,
                        )
                    checks["extensions"] = "ok"
                except Exception as exc:
                    err = _classify_connection_error(exc, params.password)
                    code = 3 if err.retryable else 1
                    return _fail_report(
                        params,
                        exit_code=code,
                        checks=checks,
                        error=err,
                        warnings=warnings,
                        server_version=server_version,
                        checked_via=checked_via,
                    )

            isolation_warnings: list[str] = []
            isolation_failed = False
            try:
                cur.execute(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
                    "WHERE rolname = current_user"
                )
                role_row = cur.fetchone()
                if role_row and (role_row[0] or role_row[1] or role_row[2]):
                    isolation_warnings.append("superuser_credentials")
                    isolation_failed = True
                cur.execute(
                    "SELECT datname FROM pg_database WHERE datallowconn "
                    "AND datname NOT IN (current_database(), 'template0', 'template1') "
                    "AND has_database_privilege(datname, 'CONNECT')"
                )
                other_dbs = [row[0] for row in cur.fetchall() if row and row[0]]
                if other_dbs:
                    isolation_warnings.append("connect_to_other_databases")
                    isolation_failed = True
                if isolation_failed:
                    if require_isolation:
                        return _fail_report(
                            params,
                            exit_code=1,
                            checks={**checks, "isolation": "failed"},
                            error=ProbeError(
                                code="not_isolated",
                                message="role is not isolated for shared postgres",
                                retryable=False,
                            ),
                            warnings=[*warnings, *isolation_warnings],
                            server_version=server_version,
                            checked_via=checked_via,
                        )
                    checks["isolation"] = "weak"
                    warnings = [*warnings, *isolation_warnings]
                else:
                    checks["isolation"] = "ok"
            except Exception as exc:
                err = _classify_connection_error(exc, params.password)
                code = 3 if err.retryable else 1
                return _fail_report(
                    params,
                    exit_code=code,
                    checks=checks,
                    error=err,
                    warnings=warnings,
                    server_version=server_version,
                    checked_via=checked_via,
                )

        return _ok_report(
            params,
            checks,
            warnings,
            server_version=server_version,
            checked_via=checked_via,
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _parse_connect_override(value: str) -> tuple[str, int]:
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            raise ParamsError(message="invalid connect-override")
        host = value[: end + 1]
        rest = value[end + 1 :]
        if not rest.startswith(":"):
            raise ParamsError(message="invalid connect-override")
        port = int(rest[1:])
        return host, port
    if ":" not in value:
        raise ParamsError(message="invalid connect-override")
    host, port_str = value.rsplit(":", 1)
    return host, int(port_str)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe Postgres/v1 resource parameters")
    parser.add_argument("--params-file", type=Path, help="YAML params file (chmod 600)")
    parser.add_argument(
        "--extensions",
        default="",
        help="Comma-separated extension names to verify (optional)",
    )
    parser.add_argument(
        "--connect-override",
        help="Connect using HOST:PORT while reporting configured host",
    )
    parser.add_argument(
        "--require-isolation",
        action="store_true",
        help="Treat weak isolation as a hard error",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON report on stdout")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)

    try:
        if args.params_file:
            params = params_from_file(args.params_file)
        else:
            import os

            params = params_from_env(os.environ)
    except ParamsError as exc:
        if exc.missing:
            report = ProbeReport(
                ok=False,
                exit_code=2,
                host="",
                port=0,
                database="",
                user="",
                sslmode="prefer",
                server_version="",
                checks={},
                warnings=[],
                error=ProbeError(
                    code="missing_parameters",
                    message=f"missing: {', '.join(exc.missing)}",
                    retryable=False,
                ),
                missing=exc.missing,
            )
        else:
            report = ProbeReport(
                ok=False,
                exit_code=2,
                host="",
                port=0,
                database="",
                user="",
                sslmode="prefer",
                server_version="",
                checks={},
                warnings=[],
                error=ProbeError(
                    code="invalid_parameters",
                    message=str(exc),
                    retryable=False,
                ),
                missing=[],
            )
        if args.json:
            print(json.dumps(report.to_json_dict(), indent=2))
        else:
            print(report.error.message if report.error else "parameter error", file=sys.stderr)
        return report.exit_code

    connect_host: str | None = None
    connect_port: int | None = None
    if args.connect_override:
        try:
            connect_host, connect_port = _parse_connect_override(args.connect_override)
        except (ParamsError, ValueError) as exc:
            report = ProbeReport(
                ok=False,
                exit_code=2,
                host=params.host,
                port=params.port,
                database=params.database,
                user=params.user,
                sslmode=params.sslmode,
                server_version="",
                checks={},
                warnings=[],
                error=ProbeError(
                    code="invalid_parameters",
                    message=str(exc),
                    retryable=False,
                ),
                missing=[],
            )
            if args.json:
                print(json.dumps(report.to_json_dict(), indent=2))
            return 2

    ext_list = [part.strip() for part in args.extensions.split(",") if part.strip()]
    report = probe_postgres(
        params,
        extensions=ext_list,
        connect_host=connect_host,
        connect_port=connect_port,
        require_isolation=args.require_isolation,
    )

    if args.json:
        print(json.dumps(report.to_json_dict(), indent=2))
    elif report.error:
        print(report.error.message, file=sys.stderr)

    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
