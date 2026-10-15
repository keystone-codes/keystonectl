# keystonectl commands

Global flags apply to every command. `init` and `lint` do not search for a deployment directory.

| Flag | Meaning |
|---|---|
| `--root DIR` | Deployment directory. Otherwise `KEYSTONECTL_ROOT`, otherwise the nearest parent that contains `services.lock.yaml`. |
| `--json` | One JSON document on stdout. Implies `--non-interactive`. |
| `--non-interactive` | Do not prompt. A missing answer exits 2. |
| `--set KEY=VALUE` | Answer a question. Refused when `KEY` is a secret (`*_FILE`). |
| `--version` | Print `keystonectl 0.1.1`. |

| Command | Purpose |
|---|---|
| `init <root>` | Create the files keystonectl owns. Does not write the operator's compose file, `.env`, or policy. |
| `init --adopt <root>` | Adopt an existing keystone-mcp directory. Prints the two-file compose command. |
| `init --adopt <root> --keystone-service NAME` | Choose the compose service when more than one looks like keystone-mcp. |
| `lint <manifest-or-directory>` | Check a package manifest, or `catalog.yaml`, without a deployment directory. |
| `lint --keystone-mcp-version X` | Warn when `X` is newer than the built-in tool list copied into keystonectl. |
| `package lint <manifest-or-directory>` | Same as `lint`. |
| `check` | Check the deployment directory. No network and no Docker. |
| `catalog add <url>` | Add a catalog source to `sources.yaml`. Optional `--name`, `--ref`, `--priority`. |
| `catalog list` | List sources, packages, and bundles. |
| `service list` | List installed services and resources, including `keystone_mcp_version`. |
| `service plan <ref>` | Show what would change. Writes only the cache. |
| `service apply <ref>` | Apply the plan. Without a deploy step this exits 1, `not_implemented`. |
| `service install <ref>` | Same as `service apply`. |
| `explain <id>` | Print what an error or warning id means. |
| `service explain <id>` | Same as `explain`. |

`<ref>` is `name`, `name@X.Y.Z`, `source/name`, or `source/name@X.Y.Z`.

`service plan` and `service apply` ask where each `needs` entry should live. Answer `KEYSTONE_SETUP_RESOURCE_S3` or `KEYSTONE_SETUP_RESOURCE_POSTGRES` with `external` or `catalog`. Secret values are read from mode 600 files (`*_FILE`); `--set` refuses those names. See `docs/resources.md`.

Later commands (`service status`, `remove`, `upgrade`, `rollback`, `eject`, `adopt`, `add-remote`, `bundle install`) are added by their own modules through `register(subparsers)`.
