# Architecture decision records (ADR)

This folder holds short, durable notes on decisions that shape the service catalog,
package format, resource interfaces, deployment layout, or `keystonectl` behaviour.
Each record answers one question: *why is it built this way?*

## When to add a record

Write a new ADR when a change would break compatibility in `docs/spec/*`: package
manifest fields, catalog or sources format, resource interface parameters or promises,
deployment layout files, or installer behaviour operators depend on.

Do not edit an accepted record in place. Supersede it: add a new numbered file and set
the old record's status to **Superseded by** `NNNN-short-slug.md` (use a markdown link in that record).

## Template

Copy this skeleton into `NNNN-short-slug.md` (four-digit id, kebab-case slug):

```markdown
# NNNN. Decision title

## Status
Accepted (keystonectl X.Y, YYYY-MM-DD). | Superseded by [NNNN](NNNN-slug.md).

## Context
What was happening and what hurt. Facts: files, fields, variables.

## Decision
We … (one to three sentences), then details as a list.

## Consequences
What this gives and what it costs.

## Alternatives considered
- Option — one or two sentences on why not.

## Links
- Relative paths to documents in this repository.
```

Keep each record under roughly 80 lines. Use short sentences and present tense.

## Index

| ID | Title | Status |
|---|---|---|
| [0001](0001-catalog-outside-core.md) | The service catalog lives outside the keystone-mcp core | Accepted (keystonectl 0.1, 2026-10-12) |
| [0002](0002-handoff-through-agent-and-signed-link.md) | Services hand data to each other through the agent and a signed link | Accepted (keystonectl 0.1, 2026-10-12) |
| [0003](0003-resources-as-interfaces.md) | Resources are versioned interfaces: one provider per kind, an isolated slice per consumer | Accepted (keystonectl 0.1, 2026-10-12) |
| [0004](0004-compose-generated-from-manifest.md) | Compose is generated from the manifest; a package's own compose file is not read | Accepted (keystonectl 0.1, 2026-10-12) |
| [0005](0005-operator-freedom-override-eject-adopt.md) | The operator can change anything: override, eject, adopt; a fragment hash prevents silent overwrites | Accepted (keystonectl 0.1, 2026-10-12) |
| [0006](0006-keystonectl-is-a-separate-product.md) | keystonectl is a separate product, not part of keystone-deploy or keystone-mcp | Accepted (keystonectl 0.1, 2026-10-12) |
| [0007](0007-stdio-servers-as-boxes.md) | Stdio-only MCP servers are packaged as boxes; credential profiles live in the box | Accepted (keystonectl 0.2, 2026-10-15) |
| [0010](0010-package-files-read-only.md) | Package files mounted read-only | Accepted (keystonectl 0.3, 2026-10-19) |
| [0011](0011-local-packages-without-git.md) | Local packages live in the deployment, without git | Accepted (keystonectl 0.3, 2026-10-19) |
