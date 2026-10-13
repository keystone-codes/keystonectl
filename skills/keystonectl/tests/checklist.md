# keystonectl skill walkthrough

Text walkthrough of the five scenarios in `SKILL.md`. Automated checks are `bats skills/keystonectl/tests` and `tests/test_skill_contract.py`. The manual protocol at the end is filled in on the owner's stand.

## file-mcp-minio

| Step | What the agent does | What the operator sees | Success |
|---|---|---|---|
| 1 | `check`, `catalog list`, `service plan file-mcp` | A short retelling, then one question about storage | Plan returns code 2 with `KEYSTONE_SETUP_RESOURCE_S3` |
| 2 | Asks where files should live | Chooses a new MinIO | Answer `catalog`, not typed as a flag |
| 3 | Asks about the certificate for files.example.com. The domain from the first sentence is not asked again | Chooses Let's Encrypt and gives an email | One `apply` with `RESOURCE_S3`, `DOMAIN`, `TLS`, and `TLS_EMAIL` |
| 4 | Retells version, namespace, tools, MinIO, domain, and warnings | Hears that nobody can call the tools yet | `apply` code 0, `tools_unreachable` included |
| 5 | Passes `{role, allow}` and critical tools to the policy skill | The policy skill asks before it changes anything | List matches `result.recommended_policy`, nothing added |
| 6 | `service status file-mcp` | Container, link, certificate expiry | `problems` empty |

## docs-service-external-postgres

| Step | What the agent does | What the operator sees | Success |
|---|---|---|---|
| 1 | `check`, `catalog list`, `service plan docs-service` | MinIO is already installed, so S3 is not asked | Plan `missing` has Postgres only |
| 2 | Asks external versus catalog Postgres | "Our own Postgres" | `KEYSTONE_SETUP_RESOURCE_POSTGRES=external` |
| 3 | Asks for a mode 600 file path. Does not ask for the connection string | Operator creates the file and sends the path | Variable ends in `_FILE`, mode 600, contents never repeated |
| 4 | `apply` | Hears that Postgres refused the user or password, and that the agent will not retry yet | Code 3, `postgres_access_denied`, `retryable` false |
| 5 | Waits until the operator says the same file was updated | Operator updates the file | Second `apply` only after the file changed |
| 6 | Retells the install and `service status` | Hears that the migrate container finished | Code 0, migrate completed |

## dns-retry

| Step | What the agent does | What the operator sees | Success |
|---|---|---|---|
| 1 | `apply` | The hostname does not point at this server yet | Code 3, `dns_mismatch`, `retryable` true |
| 2 | Explains and waits | Operator fixes the A record and says so | No retry before that |
| 3 | Repeats `apply` with the same answers | Install succeeded | Code 0 |

## eject

| Step | What the agent does | What the operator sees | Success |
|---|---|---|---|
| 1 | Explains upgrade, compose, the image, and adopt before any command | Consequences, including that hand edits are dropped on adopt | No `service eject` yet |
| 2 | After "yes", `service eject file-mcp` | Who owns the fragment, and what keystonectl still keeps | Code 0. No confirm variable |

## remove

| Step | What the agent does | What the operator sees | Success |
|---|---|---|---|
| 1 | Says the volume, bucket, and certificate stay | Asks for a yes before remove | No remove before that |
| 2 | `service remove` | A confirm question | Code 2, `CONFIRM_REMOVE` |
| 3 | Repeats remove with `CONFIRM_REMOVE=yes` | What was kept | Code 0, `result.kept` retold |

## Manual protocol

Filled on the owner's stand. Not run from the repository checkout.

- Date:
- keystonectl version:
- keystone-mcp version:
- Host: `<stand-host>` (keystone-mcp directory is `mcp/` of the keystone-deploy checkout)
- Skill connected in Claude Code on that host. The agent is connected to keystone-mcp as an administrator.

Operator message: "Install the file service on <stand-host>."

- [ ] Agent runs `--version`, `check`, `service list`, `catalog list`, and `service plan` itself
- [ ] One question at a time about storage and the certificate
- [ ] External S3: operator is asked to put keys in a mode 600 file and send the path
- [ ] `apply` reaches code 0
- [ ] Agent retells namespace, tools, and domain
- [ ] Agent passes the `{role, allow}` list to `keystone-policy-admin`, which applies it after the administrator confirms
- [ ] `service status` has no `problems`
- [ ] Eject, then adopt back
- [ ] The operator types no commands except creating secret files

The end-to-end release check copies this protocol into `docs/release-checklist.md`.
