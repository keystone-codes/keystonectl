# 0002. Services hand data to each other through the agent and a signed link

## Status

Accepted (keystonectl 0.1, 2026-10-12).

## Context

A service sometimes needs bytes that passed through another service. The old platform file
API had a service side: anyone with a service token could read any object, and the human
whose work was in flight was lost. The MCP Authorization specification forbids forwarding
a received access token (token passthrough). The `X-Keystone-Identity` assertion is signed
with a per-upstream secret; its audience is the registry entry name, so it is useless to
another upstream.

## Decision

We do not let services call each other on a person's behalf, and the installer gives them
no access to each other. The agent moves data using skills: it obtains a short-lived signed
link from one service (`files_get_download_link`; for uploads `files_create_upload_url`) and
passes that URL to another service's tool. Rights are checked when the link is issued (object
owner, role, audit in keystone-mcp). Links live for `FILE_MCP_LINK_TTL_SECONDS` (default 900 s).
`requires` in the manifest is an install-order dependency only. Bulk or long work uses the
service's own resource (`needs: s3`); a shared bucket is an explicit operator binding.

## Consequences

There are no service tokens between packages and no service API on file-mcp; each service is
an independent brick. A link carries authority for its lifetime; services must not log links.
Links must work from outside and from inside the service network (`service status` checks
hairpin). Not covered: hundreds of files per step or jobs longer than link TTL — use dedicated
`s3` for those.

## Alternatives considered

- **Service token between packages** — Recreates the old trust hole.
- **Token exchange (RFC 8693) and passing the human across services** — Identity model unset;
  core does not support it.
- **keystone-mcp as a bus between services** — Keystone is the door for clients, not middleware.
- **Shared bucket by default** — One leaked key reads everyone's objects.

## Links

- [Service package](../spec/service-package.md) (`requires`, `identity`),
  [S3 interface](../spec/resources/s3-v1.md)
- `keystone-mcp: docs/connecting-services.md`
- [MCP Authorization — security considerations (token passthrough)](https://modelcontextprotocol.io/specification/2025-03-26/basic/authorization#security-considerations)
