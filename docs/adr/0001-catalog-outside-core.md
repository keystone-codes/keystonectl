# 0001. The service catalog lives outside the keystone-mcp core

## Status

Accepted (keystonectl 0.1, 2026-10-12); revised 2026-10-14 to list the core internals
keystonectl relies on beyond the frozen contract.

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

## What keystonectl relies on beyond the frozen contract

Packages declare `>=0.5.0,<2.0`, but the installer also depends on things the core's
compatibility contract does not cover. `keystone-mcp-ro/docs/compatibility.md:260` is
explicit about the boundary: "Exit codes are part of the contract; command output text
is not." None of the four points below are promised to stay stable across a 0.5.x
release.

- **An internal Python module, run inside the container.**
  `src/keystonectl/generate/install.py:715-728` and `:1160-1170`, and
  `src/keystonectl/remote.py:415-420` run `python -m keystone_mcp.rbac.catalog` to list
  tool names the running keystone-mcp has already published. The core does not promise
  this module's existence, name, or output; it is not part of the frozen CLI. When the
  command fails, the installer currently returns an empty list without a warning — a
  known weak spot. The fix (0.2): warn `builtin_tools_unavailable` instead of staying
  silent.
- **Command output text, parsed with regular expressions.**
  `src/keystonectl/generate/validators.py:8-11` matches the lines `rbac policy leaves …
  tool(s) unreachable by any role: …` and `upstream discovery: … of … upstream(s)
  unavailable (…); proxying the rest`; `validators.py:26-27` (`is_namespace_error`) looks
  for the word `namespace` anywhere in the text. The core only commits to exit codes
  here, not to this wording.
- **A private, version-tagged copy of the built-in tool list.**
  `src/keystonectl/keystone_mcp_rules.py:15` pins `BUILTIN_TOOL_NAMES_VERSION = "0.5.0"`
  and `:38-65` carries a copy of `BUILTIN_TOOL_NAMES`. The core does not publish this
  list as a contract. Against keystone-mcp 0.5.1 (or any later 0.5.x), keystonectl cannot
  tell whether the copy is still accurate, so `plan.py:487-494` adds the warning
  `builtin_list_may_be_stale` to every plan.
- **The timing of the registry read.**
  Keystone-mcp's `config.py:734-746` loads the upstream registry while resolving
  `Settings`, i.e. once at process startup. This ADR's Consequences section relies on
  that and tells operators "Until 0.6-3 the registry is read only at startup, so
  keystone-mcp is restarted after install." The core's `compatibility.md` does not
  document this timing, so a later 0.5.x release could start reloading the registry
  (as already happens for snapshot-only changes, via `SIGHUP`) without that counting as
  a compatibility break.

## Asks for keystone-mcp 0.6

To retire the four reliances above, keystone-mcp 0.6 should add:

- A `keystone-mcp catalog builtin --json` CLI command that returns the built-in tool
  names as structured data, replacing `python -m keystone_mcp.rbac.catalog`.
- A machine-readable output mode for `keystone-mcp upstream validate` and
  `keystone-mcp catalog validate` (`--json`), so keystonectl stops parsing message text.
- A line in `compatibility.md` documenting when the registry is read (today: once, at
  startup), so installers do not have to infer it from behavior.

## Alternatives considered

- **Install command inside keystone-mcp** — Pulls git, docker, and TLS into frozen core.
- **Desired service list in git** — Owner chose environment state over versioned intent.
- **Curated marketplace with ratings** — Out of scope for this release.
- **Official MCP registry `server.json` compatibility** — No clear win yet.

## Links

- [Catalog](../spec/catalog.md), [Service package](../spec/service-package.md),
  [Deployment layout](../spec/deployment-layout.md)
- `keystone-mcp: docs/connecting-services.md`, `docs/rbac-catalog-registry.md`
