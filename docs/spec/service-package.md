# Service package manifest (`schema_version` 1)

For a step-by-step guide see [../packaging-a-service.md](../packaging-a-service.md).

A package ships one `keystone-service.yaml`. The file is the contract between the author and the operator: what the package runs, which resource interfaces it needs, and which tools it publishes. The installer reads that file and asks the operator only for what the file does not already say.

The machine-readable contract is a single Draft 2020-12 document, [`src/keystonectl/schemas/service-package-v1.json`](../../src/keystonectl/schemas/service-package-v1.json) (`$id`: `https://keystone.codes/schemas/service-package-v1.json`). Validators load that file. `default` values in the schema are notes for the installer. The validator does not fill them in.

`additionalProperties` is false at every object. A key that is not listed here is rejected.

## Why a manifest

Without a manifest, every operator reconstructs the same facts by hand: image, port, environment, networks, bearer, and the signing secret. Mistakes show up only after the service is already running. The manifest records those facts once. A package that never calls the installer can still lint the file in its own CI.

There are two kinds:

- `kind: service` — a workload. It may publish an MCP facade, or it may be an ordinary service with no MCP block.
- `kind: resource` — a provider of one resource interface (`s3` or `postgres`, version 1). Consumers declare the interface with `needs`. They do not embed the provider.

Launch is data under `containers`. The installer turns that data into a compose fragment. A `compose.yaml` next to the manifest is fine for the author's own development and is not read by the installer.

## Service example

[file-mcp](examples/file-mcp.keystone-service.yaml) is one long-running container that is also the MCP facade. It needs an `s3/v1` resource, publishes signed public paths `/u/` and `/d/`, and checks both the bearer and `X-Keystone-Identity`.

## Multi-container example

[docs-service](examples/docs-service.keystone-service.yaml) has three roles:

- `migrate` runs once, before the others, and applies database migrations.
- `api` serves the application and is the only container that receives S3 settings.
- `mcp` is the facade. It calls the API at `http://api:8000`.

`migrate` and `api` receive the Postgres URL. `api` and `mcp` share one generated secret, `DOCS_INTERNAL_TOKEN`, declared the same way on both.

## Resource example

- [minio](examples/minio.keystone-service.yaml) provides `s3/v1`. One consumer gets one bucket and one user (`bucket_and_user`).
- [postgres](examples/postgres.keystone-service.yaml) provides `postgres/v1`. One consumer gets one database and one user (`database_and_user`).

Digests in the examples are fictional (`sha256` plus 64 hex digits). Replace them with the digest of the image you publish.

## Fields

### Both kinds

| Field | Required | Constraints | Default |
|---|---|---|---|
| `schema_version` | yes | integer `1` | |
| `kind` | yes | `service` or `resource` | |
| `name` | yes | `^(?!.*--)[a-z][a-z0-9-]{1,38}[a-z0-9]$` (3–40 characters). `keystone` and `keystone-*` are reserved | |
| `version` | yes | three numeric components, no `v` and no suffix: `1.2.3` | |
| `publisher` | yes | `^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$` | |
| `summary` | yes | 1–200 characters, one line | |
| `homepage` | no | `^https://\S+$` | |
| `containers` | yes | 1–8 roles. See [Containers and roles](#containers-and-roles) | |

The order of keys under `containers` is the order of `init` containers. Do not sort it.

### `kind: service` only

These fields are rejected on a resource.

| Field | Required | Constraints | Default |
|---|---|---|---|
| `compat.keystone_mcp` | yes | string, 1–100 characters. Parsed by the same rules as catalog `keystone_mcp`; see [Compatibility with keystone-mcp](catalog.md#compatibility-with-keystone-mcp) | |
| `compat.contracts` | yes | unique values from `upstream-registry/1` and `identity-assertion/1`. Both are required when `mcp` is set. Empty is allowed when there is no `mcp` | |
| `mcp` | no | Facade block. Without it the package still installs, and it is not registered with keystone-mcp | |
| `mcp.container` | yes, if `mcp` | Role of the facade. Only this container is registered, and only it receives the bearer and the assertion secret | |
| `mcp.transport` | no | `http` | `http` |
| `mcp.path` | no | `^/[A-Za-z0-9._~/-]{0,99}$` | `/mcp` |
| `mcp.healthz` | no | same pattern as `mcp.path` | `/healthz` |
| `mcp.namespace` | yes, if `mcp` | `^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$`, at most 32 characters. No dash: a dash would publish names like `a-b_tool`, while upstream variable names replace `-` with `_` | |
| `mcp.tools` | yes, if `mcp` | 1–200 unique `^[a-z][a-z0-9_]{0,63}$`, without the namespace | |
| `mcp.critical` | no | unique; each name must also be in `mcp.tools` | |
| `mcp.bearer_env` | yes, if `mcp` | variable on the facade that receives the bearer keystone-mcp sends as `Authorization: Bearer` | |
| `identity` | yes if `mcp` is set; forbidden otherwise | | |
| `identity.signed_assertion` | yes | `required`. The service verifies `X-Keystone-Identity` | |
| `identity.secret_env` | yes | facade variable for the signing secret. keystone-mcp receives the same value as `KEYSTONE_MCP_UPSTREAM_<NS>_IDENTITY_SECRET` | |
| `identity.audience_env` | yes | facade variable for the expected `aud`, which is the registry entry name | |
| `needs` | no | up to 4 entries, at most one per `kind`. See below | |
| `requires` | no | up to 16 unique package names (same pattern as `name`). A package cannot require itself | |
| `public_paths` | no | 1–8 unique `^/[a-z0-9][a-z0-9._-]{0,30}/$` | |
| `public_container` | no | role that serves `public_paths`. Required when `public_paths` is set and `mcp` is absent | `mcp.container` |
| `public_base_url_env` | yes if `public_paths` is set; forbidden otherwise | variable that receives `https://<service domain>`. Every long-running container gets it. The value is not a secret | |
| `recommended_policy` | no, and only when `mcp` is set | `^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]{1,128}\.ya?ml$`. An example policy file inside the package. The installer prints it and does not merge it | |

`requires` orders installation only; services do not call each other on a person's behalf. The
facade verifies `X-Keystone-Identity` with `identity.secret_env` and checks `aud` against
`identity.audience_env` (the registry entry name). Why: [ADR 0002](../adr/0002-handoff-through-agent-and-signed-link.md).

`needs[]`:

| Field | Required | Constraints | Default |
|---|---|---|---|
| `kind` | yes | `s3` or `postgres` | |
| `version` | yes | `1` | |
| `bind_env` | yes | names of variables, same rule as `env[].name`. `s3` requires exactly `endpoint`, `region`, `bucket`, `access_key`, `secret_key`, `path_style`. `postgres` accepts `{url}`, or all of `host`, `port`, `database`, `user`, `password`, `sslmode`, or both forms together. A partial set of the six names is an error | |
| `containers` | no | roles that receive the resource parameters and join the `keystone-resources` network | every container in the package, including `init` |
| `extensions` | no | `postgres` only. Up to 16 unique `^[a-z][a-z0-9_]{0,62}$` | |

When `containers` is set and does not name every container in the package, the installer writes the resource variables to `packages/<name>.<kind>.env` and gives that file only to the listed roles. If the key is omitted, or it names every container, the variables go in the shared package env file.

What an `s3` provider must promise, and how `s3_probe` checks it, is [resources/s3-v1.md](resources/s3-v1.md). What a `postgres` provider must promise, and how `postgres_probe` checks it, is [resources/postgres-v1.md](resources/postgres-v1.md). Optional `needs[].extensions` names extensions the provider creates in the consumer database.

### `kind: resource` only

These fields are rejected on a service.

| Field | Required | Constraints | Default |
|---|---|---|---|
| `provides.kind` | yes | `s3` or `postgres` | |
| `provides.version` | yes | `1` | |
| `provides.container` | yes | role that serves the interface. Consumers receive its address | |
| `provision.per_consumer` | yes | `bucket_and_user` for `s3`, `database_and_user` for `postgres` | |
| `provision.image` | yes | one-shot image that grants access, pinned by digest (same pattern as `image`) | |
| `provision.script` | yes | path inside the package: `^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]{1,128}$` | |
| `provision.admin_env.user` | yes | a name declared in `env` or `secrets` of `provides.container` | |
| `provision.admin_env.password` | yes | a name declared in `secrets` of `provides.container` | |

### Container (`containers.<role>`)

The role key matches `^[a-z](?:[a-z0-9-]{0,22}[a-z0-9])?$`.

| Field | Required | Constraints | Default |
|---|---|---|---|
| `image` | yes | pinned by digest. A tag before the digest is allowed. `latest` is not | |
| `init` | no | `true`: one-shot job (migrations, seed data), run before the other containers. Several `init` containers run in manifest order | `false` |
| `port` | no | 1–65535, inside the container, not published on the host. Required on `mcp.container`, `public_container`, and `provides.container`. Forbidden on `init` | |
| `command` | no | 1–64 strings | |
| `user` | no | numeric `uid` or `uid:gid` | |
| `env` | no | up to 100 `{name, default?, required?, description?}`. `name` matches `^[A-Z][A-Z0-9_]{0,63}$` and must not start with `KEYSTONE_MCP_` or `KEYSTONE_SETUP_`. `required` and `default` together are an error | `required: false` |
| `secrets` | no | up to 20. Either `{name, generate: {bytes}}` with `16 <= bytes <= 64`, or `{name, required: true, description?}`. The operator supplies the second form | |
| `volumes` | no | up to 8 `{name, path}`. `name` matches `^[a-z][a-z0-9_-]{0,30}$`. `path` matches `^/[^\s:]+$`. The same `name` on two containers of one package is one volume. Names and paths are unique inside one container | |
| `healthcheck` | yes on long-running containers; forbidden on `init` | exactly one of `path` (same pattern as `mcp.path`, and the container must have `port`) or `command` (1–16 strings) | `interval` `10s`, `timeout` `5s`, `start_period` `20s`, `retries` `5` |

`interval`, `timeout`, and `start_period` match `^[1-9][0-9]{0,3}s$`. `retries` is 1–20.

The facade's `healthcheck.path` must equal `mcp.healthz` (default `/healthz`).

Image reference, tag optional, digest required:

```text
[registry[:port]/]name[:tag]@sha256:<64 lowercase hex>
```

`registry` is lowercase DNS-like. `tag` matches `[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`.

## Containers and roles

In the generated compose file a container becomes the service `<package>-<role>` (`file-mcp-mcp`, `docs-service-api`, `minio-server`). Two packages that would produce the same service name are refused at install time.

A package with more than one container gets a bridge network `ks-pkg-<package>`. Containers on it reach each other by role name (`http://api:8000`). Other packages are not on that network. The bridge is an ordinary bridge, not an internal network: the package is kept off `keystone-mcp-upstreams`, and it can still open outbound connections (an external API, an external S3 endpoint). The installer creates the networks.

Only `mcp.container` joins `keystone-mcp-upstreams` with a network alias equal to the package name. Only that container receives the bearer and the assertion secret. The registry URL is `http://<package>:<port><mcp.path>`.

`provides.container` is reachable by consumers as `<package>-<role>` on the external bridge `keystone-resources`. A consumer joins that network only with the roles listed in `needs[].containers` (by default, every container of a package that declares the need, including `init`). A provider that has no `mcp` block is not attached to `keystone-mcp-upstreams`.

`keystone-resources` is one bridge shared by every consumer package and every provider. Compose makes each service name resolvable on every network it joins, so a container of one package can reach a container of another package that is also on `keystone-resources`. Treat internal roles as reachable: check the caller, do not rely on the network.

`init` containers use `restart: "no"` and run in manifest order. Long-running containers start after every `init` container has finished (`depends_on` with `condition: service_completed_successfully`).

The manifest does not restrict the service's own outbound calls. Package networks and `keystone-resources` are ordinary bridges, so egress exists. The generator, not the manifest, builds those networks.

Names of variables must be unique inside one container. The set is `env`, `secrets`, every `bind_env` value that reaches that container, and the variables the installer injects: `mcp.bearer_env`, `identity.secret_env`, and `identity.audience_env` on the facade; `public_base_url_env` on every long-running container. The same `env` name on a different container is fine.

A secret `name` shared by two containers is one value. The two declarations must be identical.

## What is forbidden and why

The manifest has nowhere to put a host-level setting. That is deliberate: the same document later drives other deploy targets, and a package must not be able to ask for the host.

| Rejected | Why |
|---|---|
| `ports`, `expose` | Callers reach the service only over the internal `keystone-mcp-upstreams` network |
| `build` | Packages ship a pinned image |
| `privileged`, `cap_add`, `devices`, `network_mode`, `pid`, `ipc`, `security_opt`, `extra_hosts`, `volumes_from`, `sysctls`, `userns_mode` | A package cannot request host access |
| `networks`, `depends_on`, `restart`, `container_name`, `links` | The installer sets these |
| a volume that is a string (`/srv:/data`) or an object with `host`, `source`, or `type` | Only named volumes `{name, path}`. Host paths are not allowed |
| an image or `provision.image` without `@sha256:` | Images are pinned by digest |
| tag `latest` | Use a version tag together with a digest |
| top-level `run` | Describe containers under `containers.<role>` |
| `mcp.transport: stdio` | Not in this release. Use `http` |

`compose.yaml` in the package repository is allowed for the author's development loop. The installer does not read it.

Why: [ADR 0004](../adr/0004-compose-generated-from-manifest.md).

## Image requirements

- The process listens on `0.0.0.0:<port>` where `<port>` is the manifest `port`. Nothing is published on the host by the manifest.
- An image whose healthcheck uses `path` includes `wget` or `curl`.
- The healthcheck path (for a facade, `/healthz` unless `mcp.healthz` says otherwise) returns HTTP 200 when the process is ready to serve.
- The facade checks `Authorization: Bearer` and `X-Keystone-Identity`. It does not accept `X-Author-Email` as a caller identity.

## Variables the installer supplies

Generated secrets (`generate.bytes`) are created by the installer and are not written into the manifest.

`bind_env` values are variable names. The installer fills them from the chosen resource (a provider from the catalog, or an external endpoint). Postgres may be requested as one URL, as the six separate fields, or as both at once. The provider returns every form that was requested.

keystone-mcp receives the bearer as `KEYSTONE_MCP_UPSTREAM_<NS>_BEARER` and the assertion secret as `KEYSTONE_MCP_UPSTREAM_<NS>_IDENTITY_SECRET`. `<NS>` is the namespace with `-` replaced by `_`. The facade receives the same two values under `mcp.bearer_env` and `identity.secret_env`. The registry entry name is placed in `identity.audience_env`. The public origin is placed in `public_base_url_env` on every long-running container.

Those values are split across files, all mode `600`:

| File | Who receives it |
|---|---|
| `packages/<name>.env` | every container |
| `packages/<name>.mcp.env` | only `mcp.container` (bearer, assertion secret, audience) |
| `packages/<name>.<kind>.env` | only the roles in `needs[].containers`, and only when that list is a subset of the package. Otherwise `bind_env` is in the shared file |

The installer records the file list in the deployment lock. Secret values never appear on a command line, in a log line, or in `--json` output.

## Operator layer

An operator changes how a generated package is launched by editing `packages/<name>.override.yml`, or by running `service eject` and taking the fragment over. That is installer behavior, not a field of this manifest.

Why: [ADR 0005](../adr/0005-operator-freedom-override-eject-adopt.md).

## Format versions

A new optional field keeps `schema_version: 1`. A new required field, or a change in the meaning of an existing field, is `schema_version: 2`.

## Reserved, not in this release

A stdio-only server is packaged as an HTTP box on the stdio-box image; see [packaging-a-service.md](../packaging-a-service.md#wrapping-a-stdio-server). `transport: stdio` stays rejected: the manifest only allows `http`.

There is no Redis resource interface in this version. `needs[].kind` is `s3` or `postgres`.

## How to check your manifest

From a checkout or an installed `keystonectl`:

```bash
python -m keystonectl.manifest path/to/keystone-service.yaml
```

Several files may be passed. Exit code 0 means every file is valid. Exit code 1 means at least one is not. Every error is printed to stdout, one line each, sorted by path, with no duplicates. The line looks like `containers.api.image: must be pinned by digest: ghcr.io/x/y:1.0.0@sha256:<64 hex>`.

The namespace check in this command uses the built-in tool list copied from keystone-mcp 0.5.0. That list is not a contract. If the keystone-mcp you install is newer, the installer warns that the copy may be stale. The final decision is `keystone-mcp upstream validate` when the package is applied.

`keystonectl lint` is a thin wrapper over this check and is provided by the installer command set, not by this document.
