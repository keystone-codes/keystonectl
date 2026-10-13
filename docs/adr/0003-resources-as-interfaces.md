# 0003. Resources are versioned interfaces: one provider per kind, an isolated slice per consumer

## Status

Accepted (keystonectl 0.1, 2026-10-12).

## Context

Services need object storage and databases. Operators may use AWS, Yandex Object Storage,
self-hosted MinIO, managed Postgres, or nothing yet. A package that embeds its own MinIO or
Postgres duplicates infrastructure, superuser passwords, and volumes without backup discipline
and ties data to the package lifecycle.

## Decision

We describe each resource as a versioned interface. `s3/v1`: six parameters (`endpoint`,
`region`, `bucket`, `access_key`, `secret_key`, `path_style`) and three promises — object
operations, presigned `PUT`/`GET`, prefix lifecycle rules. `postgres/v1`: `url` and/or
`host`, `port`, `database`, `user`, `password`, `sslmode`, with promises of a dedicated
database and role, extensions on request, server ≥ 14. Packages declare `needs`; providers
declare `provides`. On install there is one provider per kind: external operator resource or
a catalog provider (`minio`, `postgres`). Each consumer gets its own bucket or database and
user; root keys and superuser passwords never reach services. Fitness is checked with
`s3_probe` and `postgres_probe`. Redis is not a resource interface — keystone-mcp keeps its own.

## Consequences

Operators can swap providers without changing consumer packages (including leaving MinIO whose
update risk is documented in `s3-v1.md`). One leaked consumer key does not expose other
consumers. External resources require the operator to create bucket, database, and keys.
A new promise needs a new interface version (`s3/v2`). `service remove` leaves data in place.

## Alternatives considered

- **Bundled MinIO or Postgres per package** — Duplication and weak ops story.
- **Shared bucket with prefixes or shared DB with schemas** — One key reads everything.
- **Handing out root credentials** — Unacceptable blast radius.
- **Several providers of one kind on one install** — Unneeded complexity today.
- **Redis as a resource** — Not required for catalog services in v1.

## Links

- [S3 interface](../spec/resources/s3-v1.md), [Postgres interface](../spec/resources/postgres-v1.md),
  [Service package](../spec/service-package.md) (`needs`, `provides`, `provision`)
