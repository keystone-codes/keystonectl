# Release checklist

Fill this in when a person runs the manual checks. The result column stays empty until that run. A CI report is not a substitute for sections A–H.

The CI profile does not run keystone-deploy `setup.sh`; the partner path is proven only by section B.

| Field | Value |
|---|---|
| keystonectl (PyPI) | not published; install from the checkout (`--profile ci` does this) |
| keystonectl commit | |
| Date | |
| Checked by | |
| keystone-mcp image | `ghcr.io/keystone-codes/keystone-mcp:0.5.0` |
| keystone-mcp commit | `6b04a198101dac22815b17d5bcf55b0e1c6463d8` (`release/0.5.0`) |
| keystone-deploy commit | `ffd719701f3bb0eb53bdd212447bcd43bbd07c43` (`release/catalog`) |
| keystone-catalog commit | `1bc59512acccbd30cfbd958f78b713b21a7276a2` (tag `v1.0.0` = `1bc5951`) |
| file-mcp | `1.0.0` — `ghcr.io/keystone-codes/file-mcp@sha256:5976b83fe6b256d66246044c99fb769fff53981104bb1a769e8799bb37fef4c0` |
| hello-upstream | `1.0.0` — `ghcr.io/keystone-codes/hello-upstream@sha256:f73a378bd40c3a176df5c7cf560464827d2a9b2c38238da6674342a73bff5337` |
| minio-resource / postgres-resource | git tag `v1.0.0`; images are the foreign digests in those manifests |
| Package versions from the lock | |

`scripts/e2e.sh --profile vm` is the command for sections A and B. It has not been run for this document. Do not mark a row from a CI log. Until 0.1.0 is on PyPI, that command requires `--keystonectl <path to keystonectl-0.1.0-py3-none-any.whl>`; without it the script exits 1 before installing anything.

## 0. Candidate commit

Commit K is the candidate under test for every row below, including A–H. Fill in the link once each run exists; leave rows empty until then.

| Check | Expected | Link |
|---|---|---|
| `ci` on commit K | green | |
| Manual `e2e` workflow_dispatch on commit K | green | |
| Trial `Release` run with `dry_run` on commit K | green | |

## A. Clean VM, open example

Ubuntu 22.04. Install keystone-mcp from `examples/production` by following `docs/paths/production-docker.md` in keystone-mcp. Build the wheel from the commit under test (`uv build`), copy it to the VM, then:

```bash
scripts/e2e.sh --layout production-example --profile vm --tls certbot --domain <vm-domain> --keystonectl <path to keystonectl-0.1.0-py3-none-any.whl>
```

Attach the JSONL report next to this file when the run exists.

| Step | Command or action | Expected | Result |
|---|---|---|---|
| Install keystone-mcp | Follow `docs/paths/production-docker.md` | Gateway answers on the VM | |
| Run the script | command above | `keystonectl` installs from the wheel; exit 0; scenarios 1–15 pass; certbot issued the certificate | |

## B. Clean VM, keystone-deploy

Same script, layout `keystone-deploy`, profile `vm`, `--tls certbot`, on Ubuntu 22.04 and on Debian 12. The script calls `./setup.sh --non-interactive` and does not start the product backend or frontend.

| Step | Command or action | Expected | Result |
|---|---|---|---|
| Ubuntu 22.04 | `scripts/e2e.sh --layout keystone-deploy --profile vm --tls certbot --domain <vm-domain> --keystonectl <path to keystonectl-0.1.0-py3-none-any.whl>` | Exit 0; scenarios 1–15 pass | |
| Debian 12 | same command | Exit 0; scenarios 1–15 pass | |
| bats | Run `tests/bash/run.sh` in keystone-deploy on the commit under test | all bats files pass | |

## C. Owner stand, through chat

Host: `files-stage.keystone.codes`. Catalog on the stand follows the owner's answer for that stand. Claude Code on the host has the `keystonectl` skill. An administrator has connected the agent to keystone-mcp. Scenarios from `skills/keystonectl/tests/checklist.md`: `file-mcp-minio`, `dns-retry`, `docs-service-external-postgres`, `eject`, `remove`.

Success: the operator typed no commands except creating secret files; the agent did not open the policy file and did not call `docker`; recommended roles went to `keystone-policy-admin` as `{role, allow}` and were applied after the administrator confirmed.

| Step | Command or action | Expected | Result |
|---|---|---|---|
| file-mcp-minio | chat, per the skill checklist | file-mcp and MinIO installed; operator typed no install commands | |
| dns-retry | temporary bad or missing A record, then the real record | first apply stops; retry reaches ready | |
| docs-service-external-postgres | chat | docs-service healthy on the operator Postgres | |
| eject | chat | fragment ejected; policy file untouched | |
| remove | chat, per the skill checklist | removed only after `CONFIRM_REMOVE=yes`; kept resources retold | |
| Recommended roles | agent hands `{role, allow}` to keystone-policy-admin | applied only after the administrator confirms | |

## D. Operator certificate files on the stand

| Step | Command or action | Expected | Result |
|---|---|---|---|
| TLS custom | operator certificate and key files | `service status` shows the expiry and that renewal is the operator's job | |

## E. External S3 and Postgres on the stand

| Step | Command or action | Expected | Result |
|---|---|---|---|
| External S3 | operator endpoint, bucket, and key files | upload and download through the agent succeed | |
| External Postgres | operator host and password file | docs-service is healthy | |

## F. Repeat, override, eject, adopt, remove

Same checks as script scenarios 4 and 6–9, on the stand.

| Step | Command or action | Expected | Result |
|---|---|---|---|
| Repeat | plan and apply again | no new questions; zero changes | |
| Override | `packages/<name>.override.yml` | survives upgrade | |
| Eject and adopt | eject, edit, adopt | adopt regenerates; the operator edit is gone | |
| Remove and install again | remove, then install | kept resources stay; a fresh install works | |

## G. Partner catalog

Same check as script scenario 12. The partner catalog is a separate git repository, not a `file://` URL.

| Step | Command or action | Expected | Result |
|---|---|---|---|
| Add the partner source | `catalog add https://github.com/keystone-codes/e2e-partner-catalog.git --name partner` | source added | |
| Plan the partner package | `service plan partner/file-mcp` | the package plans on the same path as a catalog package | |

## H. Concept readiness

One row per item. Point at the step above that shows it. Leave the result empty until the stand run.

| Step | Command or action | Expected | Result |
|---|---|---|---|
| file-mcp from the catalog on the open example | no hand edit of compose, the registry, or keystone-mcp `.env` | matches section A | |
| Same through keystone-deploy `setup.sh` | section B | matches section B | |
| External S3 and catalog MinIO | sections C and E | both work | |
| Shared Postgres, catalog provider and external | sections C and E | both work | |
| Certificate through certbot | section C | issued | |
| Certificate from operator files | section D | status shows expiry and operator renewal | |
| Manual TLS on the open example | proxy and certificate stay with the operator | sample vhost only; keystonectl does not install nginx | |
| Install through chat | section C | operator typed no commands except secret files | |
| Policy | section C | keystonectl only checks; an administrator edits | |
| Override, eject, adopt | section F | override survives upgrade; adopt regenerates | |
| Partner catalog | section G | package installs the same way | |
| Repeat, remove, reinstall | section F | no questions on the second plan; reinstall works | |
| Final check | `keystonectl check` | clean, apart from warnings the release note allows | |
| Forbidden leftovers | search the deployment | no `EXTERNAL_APIS`, `docker-compose.external.yml`, a "managed" flag, `FILE_API_URL`, or a keystone-mcp image built from git | |
