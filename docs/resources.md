# Resources

A service package can declare `needs`. Each entry names a resource kind (`s3` or `postgres`), interface version `1`, and the environment names that package wants (`bind_env`). keystonectl binds every need to one resource of that kind for the whole installation.

There are two ways to satisfy a need:

- **external** — storage or a database you already run. You create the bucket, database, user, and extensions. keystonectl only checks them.
- **catalog** — one provider package from a configured catalog (`kind: resource`, `provides.kind` matching the need). keystonectl installs that provider, then creates the consumer's bucket or database.

One provider per kind. Installing a second provider package of the same kind is refused with `resource_already_bound`. Once a kind is bound, later consumers use that provider; the `KEYSTONE_SETUP_RESOURCE_<KIND>` answer is not asked again and an `external` answer is ignored.

## Questions

The first plan of a package with `needs` asks:

| Variable | Answer |
|---|---|
| `KEYSTONE_SETUP_RESOURCE_S3` | `external` or `catalog` |
| `KEYSTONE_SETUP_RESOURCE_POSTGRES` | `external` or `catalog` |
| `KEYSTONE_SETUP_RESOURCE_S3_PACKAGE` | `source/name`, when more than one S3 provider is in the catalogs |
| `KEYSTONE_SETUP_RESOURCE_POSTGRES_PACKAGE` | `source/name`, when more than one PostgreSQL provider is in the catalogs |

External S3, asked once for the installation unless noted:

| Variable | Meaning | Default |
|---|---|---|
| `KEYSTONE_SETUP_S3_ENDPOINT` | Endpoint URL. `http://` warns `s3_plain_http`. | |
| `KEYSTONE_SETUP_S3_REGION` | Bucket region. | `us-east-1` |
| `KEYSTONE_SETUP_S3_PATH_STYLE` | `true` or `false`. | `true` |
| `KEYSTONE_SETUP_S3_BUCKET` | Bucket for this package. | |
| `KEYSTONE_SETUP_S3_ACCESS_KEY_FILE` | Mode 600 file with the access key. | |
| `KEYSTONE_SETUP_S3_SECRET_KEY_FILE` | Mode 600 file with the secret key. | |

External PostgreSQL:

| Variable | Meaning | Default |
|---|---|---|
| `KEYSTONE_SETUP_POSTGRES_HOST` | Server host. | |
| `KEYSTONE_SETUP_POSTGRES_PORT` | Server port. | `5432` |
| `KEYSTONE_SETUP_POSTGRES_SSLMODE` | `disable`, `prefer`, `require`, `verify-ca`, or `verify-full`. `disable` and `prefer` warn `postgres_plain_connection`. | `require` |
| `KEYSTONE_SETUP_POSTGRES_DATABASE` | Database for this package. | |
| `KEYSTONE_SETUP_POSTGRES_USER` | User for this package. | |
| `KEYSTONE_SETUP_POSTGRES_PASSWORD_FILE` | Mode 600 file with the password. | |
| `KEYSTONE_SETUP_POSTGRES_URL_FILE` | Mode 600 file with one `postgresql://…` URL. When set, the other PostgreSQL questions are skipped. `sslmode` in the URL is required. | |

A name that starts with `KEYSTONE_SETUP_SERVICE_<PKG>_` overrides the general value for that package only. `<PKG>` is the package name in upper case with `-` replaced by `_`. The overrides are `S3_BUCKET`, `S3_ACCESS_KEY_FILE`, `S3_SECRET_KEY_FILE`, `POSTGRES_DATABASE`, `POSTGRES_USER`, `POSTGRES_PASSWORD_FILE`, and `POSTGRES_URL_FILE`. When the override is missing, the question shows the general name.

Secrets never go on the command line, in `--set`, in logs, in `--json`, or in `services.lock.yaml`. Pass a mode 600 file that you own. `--set` refuses any name ending in `_FILE`.

## What is written

The consumer receives `packages/<package>.env` (mode 600) with the names from `bind_env`. If `needs[].containers` lists only some of the package's containers, that resource is written to `packages/<package>.<kind>.env` instead, and the lock records that path.

A catalog provider's admin credentials stay in `packages/<provider>.env`.

`services.lock.yaml` records the resource address (endpoint, region, and path style, or host, port, and sslmode) and the consumer's bucket or database name. It does not record access keys, passwords, or URLs.

Removing a service drops it from `resources[].consumers`. The bucket, database, and user are kept. Removing a provider that still has consumers is refused (`resource_in_use`).

## Provider contract

`provision.sh` is the same shape for `s3/v1` and `postgres/v1`. keystonectl runs it after the provider is healthy. Secrets are not arguments and not an env file. Each `provision.admin_env` name is passed as `-e NAME` with no `=`. The value is in the environment of `docker run`.

```
docker run --rm --network keystone-resources \
  -e <each name in provision.admin_env> [-e MC_CONFIG_DIR=/tmp/mc for s3] \
  -v <provider package directory>:/pkg:ro -v <tmp>:/out \
  --entrypoint /bin/sh <provision.image> /pkg/<provision.script> \
  --consumer <package> --secret-file /out/<package>.secret \
  <kind arguments>
```

S3 arguments: `--endpoint http://<provider>-<container>:<port>`.

PostgreSQL arguments: `--host <provider>-<container> --port <port> --database <ident> --extensions <a,b>` or `--extensions` with an empty value. `<ident>` is the package name with `-` replaced by `_`.

The installer does not pass `--region`, `--sslmode`, or `--ready-timeout`.

`<tmp>` is mode 700 and is removed when the script returns. `<package>.secret` is mode 600. If the consumer already has a secret (the `secret_key` or `password` binding, or the password inside a `url` binding), that value is reused. Otherwise the file contains `token_hex(32)`.

Stdout is one JSON object, on its last non-empty line.

S3:

```json
{"interface": "s3/v1", "endpoint": "", "region": "", "bucket": "", "access_key": "", "path_style": true, "secret_key_file": "/out/<package>.secret"}
```

PostgreSQL:

```json
{"interface": "postgres/v1", "host": "", "port": 5432, "database": "", "user": "", "sslmode": "disable", "password_file": "/out/<package>.secret", "extensions": []}
```

A provider on the Docker network reports `sslmode: disable`. keystonectl reads the secret back from `/out/<package>.secret`, not from stdout.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Access is ready. A repeat must not rotate the password. |
| 2 | `provision_failed`, not retryable. The package or the installer is wrong. |
| 3 | `provision_failed`, retryable. The provider is not ready yet, or the admin key was rejected. |

Anything that is not JSON, has the wrong `interface`, or omits a field is `provision_failed`. The error text is the last 20 lines of stderr with secrets removed.
