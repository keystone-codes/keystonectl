"""S3/v1 resource probe using boto3 and httpx."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
import httpx
import yaml
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

PARAM_NAMES = ("endpoint", "region", "bucket", "access_key", "secret_key", "path_style")
ENV_PREFIX = "KEYSTONE_S3_"
PROBE_PREFIX = "keystone-probe/"
PROBE_RULE_ID = "keystone-s3-probe"

_ENDPOINT_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
_REGION_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_CREDENTIAL_RE = re.compile(r"^\S{1,128}$")

logger = logging.getLogger(__name__)


class ParamsError(ValueError):
    """Missing or invalid parameters before probe."""

    def __init__(self, *, missing: list[str] | None = None, message: str = "") -> None:
        self.missing = missing or []
        super().__init__(message or f"missing parameters: {', '.join(self.missing)}")


@dataclass(frozen=True)
class S3Params:
    endpoint: str
    region: str
    bucket: str
    access_key: str
    secret_key: str = field(repr=False)
    path_style: bool = True


@dataclass
class ProbeError:
    code: str
    message: str
    retryable: bool = False


@dataclass
class ProbeReport:
    ok: bool
    exit_code: int
    endpoint: str
    bucket: str
    region: str
    path_style: bool
    checks: dict[str, str]
    warnings: list[str]
    error: ProbeError | None
    missing: list[str]
    checked_via: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "endpoint": self.endpoint,
            "bucket": self.bucket,
            "region": self.region,
            "path_style": self.path_style,
            "checks": self.checks,
            "warnings": self.warnings,
            "error": None if self.error is None else asdict(self.error),
            "missing": self.missing,
        }
        if self.checked_via is not None:
            data["checked_via"] = self.checked_via
        return data


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
        if name == "endpoint":
            if not _ENDPOINT_RE.match(value):
                errors[name] = "invalid endpoint URL"
        elif name == "region":
            if not _REGION_RE.match(value):
                errors[name] = "invalid region"
        elif name == "bucket":
            if not _BUCKET_RE.match(value):
                errors[name] = "invalid bucket name"
        elif name in ("access_key", "secret_key"):
            if not _CREDENTIAL_RE.match(value):
                errors[name] = "invalid credential"
        elif name == "path_style" and value not in ("true", "false"):
            errors[name] = "must be true or false"
    return errors


def _parse_path_style(value: str) -> bool:
    return value.strip().lower() == "true"


def params_from_env(environ: Mapping[str, str]) -> S3Params:
    raw = {
        "endpoint": environ.get(f"{ENV_PREFIX}ENDPOINT", ""),
        "region": environ.get(f"{ENV_PREFIX}REGION", ""),
        "bucket": environ.get(f"{ENV_PREFIX}BUCKET", ""),
        "access_key": environ.get(f"{ENV_PREFIX}ACCESS_KEY", ""),
        "secret_key": environ.get(f"{ENV_PREFIX}SECRET_KEY", ""),
        "path_style": environ.get(f"{ENV_PREFIX}PATH_STYLE", "true"),
    }
    errors = param_errors(raw)
    if errors:
        missing = [k for k, v in errors.items() if v == "required"]
        if missing:
            raise ParamsError(missing=sorted(missing))
        invalid = [k for k in errors if k not in missing]
        raise ParamsError(message=f"invalid parameters: {', '.join(sorted(invalid))}")
    return S3Params(
        endpoint=raw["endpoint"].strip(),
        region=raw["region"].strip(),
        bucket=raw["bucket"].strip(),
        access_key=raw["access_key"].strip(),
        secret_key=raw["secret_key"].strip(),
        path_style=_parse_path_style(raw["path_style"]),
    )


def params_from_file(path: Path) -> S3Params:
    st = path.stat()
    if st.st_mode & 0o077:
        raise ParamsError(
            message=f"params file {path} must not be readable by group or others (chmod 600)"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ParamsError(message=f"params file {path} must be a YAML mapping")
    raw = {
        name: str(data.get(name, "")) if data.get(name) is not None else "" for name in PARAM_NAMES
    }
    if "path_style" in data and data["path_style"] is not None:
        raw["path_style"] = str(data["path_style"])
    errors = param_errors(raw)
    if errors:
        missing = [k for k, v in errors.items() if v == "required"]
        if missing:
            raise ParamsError(missing=sorted(missing))
        invalid = [k for k in errors if k not in missing]
        raise ParamsError(message=f"invalid parameters: {', '.join(sorted(invalid))}")
    return S3Params(
        endpoint=raw["endpoint"].strip(),
        region=raw["region"].strip(),
        bucket=raw["bucket"].strip(),
        access_key=raw["access_key"].strip(),
        secret_key=raw["secret_key"].strip(),
        path_style=_parse_path_style(raw["path_style"]),
    )


def make_client(params: S3Params, *, endpoint_url: str | None = None):
    url = endpoint_url if endpoint_url is not None else params.endpoint
    session = boto3.session.Session()
    return session.client(
        "s3",
        endpoint_url=url,
        region_name=params.region,
        aws_access_key_id=params.access_key,
        aws_secret_access_key=params.secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if params.path_style else "virtual"},
            connect_timeout=5,
            read_timeout=10,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def _client_error_code(exc: ClientError) -> str:
    err = exc.response.get("Error", {})
    code = err.get("Code") or ""
    if not code:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == 301:
            return "PermanentRedirect"
        if status:
            return str(status)
    return str(code)


def _client_error_message(exc: ClientError) -> str:
    err = exc.response.get("Error", {})
    code = _client_error_code(exc)
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    msg = err.get("Message") or code
    if status:
        return f"{code} (HTTP {status}): {msg}"
    return f"{code}: {msg}"


def _is_private_ip(host: str) -> bool:
    if re.match(r"^10\.", host):
        return True
    if re.match(r"^192\.168\.", host):
        return True
    m = re.match(r"^172\.(\d+)\.", host)
    if m and 16 <= int(m.group(1)) <= 31:
        return True
    return host in ("127.0.0.1", "localhost")


def _insecure_endpoint_warnings(endpoint: str) -> list[str]:
    parsed = urlparse(endpoint)
    if parsed.scheme != "http":
        return []
    host = parsed.hostname or ""
    if host and "." not in host:
        return []
    if _is_private_ip(host):
        return []
    return ["insecure_endpoint"]


def _merge_lifecycle_rules(existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = [rule for rule in existing if rule.get("ID") != PROBE_RULE_ID]
    probe_rule = {
        "ID": PROBE_RULE_ID,
        "Filter": {"Prefix": PROBE_PREFIX},
        "Status": "Enabled",
        "Expiration": {"Days": 1},
    }
    return [*kept, probe_rule]


def _fail_report(
    params: S3Params,
    *,
    exit_code: int,
    checks: dict[str, str],
    error: ProbeError,
    warnings: list[str] | None = None,
    missing: list[str] | None = None,
    checked_via: str | None = None,
) -> ProbeReport:
    return ProbeReport(
        ok=False,
        exit_code=exit_code,
        endpoint=params.endpoint,
        bucket=params.bucket,
        region=params.region,
        path_style=params.path_style,
        checks=checks,
        warnings=warnings or [],
        error=error,
        missing=missing or [],
        checked_via=checked_via,
    )


def _ok_report(
    params: S3Params,
    checks: dict[str, str],
    warnings: list[str],
    *,
    exit_code: int = 0,
    checked_via: str | None = None,
) -> ProbeReport:
    return ProbeReport(
        ok=exit_code == 0,
        exit_code=exit_code,
        endpoint=params.endpoint,
        bucket=params.bucket,
        region=params.region,
        path_style=params.path_style,
        checks=checks,
        warnings=warnings,
        error=None,
        missing=[],
        checked_via=checked_via,
    )


def probe_s3(
    params: S3Params,
    *,
    client=None,
    http: httpx.Client | None = None,
    check_lifecycle: bool = True,
    probe_id: str | None = None,
    connect_endpoint: str | None = None,
) -> ProbeReport:
    raw = {
        "endpoint": params.endpoint,
        "region": params.region,
        "bucket": params.bucket,
        "access_key": params.access_key,
        "secret_key": params.secret_key,
        "path_style": "true" if params.path_style else "false",
    }
    errors = param_errors(raw)
    if errors:
        missing = sorted(k for k, v in errors.items() if v == "required")
        if missing:
            return ProbeReport(
                ok=False,
                exit_code=2,
                endpoint=params.endpoint or "",
                bucket=params.bucket or "",
                region=params.region or "",
                path_style=params.path_style,
                checks={},
                warnings=[],
                error=ProbeError(
                    code="missing_parameters",
                    message=f"missing: {', '.join(missing)}",
                    retryable=False,
                ),
                missing=missing,
            )
        invalid = sorted(k for k in errors if errors[k] != "required")
        return ProbeReport(
            ok=False,
            exit_code=2,
            endpoint=params.endpoint or "",
            bucket=params.bucket or "",
            region=params.region or "",
            path_style=params.path_style,
            checks={},
            warnings=[],
            error=ProbeError(
                code="invalid_parameters",
                message=f"invalid: {', '.join(invalid)}",
                retryable=False,
            ),
            missing=[],
        )

    warnings = _insecure_endpoint_warnings(params.endpoint)
    checked_via = connect_endpoint
    request_endpoint = connect_endpoint or params.endpoint

    if client is None:
        client = make_client(params, endpoint_url=request_endpoint)

    own_http = http is None
    if own_http:
        http = httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))

    checks: dict[str, str] = {}
    pid = probe_id or uuid.uuid4().hex[:12]
    object_key = f"{PROBE_PREFIX}{pid}.txt"
    body = f"keystone s3 probe {pid}\n".encode()

    try:
        try:
            client.head_bucket(Bucket=params.bucket)
            checks["head_bucket"] = "ok"
        except ClientError as exc:
            code = _client_error_code(exc)
            if code in ("403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
                return _fail_report(
                    params,
                    exit_code=1,
                    checks=checks,
                    error=ProbeError(
                        code="auth_failed",
                        message=_client_error_message(exc),
                        retryable=False,
                    ),
                    warnings=warnings,
                    checked_via=checked_via,
                )
            if code in ("404", "NoSuchBucket"):
                return _fail_report(
                    params,
                    exit_code=1,
                    checks=checks,
                    error=ProbeError(
                        code="bucket_not_found",
                        message=_client_error_message(exc),
                        retryable=False,
                    ),
                    warnings=warnings,
                    checked_via=checked_via,
                )
            if code in ("301", "PermanentRedirect", "AuthorizationHeaderMalformed"):
                return _fail_report(
                    params,
                    exit_code=1,
                    checks=checks,
                    error=ProbeError(
                        code="wrong_region",
                        message=_client_error_message(exc),
                        retryable=False,
                    ),
                    warnings=warnings,
                    checked_via=checked_via,
                )
            if int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0) >= 500:
                return _fail_report(
                    params,
                    exit_code=3,
                    checks=checks,
                    error=ProbeError(
                        code="provider_error",
                        message=_client_error_message(exc),
                        retryable=True,
                    ),
                    warnings=warnings,
                    checked_via=checked_via,
                )
            return _fail_report(
                params,
                exit_code=1,
                checks=checks,
                error=ProbeError(
                    code="auth_failed",
                    message=_client_error_message(exc),
                    retryable=False,
                ),
                warnings=warnings,
                checked_via=checked_via,
            )
        except EndpointConnectionError:
            return _fail_report(
                params,
                exit_code=3,
                checks=checks,
                error=ProbeError(
                    code="endpoint_unreachable",
                    message="could not connect to endpoint",
                    retryable=True,
                ),
                warnings=warnings,
                checked_via=checked_via,
            )
        except BotoCoreError as exc:
            return _fail_report(
                params,
                exit_code=3,
                checks=checks,
                error=ProbeError(
                    code="endpoint_unreachable",
                    message=type(exc).__name__,
                    retryable=True,
                ),
                warnings=warnings,
                checked_via=checked_via,
            )

        try:
            put_url = client.generate_presigned_url(
                "put_object",
                Params={"Bucket": params.bucket, "Key": object_key},
                ExpiresIn=300,
            )
            if connect_endpoint:
                put_url = _rewrite_presigned_host(put_url, connect_endpoint)
            assert http is not None
            put_resp = http.put(put_url, content=body)
            if put_resp.status_code >= 500:
                return _fail_report(
                    params,
                    exit_code=3,
                    checks={**checks, "presign_put": "failed"},
                    error=ProbeError(
                        code="provider_error",
                        message=f"presigned PUT HTTP {put_resp.status_code}",
                        retryable=True,
                    ),
                    warnings=warnings,
                    checked_via=checked_via,
                )
            if put_resp.status_code >= 400:
                return _fail_report(
                    params,
                    exit_code=1,
                    checks={**checks, "presign_put": "failed"},
                    error=ProbeError(
                        code="presign_put_failed",
                        message=_http_s3_error_message(put_resp),
                        retryable=False,
                    ),
                    warnings=warnings,
                    checked_via=checked_via,
                )
            checks["presign_put"] = "ok"
        except httpx.HTTPError:
            return _fail_report(
                params,
                exit_code=3,
                checks={**checks, "presign_put": "failed"},
                error=ProbeError(
                    code="endpoint_unreachable",
                    message="presigned PUT request failed",
                    retryable=True,
                ),
                warnings=warnings,
                checked_via=checked_via,
            )

        try:
            get_url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": params.bucket, "Key": object_key},
                ExpiresIn=300,
            )
            if connect_endpoint:
                get_url = _rewrite_presigned_host(get_url, connect_endpoint)
            assert http is not None
            get_resp = http.get(get_url)
            if get_resp.status_code >= 500:
                return _fail_report(
                    params,
                    exit_code=3,
                    checks={**checks, "presign_get": "failed"},
                    error=ProbeError(
                        code="provider_error",
                        message=f"presigned GET HTTP {get_resp.status_code}",
                        retryable=True,
                    ),
                    warnings=warnings,
                    checked_via=checked_via,
                )
            if get_resp.status_code >= 400:
                return _fail_report(
                    params,
                    exit_code=1,
                    checks={**checks, "presign_get": "failed"},
                    error=ProbeError(
                        code="presign_get_failed",
                        message=_http_s3_error_message(get_resp),
                        retryable=False,
                    ),
                    warnings=warnings,
                    checked_via=checked_via,
                )
            if get_resp.content != body:
                return _fail_report(
                    params,
                    exit_code=1,
                    checks={**checks, "presign_get": "failed"},
                    error=ProbeError(
                        code="presign_get_mismatch",
                        message="presigned GET body mismatch",
                        retryable=False,
                    ),
                    warnings=warnings,
                    checked_via=checked_via,
                )
            checks["presign_get"] = "ok"
        except httpx.HTTPError:
            return _fail_report(
                params,
                exit_code=3,
                checks={**checks, "presign_get": "failed"},
                error=ProbeError(
                    code="endpoint_unreachable",
                    message="presigned GET request failed",
                    retryable=True,
                ),
                warnings=warnings,
                checked_via=checked_via,
            )

        lifecycle_exit: int | None = None
        if not check_lifecycle:
            checks["lifecycle"] = "skipped"
        else:
            try:
                existing_rules: list[dict[str, Any]] = []
                try:
                    lc = client.get_bucket_lifecycle_configuration(Bucket=params.bucket)
                    existing_rules = list(lc.get("Rules") or [])
                except ClientError as exc:
                    if _client_error_code(exc) != "NoSuchLifecycleConfiguration":
                        raise
                merged = _merge_lifecycle_rules(existing_rules)
                client.put_bucket_lifecycle_configuration(
                    Bucket=params.bucket,
                    LifecycleConfiguration={"Rules": merged},
                )
                checks["lifecycle"] = "ok"
            except ClientError as exc:
                code = _client_error_code(exc)
                if code in ("AccessDenied", "NotImplemented", "MalformedXML"):
                    checks["lifecycle"] = "unsupported"
                    lifecycle_exit = 4
                elif (
                    int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
                    >= 500
                ):
                    return _fail_report(
                        params,
                        exit_code=3,
                        checks=checks,
                        error=ProbeError(
                            code="provider_error",
                            message=_client_error_message(exc),
                            retryable=True,
                        ),
                        warnings=warnings,
                        checked_via=checked_via,
                    )
                else:
                    return _fail_report(
                        params,
                        exit_code=3,
                        checks=checks,
                        error=ProbeError(
                            code="provider_error",
                            message=_client_error_message(exc),
                            retryable=True,
                        ),
                        warnings=warnings,
                        checked_via=checked_via,
                    )
            except (EndpointConnectionError, BotoCoreError):
                return _fail_report(
                    params,
                    exit_code=3,
                    checks=checks,
                    error=ProbeError(
                        code="endpoint_unreachable",
                        message="lifecycle request failed",
                        retryable=True,
                    ),
                    warnings=warnings,
                    checked_via=checked_via,
                )

        try:
            client.delete_object(Bucket=params.bucket, Key=object_key)
            checks["cleanup"] = "ok"
        except (ClientError, BotoCoreError):
            checks["cleanup"] = "failed"
            warnings = [*warnings, "cleanup_failed"]

        if lifecycle_exit == 4:
            return _fail_report(
                params,
                exit_code=4,
                checks=checks,
                error=ProbeError(
                    code="lifecycle_unsupported",
                    message="bucket lifecycle configuration is not supported",
                    retryable=False,
                ),
                warnings=warnings,
                checked_via=checked_via,
            )

        return _ok_report(params, checks, warnings, checked_via=checked_via)
    finally:
        if own_http and http is not None:
            http.close()


def _rewrite_presigned_host(url: str, connect_endpoint: str) -> str:
    parsed_override = urlparse(connect_endpoint)
    parsed_url = urlparse(url)
    netloc = parsed_override.netloc
    scheme = parsed_override.scheme or parsed_url.scheme
    return parsed_url._replace(scheme=scheme, netloc=netloc).geturl()


def _http_s3_error_message(resp: httpx.Response) -> str:
    text = resp.text
    code_match = re.search(r"<Code>([^<]+)</Code>", text)
    code = code_match.group(1) if code_match else "HTTPError"
    return f"{code} (HTTP {resp.status_code})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe S3/v1 resource parameters")
    parser.add_argument("--params-file", type=Path, help="YAML file with S3 parameters (chmod 600)")
    parser.add_argument(
        "--endpoint-override",
        help="Connect using this URL instead of endpoint (presigned URLs rewritten)",
    )
    parser.add_argument(
        "--skip-lifecycle", action="store_true", help="Skip lifecycle configuration check"
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
                endpoint="",
                bucket="",
                region="",
                path_style=True,
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
                endpoint="",
                bucket="",
                region="",
                path_style=True,
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

    report = probe_s3(
        params,
        check_lifecycle=not args.skip_lifecycle,
        connect_endpoint=args.endpoint_override,
    )

    if args.json:
        print(json.dumps(report.to_json_dict(), indent=2))
    elif report.error:
        print(report.error.message, file=sys.stderr)

    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
