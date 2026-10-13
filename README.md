# keystonectl

`keystonectl` installs and checks services that sit behind a Keystone MCP gateway.
It works on a deployment directory of a known layout and does not install keystone-mcp itself.

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

Why things are the way they are: [docs/adr/README.md](docs/adr/README.md). Why: [ADR 0006](docs/adr/0006-keystonectl-is-a-separate-product.md).
