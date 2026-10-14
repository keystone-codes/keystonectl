# Postgres resource interface (version 1)

Packages declare `needs: [{ kind: postgres, version: 1, bind_env: { ... }, extensions?: [...] }]`.
The installer binds connection parameters on consumer containers. External operators and catalog
providers must satisfy the same contract; `keystonectl` verifies bindings with `postgres_probe`.

## Parameters

Providers and external resources supply **both** binding forms (K26, K36): a single `url` variable
and/or six separate fields. Consumers choose one form or both in `bind_env`.

| Parameter | Format | Example |
|---|---|---|
| `host` | Hostname or IP, `^[A-Za-z0-9.-]{1,253}$`, or IPv6 in `[]` | `postgres-db`, `db.example.com` |
| `port` | `^[0-9]{1,5}$`, 1–65535 | `5432` |
| `database` | `^[a-z_][a-z0-9_]{0,62}$` | `docs_service` |
| `user` | `^[a-z_][a-z0-9_]{0,62}$` | `docs_service` |
| `password` | 1–128 characters, no whitespace or control characters; secret | |
| `sslmode` | `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full` | `disable` on the resource network, `verify-full` for external |
| `url` | `postgresql://<user>:<percent-encoded-password>@<host>:<port>/<database>?sslmode=<sslmode>`; secret | `postgresql://docs_service:…@postgres-db:5432/docs_service?sslmode=disable` |

`keystonectl` builds `url` from the six fields using `urllib.parse.quote(password, safe="")`.
External operators may supply either form; the installer may parse `url` back into fields.

For hostnames with a dot (not internal) and `sslmode` in `disable`, `allow`, or `prefer`, the probe
adds warning `insecure_sslmode`. Hostnames without a dot (for example `postgres-db`) and private IPs
are internal and do not trigger that warning.

Environment names used by the probe CLI: `KEYSTONE_POSTGRES_HOST`, `KEYSTONE_POSTGRES_PORT`,
`KEYSTONE_POSTGRES_DATABASE`, `KEYSTONE_POSTGRES_USER`, `KEYSTONE_POSTGRES_PASSWORD`,
`KEYSTONE_POSTGRES_SSLMODE`, or `KEYSTONE_POSTGRES_URL` (URL wins when set).

Services that need SQLAlchemy’s `postgresql+psycopg://` prefix must add it themselves; v1 exposes
libpq-style `postgresql://` in `url`.

## Provider promises

For consumer `<consumer>` (package name):

1. Database `<db>` and role `<db>` where `<db>` is the package name with `-` replaced by `_`
   (`docs-service` → `docs_service`; package names are ≤ 40 characters so the 63-byte PostgreSQL
   limit holds).
2. The role owns its database and the `public` schema; `LOGIN` only — no `SUPERUSER`, `CREATEDB`,
   `CREATEROLE`, `REPLICATION`, or `BYPASSRLS`.
3. The role owns `<db>` and `PUBLIC` is revoked on it (`REVOKE ALL ON DATABASE <db> FROM PUBLIC`),
   so other consumers cannot connect. Provision additionally runs
   `GRANT ALL ON DATABASE <db> TO <db>`, which adds nothing for the owner; rights are the same as
   promised here. The role has no `CONNECT` on other databases (including revoking `PUBLIC` on
   `postgres` where applicable).
4. Extensions listed in `needs[].extensions` are created in the consumer database at provision time
   (`CREATE EXTENSION IF NOT EXISTS` with provider admin rights). Unknown extensions fail provision.
5. Server version ≥ 14 (`server_version_num >= 140000`).
6. Re-provisioning the same consumer is idempotent (same database, role, and password).

Not part of v1: superuser access, creating other databases, access to other consumers’ databases,
logical replication, cross-service `LISTEN/NOTIFY`, backups, or extensions not listed in
`needs[].extensions`.

## Consumer obligations

Consumers create their own tables (often via an `init: true` container); they must not rely on
`search_path` outside their schema; they must not create extensions themselves; connection pools
should stay small (default guidance: ≤ 10 connections) because the server is shared.

## Catalog provider (`kind: resource`)

Why: [ADR 0003](../../adr/0003-resources-as-interfaces.md).

`provides: { kind: postgres, version: 1, container: <role> }` with
`provision.per_consumer: database_and_user` runs a one-shot provision image on the
`keystone-resources` network. Consumer parameters use `host` = `<package>-<provides.container>` (catalog
default: `postgres-db`), container port, and `sslmode: disable` (traffic stays on the resource network).

Provision stdout is one JSON line (password never in process arguments); `keystonectl` assembles
`url` from the returned fields and the password file.

## External Postgres

The operator creates database, role, extensions, and isolation (SQL samples in the installer docs).
Secrets via environment or mode-`600` files only. Exit code 0 from `postgres_probe` means the binding
is ready.

Example isolation setup (operator, as superuser):

```sql
CREATE DATABASE docs_service OWNER docs_service;
REVOKE ALL ON DATABASE docs_service FROM PUBLIC;
GRANT CONNECT ON DATABASE docs_service TO docs_service;
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
-- CREATE EXTENSION for each name in needs[].extensions
```

## Interface versioning

Behavior old providers cannot implement, or a breaking parameter change, requires `postgres/v2`.
Clarifications that do not change behavior stay on version 1.

## Probe

Command:

```bash
python -m keystonectl.resources.postgres_probe [--params-file FILE] [--extensions a,b] \
  [--connect-override HOST:PORT] [--require-isolation] [--json]
```

Without `--params-file`, parameters come from `KEYSTONE_POSTGRES_*`. There is no CLI flag for
passwords. `--connect-override` connects to another host/port (for example a container IP on the
host) while the report keeps configured `host`/`port` and sets `checked_via`. `--require-isolation`
turns isolation weaknesses into a hard error (catalog provider checks).

YAML params files must be mode `600`. The probe uses **psycopg** (not `psql` or SQLAlchemy).

Steps (first failure stops, except step 5 without `--require-isolation`):

| Step | Action | Check field | Failure → exit, `error.code` |
|---|---|---|---|
| 0 | Validate parameters | — | **2** `missing_parameters` / `invalid_parameters` |
| 1 | Connect | `connect` | **1** `auth_failed`, `database_not_found`; **3** `endpoint_unreachable` |
| 2 | `SELECT version()`, `SHOW server_version_num` | `version` | **1** `version_too_old` if `< 140000` |
| 3 | Transaction: `current_schema`, `CREATE TABLE keystone_probe_<id>`, DML, `ROLLBACK` | `create_table` | **1** `create_table_failed` (`42501`, empty schema) |
| 4 | `SELECT extname FROM pg_extension` | `extensions` | **1** `extension_missing` (or `skipped` if none requested) |
| 5 | Role privileges and `CONNECT` on other databases | `isolation` | warnings or **1** `not_isolated` with `--require-isolation` |

Other server errors on steps 1–4: SQLSTATE class `08` or `57P0x` → **3** `provider_error`
(`retryable: true`); other SQL errors → **1** `provider_error`.

Exit codes: **0** success; **1** binding invalid; **2** missing/invalid parameters; **3** transient
(`retryable: true` where noted). There is no exit code 4 (unlike S3 lifecycle).

Warnings (exit 0): `insecure_sslmode`, `superuser_credentials`, `connect_to_other_databases`.

Example success JSON:

```json
{
  "ok": true,
  "exit_code": 0,
  "host": "postgres-db",
  "port": 5432,
  "database": "docs_service",
  "user": "docs_service",
  "sslmode": "disable",
  "server_version": "16.4",
  "checks": {
    "connect": "ok",
    "version": "ok",
    "create_table": "ok",
    "extensions": "ok",
    "isolation": "ok"
  },
  "warnings": [],
  "error": null,
  "missing": []
}
```

JSON output, logs, and error messages must not contain `password` or `url` (passwords in server
messages are replaced with `***`).
