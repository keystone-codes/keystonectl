# End-to-end check

`scripts/e2e.sh` installs file-mcp, MinIO, Postgres, and a small docs-service behind a real keystone-mcp and checks the gateway with `mcp_check.py`. It does not edit keystone-mcp, keystone-deploy, or the package repositories.

## Machine

Linux with Docker, `uv`, `git`, `openssl`, and `curl`. The CI profile also needs `sudo` to install nginx, write `/etc/hosts`, and trust the generated certificate. The VM profile requires an explicit `--domain` and, with `--tls certbot`, a real `E2E_TLS_EMAIL` (Let's Encrypt rejects `@example.com`, `@example.org`, and `@example.net`). It needs port 80 for certbot and `sudo` for the same nginx vhost the CI profile uses for manual TLS (a self-signed certificate on the sample vhost; keystonectl only checks that TLS answers, not that the chain is trusted). The VM profile installs the checkout's `[project].version` from PyPI unless you pass `--keystonectl <wheel or checkout>`; it checks that version with `curl https://pypi.org/pypi/keystonectl/<version>/json`. Without `curl` the script only warns and continues; the install itself then fails with a clear error if the version is missing. Do not run the VM profile unless that machine is the one you are recording in `docs/release-checklist.md`.

Port 8080 must be free. If it is not, pass `--publish-port` with a free loopback port. The script rewrites only its own copy of the compose file and records that as a note in the report.

Containers and the temp directory use the `ks-cat-e2e-` prefix. Networks required by the keystone-deploy compose file keep their own names (`keystone_mcp_net`, `keystone-mcp-upstreams`).

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--layout` | `production-example` | `production-example` (open example) or `keystone-deploy` |
| `--profile` | `ci` | `ci` installs from this checkout and uses a self-signed certificate. `vm` checks PyPI for the requested version and installs from there unless `--keystonectl <wheel or checkout>` is given; it may use certbot |
| `--storage` | `both` | `minio`, `external`, or `both` |
| `--postgres` | `both` | `provider`, `external`, or `both` |
| `--tls` | `custom` | `custom` or `certbot` (`certbot` only with `--profile vm`) |
| `--domain` | `files.e2e.test` in CI; required on `vm` | public name |
| `--publish-port` | `8080` | host port for keystone-mcp |
| `--report` | `e2e-report.jsonl` | one JSON object per line |
| `--keep` | off | leave the deployment in place after a passing run |

`E2E_PARTNER_CATALOG_REF` defaults to `v1.0.0`. `E2E_PARTNER_CATALOG_URL` defaults to empty on the `ci` profile and to `https://github.com/keystone-codes/e2e-partner-catalog.git` on the `vm` profile. An empty URL builds a local `file://` catalog. A set URL is added with `--ref` and without `--name` (the source name is the catalog `name`, `e2e-partner`). An explicit value, including empty, overrides the profile default.

CI example:

```bash
scripts/e2e.sh --layout production-example --profile ci --storage both --postgres both --tls custom
```

## Layouts

- **production-example** copies `examples/production` from keystone-mcp `5d7084ddc2fd65fc5377e66710781d9e9cd613b3` and adopts it. The one-time patch is `scripts/e2e/fixtures/production-example.patch`. That file has no diff. An empty diff means the open example needs no one-time edit: `init --adopt` already adds the second compose file and the upstream network.
- **keystone-deploy** checks out `https://github.com/keystone-codes/deploy.git` at `5b6c8f5db02a09248c1f12e48e03500c1becb8a1` (branch `release/catalog`, after D01 and D02). CI starts only the `keystone-mcp` profile plus a Redis container named `ks-cat-e2e-redis`. The VM profile calls `./setup.sh --non-interactive` and still does not start the product backend. That profile skips `agent_path_missing` because setup already installed the minimal bundle.

Image digests are pinned at the top of `scripts/e2e.sh`. file-mcp and hello-upstream are the GHCR digests for their published 1.0.1 (`FILE_MCP_IMAGE`, `HELLO_IMAGE`). MinIO, mc, Postgres, and Redis are the foreign digests from the package manifests (Redis from `docker buildx imagetools inspect`; MinIO and mc are from minio-resource 1.0.1). stdio-box is `ghcr.io/keystone-codes/stdio-box:0.3.0@sha256:fe00911601f626a333897c42efa35f513b8984ca6a73f0dae7809d6f2c7e2ac9` from commit `855589babe7e9c7212b60654855b7ccd4086f53a` (tag `atlassian/v1.1.0`, read from `IMAGE` on 2026-10-27; the same commit and digest the catalog's `atlassian` and `notion` packages use at `v1.3.0`). The catalog itself is pinned at `CATALOG_REF`, the commit of tag `v1.3.0`.

## Scenarios

1. `bundle install` with no answers exits 2 and changes nothing. The keystone-deploy VM profile skips this scenario because `setup.sh` already installed the minimal bundle.
2. Install file-mcp and MinIO, upload and download 1 MiB. MinIO is pinned at `1.0.0` (the default catalog's next version, `1.0.1`, is used by the unnumbered `minio_provider_upgrade` step right after this one) so `bundle install` reuses the already-bound provider instead of picking the newest version.
3. The policy file hash is unchanged; recommended roles are in the install JSON.
4. A second plan asks nothing and changes nothing.
5. Install docs-service on the catalog Postgres.
6. `packages/file-mcp.override.yml` is kept across an upgrade from 1.0.0 to 1.0.1 (the local catalog `e2e` vendors both published manifests so the test controls both versions).
7. A direct edit is refused until eject; eject leaves the marker.
8. Adopt regenerates the fragment.
9. Remove keeps the bucket and the override; install again works. file-mcp declares no volumes, so the kept list has no volume name.
10. Point file-mcp at an external S3 (`ks-cat-e2e-external-s3`, alias `e2e-external-s3`). Then remove file-mcp (the external binding's only consumer, so it is dropped) and go back to the catalog provider: `service apply minio`, then file-mcp on it again.
11. Point docs-service at an external Postgres (`ks-cat-e2e-external-pg`).
12. A second catalog source; an ambiguous name asks for a choice. When `E2E_PARTNER_CATALOG_URL` is set, the script adds that repository at `E2E_PARTNER_CATALOG_REF` and expects neither `local_source` nor `catalog_ref_chosen`. When the URL is empty, it builds the local `file://` catalog. The report notes which path ran: the URL and ref, or `file://`.
13. On the open example, manual TLS: the first apply is retryable `public_unreachable`. Both profiles then install the sample nginx vhost with a self-signed certificate; apply reaches ready. On the VM profile this runs after the certbot scenario, so a certbot-issued certificate cannot be mistaken for the manual one.
14. `check` allows `tls_untrusted`, `s3_plain_http`, and `postgres_plain_connection`. Any other warning fails.
15. Forbidden leftovers on both layouts. Shared checks: no `.*.tmp` files outside `.keystonectl/`, an empty `.keystonectl/tmp/`, no unexplained files under `packages/` (lock names, operator `*.override.yml`, and paths kept by `service remove` are allowed), and mode `600` on `services.lock.yaml` and `packages/*.env`. On **keystone-deploy**, the script also scans `setup.sh`, `start.sh`, docs, and `mcp/**` for legacy external-API strings, refuses a `build:` on `keystone-mcp`, and on the vm profile checks `layout_version`. On **production-example**, it compares operator-file digests to a snapshot taken at the end of prepare (compose, optional `upstreams.yaml`, policy, and `.env` outside the managed block), refuses a `build:` on the layout keystone service, and checks `layout.flavor` and `layout_version`. The report notes which branch ran.
16. `atlassian_profiles`: install the `atlassian` package from the local catalog subdirectory `packages/atlassian`, then check per-person profiles, role order, a service-account profile, and a reinstall that keeps those profiles. The ci profile submits the credential form through nginx. The vm profile imports the same secrets with `credsctl`. Number 17 is reserved for `second_instance` in 0.3.

Right after scenario 2, an unnumbered scenario (`minio_provider_upgrade`) runs `service upgrade minio` from `1.0.0` to `1.0.1` (the default catalog lists both). It checks that the lock version and the pinned `MINIO_IMAGE` move to `1.0.1`, that `minio-server` is actually recreated (`StartedAt` changes), and that the objects already in the `file-mcp` bucket are still there afterward.

On the VM profile with `--tls certbot`, an unnumbered scenario runs between 12 and 13: the certificate for `--domain` was issued by Let's Encrypt, and re-applying does not change its serial number (certbot did not re-issue).

### Scenario matrix

The table lists each numbered scenario plus the certbot step. Cells assume default `--storage both` and `--postgres both`. Use `--tls custom` on the `ci` profile and `--tls certbot` on the `vm` profile (as in `docs/release-checklist.md`).

| Scenario | production-example ci | production-example vm | keystone-deploy ci | keystone-deploy vm |
|---|---|---|---|---|
| 1 `agent_path_missing` | runs | runs | runs | skipped (setup.sh already installed bundle minimal) |
| 2 `install_file_mcp_minio` | runs | runs | runs | runs |
| unnumbered `minio_provider_upgrade` | runs | runs | runs | runs |
| 3 `policy_untouched` | runs | runs | runs | runs |
| 4 `rerun_no_questions` | runs | runs | runs | runs |
| 5 `docs_service_postgres_provider` | runs | runs | runs | runs |
| 6 `override_survives_upgrade` | runs | runs | runs | runs |
| 7 `direct_edit_is_caught` | runs | runs | runs | runs |
| 8 `eject_and_adopt` | runs | runs | runs | runs |
| 9 `remove_and_reinstall` | runs | runs | runs | runs |
| 10 `external_s3` | runs | runs | runs | runs |
| 11 `external_postgres` | runs | runs | runs | runs |
| 12 `partner_catalog` | runs | runs | runs | runs |
| certbot `tls_certbot` | skipped (certbot runs only on the vm profile) | runs | skipped (certbot runs only on the vm profile) | runs |
| 13 `tls_manual` | runs | runs | skipped (only the production example) | skipped (only the production example) |
| 14 `check_clean` | runs | runs | runs | runs |
| 15 `legacy_gone` | runs | runs | runs | runs |
| 16 `atlassian_profiles` | runs | runs | runs | runs |

## Report

Each line is `{"scenario","status","code","message"}`. `status` is `pass`, `fail`, `skip`, or `deviation`. `deviation` is a note, not a failure: the empty layout patch, the catalog commit pinned in `sources.yaml`, the local vendor of the published file-mcp 1.0.0/1.0.1 manifests, and a non-default publish port.

In CI (`.github/workflows/e2e.yml`), the report and `e2e-diagnostics/` are uploaded as a build artifact on every run, not only on failure.

`scripts/e2e/jsonq.py` reads one keystonectl JSON document from stdin and prints one value per line. It has no expression language. `scripts/e2e/mcp_check.py` calls the gateway from inside the keystone-mcp image. The API key stays in `E2E_API_KEY` and in a mode 600 env file.
