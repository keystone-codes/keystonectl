# keystonectl commands

One page for every command. Shared flags and exit codes are in Conventions. Each command section is the contract for that invocation.

## Conventions

These flags exist on every command:

| Flag | Meaning |
|---|---|
| `--root DIR` | Deployment directory. Otherwise `KEYSTONECTL_ROOT`, then the current directory. |
| `--json` | One JSON document on stdout. No prompts. Shape is `schemas/output.schema.json`. |
| `--non-interactive` | Missing answers are a code 2 result, not a prompt. |
| `--set KEY=VALUE` | Answer one question. A name ending in `_FILE`, and any secret question, is refused (`secret_in_argument`). |

Exit codes: `0` done, `1` usage or a missing deployment directory, `2` one or more answers are missing (`missing` lists them; pass them with `--set` or the environment and run the command again), `3` the command cannot proceed. `retryable` is true only when repeating the same command can succeed, for example when a catalog host did not answer.

A `missing` item has `var`, `question`, `kind` (`text`, `choice`, `file`, `confirm`, or `argument`), `choices`, `default`, and `secret`. Question names are `KEYSTONE_SETUP_*`. A secret is only a path to a mode 600 file whose variable ends in `_FILE`. The value is never an argument, never a log line, and never a field of `--json`.

## Commands

### keystonectl init

    keystonectl init [--adopt] [--keystone-service NAME] <directory>

Creates a deployment directory, or adopts one that already has Compose files.

Writes: `services.lock.yaml`, `sources.yaml` when it is missing, `docker-compose.packages.yml` on adopt.

```json
{
  "ok": true,
  "code": 0,
  "command": "init",
  "target": "/srv/keystone",
  "summary": "Adopted the deployment directory.",
  "actions": [],
  "changes": 1,
  "missing": [],
  "warnings": [],
  "error": null,
  "retryable": false,
  "result": {}
}
```

### keystonectl lint

    keystonectl lint [--keystone-mcp-version VERSION] <manifest-or-directory>

Checks a package manifest or a `catalog.yaml`. Writes nothing.

Errors: `manifest_invalid`, `catalog_invalid`.

```json
{
  "ok": true,
  "code": 0,
  "command": "lint",
  "target": "keystone-service.yaml",
  "summary": "The manifest is valid.",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": null,
  "retryable": false,
  "result": {"problems": []}
}
```

### keystonectl package

`package` only groups `package lint`. Without a subcommand the exit code is 1 (`usage`).

```json
{
  "ok": false,
  "code": 1,
  "command": "package",
  "target": null,
  "summary": "The command was not understood.",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": {
    "id": "usage",
    "message": "The command was not understood.",
    "hint": "Check the command and flags. Run keystonectl --help."
  },
  "retryable": false,
  "result": {}
}
```

### keystonectl package lint

Same check as `keystonectl lint`.

```json
{
  "ok": true,
  "code": 0,
  "command": "package lint",
  "target": "keystone-service.yaml",
  "summary": "The manifest is valid.",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": null,
  "retryable": false,
  "result": {"problems": []}
}
```

### keystonectl check

    keystonectl check

Reads the deployment and reports layout, lock, registry, snapshot, and remote-tool warnings. Writes nothing.

Warnings include `remote_tools_not_in_snapshot` and `remote_tools_unknown` for a remote service whose tools are not in the snapshot.

```json
{
  "ok": true,
  "code": 0,
  "command": "check",
  "target": null,
  "summary": "Deployment check passed.",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [
    {
      "id": "remote_tools_unknown",
      "message": "Policy rows for analytics_* are checked only while the service is up."
    }
  ],
  "error": null,
  "retryable": false,
  "result": {"checks": []}
}
```

### keystonectl catalog

`catalog` groups `add` and `list`. Without a subcommand the exit code is 1 (`usage`).

```json
{
  "ok": false,
  "code": 1,
  "command": "catalog",
  "target": null,
  "summary": "The command was not understood.",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": {
    "id": "usage",
    "message": "The command was not understood.",
    "hint": "Check the command and flags. Run keystonectl --help."
  },
  "retryable": false,
  "result": {}
}
```

### keystonectl catalog add

    keystonectl catalog add [--name NAME] [--ref REF] [--priority N] <url>

Adds a git catalog. `https://` is required unless the host is a Docker DNS name with no dot. No `--ref` uses the newest `vX.Y.Z` tag and warns `catalog_ref_chosen`. A catalog that fails the schema is `source_invalid` and `sources.yaml` is left untouched (`result.problems`). A name already in use says to pass `--name`. A colliding priority, including a computed default that lands on a taken number, is `priority_taken`. Package and bundle names that already exist in another catalog are warnings `package_name_shadowed` and `bundle_name_shadowed`, listed in `result.conflicts`. Installed packages keep the source recorded in the lock.

Writes: `sources.yaml` and `sources.yaml.bak`.

```json
{
  "ok": true,
  "code": 0,
  "command": "catalog add",
  "target": "https://example.com/partner.git",
  "summary": "Added catalog partner.",
  "actions": [],
  "changes": 1,
  "missing": [],
  "warnings": [
    {
      "id": "catalog_ref_chosen",
      "message": "No --ref given; using the newest tag v1.2.0."
    }
  ],
  "error": null,
  "retryable": false,
  "result": {
    "name": "partner",
    "url": "https://example.com/partner.git",
    "ref": "v1.2.0",
    "priority": 90,
    "packages": 1,
    "bundles": ["minimal"],
    "conflicts": [
      {"kind": "package", "name": "file-mcp", "sources": ["keystone-codes", "partner"]}
    ]
  }
}
```

### keystonectl catalog list

    keystonectl catalog list

Prints configured catalogs. Writes nothing.

```json
{
  "ok": true,
  "code": 0,
  "command": "catalog list",
  "target": null,
  "summary": "2 catalogs.",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": null,
  "retryable": false,
  "result": {"sources": []}
}
```

### keystonectl service

`service` groups package and remote operations. Without a subcommand the exit code is 1 (`usage`).

```json
{
  "ok": false,
  "code": 1,
  "command": "service",
  "target": null,
  "summary": "The command was not understood.",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": {
    "id": "usage",
    "message": "The command was not understood.",
    "hint": "Check the command and flags. Run keystonectl --help."
  },
  "retryable": false,
  "result": {}
}
```

### keystonectl service list

    keystonectl service list

Lists lock entries. Writes nothing.

```json
{
  "ok": true,
  "code": 0,
  "command": "service list",
  "target": null,
  "summary": "1 service.",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": null,
  "retryable": false,
  "result": {"services": ["file-mcp"]}
}
```

### keystonectl service plan

    keystonectl service plan [--defer-public] [--certbot-staging] <source/name>

Shows the apply plan. Writes nothing. A package with `public_paths` asks for a domain unless `--defer-public` is set.

```json
{
  "ok": false,
  "code": 2,
  "command": "service plan",
  "target": "keystone-codes/file-mcp",
  "summary": "1 answer is missing",
  "actions": [],
  "changes": 0,
  "missing": [
    {
      "var": "KEYSTONE_SETUP_RESOURCE_S3",
      "question": "Where should the package keep files: in S3 storage you already have (external), or in a new MinIO on this server (catalog)?",
      "kind": "choice",
      "choices": ["external", "catalog"],
      "default": null,
      "secret": false
    }
  ],
  "warnings": [],
  "error": null,
  "retryable": false,
  "result": {}
}
```

### keystonectl service apply

    keystonectl service apply [--defer-public] [--certbot-staging] <source/name>

Installs one package. Publish ports stay on `127.0.0.1`. See `docs/generation.md` for the files.

Errors include `requires_missing`, `policy_breaks`, `fragment_modified`.

```json
{
  "ok": true,
  "code": 0,
  "command": "service apply",
  "target": "keystone-codes/file-mcp",
  "summary": "Installed file-mcp.",
  "actions": [
    {"id": "compose.fragment", "title": "Write packages/file-mcp.yml", "status": "done"}
  ],
  "changes": 1,
  "missing": [],
  "warnings": [],
  "error": null,
  "retryable": false,
  "result": {}
}
```

### keystonectl service install

Another name for `service apply`.

```json
{
  "ok": true,
  "code": 0,
  "command": "service install",
  "target": "keystone-codes/file-mcp",
  "summary": "Installed file-mcp.",
  "actions": [],
  "changes": 1,
  "missing": [],
  "warnings": [],
  "error": null,
  "retryable": false,
  "result": {}
}
```

### keystonectl service explain

    keystonectl service explain <error-id>

Prints one error or warning from the registry. Writes nothing. The top-level `explain` command is the same text.

```json
{
  "ok": true,
  "code": 0,
  "command": "service explain",
  "target": "bundle_not_found",
  "summary": "Bundle was not found.",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": null,
  "retryable": false,
  "result": {"id": "bundle_not_found", "code": 3, "retryable": false}
}
```

### keystonectl service status

    keystonectl service status

Shows container and gateway status. Writes nothing.

```json
{
  "ok": true,
  "code": 0,
  "command": "service status",
  "target": null,
  "summary": "1 service is up.",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": null,
  "retryable": false,
  "result": {"services": []}
}
```

### keystonectl service remove

    keystonectl service remove <name>

Asks `KEYSTONE_SETUP_SERVICE_<NAME>_CONFIRM_REMOVE`. Drops the lock entry. A package also drops its fragment and containers. A remote entry drops the registry row, the R12 names in `.env`, and `packages/<name>.env`. Published tool names move to `reserved`. No container, vhost, or docker network is removed for a remote service.

```json
{
  "ok": true,
  "code": 0,
  "command": "service remove",
  "target": "analytics-mcp",
  "summary": "Removed analytics-mcp.",
  "actions": [],
  "changes": 1,
  "missing": [],
  "warnings": [],
  "error": null,
  "retryable": false,
  "result": {}
}
```

### keystonectl service upgrade

    keystonectl service upgrade <name>[@version]

Moves a package to a newer catalog version. The command takes the installed service name only (`file-mcp`, not `source/file-mcp`); after confirmation it applies using the lock entry's catalog source. A remote service is `not_package`: it has no package; use `add-remote` to change its url.

```json
{
  "ok": false,
  "code": 3,
  "command": "service upgrade",
  "target": "analytics-mcp",
  "summary": "a remote service has no package; use add-remote to change its url",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": {
    "id": "not_package",
    "message": "a remote service has no package; use add-remote to change its url",
    "hint": "Use service add-remote to change the url."
  },
  "retryable": false,
  "result": {}
}
```

### keystonectl service rollback

    keystonectl service rollback <name>

Restores the previous package version. A remote service is `not_package`, same as upgrade.

```json
{
  "ok": false,
  "code": 3,
  "command": "service rollback",
  "target": "analytics-mcp",
  "summary": "a remote service has no package; use add-remote to change its url",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": {
    "id": "not_package",
    "message": "a remote service has no package; use add-remote to change its url",
    "hint": "Use service add-remote to change the url."
  },
  "retryable": false,
  "result": {}
}
```

### keystonectl service eject

    keystonectl service eject <name>

Stops keystonectl from rewriting `packages/<name>.yml`. A remote service is `not_ejectable`.

```json
{
  "ok": false,
  "code": 3,
  "command": "service eject",
  "target": "analytics-mcp",
  "summary": "A remote service has no generated fragment.",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": {
    "id": "not_ejectable",
    "message": "A remote service has no generated fragment.",
    "hint": "eject applies to a generated package fragment."
  },
  "retryable": false,
  "result": {}
}
```

### keystonectl service adopt

    keystonectl service adopt <name>

Generates the fragment again after eject. A service that was not ejected is `not_ejected`.

```json
{
  "ok": false,
  "code": 3,
  "command": "service adopt",
  "target": "file-mcp",
  "summary": "file-mcp is not ejected.",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": {
    "id": "not_ejected",
    "message": "file-mcp is not ejected.",
    "hint": "adopt follows eject."
  },
  "retryable": false,
  "result": {}
}
```

### keystonectl service add-remote

    keystonectl service add-remote <name> --url URL [--namespace NS] [--tools a,b]
                                          [--tool-prefix P] [--include-prefixes a,b]
                                          [--exclude-prefixes a,b] [--strip-prefix P]
                                          [--no-auth] [--adopt-existing]

Connects a service you run yourself. No compose is generated. The name matches `^[a-z0-9][a-z0-9_-]*$` and the lock name pattern. The URL is `http://` or `https://` with a path. `http://` is only for a hostname with no dot, and then `.env` sets `KEYSTONE_MCP_UPSTREAM_ALLOW_INSECURE_HTTP=true`. The default namespace strips a trailing `-mcp`.

Secrets are generated once into the managed block of `.env` and copied to `packages/<name>.env` (mode 600) with the comment `# audience (aud) = <name>`. The command output is the path and one sentence telling you to give that file to the team that runs the service. `--no-auth` writes no secrets and warns `remote_without_identity`. `--adopt-existing` takes a registry entry that is not in the lock, keeps its auth, filters, and secrets, and does not write a handoff file. A different `--url` or filter flag is `remote_url_mismatch`. Without the flag, a foreign name is `registry_name_taken`.

`--tools` writes `<namespace>_<tool>` into the snapshot and the bare names into the lock. Without `--tools` the snapshot is left alone and the lock stores `tools: []`. `check` then warns `remote_tools_not_in_snapshot` when a live list is available, or `remote_tools_unknown` when it is not.

Reachability is one `docker run` on `keystone-mcp-upstreams`. Any HTTP status, including 401 and 405, counts as reachable. No connection is the warning `remote_unreachable`. The candidate registry is checked with `keystone-mcp upstream validate`. If keystone-mcp is running, it is restarted with `docker compose restart`.

The same URL and tools again report `changes: 0`. A new URL or `--tools` updates the entry. A namespace change is `namespace_change_unsupported`.

Writes: `keystonectl/upstreams.yaml`, `.env`, `packages/<name>.env` (unless `--no-auth` or `--adopt-existing`), `keystonectl/catalog-snapshot.yaml` only with `--tools`, `services.lock.yaml` (`managed: remote`, `env.generated: []`).

Questions: `KEYSTONE_SETUP_SERVICE_<NAME>_NAMESPACE` when the prefix collides.

```json
{
  "ok": true,
  "code": 0,
  "command": "service add-remote",
  "target": "analytics-mcp",
  "summary": "Connected analytics-mcp. Network keystone-mcp-upstreams; the url must answer from inside that network. The service must check the bearer and X-Keystone-Identity. aud = analytics-mcp. give this file to the team that runs the service; it must check the bearer and X-Keystone-Identity (see docs/connecting-services.md in keystone-mcp)",
  "actions": [],
  "changes": 4,
  "missing": [],
  "warnings": [
    {
      "id": "remote_tools_unknown",
      "message": "Policy rows for analytics_* are checked only while the service is up."
    }
  ],
  "error": null,
  "retryable": false,
  "result": {
    "registry_name": "analytics-mcp",
    "namespace": "analytics",
    "url": "http://analytics-mcp-http:8081/mcp",
    "handoff_file": "packages/analytics-mcp.env",
    "reachable": true
  }
}
```

### keystonectl explain

    keystonectl explain <error-id>

Prints one registry entry. Writes nothing.

```json
{
  "ok": true,
  "code": 0,
  "command": "explain",
  "target": "priority_taken",
  "summary": "That priority is already used.",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": null,
  "retryable": false,
  "result": {"id": "priority_taken", "code": 3, "retryable": false}
}
```

### keystonectl bundle

`bundle` groups `plan` and `install`. Without a subcommand the exit code is 1 (`usage`).

```json
{
  "ok": false,
  "code": 1,
  "command": "bundle",
  "target": null,
  "summary": "The command was not understood.",
  "actions": [],
  "changes": 0,
  "missing": [],
  "warnings": [],
  "error": {
    "id": "usage",
    "message": "The command was not understood.",
    "hint": "Check the command and flags. Run keystonectl --help."
  },
  "retryable": false,
  "result": {}
}
```

### keystonectl bundle plan

    keystonectl bundle plan [--defer-public] [--certbot-staging] <source/bundle>

Resolves the bundle, orders packages by manifest `requires` (a tie keeps the bundle order), and plans each package. Writes nothing. An ambiguous short name is code 2, variable `bundle`, kind `argument`. A missing bundle is `bundle_not_found`. A cycle is `requires_cycle` and the message contains the cycle. A `requires` name that is neither in the bundle nor in the lock is `requires_missing`. The first code 2 or 3 stops the walk. Statuses are `planned`, `unchanged`, `needs_answers`, `failed`, and `not_started`.

`result` has `order`, `packages`, and, when the packages declare them, `recommended_policy` and `critical_tools`. Each of those rows includes `package`.

```json
{
  "ok": false,
  "code": 2,
  "command": "bundle plan",
  "target": "minimal",
  "summary": "1 answer is missing",
  "actions": [],
  "changes": 0,
  "missing": [
    {
      "var": "bundle",
      "question": "Which bundle? The name is in more than one catalog.",
      "kind": "argument",
      "choices": ["keystone-codes/minimal", "partner/minimal"],
      "default": null,
      "secret": false
    }
  ],
  "warnings": [],
  "error": null,
  "retryable": false,
  "result": {}
}
```

### keystonectl bundle install

    keystonectl bundle install [--defer-public] [--certbot-staging] <source/bundle>

Same order as `bundle plan`, then `service apply` for each package. Already installed packages stay. A later run continues after the first failure. Statuses use `installed` where plan uses `planned`. Shared storage asks for the origin only of the first consumer of a kind; the next consumer gets its own bucket or database. A package with `public_paths` needs a domain or `--defer-public`.

```json
{
  "ok": false,
  "code": 2,
  "command": "bundle install",
  "target": "keystone-codes/minimal",
  "summary": "1 answer is missing",
  "actions": [],
  "changes": 0,
  "missing": [
    {
      "var": "KEYSTONE_SETUP_SERVICE_FILE_MCP_DOMAIN",
      "question": "Public domain for file-mcp upload and download links (an existing DNS name pointing to this server)",
      "kind": "text",
      "choices": null,
      "default": null,
      "secret": false
    }
  ],
  "warnings": [],
  "error": null,
  "retryable": false,
  "result": {
    "order": ["file-mcp"],
    "packages": [{"name": "file-mcp", "status": "needs_answers"}]
  }
}
```

## Questions

<!-- generated:questions -->
| Variable | Kind | When | Owner |
|---|---|---|---|
| KEYSTONE_SETUP_KEYSTONE_MCP_VERSION | text | Which keystone-mcp version runs here (for example 0.5.0)? | I01 |
| KEYSTONE_SETUP_POSTGRES_DATABASE | text | PostgreSQL database name for this package. | I02 |
| KEYSTONE_SETUP_POSTGRES_HOST | text | PostgreSQL host name (for example db.example.com). | I02 |
| KEYSTONE_SETUP_POSTGRES_PASSWORD_FILE | file | Path to a mode 600 file with the PostgreSQL password. | I02 |
| KEYSTONE_SETUP_POSTGRES_PORT | text | PostgreSQL port. | I02 |
| KEYSTONE_SETUP_POSTGRES_SSLMODE | choice | PostgreSQL sslmode. | I02 |
| KEYSTONE_SETUP_POSTGRES_URL_FILE | file | Path to a mode 600 file with a postgresql:// URL that includes sslmode. This replaces the other PostgreSQL questions. | I02 |
| KEYSTONE_SETUP_POSTGRES_USER | text | PostgreSQL user name for this package. | I02 |
| KEYSTONE_SETUP_RESOURCE_POSTGRES | choice | Which PostgreSQL should the package use: a server you already have (external), or a new PostgreSQL on this server (catalog)? | I02 |
| KEYSTONE_SETUP_RESOURCE_POSTGRES_PACKAGE | choice | Which PostgreSQL provider from the catalogs should be installed? | I02 |
| KEYSTONE_SETUP_RESOURCE_S3 | choice | Where should the package keep files: in S3 storage you already have (external), or in a new MinIO on this server (catalog)? | I02 |
| KEYSTONE_SETUP_RESOURCE_S3_PACKAGE | choice | Which S3 provider from the catalogs should be installed? | I02 |
| KEYSTONE_SETUP_S3_ACCESS_KEY_FILE | file | Path to a mode 600 file with the S3 access key. | I02 |
| KEYSTONE_SETUP_S3_BUCKET | text | S3 bucket name for this package (for example docs-mcp). | I02 |
| KEYSTONE_SETUP_S3_ENDPOINT | text | S3 endpoint URL (for example https://s3.amazonaws.com). | I02 |
| KEYSTONE_SETUP_S3_PATH_STYLE | text | Use path-style S3 URLs? Answer true or false. | I02 |
| KEYSTONE_SETUP_S3_REGION | text | S3 region (for example us-east-1). | I02 |
| KEYSTONE_SETUP_S3_SECRET_KEY_FILE | file | Path to a mode 600 file with the S3 secret key. | I03 |
| KEYSTONE_SETUP_SERVICE_{PKG}_ENV_{NAME} | text | Value of {NAME} for package {PKG}. | I03 |
| KEYSTONE_SETUP_SERVICE_{PKG}_NAMESPACE | text | Choose another prefix for {PKG}'s tools (letters, digits, '-' or '_'). | I03 |
| KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_DATABASE | text | PostgreSQL database for package {PKG}. Overrides KEYSTONE_SETUP_POSTGRES_DATABASE. | I01 |
| KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_PASSWORD_FILE | file | Path to a mode 600 file with the PostgreSQL password for package {PKG}. | I01 |
| KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_URL_FILE | file | Path to a mode 600 file with a postgresql:// URL for package {PKG}. | I01 |
| KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_USER | text | PostgreSQL user for package {PKG}. Overrides KEYSTONE_SETUP_POSTGRES_USER. | I01 |
| KEYSTONE_SETUP_SERVICE_{PKG}_S3_ACCESS_KEY_FILE | file | Path to a mode 600 file with the S3 access key for package {PKG}. | I01 |
| KEYSTONE_SETUP_SERVICE_{PKG}_S3_BUCKET | text | S3 bucket for package {PKG}. Overrides KEYSTONE_SETUP_S3_BUCKET. | I01 |
| KEYSTONE_SETUP_SERVICE_{PKG}_S3_SECRET_KEY_FILE | file | Path to a mode 600 file with the S3 secret key for package {PKG}. | I03 |
| KEYSTONE_SETUP_SERVICE_{PKG}_SECRET_{NAME}_FILE | file | Path to a mode 600 file holding secret {NAME} for package {PKG}. | I03 |

| Id | Code | Retryable | Action |
|---|---|---|---|
| adopt_required | 3 | false | Use keystonectl init --adopt <dir>. |
| bundle_not_found | 3 | false | Check the name, or add the catalog with keystonectl catalog add. |
| busy | 3 | true | Wait for it to finish and run the command again. |
| catalog_invalid | 3 | false | Fix catalog.yaml and run keystonectl lint on it. |
| cert_expired | 3 | false | Replace KEYSTONE_SETUP_SERVICE_<NAME>_CERT_PATH with a current certificate. |
| cert_install_failed | 3 | false | Check sudo and the destination directory, then run the command again. |
| cert_not_pem | 3 | false | A .pfx or .der file must be converted: openssl pkcs12 -in x.pfx -nokeys -out fullchain.pem |
| cert_unreadable | 3 | false | Check the path and try another KEYSTONE_SETUP_SERVICE_<NAME>_CERT_PATH or _KEY_PATH. |
| certbot_failed | 3 | true | Check DNS, port 80, and /var/log/letsencrypt/letsencrypt.log, then run apply again. |
| certbot_missing | 3 | false | Install certbot and python3-certbot-nginx, or choose another TLS method. |
| check_failed | 3 | false | Read result.checks for the failing id and its hint. |
| compose_not_found | 3 | false | Add docker-compose.yml (or compose.yaml) and run init --adopt. |
| compose_too_old | 3 | false | Upgrade the Docker Compose plugin to 2.24 or newer. |
| compose_unparseable | 3 | false | Fix the compose file and run init --adopt again. |
| dns_mismatch | 3 | true | Update the A record or wait for DNS to propagate (often 1-10 min), then run apply again. |
| dns_no_record | 3 | true | Add an A record for the domain pointing at this host, then run apply again. |
| docker_unavailable | 3 | true | Start Docker. The keystone-resources network is created when a service is deployed. |
| domain_in_use | 3 | false | Pick another domain, or disable the other site and run the command again. |
| domain_invalid | 3 | false | Use a lowercase DNS name with at least two labels, for example files.example.com. |
| env_name_conflict | 3 | false | Remove the operator's copy of that name or rename it. |
| fragment_modified | 3 | false | Move your changes into the override file, or eject the service. |
| incompatible_keystone_mcp | 3 | false | Choose a version whose range includes the running keystone-mcp, or upgrade it. |
| init_failed | 3 | false | Read the container log, fix the package, and run the command again. |
| internal_error | 3 | false | Report this with the command and the keystonectl version. |
| key_mismatch | 3 | false | Point KEYSTONE_SETUP_SERVICE_<NAME>_KEY_PATH at the key for this certificate. |
| key_not_pem | 3 | false | Replace KEYSTONE_SETUP_SERVICE_<NAME>_KEY_PATH with a PEM key. |
| keystone_env_file_missing | 3 | false | Add env_file: .env to the keystone-mcp service. |
| keystone_mcp_image_unavailable | 3 | true | Pull the image recorded in the lock, or build it with your deployment's own tooling, then retry. |
| keystone_mcp_version_unknown | 2 | false | Set KEYSTONE_SETUP_KEYSTONE_MCP_VERSION to the version that is running, for example 0.5.0. |
| keystone_service_ambiguous | 2 | false | Re-run init --adopt with --keystone-service NAME. |
| keystone_service_not_found | 3 | false | Pass --keystone-service NAME if the service was renamed. |
| layout_invalid | 3 | false | Run keystonectl init --adopt <root> and start compose with both files: docker compose -f docker-compose.yml -f docker-compose.packages.yml. |
| layout_unsupported | 3 | false | layout_version must be 1. |
| lock_corrupt | 3 | false | Restore services.lock.yaml.bak or run keystonectl init again on a copy. |
| manifest_invalid | 3 | false | Fix the problems listed in result.problems and run keystonectl lint. |
| namespace_change_unsupported | 3 | false | Run keystonectl service remove, then install the service again. |
| nginx_config_invalid | 3 | false | Read the nginx -t output. The previous site file was restored. |
| nginx_missing | 3 | false | Install nginx, or answer TLS=manual. |
| no_previous_version | 3 | false | Rollback restores one upgrade. Install the version you want with service apply. |
| no_provider | 3 | false | Choose external and point at storage you already run, or add a catalog that provides it. |
| not_ejectable | 3 | false | Remote services are not generated. There is nothing to eject. |
| not_ejected | 3 | false | service adopt only takes back a fragment that was ejected. |
| not_implemented | 1 | false | No deploy step is configured for this change. Run service plan to preview it. |
| not_package | 3 | false | A remote service has no package; use add-remote to change its url. |
| package_ambiguous | 2 | false | Pass the package as source/name, for example local/file-mcp. service upgrade reads the source from the lock. |
| package_not_found | 3 | false | Check the name, or add the catalog with keystonectl catalog add. |
| path_outside_root | 3 | false | Keep registry, snapshot, and policy files inside the deployment root. |
| policy_breaks | 3 | false | Fix the policy, or install a version whose tools the policy already allows. |
| port_range_exhausted | 3 | false | Free a port in that range and run the command again. |
| postgres_access_denied | 3 | false | Check the user and the password file, then run the command again. |
| postgres_address_mismatch | 3 | false | Use the host, port, and sslmode already recorded for PostgreSQL. Database, user, and password can differ for each service. |
| postgres_create_table_failed | 3 | false | Grant the user rights on its own schema, then run the command again. |
| postgres_database_missing | 3 | false | Create the database and the user, then run the command again. |
| postgres_extension_missing | 3 | false | Create the extensions named in the message, then run the command again. |
| postgres_not_isolated | 3 | false | This is a provider bug. The consumer must not connect to other databases. |
| postgres_unreachable | 3 | true | Check the host, the port, and the network, then run the command again. |
| postgres_url_invalid | 3 | false | Add sslmode to the URL, for example ?sslmode=require. |
| postgres_version_too_old | 3 | false | Use a PostgreSQL server that is version 14 or newer. |
| priority_taken | 3 | false | Pick another --priority. Priority only affects listing order. |
| provision_failed | 3 | false | If the provider was not ready yet, run the command again. Otherwise fix the provider package. |
| public_ip_unknown | 3 | true | Check outbound HTTPS and run the command again, or answer DNS=skip. |
| public_unreachable | 3 | true | Finish the proxy and certificate, then run apply again. |
| registry_full | 3 | false | Remove an entry that keystonectl does not manage, then run the command again. |
| registry_inline | 3 | false | Move these entries to upstreams.yaml and set KEYSTONE_MCP_UPSTREAMS_PATH. keystonectl does not edit inline JSON. |
| registry_invalid | 3 | false | The file needs schema_version: 1 and an entries list. |
| registry_name_taken | 3 | false | Pick another package name, or remove the hand-written registry entry. |
| registry_path_unmapped | 3 | false | Bind-mount the file from inside the deployment root. |
| remote_url_mismatch | 3 | false | Pass the URL already stored in keystonectl/upstreams.yaml. |
| required_by | 3 | false | Remove the services listed in the message first. |
| requires_cycle | 3 | false | Remove the cycle in the package manifests and run the command again. |
| requires_missing | 3 | false | Install them first or use bundle install. |
| resource_already_bound | 3 | false | One provider per kind. Use the provider that is already bound, or remove it first. |
| resource_in_use | 3 | false | Unbind the consumers first. Their buckets and databases are kept. |
| root_not_empty | 3 | false | Use an empty directory, or pass a directory that already runs keystone-mcp. |
| root_not_found | 1 | false | Pass --root DIR, set KEYSTONECTL_ROOT, or run keystonectl init --adopt <dir>. |
| s3_access_denied | 3 | false | Check the access key and the secret key, then run the command again. |
| s3_bucket_missing | 3 | false | Create the bucket in the storage you already have, then run the command again. |
| s3_presign_failed | 3 | false | Allow s3:GetObject and s3:PutObject on the bucket, then run the command again. |
| s3_unreachable | 3 | true | Check the endpoint and the network, then run the command again. |
| s3_wrong_region | 3 | false | Set KEYSTONE_SETUP_S3_REGION to the bucket's region. |
| secret_file_unsafe | 3 | false | Use a regular file owned by you or root and run chmod 600. |
| secret_in_argument | 1 | false | Secrets are accepted only as a path to a mode 600 file, via the environment. |
| service_unhealthy | 3 | true | Read the container log and run the command again. |
| source_exists | 3 | false | Pick another --name, or edit sources.yaml. |
| source_invalid | 3 | false | Use an https://, ssh://, git@host:path, or file:// URL. http:// is refused. |
| source_unreachable | 3 | true | Check the network and the URL, then run the command again. |
| sudo_required | 3 | false | Allow sudo -n for nginx and certbot, or answer TLS=manual. |
| tag_moved | 3 | false | Refuse the move. Pin a commit, or upgrade on purpose with service upgrade. |
| tag_not_found | 3 | false | Check the ref in sources.yaml or catalog.yaml. For the default source, update keystonectl or add sources.yaml with a ref that exists. |
| usage | 1 | false | Check the command and flags. Run keystonectl --help. |
| version_not_found | 3 | false | Run keystonectl catalog list and pick a listed version. |
| builtin_list_may_be_stale | warning | false | built-in tool names are known up to keystone-mcp 0.5.0; the final check is upstream validate during apply. |
| bundle_name_shadowed | warning | false | Install it by full name source/name. |
| catalog_cached | warning | false | Restore network access and run the command again to refresh the cache. |
| catalog_ref_chosen | warning | false | Pass --ref to pin a different catalog tag. |
| cert_expiring | warning | false | Renew it before it expires. service status warns again until you do. |
| cert_may_not_cover_domain | warning | false | Check the certificate names. A wildcard covers one label only. |
| chain_missing | warning | false | Add the intermediate certificates to the full chain. |
| check_skipped_no_docker | warning | false | Install Docker to run this check. |
| comments_dropped | warning | false | A one-time .orig copy was saved next to the file. |
| compat_mismatch | warning | false | The manifest range is the one that is enforced. |
| compose_file_ignored | warning | false | The installer never reads it. |
| compose_variable_unset | warning | false | Set the variable in the environment or in the deployment .env file. |
| compose_version_unknown | warning | false | Install the Docker Compose plugin 2.24 or newer. |
| discovery_pending | warning | false | keystone-mcp will try again on its own. Check keystonectl service status. |
| dns_system_resolver | warning | false | Install dig for a check against 1.1.1.1. The system resolver can be cached. |
| extra_hosts_manual | warning | false | Add the host line yourself in the override or in your own compose file. |
| fragment_modified | warning | false | Move your changes into the override file, or eject the service. |
| inline_registry_ignored | warning | false | Upstreams are read from keystonectl/upstreams.yaml. |
| keystone_compose_missing | warning | false | Put keystone-mcp's docker-compose.yml and .env here (see keystone-mcp examples/production) and run keystonectl init again. |
| layout_changed | warning | false | Review the listed fields. The lock was updated to match the compose file. |
| local_source | warning | false | file:// is for tests and local development, not for a shared catalog. |
| orphan_fragment | warning | false | Remove the file or install the package that owns it. |
| override_not_included | warning | false | Run keystonectl service apply <name> to include it. |
| override_unchecked | warning | false | Fix the compose files and run keystonectl check again. |
| override_widens_access | warning | false | This is the operator's choice. Review ports, privileges, and host mounts. |
| package_name_shadowed | warning | false | Install it by full name source/name. |
| policy_already_inconsistent | warning | false | The new snapshot does not add unmatched patterns. Fix the policy when you can. |
| policy_check_skipped | warning | false | Set KEYSTONE_MCP_RBAC_MODE to something other than off and point KEYSTONE_MCP_RBAC_POLICY_PATH at a policy file. |
| policy_not_configured | warning | false | check will skip the policy comparison until a policy file is configured. |
| policy_rows_orphaned | warning | false | Delete these policy rows. The tool names stay reserved until you do. |
| postgres_plain_connection | warning | false | Prefer sslmode require, verify-ca, or verify-full for a server you do not run here. |
| public_check_failed | warning | false | The service is installed. Check the proxy and run apply again. |
| public_pending | warning | false | Run service apply again with a domain when you want the public paths published. |
| published_name_long | warning | false | Many MCP clients reject names longer than 64 characters. |
| registry_moved | warning | false | keystone-mcp now reads keystonectl/upstreams.yaml; your old registry file is no longer read. |
| remote_tools_not_in_snapshot | warning | false | Run keystonectl service add-remote again with --tools. |
| remote_tools_unknown | warning | false | Start the service on the keystone-mcp-upstreams network and run check again. |
| remote_unreachable | warning | false | keystone-mcp will keep retrying; make sure the service is on the keystone-mcp-upstreams network. |
| remote_without_identity | warning | false | Catalog packages must check the assertion. Here that check is yours. |
| renewal_manual | warning | false | service status warns 30 days before expiry. |
| renewal_timer_missing | warning | false | Enable the certbot systemd timer or /etc/cron.d/certbot. |
| s3_lifecycle_unsupported | warning | false | The service still runs. Add a lifecycle rule on the bucket later if you need it. |
| s3_plain_http | warning | false | Prefer https:// for storage that is not on your own Docker network. |
| snapshot_moved | warning | false | keystone-mcp now reads the snapshot from keystonectl/. |
| tls_untrusted | warning | false | The service answered. Install the certificate in the system trust store if clients should trust it. |
| tools_unreachable | warning | false | Ask an administrator to add policy rows (see the recommended policy below). |
| unmanaged_upstream | warning | false | The entry is left as-is. It belongs to the operator. |
| unparsed_enddate | warning | false | Check the certificate file format and run keystonectl check again. |
| validation_skipped_local | warning | false | Build the keystone-mcp image, then run keystonectl service apply again. |
<!-- /generated -->

## Files keystonectl writes

| Path | Owner |
|---|---|
| `docker-compose.yml` | Operator. keystonectl does not edit it. |
| `docker-compose.packages.yml` | keystonectl. Lists package fragments and overrides. |
| `packages/<name>.yml` | keystonectl, until `service eject`. |
| `packages/<name>.override.yml` | Operator. keystonectl never writes or deletes it. |
| `packages/<name>.env` and `packages/<name>.<kind>.env` | keystonectl. Mode 600. A remote handoff file uses the same `packages/<name>.env` name and is not a Compose `env_file`. |
| `.env` | keystonectl owns the managed block (bearer and identity names, and `KEYSTONE_MCP_UPSTREAM_ALLOW_INSECURE_HTTP` when a remote URL is `http://`). Mode 600. |
| `services.lock.yaml` | keystonectl. A remote entry is `managed: remote` and includes `env.generated`. |
| `sources.yaml` | keystonectl on `catalog add`. Removing a source is a hand edit. |
| `keystonectl/upstreams.yaml` | keystonectl. |
| `keystonectl/catalog-snapshot.yaml` | keystonectl. `add-remote` changes it only when `--tools` is passed. |
