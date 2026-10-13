"""s3/v1 and postgres/v1 binding interfaces."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from keystonectl.resources.postgres_probe import PgParams, build_url


@dataclass(frozen=True)
class ResourceInterface:
    kind: str
    version: int
    params: tuple[str, ...]
    secret_params: frozenset[str]
    resource_params: tuple[str, ...]
    consumer_params: tuple[str, ...]

    def bind(self, values: Mapping[str, str], bind_env: Mapping[str, str]) -> dict[str, str]:
        """Map interface values onto the env names the package asked for."""
        rendered = _render(self.kind, values)
        bound: dict[str, str] = {}
        for param, env_name in bind_env.items():
            if not isinstance(env_name, str) or not env_name:
                continue
            if param not in rendered:
                continue
            bound[env_name] = rendered[param]
        return bound


def _render(kind: str, values: Mapping[str, str]) -> dict[str, str]:
    rendered = {key: str(value) for key, value in values.items() if value is not None}
    if kind == "s3":
        path_style = rendered.get("path_style", "")
        if path_style in {"1", "true", "True"}:
            rendered["path_style"] = "true"
        elif path_style in {"0", "false", "False"}:
            rendered["path_style"] = "false"
    if kind == "postgres":
        # Built only from the parts. A script must not choose the URL: it can
        # see the admin password and hide it in a url field.
        rendered["url"] = build_url(
            PgParams(
                host=rendered.get("host", ""),
                port=int(rendered.get("port") or "5432"),
                database=rendered.get("database", ""),
                user=rendered.get("user", ""),
                password=rendered.get("password", ""),
                sslmode=rendered.get("sslmode") or "require",
            )
        )
    return rendered


S3_V1 = ResourceInterface(
    kind="s3",
    version=1,
    params=("endpoint", "region", "bucket", "access_key", "secret_key", "path_style"),
    secret_params=frozenset({"secret_key"}),
    resource_params=("endpoint", "region", "path_style"),
    consumer_params=("bucket", "access_key", "secret_key"),
)

POSTGRES_V1 = ResourceInterface(
    kind="postgres",
    version=1,
    params=("host", "port", "database", "user", "password", "sslmode", "url"),
    secret_params=frozenset({"password", "url"}),
    resource_params=("host", "port", "sslmode"),
    consumer_params=("database", "user", "password"),
)

INTERFACES = {"s3": S3_V1, "postgres": POSTGRES_V1}
