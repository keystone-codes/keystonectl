# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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

### Fixed

- test: consumer secret comparison after a failed probe
- State-changing commands hold the lock for their whole run; a second process gets `busy`.
- Catalog and package fetches no longer fail with EXDEV when /tmp is a different file system.
