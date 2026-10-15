---
name: keystonectl
description: Install, upgrade, remove or check services behind Keystone-MCP from a service catalog (for example file-mcp with S3 or MinIO storage, or a service that needs Postgres) on the host where Keystone-MCP runs. Use when asked to add a service or a set of tools to Keystone, connect S3 or Postgres, publish a service on its own domain with a certificate, check why a service does not show up, or detach a service so the operator can run it their own way.
---

# Install services with keystonectl

Install, upgrade, remove, or check catalog services by running `keystonectl` and asking the operator the questions it returns. Do not edit policy, compose, or secret files yourself.

## Where you run

Work on the keystone-mcp host, or through an SSH command the operator already gave you. Do not store SSH keys and do not set up access.

Every command is `keystonectl … --json`. When the operator gave an SSH wrapper, wrap each command the way they showed (`ssh <host> '<command>'`).

Ask once for the deployment directory (the folder with keystone-mcp's `docker-compose.yml` and `.env`; after `init --adopt` it also has `keystonectl/`, `packages/`, and `services.lock.yaml`). Pass it as `--root` on every command. On keystone-deploy that directory is the `mcp` folder of the keystone-deploy checkout.

## Before you start

Run these, then retell the result in one or two sentences:

```
keystonectl --version --json
keystonectl --root /srv/keystone check --json
keystonectl --root /srv/keystone service list --json
keystonectl --root /srv/keystone catalog list --json
```

Use the operator's directory instead of `/srv/keystone`.

- If `keystonectl` is missing, tell the operator to install it (`uv tool install keystonectl`, or once with `uvx keystonectl`). Wait. Do not install it yourself.
- If `check` returns code 3 `check_failed` and `result.checks` says `services.lock.yaml` is missing, say that `init --adopt` only adds missing keystonectl files and does not overwrite compose or `.env`. After the operator says yes, run:

```
keystonectl init --adopt /srv/keystone --json
```

Then repeat `result.compose_command` to the operator. keystone-mcp starts with two compose files (`docker-compose.yml` and `docker-compose.packages.yml`). The operator runs that command, or keystone-deploy's `start.sh` does. You do not.
- If any other check has `status: error`, retell those checks and do not install anything until the operator decides.

## Install flow

1. **Choose.** From `catalog list`, say each bundle and package in plain words: name, the `summary` sentence, and whether it fits this keystone-mcp. Ask what to install. Install only from catalogs the operator already connected. Run `catalog add` only when they ask, and only with the address they gave:

```
keystonectl --root /srv/keystone catalog add https://example.com/catalog.git --json
```

2. **Plan.** For one package:

```
keystonectl --root /srv/keystone service plan file-mcp --json
```

For a bundle:

```
keystonectl --root /srv/keystone bundle install minimal --json
```

Code 2 on a bundle stops at the first package that still needs answers. Packages already installed stay installed. Retell `result.packages`. Do not say that nothing changed.

3. **Ask.** For each `missing` item, ask one question. Use the Questions table, or the item's `question` text when the name is not in the table. List `choices`. Offer `default` when it is set. If the operator already gave that answer, do not ask again.

4. **Answer.** Run the same command again with answers in the environment. Secrets are paths (rule 2):

```
KEYSTONE_SETUP_RESOURCE_S3=catalog \
KEYSTONE_SETUP_SERVICE_FILE_MCP_DOMAIN=files.example.com \
KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS=certbot \
KEYSTONE_SETUP_TLS_EMAIL=ops@example.com \
keystonectl --root /srv/keystone service apply file-mcp --json
```

While the code is 2, go back to step 3 with the new `missing` list. Running `apply` again with the same answers is safe: answers are stored in the lock, and finished work is not redone.

`{PKG}` in a variable is the package name in upper case with `-` turned into `_` (`file-mcp` → `FILE_MCP`).

5. **Errors.** Code 3: read `error.id`. If `retryable` is true, explain `error.hint` in plain words (or `explain`) and ask before you retry. If it is false, do not retry; say what must be fixed and who fixes it. Code 1: the command itself is wrong. Re-read this skill. Do not invent flags.

```
keystonectl explain dns_mismatch --json
```

6. **Summary.** From `result`, say what was installed, the version, namespace, new tools, which resources are external or from the catalog, the domain and certificate (until when, and who renews it), what to check, and warnings. `tools_unreachable` means nobody can call the tools until an administrator adds roles.

7. **Policy.** Follow Policy below.

8. **Check.**

```
keystonectl --root /srv/keystone service status file-mcp --json
```

Retell the container, whether keystone-mcp can see the service, the in-network link, the certificate expiry, and `problems`. If you can call keystone-mcp, confirm the `<namespace>_*` tools are visible.

Publishing later is `--defer-public`. Ask before you use it:

```
keystonectl --root /srv/keystone service apply file-mcp --defer-public --json
```

## Resources

Ask with the Questions table.

- **External** means the operator already created the bucket and key, or the database and user, and puts secrets in mode 600 files.
- **Catalog** means keystonectl starts `minio` or `postgres` once for the whole installation. Each service gets its own bucket, or its own database and user.
- The second service that needs the same kind of resource is not asked again. Say it will use the provider already installed. If the operator sets a conflicting answer anyway, retell `resource_answer_ignored` and name the provider that is already bound.
- A service may receive both a Postgres URL and the separate host, port, database, user, and sslmode fields. Do not ask the operator to choose between those forms.
- When several providers exist, ask `KEYSTONE_SETUP_RESOURCE_S3_PACKAGE` or `KEYSTONE_SETUP_RESOURCE_POSTGRES_PACKAGE` (`source/name`).

## Upgrade

```
keystonectl --root /srv/keystone service upgrade file-mcp --json
```

Retell `result`: the new version, tools that appear and disappear, and policy rows that stop matching. After the operator says yes, answer the `missing` confirm. Its value is the target version, not the word yes:

```
KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_UPGRADE=1.1.0 \
keystonectl --root /srv/keystone service upgrade file-mcp --json
```

If the service was ejected, say that upgrade updates the registry, the snapshot, and secrets, and does not change compose or the image. The operator changes those. If upgrade stops with `fragment_modified`, follow Operator's own changes.

## Rollback

Ask first. There is no confirm variable.

```
keystonectl --root /srv/keystone service rollback file-mcp --json
```

## Remove

First say what stays (volumes, bucket or database, certificate). After the operator says yes, run remove. Code 2 asks for the confirm variable. Answer `yes`:

```
keystonectl --root /srv/keystone service remove file-mcp --json
```

```
KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE=yes \
keystonectl --root /srv/keystone service remove file-mcp --json
```

Retell `result.kept`.

## Connect a running service

Use this only when the operator wants a service they already run, and they give the URL. Add filter flags only when they named them (`--tool-prefix`, `--include-prefixes`, `--exclude-prefixes`, `--strip-prefix`).

```
keystonectl --root /srv/keystone service add-remote billing --url https://billing.example.com/mcp --json
```

If the registry entry already exists, ask, then pass `--adopt-existing`. Secrets are not reissued.

```
keystonectl --root /srv/keystone service add-remote billing --url https://billing.example.com/mcp --tool-prefix bill --include-prefixes bill_ --exclude-prefixes bill_admin --strip-prefix bill_ --adopt-existing --json
```

The result includes a path to a mode 600 file for the team that runs the service. Do not read that file and do not retell its contents.

## Operator's own changes

Do this only when the operator asks. The exception: `upgrade` or `check` stopped on `fragment_modified`. Do not suggest override or eject on your own. Do not edit files yourself.

- **Override.** They want to change how the service runs (memory limit, a volume, an extra variable, a sidecar container). Edits go in `packages/<name>.override.yml` in the deployment directory. keystonectl does not change that file, and upgrade keeps it. The operator is responsible if a new package version renames variables or containers; the upgrade result shows that. The operator edits the file. Then run:

```
keystonectl --root /srv/keystone check --json
```

Retell warnings about open ports and privileges. The operator decides.

- **Eject.** They want to own the compose fragment. Before the command, say what changes: keystonectl stops writing `packages/<name>.yml` and the operator owns that file; the registry entry, snapshot, secrets, and `status` continue; upgrade no longer changes the image or compose; `service adopt` generates the fragment again and drops hand edits in it (move those edits into the override first). There is no confirm variable. After they say yes:

```
keystonectl --root /srv/keystone service eject file-mcp --json
```

- **Adopt.** Say that the hand-written fragment will be replaced. After they say yes, run adopt. Code 2 asks for `CONFIRM_ADOPT`. Answer `yes`:

```
KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_ADOPT=yes \
keystonectl --root /srv/keystone service adopt file-mcp --json
```

- **Fragment edited by hand.** `check` or `upgrade` reports `fragment_modified`. Say that `packages/<name>.yml` was edited by hand and keystonectl would overwrite it. Offer two paths: the operator moves the edits into the override, or eject. Do not choose.

## Status only

```
keystonectl --root /srv/keystone service status --json
```

```
keystonectl --root /srv/keystone service status file-mcp --json
```

```
keystonectl --root /srv/keystone check --json
```

Retell `problems` on status. They can include `container_missing`, `container_unhealthy`, `init_failed`, `init_pending`, `public_upstream_down`, and `hairpin_failed`. Those are status problems, not the warning table below.

## Questions

`{PKG}` is the package name in upper case with `-` turned into `_`. `{NAME}` is the variable name from the question. Ask one question at a time.

| Variable | Ask |
|---|---|
| `KEYSTONE_SETUP_KEYSTONE_MCP_VERSION` | Which keystone-mcp version is running here (for example 0.5.0)? |
| `KEYSTONE_SETUP_SERVICE_{PKG}_ENV_{NAME}` | A setting of this service. Use the `question` text. Offer `default` when it is set. |
| `KEYSTONE_SETUP_SERVICE_{PKG}_SECRET_{NAME}_FILE` | Secret. Rule 2. |
| `KEYSTONE_SETUP_RESOURCE_S3` | Where should files be stored: in S3 you already have, or a new MinIO on this server? `external` or `catalog`. |
| `KEYSTONE_SETUP_RESOURCE_S3_PACKAGE` | Several storage packages are in the catalogs. Name them. Which one should be installed? |
| `KEYSTONE_SETUP_S3_ENDPOINT` | What is the address of your S3 (for example `https://storage.example.com`)? |
| `KEYSTONE_SETUP_S3_REGION` | What is the S3 region? If you do not know, use `us-east-1`. |
| `KEYSTONE_SETUP_S3_PATH_STYLE` | Is your S3 MinIO or similar (bucket name in the path)? If you do not know, use `true`. |
| `KEYSTONE_SETUP_S3_BUCKET` | What is the name of the bucket you created for this service? |
| `KEYSTONE_SETUP_S3_ACCESS_KEY_FILE`, `KEYSTONE_SETUP_S3_SECRET_KEY_FILE` | Secret. Rule 2. |
| `KEYSTONE_SETUP_RESOURCE_POSTGRES` | Where should the service store data: in Postgres you already have, or a shared Postgres on this server? Each service gets its own database. `external` or `catalog`. |
| `KEYSTONE_SETUP_RESOURCE_POSTGRES_PACKAGE` | Several Postgres packages are in the catalogs. Name them. Which one should be installed? |
| `KEYSTONE_SETUP_POSTGRES_URL_FILE` | A single connection string can go in a mode 600 file; send the path. Or answer the separate questions. Secret. Rule 2. |
| `KEYSTONE_SETUP_POSTGRES_HOST`, `KEYSTONE_SETUP_POSTGRES_PORT`, `KEYSTONE_SETUP_POSTGRES_DATABASE`, `KEYSTONE_SETUP_POSTGRES_USER`, `KEYSTONE_SETUP_POSTGRES_SSLMODE` | What are the Postgres host and port? What database and user did you create for this service? Should the connection be encrypted? If the server is not on this machine, use `require`. |
| `KEYSTONE_SETUP_POSTGRES_PASSWORD_FILE` | Secret. Rule 2. |
| `KEYSTONE_SETUP_SERVICE_{PKG}_S3_BUCKET`, `KEYSTONE_SETUP_SERVICE_{PKG}_S3_ACCESS_KEY_FILE`, `KEYSTONE_SETUP_SERVICE_{PKG}_S3_SECRET_KEY_FILE` | Same S3 questions for this service only. They override the shared answers. Secret names are rule 2. |
| `KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_DATABASE`, `KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_USER`, `KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_PASSWORD_FILE`, `KEYSTONE_SETUP_SERVICE_{PKG}_POSTGRES_URL_FILE` | Same Postgres questions for this service only. They override the shared answers. Secret names are rule 2. |
| `KEYSTONE_SETUP_SERVICE_{PKG}_DOMAIN` | Which hostname should links use? It must already point at this server (for example `files.example.com`). |
| `KEYSTONE_SETUP_SERVICE_{PKG}_DNS` | Not asked; read with default `check`. Set `skip` only when the operator says the name is behind a proxy or outbound DNS is closed. |
| `KEYSTONE_SETUP_SERVICE_{PKG}_TLS` | Certificate: keep the one found (say the expiry), a free Let's Encrypt certificate, your own files, or you run the proxy and certificate (for example Caddy)? `keep`, `certbot`, `custom`, or `manual`. For `manual`, repeat the sample vhost path and say keystonectl will check the hostname over https. |
| `KEYSTONE_SETUP_SERVICE_{PKG}_CERT_PATH`, `KEYSTONE_SETUP_SERVICE_{PKG}_KEY_PATH` | Where on the server are the certificate (full chain, PEM) and the key? Paths, not contents. |
| `KEYSTONE_SETUP_TLS_EMAIL` | Which email should Let's Encrypt use for expiry reminders? |
| `KEYSTONE_SETUP_SERVICE_{PKG}_TAKEOVER` | Site <file> serves <domain>. keystonectl will disable that site entirely: only the service paths and /healthz will answer on this name, everything else returns 404. Disable it? If apply then fails, the site is enabled again: tell the operator and read the names from `result.public.restored_sites`. |
| `KEYSTONE_SETUP_SERVICE_{PKG}_NAMESPACE` | The tool-name prefix is taken. Which prefix should be used instead? |
| `KEYSTONE_SETUP_SERVICE_{PKG}_CONFIRM_REMOVE`, `KEYSTONE_SETUP_SERVICE_{PKG}_CONFIRM_UPGRADE`, `KEYSTONE_SETUP_SERVICE_{PKG}_CONFIRM_ADOPT` | Confirm only after you have retold the question and the consequences. `CONFIRM_REMOVE` and `CONFIRM_ADOPT` are `yes`. `CONFIRM_UPGRADE` is the target version. `eject` and `rollback` have no confirm variable. |
| `package` (`kind: argument`) | This package name is in two catalogs. Which one? Repeat the command as `source/name`. |

## Rules

1. **Only keystonectl.** Do not run `docker`, `nginx`, `certbot`, `systemctl`, `psql`, or `mc`. Do not edit files. Do not read `.env`, `packages/*.env`, `services.lock.yaml`, or secret files. The exception is the operator's own terminal command in rule 2. They run it. You do not. An SSH wrapper the operator gave you is allowed.
2. **Secrets never travel in the chat.** External S3 keys, an external Postgres password or connection string, and any `missing` item with `secret: true`: ask the operator to create a mode 600 file on the host and send only the path. Give them this command for their terminal. Do not run it.

```
<!-- operator-command -->
install -m 600 /dev/null /root/keystone-secrets/pg-url && nano /root/keystone-secrets/pg-url
```

Pass the path in a `…_FILE` variable. Do not read the file and do not print it. If the operator pastes a secret anyway, do not repeat it, do not use it, and tell them to rotate that key or password.
3. Never pass a secret as a command argument or as a variable value. Only a path, and only in a name that ends with `_FILE`.
4. **Do not touch policy.** Do not edit the policy file and do not call `policy_*`. Hand the recommended rows to the policy skill (Policy).
5. **Ask the operator before** `remove`, `upgrade`, `rollback`, `eject`, `adopt`, `catalog add`, `init --adopt`, `--defer-public`, `TAKEOVER=yes`, and `DNS=skip`. If apply fails after a takeover, tell the operator the site was enabled again. The names are `result.public.restored_sites`.
6. **Override and eject only when the operator asks**, after you have explained what upgrade will do. The `fragment_modified` stop is the exception in Operator's own changes.
7. **Do not retry blindly.** `retryable: false`: do not retry. `retryable: true`: retry only after you have explained and the operator agrees (or says the DNS record is fixed). For `busy`, wait one minute and retry once.
8. **Do not invent answers.** If there is no `default`, ask. Do not install packages the operator did not ask for, except packages a bundle pulls in through `requires`, and a resource provider they chose with `catalog`. Say that you are installing those.
9. **Retell the result.** Do not stop at "done". Say the namespace, tools, resources, domain, warnings, and what an administrator still has to do.

## Policy

Read `result.recommended_policy` from `apply` or from `bundle plan`/`bundle install`. There are three forms. Tell them apart by shape, not by command:

- **A list of roles** (each item has `allow`): use that list as is.
- **One example file** (an object with `path` and, when it fits, `content`; or only the example text): build the list from its `roles` section. One entry per role: `{role: <name>, allow: [<patterns>]}`. Add nothing. Do not invent tool names.
- **A list of files by package** (each item has `package`, and `path` and, when it fits, `content`, but no `allow`): this is what `bundle plan`/`bundle install` return, one item per package that recommends a policy. For each item, parse `content` as the policy example of that package, collect its `roles`, then merge into one `{role, allow}` list; the same role name from two packages is one entry with the union of patterns.

Also take `result.critical_tools` (or the package `critical` list). For bundles it is a list of `{package, name}`; use the `name` values the same way as a flat list. That is the hint for `critical_tools`.

If you have the `policy_*` tools and the `keystone-policy-admin` skill (you are connected to keystone-mcp as an administrator): say that an administrator sets roles, and pass the list in one sentence: `Recommended rows from package file-mcp 1.0.0 (namespace files): [{"role": "files_user", "allow": ["files_create_upload_url", "files_get_download_link", "files_get_file_info"]}]. Critical tools: files_create_upload_url, files_get_download_link.` The policy skill shows the change and applies it after the administrator confirms. Do not call `policy_*` yourself.

If you do not have `policy_*` (keystone-mcp is older than 0.5.0, or you are not an administrator): show the same list and the policy path from `check` (`KEYSTONE_MCP_RBAC_POLICY_PATH`, or the `policy_not_configured` warning). Say the administrator adds the rows and reloads the policy (SIGHUP). Do not open the policy file.

If the namespace changed during install, the example patterns use the default prefix. Say so, and replace that prefix in the list. That is the same list under the new name, not a new list.

## Errors

Match `error.id`. Code and `retryable` are the registry values. Say the sentence in plain words. Do the action. For the registry text, run `keystonectl explain <id> --json`.

| id | code | retryable | Tell the operator | Do |
|---|---|---|---|---|
| `usage` | 1 | false | The command was not understood. | Check the command and flags. Run keystonectl --help. |
| `root_not_found` | 1 | false | No deployment directory was found. | Pass --root DIR, set KEYSTONECTL_ROOT, or run keystonectl init --adopt <dir>. |
| `secret_in_argument` | 1 | false | A secret was passed on the command line. | Secrets are accepted only as a path to a mode 600 file, via the environment. |
| `not_implemented` | 1 | false | This command cannot apply changes yet. | No deploy step is configured for this change. Run service plan to preview it. |
| `package_ambiguous` | 2 | false | More than one catalog has this package. | Pass the package as source/name, for example local/file-mcp. service upgrade reads the source from the lock. |
| `keystone_mcp_version_unknown` | 2 | false | The keystone-mcp version could not be read from the image. | Set KEYSTONE_SETUP_KEYSTONE_MCP_VERSION to the version that is running, for example 0.5.0. |
| `keystone_service_ambiguous` | 2 | false | More than one compose service looks like keystone-mcp. | Re-run init --adopt with --keystone-service NAME. |
| `package_not_found` | 3 | false | The package is not in any configured catalog. | Check the name, or add the catalog with keystonectl catalog add. |
| `version_not_found` | 3 | false | That version is not listed in the catalog. | Run keystonectl catalog list and pick a listed version. |
| `source_invalid` | 3 | false | The catalog source is not valid. | Use an https://, ssh://, git@host:path, or file:// URL. http:// is refused. |
| `source_exists` | 3 | false | A source with that name or address is already configured. | Pick another --name, or edit sources.yaml. |
| `tag_not_found` | 3 | false | That git tag does not exist. | Check the ref in sources.yaml or catalog.yaml. For the default source, update keystonectl or add sources.yaml with a ref that exists. |
| `tag_moved` | 3 | false | The tag now points at a different commit than the one recorded in the lock. | Refuse the move. Pin a commit, or upgrade on purpose with service upgrade. |
| `manifest_invalid` | 3 | false | The package manifest is not valid. | Fix the problems listed in result.problems and run keystonectl lint. |
| `catalog_invalid` | 3 | false | The catalog file is not valid. | Fix catalog.yaml and run keystonectl lint on it. |
| `incompatible_keystone_mcp` | 3 | false | The package does not support this keystone-mcp version. | Choose a version whose range includes the running keystone-mcp, or upgrade it. |
| `requires_missing` | 3 | false | A required package is not installed. | Install them first or use bundle install. |
| `lock_corrupt` | 3 | false | services.lock.yaml is missing, unreadable, or not layout version 1. | Restore services.lock.yaml.bak or run keystonectl init again on a copy. |
| `secret_file_unsafe` | 3 | false | A secret file is missing, is a link, or is readable by others. | Use a regular file owned by you or root and run chmod 600. |
| `layout_unsupported` | 3 | false | This deployment uses a layout version this keystonectl does not know. | layout_version must be 1. |
| `check_failed` | 3 | false | The deployment check found an error. | Read result.checks for the failing id and its hint. |
| `source_unreachable` | 3 | true | The catalog or package source could not be reached. | Check the network and the URL, then run the command again. |
| `busy` | 3 | true | Another keystonectl process holds the lock. | Wait for it to finish and run the command again. |
| `adopt_required` | 3 | false | This directory already runs a compose project. | Use keystonectl init --adopt <dir>. |
| `root_not_empty` | 3 | false | The directory is not empty and has no compose file to adopt. | Use an empty directory, or pass a directory that already runs keystone-mcp. |
| `compose_not_found` | 3 | false | No compose file was found. | Add docker-compose.yml (or compose.yaml) and run init --adopt. |
| `compose_unparseable` | 3 | false | The compose file is not valid YAML. | Fix the compose file and run init --adopt again. |
| `keystone_service_not_found` | 3 | false | No service runs keystone-mcp. | Pass --keystone-service NAME if the service was renamed. |
| `keystone_env_file_missing` | 3 | false | The keystone-mcp service has no env file inside the deployment. | Add env_file: .env to the keystone-mcp service. |
| `path_outside_root` | 3 | false | A mounted path points outside the deployment directory. | Keep registry, snapshot, and policy files inside the deployment root. |
| `registry_path_unmapped` | 3 | false | A container path is not mounted from the deployment directory. | Bind-mount the file from inside the deployment root. |
| `registry_invalid` | 3 | false | The upstream registry is not a schema_version 1 document. | The file needs schema_version: 1 and an entries list. |
| `registry_inline` | 3 | false | Upstreams are still an inline JSON array. | Move these entries to upstreams.yaml and set KEYSTONE_MCP_UPSTREAMS_PATH. keystonectl does not edit inline JSON. |
| `env_name_conflict` | 3 | false | An environment name in the managed block is already set outside it. | Remove the operator's copy of that name or rename it. |
| `no_provider` | 3 | false | No catalog provider offers this resource. | Choose external and point at storage you already run, or add a catalog that provides it. |
| `resource_already_bound` | 3 | false | This installation already has a provider for that resource. | One provider per kind. Use the provider that is already bound, or remove it first. |
| `resource_in_use` | 3 | false | That provider still has consumers. | Unbind the consumers first. Their buckets and databases are kept. |
| `s3_access_denied` | 3 | false | S3 refused the access key. | Check the access key and the secret key, then run the command again. |
| `s3_bucket_missing` | 3 | false | The S3 bucket does not exist. | Create the bucket in the storage you already have, then run the command again. |
| `s3_wrong_region` | 3 | false | The S3 region does not match the bucket. | Set KEYSTONE_SETUP_S3_REGION to the bucket's region. |
| `s3_presign_failed` | 3 | false | S3 could not sign an upload or download link. | Allow s3:GetObject and s3:PutObject on the bucket, then run the command again. |
| `postgres_access_denied` | 3 | false | PostgreSQL refused the user or password. | Check the user and the password file, then run the command again. |
| `postgres_database_missing` | 3 | false | The PostgreSQL database does not exist. | Create the database and the user, then run the command again. |
| `postgres_version_too_old` | 3 | false | PostgreSQL is older than version 14. | Use a PostgreSQL server that is version 14 or newer. |
| `postgres_create_table_failed` | 3 | false | The PostgreSQL user cannot create tables in its database. | Grant the user rights on its own schema, then run the command again. |
| `postgres_not_isolated` | 3 | false | The PostgreSQL user can reach another database. | This is a provider bug. The consumer must not connect to other databases. |
| `postgres_extension_missing` | 3 | false | A required PostgreSQL extension is missing. | Create the extensions named in the message, then run the command again. |
| `postgres_url_invalid` | 3 | false | The PostgreSQL URL is missing sslmode. | Add sslmode to the URL, for example ?sslmode=require. |
| `postgres_address_mismatch` | 3 | false | The PostgreSQL URL points at a different server than the one already bound. | Use the host, port, and sslmode already recorded for PostgreSQL. Database, user, and password can differ for each service. |
| `s3_unreachable` | 3 | true | S3 could not be reached. | Check the endpoint and the network, then run the command again. |
| `postgres_unreachable` | 3 | true | PostgreSQL could not be reached. | Check the host, the port, and the network, then run the command again. |
| `provision_failed` | 3 | false | The provider could not create the consumer's access. | If the provider was not ready yet, run the command again. Otherwise fix the provider package. |
| `docker_unavailable` | 3 | true | Docker or the keystone-resources network is not available. | Start Docker. The keystone-resources network is created when a service is deployed. |
| `registry_name_taken` | 3 | false | That registry name is already used by an entry keystonectl does not own. | Pick another package name, or remove the hand-written registry entry. |
| `registry_full` | 3 | false | The upstream registry already has 64 entries. | Remove an entry that keystonectl does not manage, then run the command again. |
| `namespace_change_unsupported` | 3 | false | The tool prefix of an installed service cannot be changed. | Run keystonectl service remove, then install the service again. |
| `policy_breaks` | 3 | false | The catalog snapshot would make the current policy invalid. | Fix the policy, or install a version whose tools the policy already allows. |
| `compose_too_old` | 3 | false | Docker Compose is older than 2.24. | Upgrade the Docker Compose plugin to 2.24 or newer. |
| `layout_invalid` | 3 | false | keystone-mcp cannot see the registry, the snapshot, or the service network. | Run keystonectl init --adopt <root> and start compose with both files: docker compose -f docker-compose.yml -f docker-compose.packages.yml. |
| `fragment_modified` | 3 | false | A generated package fragment was edited by hand. | Move your changes into the override file, or eject the service. |
| `init_failed` | 3 | false | An init container exited with an error. | Read the container log, fix the package, and run the command again. |
| `keystone_mcp_image_unavailable` | 3 | true | The keystone-mcp image is not available. | Pull the image recorded in the lock, or build it with your deployment's own tooling, then retry. |
| `service_unhealthy` | 3 | true | A service container did not become healthy. | Read the container log and run the command again. |
| `domain_invalid` | 3 | false | The public domain is not a lowercase hostname. | Use a lowercase DNS name with at least two labels, for example files.example.com. |
| `domain_in_use` | 3 | false | That domain is already used by another service or nginx site. | Pick another domain, or disable the other site and run the command again. |
| `sudo_required` | 3 | false | nginx and certificates need passwordless sudo, or TLS=manual. | Allow sudo -n for nginx and certbot, or answer TLS=manual. |
| `nginx_missing` | 3 | false | nginx is not installed. | Install nginx, or answer TLS=manual. |
| `nginx_config_invalid` | 3 | false | nginx -t rejected the new site. | Read the nginx -t output. The previous site file was restored. |
| `certbot_missing` | 3 | false | certbot is not installed. | Install certbot and python3-certbot-nginx, or choose another TLS method. |
| `cert_unreadable` | 3 | false | The certificate or key file cannot be read. | Check the path and try another KEYSTONE_SETUP_SERVICE_<NAME>_CERT_PATH or _KEY_PATH. |
| `cert_not_pem` | 3 | false | The certificate is not a PEM file. | A .pfx or .der file must be converted: openssl pkcs12 -in x.pfx -nokeys -out fullchain.pem |
| `key_not_pem` | 3 | false | The private key is not a PEM file. | Replace KEYSTONE_SETUP_SERVICE_<NAME>_KEY_PATH with a PEM key. |
| `key_mismatch` | 3 | false | The private key does not match the certificate. | Point KEYSTONE_SETUP_SERVICE_<NAME>_KEY_PATH at the key for this certificate. |
| `cert_expired` | 3 | false | The certificate has expired. | Replace KEYSTONE_SETUP_SERVICE_<NAME>_CERT_PATH with a current certificate. |
| `cert_install_failed` | 3 | false | The certificate files could not be installed. | Check sudo and the destination directory, then run the command again. |
| `port_range_exhausted` | 3 | false | Every loopback port from 47600 to 47699 is in use. | Free a port in that range and run the command again. |
| `required_by` | 3 | false | Another installed service requires this one. | Remove the services listed in the message first. |
| `no_previous_version` | 3 | false | There is no previous version to roll back to. | Rollback restores one upgrade. Install the version you want with service apply. |
| `not_ejectable` | 3 | false | A remote service has no generated fragment to eject. | Remote services are not generated. There is nothing to eject. |
| `not_ejected` | 3 | false | This service was not ejected. | service adopt only takes back a fragment that was ejected. |
| `bundle_not_found` | 3 | false | The bundle is not in any configured catalog. | Check the name, or add the catalog with keystonectl catalog add. |
| `requires_cycle` | 3 | false | The bundle's requires dependencies contain a cycle. | Remove the cycle in the package manifests and run the command again. |
| `remote_url_mismatch` | 3 | false | The URL does not match the registry entry being adopted. | Pass the URL already stored in keystonectl/upstreams.yaml. |
| `not_package` | 3 | false | A remote service has no package. | A remote service has no package; use add-remote to change its url. |
| `priority_taken` | 3 | false | That source priority is already used. | Pick another --priority. Priority only affects listing order. |
| `public_ip_unknown` | 3 | true | The public address of this host could not be read. | Check outbound HTTPS and run the command again, or answer DNS=skip. |
| `dns_no_record` | 3 | true | The domain has no A record. | Add an A record for the domain pointing at this host, then run apply again. |
| `dns_mismatch` | 3 | true | The domain does not point at this host. | Update the A record or wait for DNS to propagate (often 1-10 min), then run apply again. |
| `certbot_failed` | 3 | true | certbot could not issue the certificate. | Check DNS, port 80, and /var/log/letsencrypt/letsencrypt.log, then run apply again. |
| `public_unreachable` | 3 | true | https on the public domain did not answer. | Finish the proxy and certificate, then run apply again. |
| `internal_error` | 3 | false | An unexpected error occurred. | Report this with the command and the keystonectl version. |

When `service apply` ends in `service_unhealthy` or `init_failed`, read `result.rollback` and tell the operator what is left. With `lock_entry` `dropped`, the containers in `containers` were stopped and removed, the package is not in `services.lock.yaml`, and only data stays: the resource, its env file, the secrets, volumes, buckets, and databases. With `lock_entry` `kept`, the service was installed before, its files were restored, and its containers were not touched. Running the command again is safe and does not ask about the resource a second time.

## Warnings

Warnings stay on code 0. Retell them. Do not hide them behind "done".

| id | code | retryable | Tell the operator | Do |
|---|---|---|---|---|
| `catalog_cached` | 0 | false | The catalog source could not be updated; the cached copy was used. | Restore network access and run the command again to refresh the cache. |
| `local_source` | 0 | false | This source uses a file:// URL. | file:// is for tests and local development, not for a shared catalog. |
| `compat_mismatch` | 0 | false | The manifest compatibility range differs from the catalog. | The manifest range is the one that is enforced. |
| `catalog_ref_chosen` | 0 | false | No --ref was given, so the newest vX.Y.Z tag was selected. | Pass --ref to pin a different catalog tag. |
| `compose_file_ignored` | 0 | false | A compose file in the package is ignored. | The installer never reads it. |
| `published_name_long` | 0 | false | A published tool name is longer than 64 characters. | Many MCP clients reject names longer than 64 characters. |
| `builtin_list_may_be_stale` | 0 | false | built-in tool names are known up to keystone-mcp 0.5.0 | built-in tool names are known up to keystone-mcp 0.5.0; the final check is upstream validate during apply. |
| `builtin_tools_unavailable` | 0 | false | The live built-in tool list could not be read during apply. | Say the policy check was partial; suggest `check`. |
| `keystone_compose_missing` | 0 | false | No keystone-mcp compose file was found. | Put keystone-mcp's docker-compose.yml and .env here (see keystone-mcp examples/production) and run keystonectl init again. |
| `layout_changed` | 0 | false | The recognized layout differs from the lock. | Review the listed fields. The lock was updated to match the compose file. |
| `compose_variable_unset` | 0 | false | A compose variable was empty because it was not set. | Set the variable in the environment or in the deployment .env file. |
| `inline_registry_ignored` | 0 | false | KEYSTONE_MCP_UPSTREAMS is an empty JSON array and was ignored. | Upstreams are read from keystonectl/upstreams.yaml. |
| `registry_moved` | 0 | false | Existing upstream entries were copied into keystonectl/upstreams.yaml. | keystone-mcp now reads keystonectl/upstreams.yaml; your old registry file is no longer read. |
| `snapshot_moved` | 0 | false | The catalog snapshot was copied into keystonectl/catalog-snapshot.yaml. | keystone-mcp now reads the snapshot from keystonectl/. |
| `policy_not_configured` | 0 | false | No RBAC policy file was found. | check will skip the policy comparison until a policy file is configured. |
| `comments_dropped` | 0 | false | Comments in a rewritten YAML file were not kept. | A one-time .orig copy was saved next to the file. |
| `unmanaged_upstream` | 0 | false | An upstream registry entry is not managed by keystonectl. | The entry is left as-is. It belongs to the operator. |
| `orphan_fragment` | 0 | false | A package fragment has no matching lock entry. | Remove the file or install the package that owns it. |
| `fragment_modified` | 0 | false | A generated package fragment was edited by hand. | Move your changes into the override file, or eject the service. |
| `s3_plain_http` | 0 | false | The S3 endpoint uses plain HTTP. | Prefer https:// for storage that is not on your own Docker network. |
| `postgres_plain_connection` | 0 | false | The PostgreSQL connection does not require certificate verification. | Prefer sslmode require, verify-ca, or verify-full for a server you do not run here. |
| `s3_lifecycle_unsupported` | 0 | false | Storage works, but old uploads will not be deleted automatically. | The service still runs. Add a lifecycle rule on the bucket later if you need it. |
| `policy_check_skipped` | 0 | false | The policy check was skipped. | Set KEYSTONE_MCP_RBAC_MODE to something other than off and point KEYSTONE_MCP_RBAC_POLICY_PATH at a policy file. |
| `policy_already_inconsistent` | 0 | false | The policy already has the same unmatched allow patterns. | The new snapshot does not add unmatched patterns. Fix the policy when you can. |
| `tools_unreachable` | 0 | false | No role can call these tools yet. | Ask an administrator to add policy rows (see the recommended policy below). |
| `discovery_pending` | 0 | false | keystone-mcp has not discovered this service yet. | keystone-mcp will try again on its own. Check keystonectl service status. |
| `validation_skipped_local` | 0 | false | Upstream and policy checks were skipped because the local image is not built. | Build the keystone-mcp image, then run keystonectl service apply again. |
| `override_not_included` | 0 | false | An override file exists but is not included. | Run keystonectl service apply <name> to include it. |
| `override_widens_access` | 0 | false | The merged compose model gives a package service more access than the manifest. | This is the operator's choice. Review ports, privileges, and host mounts. |
| `override_unchecked` | 0 | false | Override access could not be checked. | Say the check did not run; fix the compose files and run check again. |
| `compose_version_unknown` | 0 | false | Docker Compose version could not be read. | Say compose version is unknown; install the Docker Compose plugin. |
| `check_skipped_no_docker` | 0 | false | A check that needs Docker was skipped. | Say the check did not run because Docker is unavailable. |
| `unparsed_enddate` | 0 | false | The certificate end date could not be read. | Say the certificate end date is unreadable; check the certificate file. |
| `tls_untrusted` | 0 | false | The public certificate is not trusted by the system trust store. | The service answered. Install the certificate in the system trust store if clients should trust it. |
| `dns_system_resolver` | 0 | false | dig is not installed, so the system resolver was used. | Install dig for a check against 1.1.1.1. The system resolver can be cached. |
| `chain_missing` | 0 | false | The certificate file has no chain. | Add the intermediate certificates to the full chain. |
| `cert_may_not_cover_domain` | 0 | false | The certificate may not cover this domain. | Check the certificate names. A wildcard covers one label only. |
| `cert_expiring` | 0 | false | The certificate expires soon. | Renew it before it expires. service status warns again until you do. |
| `renewal_timer_missing` | 0 | false | certbot has no renewal timer. | Enable the certbot systemd timer or /etc/cron.d/certbot. |
| `renewal_manual` | 0 | false | Renewal of this certificate is on you. | service status warns 30 days before expiry. |
| `public_check_failed` | 0 | false | The public health check did not return 200. | The service is installed. Check the proxy and run apply again. |
| `public_pending` | 0 | false | The public address is not ready. | Run service apply again with a domain when you want the public paths published. |
| `extra_hosts_manual` | 0 | false | A fragment was not updated with extra_hosts. | Add the host line yourself in the override or in your own compose file. |
| `policy_rows_orphaned` | 0 | false | Policy rows still match tools this service no longer publishes. | Delete these policy rows. The tool names stay reserved until you do. |
| `remote_without_identity` | 0 | false | This remote service has no bearer or identity secret. | Catalog packages must check the assertion. Here that check is yours. |
| `remote_tools_not_in_snapshot` | 0 | false | Live tools are missing from the catalog snapshot. | Run keystonectl service add-remote again with --tools. |
| `remote_tools_unknown` | 0 | false | Policy rows for this namespace are checked only while the service is up. | Start the service on the keystone-mcp-upstreams network and run check again. |
| `remote_unreachable` | 0 | false | The remote URL did not answer from the keystone-mcp-upstreams network. | keystone-mcp will keep retrying; make sure the service is on the keystone-mcp-upstreams network. |
| `resource_answer_ignored` | 0 | false | An answer about a resource that is already bound was ignored. | Tell the operator the service uses the provider that is already there, and name it. |
| `package_name_shadowed` | 0 | false | A package name now exists in more than one catalog. | Install it by full name source/name. |
| `bundle_name_shadowed` | 0 | false | A bundle name now exists in more than one catalog. | Install it by full name source/name. |

## Examples

Operator: Install the file service. Links should open at files.example.com.

```
<!-- scenario: file-mcp-minio -->
keystonectl --root /srv/keystone check --json
keystonectl --root /srv/keystone catalog list --json
keystonectl --root /srv/keystone service plan file-mcp --json
# code 2, missing: KEYSTONE_SETUP_RESOURCE_S3, KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS
Agent: Where should file-mcp keep files: in S3 you already have, or in a new MinIO on this server?
Operator: New MinIO.
Agent: For files.example.com, issue a free Let's Encrypt certificate, or use your own files?
Operator: Let's Encrypt, reminders to ops@example.com.
KEYSTONE_SETUP_RESOURCE_S3=catalog \
KEYSTONE_SETUP_SERVICE_FILE_MCP_DOMAIN=files.example.com \
KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS=certbot \
KEYSTONE_SETUP_TLS_EMAIL=ops@example.com \
keystonectl --root /srv/keystone service apply file-mcp --json
# code 0
keystonectl --root /srv/keystone service status file-mcp --json
<!-- /scenario -->
```

Agent: file-mcp 1.0.0 is installed with a new MinIO on this server (other services that need S3 get their own bucket in it). Tools: files_create_upload_url, files_get_download_link, files_get_file_info. Links open at https://files.example.com (Let's Encrypt, renews automatically). Nobody can call the tools yet. Recommended rows from package file-mcp 1.0.0 (namespace files): [{"role": "files_user", "allow": ["files_create_upload_url", "files_get_download_link", "files_get_file_info"]}]. Critical tools: files_create_upload_url, files_get_download_link.

Operator: Install the minimal bundle. We already have S3.

```
<!-- scenario: bundle-install -->
keystonectl --root /srv/keystone check --json
keystonectl --root /srv/keystone catalog list --json
keystonectl --root /srv/keystone bundle plan minimal --json
# code 2, missing: KEYSTONE_SETUP_RESOURCE_S3, KEYSTONE_SETUP_SERVICE_FILE_MCP_ENV_FILE_MCP_MAX_BYTES, KEYSTONE_SETUP_SERVICE_FILE_MCP_ENV_FILE_MCP_LINK_TTL_SECONDS
Agent: Where should file-mcp keep files: in S3 you already have, or in a new MinIO on this server?
Operator: We already have S3.
Agent: Put the access key and the secret key each in their own mode 600 file and send the paths.
Operator: The paths are ready.
Agent: What is the maximum upload size in bytes, and how long should a download link stay valid, in seconds?
Operator: 5368709120 and 900.
KEYSTONE_SETUP_RESOURCE_S3=external \
KEYSTONE_SETUP_S3_ENDPOINT=https://s3.example \
KEYSTONE_SETUP_S3_BUCKET=file-mcp \
KEYSTONE_SETUP_S3_ACCESS_KEY_FILE="$KEYSTONE_SETUP_S3_ACCESS_KEY_FILE" \
KEYSTONE_SETUP_S3_SECRET_KEY_FILE="$KEYSTONE_SETUP_S3_SECRET_KEY_FILE" \
KEYSTONE_SETUP_SERVICE_FILE_MCP_ENV_FILE_MCP_MAX_BYTES=5368709120 \
KEYSTONE_SETUP_SERVICE_FILE_MCP_ENV_FILE_MCP_LINK_TTL_SECONDS=900 \
keystonectl --root /srv/keystone bundle install minimal --json
# code 0
<!-- /scenario -->
```

Agent: The minimal bundle (file-mcp 1.0.0) is installed, S3 is the one you already have. `recommended_policy` came back as one file per package; file-mcp's `policy.example.yaml` has one role. Recommended rows from the bundle: [{"role": "files_user", "allow": ["files_create_upload_url", "files_get_download_link", "files_get_file_info"]}]. Critical tools: files_create_upload_url, files_get_download_link.

Operator: Install the document service. The database is ours.

```
<!-- scenario: docs-service-external-postgres -->
keystonectl --root /srv/keystone check --json
keystonectl --root /srv/keystone catalog list --json
keystonectl --root /srv/keystone service plan docs-service --json
# code 2, missing: KEYSTONE_SETUP_RESOURCE_POSTGRES, KEYSTONE_SETUP_POSTGRES_URL_FILE
Agent: MinIO is already installed. docs-service will get its own bucket there. I will not ask about S3.
Agent: Where should docs-service store data: in Postgres you already have, or a shared Postgres on this server? Each service gets its own database.
Operator: Our own Postgres.
Agent: Put the connection string in a new mode 600 file and send only the path.
Operator: The path is ready.
KEYSTONE_SETUP_RESOURCE_POSTGRES=external \
KEYSTONE_SETUP_POSTGRES_URL_FILE="$KEYSTONE_SETUP_POSTGRES_URL_FILE" \
keystonectl --root /srv/keystone service apply docs-service --json
# code 3, postgres_access_denied, retryable false
Agent: PostgreSQL refused the user or password. Update that same file, then tell me. I will not retry until you do.
Operator: The file is updated.
KEYSTONE_SETUP_RESOURCE_POSTGRES=external \
KEYSTONE_SETUP_POSTGRES_URL_FILE="$KEYSTONE_SETUP_POSTGRES_URL_FILE" \
keystonectl --root /srv/keystone service apply docs-service --json
# code 0
keystonectl --root /srv/keystone service status docs-service --json
<!-- /scenario -->
```

Agent: docs-service 1.0.0 is installed. Postgres is yours. S3 is the MinIO already on this server. The migrate container finished. Tools: docs_search_docs, docs_read_page.

Operator: The address did not match. I fixed DNS. Try again.

```
<!-- scenario: dns-retry -->
KEYSTONE_SETUP_SERVICE_FILE_MCP_DOMAIN=files.example.com \
KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS=certbot \
KEYSTONE_SETUP_TLS_EMAIL=ops@example.com \
keystonectl --root /srv/keystone service apply file-mcp --json
# code 3, dns_mismatch, retryable true
Agent: files.example.com does not point at this server yet. Update the A record, or wait a few minutes, then tell me to retry.
Operator: The record is updated.
KEYSTONE_SETUP_SERVICE_FILE_MCP_DOMAIN=files.example.com \
KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS=certbot \
KEYSTONE_SETUP_TLS_EMAIL=ops@example.com \
keystonectl --root /srv/keystone service apply file-mcp --json
<!-- /scenario -->
```

Agent: file-mcp is installed. Links open at https://files.example.com.

Operator: I want to run the file-mcp compose file myself.

```
<!-- scenario: eject -->
Agent: After eject, upgrade updates the registry, the snapshot, and secrets, but not the compose file or the image. You edit those yourself. adopt generates the fragment again and drops hand edits in it. Move hand edits into the override first if you want to keep them.
Operator: Yes.
keystonectl --root /srv/keystone service eject file-mcp --json
<!-- /scenario -->
```

Agent: file-mcp is ejected. You own packages/file-mcp.yml. keystonectl still keeps the registry entry, the snapshot, the secrets, and status.

Operator: Remove the file service.

```
<!-- scenario: remove -->
Agent: Removing file-mcp stops its container. The volume, the bucket, and the certificate stay.
Operator: Yes.
keystonectl --root /srv/keystone service remove file-mcp --json
# code 2, missing: KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE
KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE=yes \
keystonectl --root /srv/keystone service remove file-mcp --json
<!-- /scenario -->
```

Agent: file-mcp is removed. Kept: the volume, the bucket, and the certificate.
