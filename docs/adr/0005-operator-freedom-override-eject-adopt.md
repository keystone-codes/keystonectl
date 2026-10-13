# 0005. The operator can change anything: override, eject, adopt; a fragment hash prevents silent overwrites

## Status

Accepted (keystonectl 0.1, 2026-10-12).

## Context

The product goal is to assemble services like bricks, then let the operator rewrite compose for
their network, limits, neighbours, and proxy. Package authors do not know that environment. If
`upgrade` silently overwrites operator edits, people stop using the installer. Manifests must
stay strict so authors cannot request host access.

## Decision

We give the operator three levels of control and never silently overwrite their changes.
(1) Generated `packages/<name>.yml` belongs to `keystonectl` and is recreated on `upgrade`.
(2) Operator layer `packages/<name>.override.yml` is never written by the installer and is
included above the fragment; `check` may warn if override opens ports or adds privileges — that
is the operator's choice. (3) `service eject <name>` stops generating compose while registry,
snapshot, secrets, and `status` continue; `service adopt <name>` returns to generated mode after
confirmation. Fully custom compose uses `service add-remote`. The lock stores `fragment_sha256`;
direct edits to the generated file make `check` and `upgrade` stop with guidance to move changes
into override or eject. The same idea applies to the whole tree: `init --adopt` accepts an
existing folder and does not rewrite operator-owned files.

## Consequences

Package descriptions stay strict; freedom sits with whoever runs the server. `upgrade` behaviour
is predictable. After `eject`, compose no longer tracks package updates — an accepted trade-off.

## Alternatives considered

- **Emergency manifest fields (`privileged`, host ports)** — Moves operator decisions to authors.
- **Always overwrite generated fragments** — Destroys trust.
- **Three-way compose merge** — Complex and surprising.
- **Kustomize-style patch language** — Another format to learn.

## Links

- [Deployment layout](../spec/deployment-layout.md) (file owners, `init --adopt`, lock),
  [Service package](../spec/service-package.md) (Operator layer)
