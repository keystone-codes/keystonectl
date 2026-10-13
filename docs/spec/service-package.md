# Service package manifest (v1)

Full field reference and JSON Schema: see card S01 (`service-package-v1.json`).

## Resource needs

### Postgres (`needs[].kind: postgres`, `version: 1`)

`bind_env` may name a single `url` variable, all six of `host`, `port`, `database`, `user`,
`password`, and `sslmode`, or both forms at once. Semantics, provider promises, and verification:

**[resources/postgres-v1.md](resources/postgres-v1.md)**

Optional `needs[].extensions` lists extension names; the provider creates them in the consumer
database at provision time (see the Postgres interface doc).
