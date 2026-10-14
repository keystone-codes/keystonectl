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

### Changed

- catalog spec explains tag versus commit pinning.
- ADR 0005 and generation.md describe `check` as reporting, `apply` and `upgrade` as stopping.
- Error hints no longer mention a `--local` flag; `package_ambiguous` names the `source/name` form with an example.
- README links are absolute for PyPI.
- ADR 0001 lists the core internals keystonectl depends on.

### Fixed

- test: consumer secret comparison after a failed probe
