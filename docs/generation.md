# What `service apply` writes

`keystonectl service apply <package>` turns a `keystone-service.yaml` into files under the deployment root. It does not edit the operator's `docker-compose.yml` and it does not edit RBAC policy.

Paths below use the `docs-service` example in `docs/spec/examples/docs-service.keystone-service.yaml`.

## Files

| Path | What it is |
|---|---|
| `packages/docs-service.yml` | Compose fragment. Generated. Do not edit it. |
| `packages/docs-service.env` | Shared env. Every container of the package receives it. |
| `packages/docs-service.mcp.env` | Bearer, assertion secret, and registry audience. Only the `mcp` container. |
| `packages/docs-service.postgres.env` | Postgres bind values. Only the roles listed on that need (`migrate`, `api`). |
| `packages/docs-service.s3.env` | S3 bind values. Only `api`. |
| `packages/docs-service.api-mcp.env` | A secret declared by only some roles (`DOCS_INTERNAL_TOKEN` on `api` and `mcp`). One value, only those containers. The lock accepts a single extra segment, so the roles are joined with `-`. |
| `docker-compose.packages.yml` | Second Compose file. `docker compose -f docker-compose.yml -f docker-compose.packages.yml`. |
| `keystonectl/upstreams.yaml` | Upstream registry. One entry per package that has `mcp`. |
| `keystonectl/catalog-snapshot.yaml` | Tool names keystone-mcp is allowed to publish. |
| `.env` | `KEYSTONE_MCP_UPSTREAM_<NAMESPACE>_BEARER` and `_IDENTITY_SECRET` inside the managed block. Mode 600. |

A secret with `generate` that every role declares goes in the shared file. The same name on several roles is one value. The bearer and the assertion secret are not in the shared file.

Env files are mode 600. Compose lists them as `./…` paths, which are relative to `packages/`.

## Networks

Every package gets a bridge named `ks-pkg-<name>`. It is a normal bridge: containers on it can reach the internet. Only the `mcp` container joins `keystone-mcp-upstreams`, so the gateway has one facade and the other roles stay off that network. Roles that need a resource, and the resource's own container, join `keystone-resources`.

## Init containers

An `init` container has `restart: "no"` and no healthcheck. Every other container waits for it with `service_completed_successfully`.

## Your own changes

Put them in `packages/<name>.override.yml`. keystonectl never writes, deletes, or opens that file. When the file exists, `docker-compose.packages.yml` includes it in the same `path` list as the fragment. `check` warns `override_not_included` when the packages file does not mention an override that is on disk, and `override_widens_access` when the merged model publishes a non-loopback port or adds privileges.

`keystonectl service eject` stops keystonectl from rewriting the fragment. Until then, a hand-edited fragment fails apply with `fragment_modified`. `service adopt` generates it again.

A service you run yourself is a fourth path. `keystonectl service add-remote` records the URL in the registry and the lock. The service is entirely the operator's: keystonectl does not generate a fragment, an override, or a container for it.

## Checks before anything is written

keystone-mcp `upstream validate` and `catalog validate` run in `docker run --network none` from the image recorded in the lock. `--strict-unreachable` is not passed: tools that no role can call are a warning (`tools_unreachable`), not a failure. If the candidate registry makes previously valid policy fail in a new way, apply stops with `policy_breaks`. The same unmatched set that already failed is a warning (`policy_already_inconsistent`). keystonectl parses the text these commands print on stderr/stdout to find those outcomes; `docs/adr/0001-catalog-outside-core.md`, "What keystonectl relies on beyond the frozen contract" lists why that text is not a contract.

## Restart

A change to the keystone-mcp wiring in `docker-compose.packages.yml` recreates that service with `up -d --no-deps`. A registry change restarts it (`docker compose restart`), because this keystone-mcp does not reload the registry on `SIGHUP`. A snapshot-only change sends `SIGHUP`. If keystone-mcp is not running, apply says so and does not start it. An unchanged apply does not pull, start, or signal anything.
