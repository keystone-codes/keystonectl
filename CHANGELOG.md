# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.0] - 2026-10-21

### Added

- `package wrap-stdio --stock-image` keeps the stdio-box image and mounts the package directory with `package_dir` and `BOX_PROVIDER_DIR`.
- `package wrap-stdio --publisher`, `--homepage` and `--version` set those manifest fields, also when the manifest is updated in place.
- `package wrap-stdio --read-only` and `--destructive` name read-only and destructive tools when the server's annotations are missing or wrong.
- `package wrap-stdio` reports `result.box_tools_source`: the `creds_*` tools come from `stdio-box box-tools` or a built-in list.
- `package wrap-stdio --resolve-lock` builds the lock file with npm or uv in the stdio-box image and checks it with `stdio-box install` before writing it.
- `package wrap-stdio` reports `result.lock`: whether the lock file was rebuilt, and its name.
- Errors `lock_tool_missing`, `lock_resolve_failed` and `lock_install_failed` for `--resolve-lock`.
- docs: Wrapping a remote HTTP server through `mcp-remote` in a box, with its limits.
- Optional manifest field `containers.<role>.package_dir` mounts a directory of the package into the container, read-only, from a copy keystonectl makes at apply.
- Warning `package_dir_missing` when a copy named in the lock is missing or changed; `package_dir_kept` when removing an ejected service keeps its copies.
- docs: ADR 0010 records package files mounted read-only; it refines ADR 0004 and ADR 0007.
- Skill: installing a box and setting up credential profiles for people, services and roles through a one-time link.
- e2e: Scenario 16 installs the atlassian package from a catalog subdirectory and checks profiles, role order, and a service account.
- docs: ADR 0007 records stdio MCP servers as HTTP boxes; profiles live in the box, and containers keep the image user and default capabilities.
- `service rotate-secret <name> <kind>` replaces a consumer's bucket key or database password from a catalog provider.
- `keystonectl package wrap-stdio` writes a box package (manifest with the server's tools, tool snapshot, policy example) from a stdio provider directory.
- Warning `resource_answer_ignored` when a resource answer disagrees with the provider already bound.
- Warning `builtin_tools_unavailable` when apply cannot read keystone-mcp's live tool list.
- `catalog set-ref <name> <ref>` moves a source to another tag or commit.
- `default_catalog_behind` warns when the default catalog is pinned behind this keystonectl.
- Guide for service authors: how to release a new version of a package.
- Warning `keystone_wiring_stale` when the running gateway is missing a compose file or the upstreams network from the layout.
- Warning `stale_external_resource` when a lock has an `external` resource binding with no consumers left.

### Changed

- The default catalog `keystone-codes` is pinned at tag `v1.2.0`: the credential boxes and 1.0.1 of `minio`, `hello-upstream`, `file-mcp`.
- A recorded `sources.yaml` keeps its `keystone-codes` ref; `keystonectl catalog set-ref keystone-codes v1.2.0` moves it.
- Skill: requires sign-in through the provider before the first service, and points to `keystone-gateway-bootstrap` otherwise.
- Skill: `service add-remote` examples pass `--tools`, name the two tools warnings, and ask a yes before `--no-auth`.
- `package wrap-stdio --no-discovery` writes an empty `tools.snapshot.json` when there is none, so the box starts.
- `package wrap-stdio` lists the `creds_*` tools the image prints, including `creds_snapshot_tools`, which is not critical.
- `package wrap-stdio` pins the default `--image` to the published stdio-box 0.1.0 digest.
- Provider contract: the secret file is the desired secret.
- docs: Backups of catalog providers, `tools_unreachable` as the state before policy, confirmations as a contract, manual registry edits during apply.
- Removing a provider uses one consumer check; warnings `provider_unused` and `provider_data_kept` name the volumes that stay.
- `CONTRIBUTING.md` sets the changelog line rules; a test checks `[Unreleased]`.
- e2e: Scenario 12 adds the partner catalog over the network when `E2E_PARTNER_CATALOG_URL` is set; the vm profile does so by default.
- e2e: The leftovers check also runs on the production-example layout.
- `check` reports `override_unchecked`, `compose_version_unknown` and `check_skipped_no_docker` instead of reusing check ids; every warning id is in the registry.
- `output.schema.json` types `result` for apply, list, status, remove, bundle and catalog list; `service apply` reports `result.resources`.
- `service remove` keeps generated secrets of a package with volumes in `.keystonectl/secrets/<name>.generated.env`; reinstalling reuses them.
- e2e: The vm profile requires `--domain` and a real `E2E_TLS_EMAIL` for certbot, and installs the keystonectl version of the checkout from PyPI.
- e2e: Pins catalog `v1.2.0`, packages 1.0.1, and the deploy commit with the public keystonectl interface; scenario 6 upgrades file-mcp 1.0.0 to 1.0.1.

### Fixed

- `package wrap-stdio --stock-image` mounts the package at `/opt/box/packages/<provider>`, so the box starts; updates move the old `/opt/box/provider`.
- `package wrap-stdio --resolve-lock` runs `uv pip compile` with `--python-version 3.12`, the Python of the stdio-box image.
- Tests marked `integration` (Docker and network) no longer run by default; run them with `uv run pytest -m integration`.
- docs: Upgrade confirmation is the target version, not `y`.
- Bundle results read the policy of the installed package version; cached versions are compared as versions.
- ``.keystonectl/`` gets its ``.gitignore`` whichever command creates it first.
- A failed apply re-enables the nginx sites it disabled in that run.
- docs: `service list --json` returns lock entries, not names.
- Skill fixtures match the real `result` of catalog list, status and apply.
- `service upgrade` records the new version, ref, commit and manifest digest in services.lock.yaml.
- `service add-remote --no-auth` summary no longer asks to check a bearer that was never issued.
- `package wrap-stdio` mounts the provider directory under its own `name` for discovery, so stdio-box accepts any provider name, not just `provider`.
- An image tag that is not a PEP 440 version (for example a build id suffix) is treated as unknown, not as that literal version string.
- `service upgrade`/`service apply` on a resource provider redeploy and update the lock when the version or an added override actually changed.
- `service upgrade` fetches a catalog version it has not cached yet, the same way `service plan <name>@<version>` already does.
- e2e: An unnumbered scenario upgrades the catalog `minio` provider from 1.0.0 to 1.0.1 and checks the image, the lock, and the bucket data.
- Binding a second service to an already-installed catalog provider no longer records the catalog's newest version in the lock without redeploying it.
- `service rollback`/`service remove` delete the `.bak` copies an upgrade left once nothing can roll back to them anymore.
- Removing the last consumer of an `external` resource drops the binding, so a later package can choose `catalog` instead of `resource_already_bound`.
- `check` reports `keystone_wiring_stale` when the gateway container is missing a compose file or the upstreams network, and names the fix.
- `TLS=custom`: `service status` reads the certificate and `renewal_manual` from the operator's copy, not a leftover `/etc/letsencrypt/live/<domain>`.
- A PostgreSQL host name that fails to resolve is reported as `postgres_unreachable` (retryable), not `postgres_access_denied`.
- `service plan`/`service apply` drop a stale `external` resource binding (no consumers, from an older lock) before deciding on a provider.
- `check` warns `stale_external_resource` for the same leftover binding instead, since `check` never writes.
- The `keystone_wiring_stale` hint adds `--no-deps`, so following it recreates only the gateway, not redis/postgres next to it.

## [0.1.1] - 2026-10-15

### Changed

- e2e: the vm profile checks PyPI for the requested version instead of refusing outright.

### Fixed

- A failed first `service apply` stops and removes the containers it started and drops the package from `services.lock.yaml`; the fragment, the include, and the lock no longer disagree with Docker.

## [0.1.0] - 2026-10-14

### Added

- Chat skill in `skills/keystonectl/` for installing, upgrading, and removing services.
- Guide for service authors: `docs/packaging-a-service.md`.
- CLI skeleton: `init`, `lint`, `check`, `plan`/`apply`, lock file, catalog sources, JSON output.
- Resource binding for `s3/v1` and `postgres/v1` with probes.
- Published on PyPI as `keystonectl`; tags `vX.Y.Z` publish through trusted publishing.
- Public paths: loopback port, nginx vhost, certificate lifecycle, and `service remove`, `upgrade`, `rollback`, `eject`, and `adopt`.
- Release gate verifies that the default catalog ref exists.
- `SECURITY.md`.
- Unexpected errors are reported as `internal_error` in JSON.

### Changed

- catalog spec explains tag versus commit pinning.
- ADR 0005 and generation.md describe `check` as reporting, `apply` and `upgrade` as stopping.
- Error hints no longer mention a `--local` flag; `package_ambiguous` names the `source/name` form with an example.
- README links are absolute for PyPI.
- ADR 0001 lists the core internals keystonectl depends on.
- `docs/packaging-a-service.md`: the file-mcp example no longer calls the published image digest a placeholder; it now points authors at copying their own registry's digest.
- Corrected five docs that had drifted from the code: resource binding question reuse (`docs/resources.md`), Postgres provider promises (`docs/spec/resources/postgres-v1.md`), the keystone-deploy `mcp/` layout example (`docs/spec/deployment-layout.md`), the public DNS check (`docs/public-paths.md`), and `keystone-resources` cross-package reachability (`docs/spec/service-package.md`, `docs/packaging-a-service.md`).
- ADR 0004 no longer mentions a `--local` flag.
- `tag_not_found` names the source, the ref, and the URL.
- e2e: vm profile requires --keystonectl until 0.1.0 is on PyPI; certbot re-issue checked by serial.
- Skill handles `recommended_policy` from `bundle install`.
- Taking over an nginx site asks every time and restores every disabled site on remove.
- e2e pins keystone-deploy at the release/catalog commit that drops the managed-mode text from `.env.example`.

### Fixed

- test: consumer secret comparison after a failed probe
- State-changing commands hold the lock for their whole run; a second process gets `busy`.
- Catalog and package fetches no longer fail with EXDEV when /tmp is a different file system.
- Fetching into a deployment root that has never run `init` now creates `.keystonectl/` itself with mode 700 and the promised `.gitignore`, instead of leaving it with a default, wider mode.
