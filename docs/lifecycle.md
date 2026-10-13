# Service lifecycle

`service apply` is the normal path. These commands change what keystonectl owns afterwards. Volumes, buckets, databases, certificates, and `packages/<name>.override.yml` are never deleted.

## remove

`service remove <name>` asks for confirmation (`y` in a terminal, or `KEYSTONE_SETUP_SERVICE_<NAME>_CONFIRM_REMOVE=yes`).

It refuses when another installed package `requires` this one (`required_by`), and when a resource provider still has consumers (`resource_in_use`).

What it removes: every container of the package (`docker compose rm -s -f`, before the fragment file is deleted), the generated fragment, the package env files, the registry row, the snapshot names, the managed bearer and identity lines in `.env`, the nginx site, and the lock entry. The package network `ks-pkg-<name>` is removed when Docker allows it. That network is an ordinary bridge.

What it leaves: volumes (`ks-pkg-<name>-*`), the consumer's bucket or database, the certificate, the override file, and an ejected fragment. The summary says the override is yours. Tool names that still match a policy `allow` pattern move from `active` to `reserved` (and into `.keystonectl/retired-tools.txt`) so keystone-mcp can still start. The warning `policy_rows_orphaned` lists those patterns. A later apply or remove drops a retired name once no pattern matches it.

A provider with no consumers can be removed. Its volume and `packages/<provider>.env` stay, so a later install reuses the same root credentials.

## upgrade

`service upgrade <name>` or `service upgrade <name>@<version>` moves to a newer compatible release. When nothing newer exists, the command exits 0 with `result.up_to_date: true`.

If `packages/<name>.yml` was edited by hand, upgrade stops with `fragment_modified` before any question and before Docker. Move the edits to the override, or eject, then upgrade.

The preview lists tools and containers added or removed, and policy rows that stop matching (`catalog diff` before and after). New tools that no role can call are `tools_unreachable`. Confirmation is `y`, or `KEYSTONE_SETUP_SERVICE_<NAME>_CONFIRM_UPGRADE` set to the target version exactly. An older confirmation does not apply.

On success, keystonectl keeps `.bak` copies (mode 600), records `previous` in the lock, and runs the normal apply. The apply step uses the installed entry's `source` (`source/name@version`) so the same package name in another catalog does not ask which package to use. `registry_name`, `namespace`, and the override stay. A failed health check tells you to run `service rollback <name>` and does not roll back by itself.

An ejected package is not rewritten. The lock, registry, and secrets move forward, and `result.manual_steps` lists the new images for the operator's file.

## rollback

`service rollback <name>` restores the fragment and env files from `.bak` and the lock fields from `previous`. It recreates the containers. `previous` is cleared: one step only, and no confirmation. Without `previous` and the `.bak` file, the error is `no_previous_version`.

## eject and adopt

These are the three ways an operator keeps control of a compose fragment:

1. Leave it generated. keystonectl rewrites it on apply. Put local changes in `packages/<name>.override.yml`.
2. `service eject <name>` marks the fragment as yours. keystonectl stops rewriting that file. Registry, secrets, the public site, and status still follow the package. Apply reports the fragment step as skipped. A remote service cannot be ejected.
3. `service adopt <name>` generates the fragment again from the installed version, including the current loopback port and `extra_hosts`. The previous file is saved as `packages/<name>.yml.ejected.bak` (mode 600). Confirmation is `KEYSTONE_SETUP_SERVICE_<NAME>_CONFIRM_ADOPT=yes`. In `--json` the preview lists changed service keys and not the values, because the operator file may contain secrets. On a terminal the preview is a unified diff.
