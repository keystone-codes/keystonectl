# 0004. Compose is generated from the manifest; a package's own compose file is not read

## Status

Accepted (keystonectl 0.1, 2026-10-12).

## Context

The old keystone-deploy “managed” mode cloned git on the server and asked the operator for
ports, variables, and networks. A foreign compose file can declare `ports`, `privileged`, host
bind mounts, `docker.sock`, or `network_mode: host`; validating arbitrary compose reliably is
not feasible. A typical service is an API, an MCP facade, and migrations — not one container.
Helm (0.6-4) needs the same launch description as compose.

## Decision

We describe how a service runs as data in the manifest, and keystonectl generates compose from it.
The `containers` section lists roles with digest-pinned images, ports, variables, secrets, named
volumes, health checks, commands, and `init` for one-shot jobs; `mcp.container` names the facade.
`keystonectl` writes `packages/<name>.yml`: one service `<package>-<role>` per container, bridge
network `ks-pkg-<name>` (ordinary bridge — isolation means “not on `keystone-mcp-upstreams`”, not
“no internet”), only the facade on `keystone-mcp-upstreams`, only `needs[].containers` on
`keystone-resources`, `init` before other containers. It includes the fragment via `include:` in
`docker-compose.packages.yml`, an override file the operator passes as a second `-f` after their
compose. A `compose.yaml` in the package repo is for author development only.

## Consequences

Dangerous fields have nowhere to appear: the schema uses `additionalProperties: false` with clear
errors. One manifest drives compose now and Helm values later. Images are digest-pinned; building
from source is `--local` only. Operator-specific architecture belongs in the operator layer
(ADR 0005), not in manifest fields.

## Alternatives considered

- **Read and whitelist package compose** — Easy to bypass.
- **Kubernetes manifest as the primary format** — Not every operator runs a cluster.
- **One container per package** — Real services need migrate + API + facade; would force
  `schema_version: 2` quickly.
- **Unpinned image plus operator questions** — The failure mode we leave behind.

## Links

- [Service package](../spec/service-package.md) (Containers and roles, What is forbidden and why),
  [Deployment layout](../spec/deployment-layout.md)
