# keystonectl

keystonectl installs MCP services behind a Keystone MCP gateway from a catalog, checks service
packages and deployments, and gives agents a plan/apply interface.

- **Service manifest** — authors and operators share one package description (`keystone-service.yaml`).
- **Check before run** — `lint` and `check` validate packages and layout without starting containers.
- **Generate and lifecycle** — `init`, `service plan`/`apply`, lock file, and compose fragments for the gateway.

keystonectl does **not** install keystone-mcp itself and does **not** edit RBAC policy files.

## What keystonectl writes

`service apply` writes the package compose fragment, env files, `docker-compose.packages.yml` (the second `-f`), the upstream registry, the catalog snapshot, and the gateway names in `.env`. It does not edit `packages/<name>.override.yml` or the operator's `docker-compose.yml`. See [docs/generation.md](docs/generation.md).

## Install

Preferred:

```bash
uv tool install keystonectl
```

Alternatives:

```bash
pipx install keystonectl
uvx keystonectl --version
```

## Quick start

```bash
keystonectl init --adopt <deployment-dir>
keystonectl --root <deployment-dir> catalog list
keystonectl --root <deployment-dir> bundle install minimal
keystonectl --root <deployment-dir> service status
```

Every command, its questions, and its error ids are in [docs/commands.md](docs/commands.md).

A service you run yourself is not a package. `keystonectl service add-remote` records its URL, namespace, and filters, and writes a mode 600 handoff file for the team that runs it. keystonectl does not generate compose for that service.

## Run from a checkout

```bash
uv run keystonectl --version
uv run keystonectl init <directory>
uv run keystonectl init --adopt <directory>
uv run keystonectl lint <manifest-or-directory>
uv run keystonectl --root <directory> check
uv run keystonectl --root <directory> catalog list
uv run keystonectl --root <directory> service plan <package>
uv run keystonectl --root <directory> service apply <package>
```

`service install` is another name for `service apply`. `package lint` is another name for `lint`.

## JSON

Pass `--json` for one JSON document on stdout. The document matches `schemas/output.schema.json`.
`--json` does not prompt. Secrets read during the run are replaced with `***`.

Exit codes are the same for text and JSON:

| Code | Meaning |
|---|---|
| 0 | Done. Warnings are allowed. |
| 1 | The command was used incorrectly. |
| 2 | An answer is missing. Nothing was changed. |
| 3 | A rule failed, or something outside keystonectl failed. `retryable` is true only for this code. |

## Answers

Non-secret answers can be given with `--set KEY=VALUE` or the environment.
The name of a per-service answer is `KEYSTONE_SETUP_SERVICE_<PKG>_<SUFFIX>`,
with the package name in upper case and `-` turned into `_`.
A secret is only a path to a mode 600 file. The variable name ends in `_FILE`.
`--set` refuses secret variables.

Lookup order: `--set`, the environment, the answer stored in the lock, then the manifest default.

## Files

| Path | Role |
|---|---|
| `services.lock.yaml` | What is installed. Not committed. |
| `sources.yaml` | Catalog sources. Absent means the default keystone-codes catalog. |
| `keystonectl/upstreams.yaml` | Upstream registry read by keystone-mcp. |
| `keystonectl/catalog-snapshot.yaml` | Catalog snapshot read by keystone-mcp. |
| `.keystonectl/` | Cache of catalogs and packages. |

Start the stack with both compose files, for example:

```bash
docker compose -f docker-compose.yml -f docker-compose.packages.yml up -d
```

`init --adopt` prints that command. Without the second file, keystone-mcp does not see the registry, the snapshot, or the service network.

## Documentation

- [Service package spec](docs/spec/service-package.md)
- [Catalog spec](docs/spec/catalog.md)
- [S3 resource interface](docs/spec/resources/s3-v1.md)
- [Postgres resource interface](docs/spec/resources/postgres-v1.md)
- [Deployment layout](docs/spec/deployment-layout.md)
- [Architecture decisions](docs/adr/README.md)
- [CLI reference](docs/cli.md)
- [Environment variables](docs/resources.md)

Why things are the way they are: [ADR 0006](docs/adr/0006-keystonectl-is-a-separate-product.md).

## Compatibility

The `keystonectl` version is independent of keystone-mcp. Service packages declare supported
keystone-mcp versions in `compat.keystone_mcp`; `keystonectl check` validates them against your
deployment.

## License

Apache-2.0 — see [LICENSE](LICENSE).
