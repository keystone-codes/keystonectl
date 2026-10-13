# End-to-end check

`scripts/e2e.sh` installs file-mcp, MinIO, Postgres, and a small docs-service behind a real keystone-mcp and checks the gateway with `mcp_check.py`. It does not edit keystone-mcp, keystone-deploy, or the package repositories.

## Machine

Linux with Docker, `uv`, `git`, `openssl`, and `curl`. The CI profile also needs `sudo` to install nginx, write `/etc/hosts`, and trust the generated certificate. The VM profile needs a public name and port 80 for certbot. Manual TLS on that profile starts Caddy from the production example Caddyfile and does not use sudo; if Caddy does not start, that scenario fails. Do not run the VM profile unless that machine is the one you are recording in `docs/release-checklist.md`.

Port 8080 must be free. If it is not, pass `--publish-port` with a free loopback port. The script rewrites only its own copy of the compose file and records that as a note in the report.

Containers and the temp directory use the `ks-cat-e2e-` prefix. Networks required by the keystone-deploy compose file keep their own names (`keystone_mcp_net`, `keystone-mcp-upstreams`).

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--layout` | `production-example` | `production-example` (open example) or `keystone-deploy` |
| `--profile` | `ci` | `ci` installs from this checkout and uses a self-signed certificate. `vm` installs from PyPI unless `--keystonectl` is set, and may use certbot |
| `--storage` | `both` | `minio`, `external`, or `both` |
| `--postgres` | `both` | `provider`, `external`, or `both` |
| `--tls` | `custom` | `custom` or `certbot` (`certbot` only with `--profile vm`) |
| `--domain` | `files.e2e.test` in CI | public name |
| `--publish-port` | `8080` | host port for keystone-mcp |
| `--report` | `e2e-report.jsonl` | one JSON object per line |
| `--keep` | off | leave the deployment in place after a passing run |

CI example:

```bash
scripts/e2e.sh --layout production-example --profile ci --storage both --postgres both --tls custom
```

## Layouts

- **production-example** copies `examples/production` from keystone-mcp `6b04a198101dac22815b17d5bcf55b0e1c6463d8` and adopts it. The one-time patch is `scripts/e2e/fixtures/production-example.patch`. That file has no diff. An empty diff means the open example needs no one-time edit: `init --adopt` already adds the second compose file and the upstream network.
- **keystone-deploy** checks out `https://github.com/keystone-codes/deploy.git` at `dd4538167af85bf5842d88e43bca53748063879f`. CI starts only the `keystone-mcp` profile plus a Redis container named `ks-cat-e2e-redis`. The VM profile calls `./setup.sh --non-interactive` and still does not start the product backend. That profile skips `agent_path_missing` because setup already installed the minimal bundle.

Image digests are pinned at the top of `scripts/e2e.sh`. file-mcp and hello-upstream are GHCR digests. MinIO, mc, Postgres, and Redis are the foreign digests from the package manifests (Redis from `docker buildx imagetools inspect`).

## Scenarios

1. `bundle install` with no answers exits 2 and changes nothing. The keystone-deploy VM profile skips this scenario because `setup.sh` already installed the minimal bundle.
2. Install file-mcp and MinIO, upload and download 1 MiB.
3. The policy file hash is unchanged; recommended roles are in the install JSON.
4. A second plan asks nothing and changes nothing.
5. Install docs-service on the catalog Postgres.
6. `packages/file-mcp.override.yml` is kept across upgrade.
7. A direct edit is refused until eject; eject leaves the marker.
8. Adopt regenerates the fragment.
9. Remove keeps the bucket and the override; install again works. file-mcp declares no volumes, so the kept list has no volume name.
10. Point file-mcp at an external S3 (`ks-cat-e2e-external-s3`, alias `e2e-external-s3`).
11. Point docs-service at an external Postgres (`ks-cat-e2e-external-pg`).
12. A second catalog source; an ambiguous name asks for a choice.
13. On the open example, manual TLS: the first apply is retryable `public_unreachable`. The CI profile installs the sample nginx vhost. The VM profile starts Caddy from the production example Caddyfile with the public-paths site, then apply reaches ready. Certbot is a separate VM scenario.
14. `check` allows `tls_untrusted`, `s3_plain_http`, and `postgres_plain_connection`. Any other warning fails.
15. The deployment has none of the forbidden names (`EXTERNAL_APIS`, `docker-compose.external.yml`, `FILE_API_URL`, a git-built keystone-mcp image).

## Report

Each line is `{"scenario","status","code","message"}`. `status` is `pass`, `fail`, `skip`, or `deviation`. `deviation` is a note, not a failure: the empty layout patch, the catalog SHA instead of a missing `v1.0.0` tag, the local stand-in for file-mcp 0.9.0, and a non-default publish port.

`scripts/e2e/jsonq.py` reads one keystonectl JSON document from stdin and prints one value per line. It has no expression language. `scripts/e2e/mcp_check.py` calls the gateway from inside the keystone-mcp image. The API key stays in `E2E_API_KEY` and in a mode 600 env file.
