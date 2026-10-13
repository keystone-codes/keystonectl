# keystonectl

keystonectl installs MCP services behind a Keystone MCP gateway from a catalog, checks service
packages and deployments, and gives agents a plan/apply interface.

- **Service manifest** — authors and operators share one package description (`keystone-service.yaml`).
- **Check before run** — `lint` and `check` validate packages and layout without starting containers.
- **Generate and lifecycle** — `init`, `service plan`/`apply`, lock file, and compose fragments for the gateway.

keystonectl does **not** install keystone-mcp itself and does **not** edit RBAC policy files.

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
keystonectl --root <deployment-dir> service plan file-mcp --json
```

## Run from a checkout

```bash
uv run keystonectl --version
uv run keystonectl init <directory>
uv run keystonectl lint <manifest-or-directory>
uv run keystonectl --root <directory> check
```

Pass `--json` for one JSON document on stdout (see `schemas/output.schema.json`). Exit codes 0–3
are documented in the CLI reference.

## Documentation

- [Service package spec](docs/spec/service-package.md)
- [Catalog spec](docs/spec/catalog.md)
- [S3 resource interface](docs/spec/resources/s3-v1.md)
- [Postgres resource interface](docs/spec/resources/postgres-v1.md)
- [Deployment layout](docs/spec/deployment-layout.md)
- [Architecture decisions](docs/adr/)
- [CLI reference](docs/cli.md)
- [Environment variables](docs/resources.md)

## Compatibility

The `keystonectl` version is independent of keystone-mcp. Service packages declare supported
keystone-mcp versions in `compat.keystone_mcp`; `keystonectl check` validates them against your
deployment.

## License

Apache-2.0 — see [LICENSE](LICENSE).
