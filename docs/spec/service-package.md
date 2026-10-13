# Service package manifest (v1)

Full field reference and JSON Schema: see card S01 (`service-package-v1.json`).

## Resource needs

### S3 (`needs[].kind: s3`, `version: 1`)

`bind_env` must name six variables: `endpoint`, `region`, `bucket`, `access_key`, `secret_key`,
`path_style`. Semantics, provider promises, and verification:

**[resources/s3-v1.md](resources/s3-v1.md)**
