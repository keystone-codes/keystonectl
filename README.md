# keystonectl

keystonectl installs MCP services behind a Keystone MCP gateway from a catalog, checks service
packages and deployments, and gives agents a plan/apply interface.

- **Service manifest** — authors and operators share one package description (`keystone-service.yaml`).
- **Check before run** — `lint` and `check` validate packages and layout without starting containers.
- **Generate and lifecycle** — `init`, `service plan`/`apply`, lock file, and compose fragments for the gateway.

keystonectl does **not** install keystone-mcp itself and does **not** edit RBAC policy files.

## What keystonectl writes

`service apply` writes the package compose fragment, env files, `docker-compose.packages.yml` (the second `-f`), the upstream registry, the catalog snapshot, and the gateway names in `.env`. It does not edit `packages/<name>.override.yml` or the operator's `docker-compose.yml`. See [docs/generation.md](https://github.com/keystone-codes/keystonectl/blob/main/docs/generation.md).

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

Without `sources.yaml` the default catalog `keystone-codes` is used at tag `v1.2.0`.

Every command, its questions, and its error ids are in [docs/commands.md](https://github.com/keystone-codes/keystonectl/blob/main/docs/commands.md).

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

## Install through chat

Agents install services by following [`skills/keystonectl/`](https://github.com/keystone-codes/keystonectl/blob/main/skills/keystonectl/SKILL.md). Copy that directory into the agent's skills directory on the host where keystone-mcp runs. The skill ships in the repository, not in the wheel: copy `skills/keystonectl/` from a checkout or a release archive.

The agent runs `keystonectl` with `--json`, asks one question at a time, and takes a secret only as a path to a mode 600 file. It does not run Docker, edit compose files, or edit the RBAC policy. Recommended roles are handed to the `keystone-policy-admin` skill from keystone-mcp 0.5.0, which applies them after an administrator confirms, also for bundles. Without that skill, the agent shows the same `{role, allow}` list and the policy path for the administrator to apply.

## JSON

Pass `--json` for one JSON document on stdout. The document matches `src/keystonectl/schemas/output.schema.json`.
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

- Packaging your own service: [docs/packaging-a-service.md](https://github.com/keystone-codes/keystonectl/blob/main/docs/packaging-a-service.md)
- [Service package spec](https://github.com/keystone-codes/keystonectl/blob/main/docs/spec/service-package.md)
- [Catalog spec](https://github.com/keystone-codes/keystonectl/blob/main/docs/spec/catalog.md)
- [S3 resource interface](https://github.com/keystone-codes/keystonectl/blob/main/docs/spec/resources/s3-v1.md)
- [Postgres resource interface](https://github.com/keystone-codes/keystonectl/blob/main/docs/spec/resources/postgres-v1.md)
- [Deployment layout](https://github.com/keystone-codes/keystonectl/blob/main/docs/spec/deployment-layout.md)
- [Architecture decisions](https://github.com/keystone-codes/keystonectl/blob/main/docs/adr/README.md)
- [CLI reference](https://github.com/keystone-codes/keystonectl/blob/main/docs/cli.md)
- [Environment variables](https://github.com/keystone-codes/keystonectl/blob/main/docs/resources.md)

Why things are the way they are: [ADR 0006](https://github.com/keystone-codes/keystonectl/blob/main/docs/adr/0006-keystonectl-is-a-separate-product.md).

## Verification

`scripts/e2e.sh` runs the release checks against a real keystone-mcp: the open example and the keystone-deploy MCP layout. Flags, scenarios, and how to read the JSONL report are in [scripts/e2e/README.md](https://github.com/keystone-codes/keystonectl/blob/main/scripts/e2e/README.md). The manual VM and stand protocol, with an empty result column until someone runs it, is [docs/release-checklist.md](https://github.com/keystone-codes/keystonectl/blob/main/docs/release-checklist.md).

## Compatibility

The `keystonectl` version is independent of keystone-mcp. Service packages declare supported
keystone-mcp versions in `compat.keystone_mcp`; `keystonectl check` validates them against your
deployment.

## License

Apache-2.0 — see [LICENSE](https://github.com/keystone-codes/keystonectl/blob/main/LICENSE).
