# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Warning `resource_answer_ignored` when a resource answer disagrees with the provider already bound.
- Warning `builtin_tools_unavailable` when apply cannot read keystone-mcp's live tool list.
- `catalog set-ref <name> <ref>` moves a source to another tag or commit.
- `default_catalog_behind` warns when the default catalog is pinned behind this keystonectl.
- Guide for service authors: how to release a new version of a package.

### Changed

- Removing a provider uses one consumer check; warnings `provider_unused` and `provider_data_kept` name the volumes that stay.
- `CONTRIBUTING.md` sets the changelog line rules; a test checks `[Unreleased]`.
- e2e: Scenario 12 adds the partner catalog over the network when `E2E_PARTNER_CATALOG_URL` is set; the vm profile does so by default.
- `check` reports `override_unchecked`, `compose_version_unknown` and `check_skipped_no_docker` instead of reusing check ids; every warning id is in the registry.
- `output.schema.json` types `result` for apply, list, status, remove, bundle and catalog list; `service apply` reports `result.resources`.

### Fixed

- Bundle results read the policy of the installed package version; cached versions are compared as versions.
- ``.keystonectl/`` gets its ``.gitignore`` whichever command creates it first.
- A failed apply re-enables the nginx sites it disabled in that run.
- docs: `service list --json` returns lock entries, not names.
- Skill fixtures match the real `result` of catalog list, status and apply.

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
