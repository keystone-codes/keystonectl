# Packaging a service for the Keystone catalog

## Who this is for

You built an MCP service and want operators to install it from a Keystone catalog the same way they install `file-mcp`: one command, the same gateway, the same resource model.

After you ship a package description, `keystonectl` generates the compose fragment, upstream registry entry, per-service secrets, optional public vhost, and bindings to shared resources (S3, Postgres). The operator only answers questions your manifest leaves open (domain, external resource endpoints, secrets without defaults). You keep owning the service code, container image, and manifest; you do not edit the operator's gateway compose or RBAC policy files.

## What your service must do

Treat this as a contract checklist. Each line links to the keystone-mcp service contract (tag `v0.5.0`):

- **Streamable HTTP MCP on `/mcp`; `/healthz` returns HTTP 200 when the service is ready** — [Requirements](https://github.com/keystone-codes/keystone-mcp/blob/v0.5.0/docs/connecting-services.md#requirements).
- **Verify `Authorization: Bearer` on every request to `/mcp`, including `list_tools`** — [Authenticating Keystone-MCP to your service](https://github.com/keystone-codes/keystone-mcp/blob/v0.5.0/docs/connecting-services.md#authenticating-keystone-mcp-to-your-service).
- **Verify `X-Keystone-Identity` on every `tools/call`**: signature with your assertion secret, `aud` equal to the registry entry name, valid expiry; take the caller only from the verified assertion** — [Signed identity](https://github.com/keystone-codes/keystone-mcp/blob/v0.5.0/docs/connecting-services.md#signed-identity-x-keystone-identity) and [examples/keystone-identity](https://github.com/keystone-codes/keystone-mcp/tree/v0.5.0/examples/keystone-identity).
- **Do not treat `X-Author-Email` as proof of identity** — anyone on the service network can set it. Catalog packages must use signed identity instead.
- **Do not publish host ports for MCP** — only paths listed in `public_paths` are exposed, via the vhost `keystonectl` writes.
- **Do not call other services on behalf of the user or expect installer-issued tokens to them** — agents pass files and data with signed links; bulk storage uses your own `needs: s3`, databases use `needs: postgres`.
- **Tool names are readable with your namespace prefix; input schemas are a public contract** — [Choosing a namespace](https://github.com/keystone-codes/keystone-mcp/blob/v0.5.0/docs/connecting-services.md#choosing-a-namespace).

Also meet the five-point [Checklist](https://github.com/keystone-codes/keystone-mcp/blob/v0.5.0/docs/connecting-services.md#checklist) in `connecting-services.md`. After install, `keystonectl` covers registry wiring and discovery; you remain responsible for health, namespace choice, and schema stability.

### Bearer token from Keystone

Keystone-MCP holds a bearer token per upstream and sends `Authorization: Bearer …` on discovery and every MCP call. Your service must reject missing or wrong tokens before MCP handling runs, including on `GET/POST/DELETE` to `/mcp` used for session setup and `list_tools`.

Environment variable names are yours; the manifest declares them under `mcp.bearer_env`. The installer generates the secret and places it in the MCP facade container's env file (see [identity](#identity) below).

### Signed identity: X-Keystone-Identity

On each `tools/call`, Keystone sends a signed JWT in `X-Keystone-Identity`. Verify it with the per-upstream secret the installer generates (`identity.secret_env`), require `aud` to match the registry entry name (`identity.audience_env`), enforce expiry (30 seconds skew is typical), and use only claims from the verified token for authorization inside the tool.

Reference implementations live under [examples/keystone-identity](https://github.com/keystone-codes/keystone-mcp/tree/v0.5.0/examples/keystone-identity). Catalog packages must set `identity.signed_assertion: required`.

### Passing files and data between services

Services do not receive cross-service tokens from `keystonectl`. An agent moves data by calling one tool, obtaining a signed URL or identifier, and passing that to another tool. If your service stores blobs, declare `needs: s3` and use the bound bucket and credentials. If it needs SQL, declare `needs: postgres` and use the provisioned database and user — do not run your own Postgres/MinIO solely for catalog consumers unless you publish a **resource** package (`kind: resource`).

## The package description

The normative field list is [`docs/spec/service-package.md`](spec/service-package.md) and [`schemas/service-package-v1.json`](../src/keystonectl/schemas/service-package-v1.json). This guide is the author-facing walkthrough; when they disagree, the spec wins and this document should be updated.

Each catalog entry is a directory (or release artifact) containing at least:

- `keystone-service.yaml` — the manifest (`schema_version: 1`).
- `policy.example.yaml` — optional example roles (never applied automatically).
- Container images referenced by digest.

There is no v0 `run:` block: even a single-container service uses `containers` with one role and `mcp.container` pointing at it.

### containers

Launch is data, not a hand-written compose file the installer reads. Under `containers`, map **role name → container spec** (`image` digest-pinned, `port`, optional `command`, `env`, `secrets`, named `volumes`, `healthcheck`, and `init: true` for one-shot jobs).

The JSON schema rejects `ports`, host paths, `privileged`, `network_mode: host`, and `docker.sock` mounts. A `compose.yaml` in your repository is fine for local dev; `keystonectl` ignores it (you may see a lint warning).

Package containers share a bridge network `ks-pkg-<name>`. They reach each other by role hostname (`http://api:8000`). Only the MCP facade joins `keystone-mcp-upstreams` (see [mcp](#mcp)).

### Files from the package

A container can receive one directory of your package, read-only, with `package_dir`:

```yaml
containers:
  mcp:
    image: ghcr.io/example/my-service:1.0.0@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    package_dir:
      from: provider      # a directory next to keystone-service.yaml; "." is the package root
      path: /srv/provider # where the container sees it
```

`keystonectl service apply` copies that directory from the pinned package version and mounts the copy read-only. It is not a host path: you cannot point it outside your package. Keep it small and plain: only files and directories, no links, devices or FIFOs, at most 900 KiB and 256 files. Files arrive with mode 0644 and directories with 0755, so an executable bit is not kept; run scripts through their interpreter. `keystonectl lint` checks all of this. Why: [ADR 0010](adr/0010-package-files-read-only.md).

### Services with several containers

Use distinct roles (`api`, `migrate`, `mcp`, …). Set `init: true` on jobs that must finish before others start. Point long-lived roles at each other with internal URLs. Only roles listed under `needs[].containers` (when narrowed) receive resource env vars and join resource provider networks; default is all package containers.

See the [docs-service example](#walkthrough-packaging-hello-upstream) sidebox and [`docs/spec/examples/docs-service.keystone-service.yaml`](spec/examples/docs-service.keystone-service.yaml).

### Packages without an MCP facade

Omit the entire `mcp` block for workers or sidecars another package depends on via `requires`. `keystonectl` still installs them, but they are not registered in keystone-mcp, publish no tools, and do not need `identity`.

### mcp

When present, `mcp` describes the facade container:

- `container` — role name under `containers` (must not be an `init` container).
- `namespace` — 1–64 characters `[A-Za-z0-9_-]`, not colliding with built-in tool prefixes.
- `tools` — bare tool names as returned by your service's `list_tools` (no `namespace_` prefix here).
- `critical` — tools whose role revocation should take effect immediately (admin hint for `critical_tools`).
- `bearer_env`, `transport`, `path`, `healthz` — wire format and env names.

Renaming or removing a tool name requires a new **major** manifest version.

### identity

For services with `mcp`:

```yaml
identity:
  signed_assertion: required
  secret_env: YOUR_IDENTITY_SECRET
  audience_env: YOUR_REGISTRY_NAME
```

`keystonectl` generates the secret and registry name, writes them to `packages/<name>.mcp.env`, and configures keystone-mcp to sign assertions the service verifies.

### needs and requires

**`needs`** — declare `s3/v1` or `postgres/v1` with `bind_env` (Postgres accepts a single `url` and/or separate fields; see [postgres-v1](spec/resources/postgres-v1.md)). Optional `extensions` and `needs[].containers` limit which roles receive credentials. Providers install once; each consumer gets its own bucket or database/user. **`requires`** — install order only, no credentials.

`keystone-resources` is one bridge shared by every consumer package and every provider. Compose makes each service name resolvable on every network it joins, so a container of one package can reach a container of another package that is also on `keystone-resources`. Treat internal roles as reachable: check the caller, do not rely on the network.

### public_paths

List URL path prefixes that must be reachable on the public service domain (for example signed download paths). Pair with `public_base_url_env` so the service learns `https://<operator-chosen-domain>`. The installer configures loopback port + nginx vhost; do not publish container ports for these paths yourself.

### recommended_policy

Point to `policy.example.yaml` with roles that allow only **this package's** published tool names (`<namespace>_<tool>`). `keystonectl` prints the file after install; it never merges into the operator's live policy. The policy skill consumes the printed `{role, allow}` list.

Authors do **not** need to document operator overrides: `packages/<name>.override.yml`, `service eject`, and `service adopt` are operator concerns (see [lifecycle.md](lifecycle.md)). Treat role names, env names, and volume names as compatibility contracts — changing them belongs in `CHANGELOG`.

## Walkthrough: packaging hello-upstream

This follows [hello-upstream-package](https://github.com/keystone-codes/hello-upstream-package/tree/v1.0.0) (`v1.0.0`), a minimal catalog service derived from keystone-mcp's `examples/hello-upstream`.

1. **Start from a working MCP HTTP service** — Streamable HTTP on `/mcp`, `/healthz`, stable tool schemas ([upstream example](https://github.com/keystone-codes/keystone-mcp/tree/v0.5.0/examples/hello-upstream)).

2. **Add bearer and signed identity** — gate `/mcp` with constant-time bearer comparison; on each `call_tool`, verify `X-Keystone-Identity` (audience, expiry, `mcp_target`) before running the handler ([server.py](https://github.com/keystone-codes/hello-upstream-package/blob/v1.0.0/server.py)). Add tests such as `test_mcp_requires_bearer` and `test_wrong_audience_rejected`.

3. **Dockerfile** — pin the base image by digest, install Python deps with hash pinning, run as non-root, define `HEALTHCHECK` ([Dockerfile](https://github.com/keystone-codes/hello-upstream-package/blob/v1.0.0/Dockerfile)).

4. **Build multi-arch, publish, record the index digest** — push to your registry, then set `containers.<role>.image` to `…@sha256:<64 hex>`. Until CI publishes the real digest, manifests may carry an obvious placeholder; catalog CI must match the tag you release.

5. **Write `keystone-service.yaml` and `policy.example.yaml`** — full manifest:

<!-- example: keystone-service.yaml -->

```yaml
schema_version: 1
kind: service
name: hello-upstream
version: 1.0.0
publisher: keystone-codes
summary: Demo MCP service on the low-level MCP SDK 1.x; two tools, no storage.
homepage: https://github.com/keystone-codes/hello-upstream-package

compat:
  keystone_mcp: ">=0.5.0,<2.0"
  contracts: [upstream-registry/1, identity-assertion/1]

mcp:
  container: mcp
  transport: http
  path: /mcp
  healthz: /healthz
  namespace: hello
  bearer_env: HELLO_BEARER_TOKEN
  tools: [greet, search, whoami]
  critical: []

identity:
  signed_assertion: required
  secret_env: HELLO_IDENTITY_SECRET
  audience_env: HELLO_IDENTITY_AUDIENCE

containers:
  mcp:
    image: ghcr.io/keystone-codes/hello-upstream@sha256:0000000000000000000000000000000000000000000000000000000000000000
    port: 8081
    env: []
    secrets: []
    volumes: []
    healthcheck: {path: /healthz, interval: 10s, start_period: 10s}

needs: []
requires: []
recommended_policy: policy.example.yaml
```

The `image` digest above is a **placeholder** for local lint and CI until release automation writes the published index digest; do not treat it as a released image.

6. **Check locally** — see [Check it locally](#check-it-locally).

7. **Install on a test deployment** — `keystonectl init --adopt <deployment-dir>` if needed, start compose with both `-f` files printed by adopt, add your catalog (`keystonectl catalog add file://<path>` warns `local_source`), then `keystonectl service apply hello-upstream`. Confirm with `keystonectl service status hello-upstream` and verify `hello_*` tools appear to agents.

> **If the service needs S3 and a public URL** — add `needs: s3`, `public_paths`, and `public_base_url_env`; full sample under [file-mcp manifest](#checklist) ([upstream repo](https://github.com/keystone-codes/file-mcp/blob/v1.0.0/keystone-service.yaml)).

> **If the service has an API, migrations, and Postgres** — multiple containers, `init: true` on migrate, `mcp.container: mcp`, and `needs` for postgres and s3:

<!-- example: keystone-service.yaml -->

```yaml
schema_version: 1
kind: service
name: docs-service
version: "1.0.0"
publisher: keystone-codes
summary: Document search behind an MCP facade, with its own API.
compat:
  keystone_mcp: ">=0.5.0,<2"
  contracts:
    - upstream-registry/1
    - identity-assertion/1
mcp:
  container: mcp
  namespace: docs
  tools:
    - search_docs
    - read_page
  bearer_env: DOCS_MCP_BEARER
identity:
  signed_assertion: required
  secret_env: DOCS_IDENTITY_SECRET
  audience_env: DOCS_REGISTRY_NAME
needs:
  - kind: postgres
    version: 1
    bind_env:
      url: DOCS_DATABASE_URL
    extensions:
      - pgcrypto
    containers:
      - migrate
      - api
  - kind: s3
    version: 1
    bind_env:
      endpoint: DOCS_S3_ENDPOINT
      region: DOCS_S3_REGION
      bucket: DOCS_S3_BUCKET
      access_key: DOCS_S3_ACCESS_KEY
      secret_key: DOCS_S3_SECRET_KEY
      path_style: DOCS_S3_PATH_STYLE
    containers:
      - api
containers:
  migrate:
    image: "ghcr.io/example/docs-service:1.0.0@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    init: true
    command:
      - docs-service
      - migrate
  api:
    image: "ghcr.io/example/docs-service:1.0.0@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    port: 8000
    volumes:
      - name: cache
        path: /var/cache/docs
    secrets:
      - name: DOCS_INTERNAL_TOKEN
        generate:
          bytes: 32
    healthcheck:
      path: /healthz
  mcp:
    image: "ghcr.io/example/docs-service:1.0.0@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    port: 8081
    env:
      - name: DOCS_API_URL
        default: "http://api:8000"
    secrets:
      - name: DOCS_INTERNAL_TOKEN
        generate:
          bytes: 32
    healthcheck:
      path: /healthz
```

Packages without an MCP facade omit `mcp` entirely; `keystonectl` installs them as dependencies only.

## Wrapping a stdio server

Use a box when the server speaks only stdio. One image, [`stdio-box`](https://github.com/keystone-codes/stdio-box/tree/v0.1.0/packages), runs every such server. In that repository each server is a directory `packages/<name>/` with a provider descriptor (`provider.yaml`) and a lock file (`requirements.txt` for `uvx`, or `package-lock.json` beside `package.json` for `npx`).

`keystonectl package wrap-stdio <provider-dir>` asks that image for the server's tools once, then writes `tools.snapshot.json`, `keystone-service.yaml` (with `mcp.tools` filled in), and `policy.example.yaml`. A catalog row points at the directory with `path`, for example `packages/atlassian`, so one repository holds several packages. keystonectl does not know about credential profiles; the command only writes files.

`--derived-image` writes a two-line `Dockerfile` (`FROM` the stdio-box image, `COPY` into `/opt/box/packages/<name>/`) for an author who publishes from their own repository.

`--stock-image` needs no image of your own. The manifest keeps the stdio-box image from `--image` and mounts the package directory into it read-only with [`package_dir`](#files-from-the-package) (`from: "."`, `path: /opt/box/provider`); `BOX_PROVIDER_DIR` points the box there. It cannot be combined with `--derived-image`. Run on an existing manifest, it adds `package_dir` and `BOX_PROVIDER_DIR` when they are missing.

The image reference written into a new manifest is the digest-pinned stdio-box 0.1.0 release (`ghcr.io/keystone-codes/stdio-box:0.1.0@sha256:4d6081cb1608c922c6c7299d30570a145187e65e4be274f9bb98aaaa7d67c519`), unless `--derived-image` selects a derived publisher image.

The box's own `creds_*` tools come from the image: wrap-stdio runs `stdio-box box-tools` in it and lists what it prints. An image without that subcommand (stdio-box 0.1.0) gets the ten `creds_*` tools that release serves; `result.box_tools_source` says which (`image` or `fallback`).

`--no-discovery` skips asking the server. The package then lists only the `creds_*` tools, and when the directory has no `tools.snapshot.json` the command writes an empty one (`tools: []`), because the box does not start without it.

`--publisher`, `--homepage` and `--version X.Y.Z` set those manifest fields, also on an existing manifest. Without them a new manifest gets `keystone-codes` and `1.0.0`, and an existing one keeps its values.

`mcp.critical` and the reader role of `policy.example.yaml` follow the server's `destructiveHint` and `readOnlyHint` annotations. When those are missing or wrong, `--read-only a,b` and `--destructive c,d` name the tools instead, by their published names (after the provider's `tools` rules). Each flag replaces the annotations as a whole; a name that is not published, or a name in both flags, is `provider_invalid`.

### Wrapping a remote HTTP server

An MCP server that only runs remotely over HTTP can sit in a box too: the box runs [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) as the stdio server, and `mcp-remote` forwards to the remote address with the account's token. Each person, service or role still uses its own account, entered on the box's form.

`provider.yaml`:

```yaml
schema_version: 1
name: example-remote
title: Example remote MCP server
server:
  command: 'npx -y mcp-remote@0.14.3 https://mcp.example.com/mcp --header "Authorization:Bearer ${EXAMPLE_TOKEN}"'
  bin: mcp-remote
  lock: package-lock.json
fields:
  - name: EXAMPLE_TOKEN
    type: string
    secret: true
    hint: "API token from the example.com account page"
    to: env
variants:
  - name: token
    fields: [EXAMPLE_TOKEN]
    checks:
      - tool:
          name: whoami
tools:
  normalize: snake
discovery:
  mode: manual
```

- The field has `to: env`: stdio-box puts the token into the server's environment under the field name, and `mcp-remote` itself replaces `${EXAMPLE_TOKEN}` in the header. The token is never part of the command text.
- `tools: {normalize: snake}` turns remote names such as `get-issue` into published names such as `get_issue`.
- `discovery: {mode: manual}`: there is no placeholder token a remote server would accept, so wrap-stdio cannot ask it for its tools.
- The check is a `tool` check: the box calls one remote tool (here `whoami`) with the account to prove the token works.
- Commit `package.json` and `package-lock.json` for `mcp-remote` beside `provider.yaml`.

Then write the package without asking the server:

    keystonectl package wrap-stdio packages/example-remote --stock-image --no-discovery --json

The package lists only the `creds_*` tools until a snapshot exists. After someone has entered an account, `creds_snapshot_tools` (stdio-box 0.2.0 and later) in the running box takes the remote tool list under that account; `credsctl snapshot export <id>` prints it, and `wrap-stdio --tools-snapshot <file>` turns it into the package's tools.

Limits:

- A server that only accepts a sign-in in the browser (OAuth without a token you can paste) does not fit: the box has no browser.
- The token goes only into a header. `mcp-remote` does not replace `${FIELD}` in the address, so a server that wants the token in the URL does not fit either.

## Check it locally

From your package directory:

```bash
keystonectl lint keystone-service.yaml
```

Expected summary line:

```text
0 errors, 0 warnings
```

Install the CLI with `uv tool install keystonectl` or run once with `uvx keystonectl lint …`. In CI, pin a version: `uvx keystonectl@<version> lint …`.

Lint prints one line per problem (`<path>: <message>`), errors before warnings, then the summary. Exit code `0` means no errors; `3` means `manifest_invalid`.

### Common lint failures

| Symptom | Fix |
|---|---|
| Image uses a tag instead of a digest | Set `image` to `name@sha256:<64 hex>` from your registry index. |
| Top-level `run:` | Move settings into `containers.<role>` and set `mcp.container`. |
| `mcp.container` points at missing or `init` role | Choose the long-lived MCP facade role. |
| `ports`, `privileged`, or host bind in a container | Remove them; use `public_paths` for external HTTP. |
| Empty `mcp.tools` or names including `namespace_` | List bare tool names exactly as `list_tools` returns. |
| `identity.signed_assertion` not `required` | Catalog services must verify assertions. |
| `recommended_policy` file missing | Add `policy.example.yaml` next to the manifest. |
| `package_dir.from` has a link, a FIFO, or is too large | Replace links with real files, remove special files, and stay under 900 KiB and 256 files. |

Fix digest errors by copying the registry index digest (`image: …@sha256:<64 hex>`). Replace legacy top-level `run:` with `containers` plus `mcp.container`. Point `mcp.container` at a long-running role, never an `init` job. Keep `policy.example.yaml` beside the manifest when `recommended_policy` is set.

## Getting into a catalog

### The keystone-codes catalog

Open a PR to [keystone-catalog](https://github.com/keystone-codes/keystone-catalog/tree/v1.0.0) following its README: immutable tag `v<version>`, digest checked in CI, green `check.yml`, review. The README links to this guide's [Checklist](#checklist) for author steps. A new version of a listed package is the same pull request with one more row; see [Releasing a new version](#releasing-a-new-version).

### Your own catalog

Host a git repository with root `catalog.yaml` per [`docs/spec/catalog.md`](spec/catalog.md). Validate with `keystonectl lint catalog.yaml`. Operators attach it via `keystonectl catalog add <url>`. The same package name in two catalogs requires `source/name` disambiguation at install time.

## Versions and compatibility

Use semantic versioning for the package `version` field and git tags `v<version>`. Tags are immutable. Declare supported gateway versions in `compat.keystone_mcp`; `keystonectl lint` and `keystonectl check` enforce the range against the deployment.

Bump the **major** version when you remove or rename tools in `mcp.tools`, break env or role names operators rely on, or change resource interfaces.

stdio-only servers are packaged as boxes; see [Wrapping a stdio server](#wrapping-a-stdio-server).

## Releasing a new version

Any change to a published package, even a comment in the manifest, is a new version. Tags `vX.Y.Z` and image tags `:X.Y.Z` are never moved or overwritten. A rebuilt image has a new digest even when the code did not change (labels and attestations differ). The old version stays in the catalog and in the registry so operators can stay on it or roll back.

1. On a branch `release/X.Y.Z`, set `version` in `keystone-service.yaml` and in every other place your build reads it, and add a changelog entry. Do not change `version` on `main` first: the tag workflow would look for an image that is not published yet.
2. Build and push the multi-arch image as `<registry>/<name>:X.Y.Z` from that branch. Refuse to overwrite an existing `:X.Y.Z`.
3. Write the published index digest into `containers.<role>.image` (and `provision.image` for a resource package) through a pull request to `main`; that pull request carries the version change and the digest.
4. After the merge, tag `vX.Y.Z` only when the registry digest of `:X.Y.Z` matches the manifest.
5. Open a catalog pull request that adds `{version: X.Y.Z, ref: vX.Y.Z, …}` under `packages.<name>.versions`. Keep the older rows.
6. Operators move with `keystonectl service upgrade <name>`; `service rollback <name>` returns to the previous version.

The [file-mcp](https://github.com/keystone-codes/file-mcp/tree/v1.0.0/.github/workflows) workflows implement steps 2–4. The same steps apply to resource packages; `minio` pins two images (server and provision client) and both carry the `:X.Y.Z` tag.

## Checklist

| Item | Verified by |
|---|---|
| `/mcp` Streamable HTTP; `/healthz` → 200 | `keystonectl service status` after install |
| Bearer checked on every `/mcp` request | Review; author tests (for example `test_mcp_requires_bearer`) |
| `X-Keystone-Identity` on every `tools/call`, valid `aud` and expiry | Review; author tests (for example `test_wrong_audience_rejected`) |
| `X-Author-Email` not used as identity | Review |
| Launch under `containers`; `mcp.container` is the facade; `init` only on one-shot roles | `keystonectl lint` (schema) |
| No published ports, host binds, or `privileged` | `keystonectl lint` (schema) |
| All container images digest-pinned | `keystonectl lint`; catalog `check.yml` compares digest to release tag |
| Storage and DB via `needs` (`postgres/v1`, `s3/v1`), not ad-hoc data containers | Review |
| `mcp.tools` lists every tool name | Review; `service apply` does not diff against live `list_tools` |
| `mcp.critical` chosen deliberately | Review |
| `recommended_policy` present and only allows this package's tools | `keystonectl lint` (file exists); review |
| `public_paths` only when strictly needed | Review |
| Immutable tag `v<version>` matches manifest `version` | Catalog CI |
| New version: branch release/X.Y.Z, image :X.Y.Z, digest pinned by PR, tag after the digest check | Catalog CI (check_refs.py); your release workflow |
| `compat.keystone_mcp` set | `keystonectl lint` |
| Five checklist items from `connecting-services.md` | Installer handles registry steps; author verifies service behavior, namespace, schemas |

For a full **file-mcp** manifest with S3 and public paths, see:

<!-- example: keystone-service.yaml -->

```yaml
schema_version: 1
kind: service
name: file-mcp
version: "1.0.0"
publisher: keystone-codes
summary: Upload and download links for large files on S3-compatible storage.
homepage: https://github.com/keystone-codes/file-mcp
compat:
  keystone_mcp: ">=0.5.0,<2.0"
  contracts: [upstream-registry/1, identity-assertion/1]
mcp:
  container: mcp
  transport: http
  path: /mcp
  healthz: /healthz
  namespace: files
  bearer_env: FILE_MCP_BEARER_TOKEN
  tools:
    - create_upload_url
    - get_download_link
    - get_file_info
  critical:
    - create_upload_url
    - get_download_link
identity:
  signed_assertion: required
  secret_env: FILE_MCP_IDENTITY_SECRET
  audience_env: FILE_MCP_REGISTRY_NAME
containers:
  mcp:
    image: ghcr.io/keystone-codes/file-mcp@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    port: 8081
    secrets:
      - name: FILE_MCP_SIGNING_SECRET
        generate: {bytes: 48}
    volumes: []
    healthcheck: {path: /healthz, interval: 10s, start_period: 20s}
needs:
  - kind: s3
    version: 1
    bind_env:
      endpoint: FILE_MCP_S3_ENDPOINT
      bucket: FILE_MCP_S3_BUCKET
      access_key: FILE_MCP_S3_ACCESS_KEY
      secret_key: FILE_MCP_S3_SECRET_KEY
      region: FILE_MCP_S3_REGION
      path_style: FILE_MCP_S3_PATH_STYLE
requires: []
public_paths:
  - /u/
  - /d/
public_base_url_env: FILE_MCP_PUBLIC_BASE_URL
recommended_policy: policy.example.yaml
```

The digest above is the one published for file-mcp 1.0.0; copy the digest your registry reports for your own image.
