# Resources

A service package can declare `needs`. Each entry names a resource kind (`s3` or `postgres`), interface version `1`, and the environment names that package wants (`bind_env`). keystonectl binds every need to one resource of that kind for the whole installation.

There are two ways to satisfy a need:

- **external** — storage or a database you already run. You create the bucket, database, user, and extensions. keystonectl only checks them.
- **catalog** — one provider package from a configured catalog (`kind: resource`, `provides.kind` matching the need). keystonectl installs that provider, then creates the consumer's bucket or database.

One provider per kind. Installing a second provider package of the same kind is refused with `resource_already_bound`. Once a kind is bound, later consumers use that provider; the `KEYSTONE_SETUP_RESOURCE_<KIND>` answer is not asked again and an answer that disagrees with the bound provider is ignored and reported as `resource_answer_ignored`.

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

Removing a service drops it from `resources[].consumers`. The bucket, database, and user are kept. When the last consumer goes, `provider_unused` says so; removing the provider warns `provider_data_kept`. Removing a provider that still has consumers is refused (`resource_in_use`).

An `external` resource has no provider package, so removing its last consumer drops the `resources[]` entry itself (there is nothing left for keystonectl to track, and no public command to clear it otherwise). The next package with that need is asked `KEYSTONE_SETUP_RESOURCE_<KIND>` again; `catalog` now installs a provider instead of reusing the dropped external endpoint. A `catalog` resource keeps its entry with zero consumers: the provider package and its volume are still installed.

## Backups

keystonectl does not back up data. Catalog resource providers keep state in Docker named volumes `ks-pkg-<provider>-<vol>` (for example `ks-pkg-minio-data`, `ks-pkg-postgres-data`). Compose services are `<provider>-<role>` (`minio-server`, `postgres-db`; with another instance name, `<name>-<role>`).

**MinIO** — stop the container, then archive the volume:

```bash
docker compose --project-directory <root> -f docker-compose.yml -f docker-compose.packages.yml stop minio-server
docker run --rm -v ks-pkg-minio-data:/data -v "$PWD":/backup busybox tar czf /backup/minio-data.tgz -C /data .
```

Or mirror with `mc` when that fits your process.

**PostgreSQL** — dump from the running service (replace `<POSTGRES_USER>` with the value in `packages/postgres.env`):

```bash
docker compose --project-directory <root> -f docker-compose.yml -f docker-compose.packages.yml \
  exec -T postgres-db pg_dumpall -U <POSTGRES_USER> > all.sql
```

Save `packages/<provider>.env` with the volume: admin keys from the installer live there and the data is useless without them. Save `.keystonectl/secrets/` as well: consumer credentials keystonectl generated for installed services.

Packages that both declare `secrets[].generate` and named volumes (section S stdio boxes) encrypt the `store` volume with `BOX_STORE_KEY` in `packages/<box>.env`. Encryption protects a backup only when the key is stored separately from the volume copy. Put the volume archive in one place and `packages/<box>.env` plus the saved `.keystonectl/secrets/<box>.generated.env` in another (password manager or separate media). Without the key the volume copy is useless; with the key beside it, encryption adds nothing. Do not back up the `cache` volume (`ks-pkg-<box>-cache` on the installed server role): it is a cache the server rebuilds on the next start. Resource providers (`minio`, `postgres`) are not encrypted; env and volume can be stored together. See the package README for migration and `credsctl store rekey`.

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

`<tmp>` is mode 700 and is removed when the script returns. `<package>.secret` is mode 600. If the consumer already has a secret (the `secret_key` or `password` binding, or the password inside a `url` binding), that value is reused. Otherwise the file contains `token_hex(32)`. The secret file is the desired secret. A repeat with the same value changes nothing; a different value replaces the consumer's key or password. `service apply` keeps passing the saved value.

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
| 0 | Access is ready. The secret file is the desired secret. A repeat with the same value changes nothing; a different value replaces the consumer's key or password. |
| 2 | `provision_failed`, not retryable. The package or the installer is wrong. |
| 3 | `provision_failed`, retryable. The provider is not ready yet, or the admin key was rejected. |

Anything that is not JSON, has the wrong `interface`, or omits a field is `provision_failed`. The error text is the last 20 lines of stderr with secrets removed.

## Rotating a consumer secret

`service rotate-secret <name> <kind>` replaces the bucket key or database password of one catalog consumer. `<name>` is the installed service and `<kind>` is `s3` or `postgres`. The command confirms with `KEYSTONE_SETUP_SERVICE_<NAME>_CONFIRM_ROTATE=yes`, generates a new secret, runs provision with that secret, updates the service env file, checks access, and recreates the containers that receive the file. It does not print the secret. `service apply` still reuses the saved secret.

An external binding is refused (`rotate_external_unsupported`). keystonectl did not create that key. Remove the service and install it again with the new secret. A second apply does not pick up a new `KEYSTONE_SETUP_<KIND>_…_FILE` answer while the env file is already complete.

If the access check fails, the previous secret is written back and the error is `rotate_failed` (retryable). If that write-back also fails, the env files hold the new secret and the provider is in an unknown state; run `service apply <name>`.
