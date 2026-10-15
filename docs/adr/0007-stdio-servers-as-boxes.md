# 0007. Stdio-only MCP servers are packaged as boxes; credential profiles live in the box

## Status

Accepted (keystonectl 0.2, 2026-10-15).

## Context

Third-party MCP servers (Jira and Confluence, Notion, Tavily, Yandex Tracker, Yandex Wiki, and
others) speak only stdio. keystone-mcp can run stdio children, but they share its OS user and
environment (keystone-mcp ADR 0012). Deployments often ran them behind an unauthenticated
stdio-to-HTTP bridge that backends called directly. One server must act for different people
and services with different accounts. The core keeps no business data and does not forward a
person's token ([0002](0002-handoff-through-agent-and-signed-link.md)).

## Decision

We package a stdio-only server as an ordinary HTTP package — a box — on one shared image,
`stdio-box`. Every provider is a directory of the
[stdio-box](https://github.com/keystone-codes/stdio-box/tree/v0.1.0) repository with its own
manifest; catalogs point at it with `path`. Credential profiles and their bindings live in the
box, not in keystone-mcp or keystonectl.

- The box is `kind: service`, `transport: http`. It checks the bearer and
  `X-Keystone-Identity`; only keystone-mcp calls it. A service that needs the stdio server
  comes to keystone-mcp as a client with its own service account. That is a gateway call, not
  service-to-service; [0002](0002-handoff-through-agent-and-signed-link.md) holds.
- Provider packages share one digest-pinned `stdio-box` image. The server is selected with
  manifest variables with defaults (`BOX_PROVIDER`, `BOX_COMMAND`) and installed at container
  start into a named cache volume using the provider lock file (hashed wheels or npm lock). The
  package and version in `BOX_COMMAND` must match the lock file.
- `mcp.tools` stays static; `keystonectl package wrap-stdio` fills it by running the server
  once inside the image.
- A profile is chosen from the assertion's `sub` and `roles`: `user:` binding, then role
  bindings in administrator order (first match), then `default`.
- The box does not check roles. keystone-mcp policy and the administrator decide who may manage
  accounts; there is no `BOX_ADMIN_ROLES`.
- Secrets enter through a one-time link on `public_paths: [/creds/]` or `credsctl` in the
  container, never through chat.
- `transport: stdio` in a service manifest stays rejected; no new manifest fields are required.
  keystonectl does not know about profiles.

## Consequences

One gateway door, roles, and audit; separate accounts for people and services; a new provider is
a catalog directory, not a new repository; the core and manifest schema stay unchanged. Costs:
first box start needs network (or a mirror, or a moved cache volume); an image release touches
every package on that digest; each box needs its own subdomain for the form (or `--defer-public`
and `credsctl` only); the container starts as root so a short spawner can run children and the
installer under separate uids. Boxes rely on keystonectl not setting `user:` and not dropping
Docker capabilities — recorded in [service-package](../spec/service-package.md). An operator who
drops capabilities via override puts the box in `shared_uid` mode, shown on `/healthz` and
`credsctl status`. Kubernetes Pod Security restricted (future Helm, [0004](0004-compose-generated-from-manifest.md))
forbids root — this box shape does not fit there yet. An administrator who grants a normal role
`<ns>_*` also grants credential management; the box does not prevent that.

## Alternatives considered

- **Stdio inside keystone-mcp** — Shared OS user with the gateway; core decision is not to install
  services inside keystone-mcp.
- **Unauthenticated stdio→HTTP bridge** — No bearer or signed assertion; one account for all;
  bypasses the gateway.
- **One repository and image per provider with a baked server** — More releases for the same
  isolation; rejected by the owner.
- **Separate secrets service called by boxes** — [0002](0002-handoff-through-agent-and-signed-link.md);
  the right long-term shape is a `secrets/v1` resource interface ([0003](0003-resources-as-interfaces.md)).
- **Refuse when several roles map to different accounts** — Unpredictable; replaced by explicit
  binding order.
- **Admin role check inside the box** — Services do not interpret role semantics; rejected.
- **`transport: stdio` in the manifest** — Same unsandboxed sharing as registry stdio.
- **User attributes in the core** — Violates [0002](0002-handoff-through-agent-and-signed-link.md).

## Links

- [Service package](../spec/service-package.md) (Containers and roles, Reserved),
  [Catalog](../spec/catalog.md) (`path`),
  [Packaging a service](../packaging-a-service.md#wrapping-a-stdio-server),
  [0002](0002-handoff-through-agent-and-signed-link.md),
  [0003](0003-resources-as-interfaces.md),
  [0004](0004-compose-generated-from-manifest.md),
  [0005](0005-operator-freedom-override-eject-adopt.md),
  [stdio-box v0.1.0](https://github.com/keystone-codes/stdio-box/tree/v0.1.0)
