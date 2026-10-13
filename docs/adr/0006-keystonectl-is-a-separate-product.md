# 0006. keystonectl is a separate product, not part of keystone-deploy or keystone-mcp

## Status

Accepted (keystonectl 0.1, 2026-10-12).

## Context

Revision 1 placed the installer inside keystone-deploy. keystone-deploy targets the
`multi-agent` platform at partners, while the installer is needed by anyone who runs
keystone-mcp from the open `examples/production/docker-compose.yml` without keystone-deploy.
It cannot live in keystone-mcp either (ADR 0001). Bash in `setup.sh` is hard to test and
unsuitable for structured agent output.

## Decision

We ship keystonectl as its own open-source product. Repository `keystone-codes/keystonectl`,
Apache 2.0, PyPI package `keystonectl`, module and CLI `keystonectl`, skill `keystonectl`.
Version is independent of keystone-mcp; compatibility uses manifest `compat.keystone_mcp` and
`keystonectl check`. The only environment contract is deployment layout v1
(`docs/spec/deployment-layout.md`); `init --adopt` accepts keystone-mcp `examples/production/`
and keystone-deploy `mcp/`. keystone-deploy calls `keystonectl` from `setup.sh` like any operator.
The certificate cycle for service domains is Python inside the product (`tls.py`). `keystonectl`
does not install keystone-mcp, does not edit RBAC policy, and knows nothing about `multi-agent`.

## Consequences

Any keystone-mcp operator can use it; there is an extra repository and PyPI release.
keystone-deploy drops “managed” mode and `EXTERNAL_APIS`. Chat install relies on `--json`,
`plan`/`apply`, and exit codes instead of parsing bash.

## Alternatives considered

- **Inside keystone-deploy** — Wrong audience.
- **Inside keystone-mcp** — Frozen core plus git, docker, TLS.
- **Bash only in setup.sh** — Untested and agent-hostile.
- **Name `keystone-services`** — Longer and narrows the product; PyPI fallback `keystone-mcp-ctl`.

## Links

- [Deployment layout](../spec/deployment-layout.md), [README](../../README.md)
- `keystone-mcp: examples/production/docker-compose.yml`
