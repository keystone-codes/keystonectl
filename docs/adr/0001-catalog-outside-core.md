# 0001. The service catalog lives outside the keystone-mcp core

## Status

Accepted (keystonectl 0.1, 2026-10-12).

## Context

From keystone-mcp 0.5.0 onward, core contracts stay frozen until 2.0. The core roadmap
(0.5-4, 0.5-7) explicitly excludes a marketplace and automatic package installation.
The core already exposes everything an installer needs: the upstream registry file
(`KEYSTONE_MCP_UPSTREAMS_PATH`; `auth.bearer_env`, `auth.identity_secret_env`), the RBAC
catalog snapshot (`KEYSTONE_MCP_RBAC_CATALOG_SNAPSHOT`), and environment variables.
Operators deploy with compose today and with Helm later (0.6-4).

## Decision

We keep the catalog, the package format, and the installer outside the keystone-mcp core.
The core gets no new fields and no new variables. `keystonectl` generates the registry,
snapshot, compose fragments, and service variables, and validates them with
`keystone-mcp upstream validate` and `keystone-mcp catalog validate --policy --snapshot`
without `--strict-unreachable` (roles appear only after install). Install state lives in
`services.lock.yaml` on the deployment machine, not in a git “desired services” repository.

## Consequences

The core stays small and frozen; the installer ships on its own release cadence. A new
installer need is a roadmap question for the core, not a new registry field. Until 0.6-3
the registry is read only at startup, so keystone-mcp is restarted after install. The
installer never merges package policy; `recommended_policy` in a package is an example only.

## Alternatives considered

- **Install command inside keystone-mcp** — Pulls git, docker, and TLS into frozen core.
- **Desired service list in git** — Owner chose environment state over versioned intent.
- **Curated marketplace with ratings** — Out of scope for this release.
- **Official MCP registry `server.json` compatibility** — No clear win yet.

## Links

- [Catalog](../spec/catalog.md), [Service package](../spec/service-package.md),
  [Deployment layout](../spec/deployment-layout.md)
- `keystone-mcp: docs/connecting-services.md`, `docs/rbac-catalog-registry.md`
