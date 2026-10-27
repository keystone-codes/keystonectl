# Catalog and sources

`keystonectl` discovers installable packages through one or more **catalog sources**. Each source is a git repository that contains a single `catalog.yaml` file. The deployment root may also hold `sources.yaml`, which lists which catalogs are enabled on that machine and in what order they appear in listings.

Machine-readable contracts:

- [`src/keystonectl/schemas/catalog-v1.json`](../../src/keystonectl/schemas/catalog-v1.json) — `catalog.yaml` (`schema_version: 1`)
- [`src/keystonectl/schemas/sources-v1.json`](../../src/keystonectl/schemas/sources-v1.json) — `sources.yaml` (`schema_version: 1`)

Examples: [catalog.yaml](examples/catalog.yaml), [sources.yaml](examples/sources.yaml).

## Catalog

A catalog names itself (`name`), optionally describes itself, lists **packages** (services and resource providers), and may define **bundles** (install sets of services).

Why: [ADR 0001](../adr/0001-catalog-outside-core.md).

| Field | Required | Meaning |
|---|---|---|
| `schema_version` | yes | integer `1` |
| `name` | yes | catalog identifier; same pattern as a source name |
| `description` | no | one line, 1–200 characters |
| `packages` | yes | map of package name → entry (1–500 names) |
| `bundles` | no | map of bundle name → `{description, packages}` |

### Package entry

| Field | Required | Meaning |
|---|---|---|
| `kind` | no | `service` (default) or `resource` |
| `summary` | no | one line, 1–200 characters |
| `source` | yes | git URL (`https://`, `ssh://`, `git@host:path`, or `file:///` for local checks only) |
| `path` | no | directory inside the repo that contains `keystone-service.yaml` (default `.`). `packages/atlassian` — several packages in one repository, as in stdio-box |
| `provides` | for `resource` | `{kind: s3 \| postgres, version: 1}` — no `container` field in the catalog |
| `versions` | yes | 1–100 rows; each row pins one release |

Each version row:

| Field | Required | Meaning |
|---|---|---|
| `version` | yes | semver `X.Y.Z` (same as package manifest) |
| `ref` | yes | git tag or 40-character commit SHA (not a branch name) |
| `keystone_mcp` | for `service` | compatibility range with installed keystone-mcp (see below) |

### Tag or commit

A git tag can be moved by anyone with push access to the package repository; a 40-character commit SHA cannot. The container image inside the manifest is pinned by digest either way, but the manifest itself is only as fixed as the `ref` that names it — on a tag, nothing stops it from pointing at a different commit tomorrow. Use a SHA for any catalog whose publishing process you do not trust. The keystone-codes catalog's CI (`scripts/check_refs.py`) re-checks, on every pull request, that the digest on the tag still matches the published image, but that check runs in CI, not on the operator's machine.

Catalog `keystone_mcp` repeats manifest `compat.keystone_mcp` so `catalog list` can show compatibility without cloning every package. If the manifest differs after fetch, the manifest wins and the installer emits warning `compat_mismatch`.

## Sources

`sources.yaml` lives at the deployment root next to `services.lock.yaml`. It is not committed to git.

| Field | Required | Meaning |
|---|---|---|
| `schema_version` | yes | integer `1` |
| `sources` | yes | list of source entries |

Each source:

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | local name on this machine (defaults to the catalog's own `name` for the built-in source) |
| `url` | yes | git URL of the catalog repository (same rules as `packages.*.source`) |
| `ref` | yes | tag or commit that pins the catalog snapshot |
| `priority` | yes | integer 0–1000; unique among sources; higher sorts earlier in listings |

When `sources.yaml` is missing, `DEFAULT_SOURCES` applies: one source `keystone-codes` pointing at `https://github.com/keystone-codes/keystone-catalog.git` with ref `DEFAULT_CATALOG_REF` (`v1.3.0`) and priority `100`.

The first `catalog add` writes that file and records the ref it resolved. Later releases of keystonectl may raise `DEFAULT_CATALOG_REF`, but they do not rewrite a pin already on disk, and reading the file never substitutes the new tag. `catalog list` and `check` warn `default_catalog_behind` when the source whose URL is the default catalog is pinned to an older `vX.Y.Z` tag than `DEFAULT_CATALOG_REF`. A commit SHA is not compared. Move the pin with `catalog set-ref <name> <ref>`: it checks the new tag or commit, rewrites only that source's `ref`, and replaces the catalog cache. Installed packages and `services.lock.yaml` stay as they are. Passing the ref the source already has changes nothing and does not contact the network.

## Referring to a package

Syntax: `[source/]name[@X.Y.Z]`

- `source` — optional; must match a configured source name.
- `name` — package name in the catalog.
- `@X.Y.Z` — optional exact version from the catalog.

Invalid references raise `ValueError` with message `package reference must look like [source/]name[@X.Y.Z]: '<text>'`.

## How a package and a version are chosen

1. The request is `[source/]name[@version]`.
2. Sources are ordered by `priority` (higher first). Order affects listing only, **not** selection.
3. With a source prefix, the package is searched only in that source.
4. Without a source, every source that lists the name is considered. None → `package_not_found`. Two or more → `package_ambiguous` with choices `source/name`, regardless of priority.
5. A requested version must exist in the catalog (`version_not_found`) and be compatible with installed keystone-mcp (`incompatible_keystone_mcp`; no force flag).
6. With no version, the highest catalog version compatible with installed keystone-mcp is chosen; if none fit → `incompatible_keystone_mcp` listing versions and ranges.
7. If the package is already installed (present in `services.lock.yaml`), `service install` without a version keeps the installed version.
8. Installed keystone-mcp version is detected from the deployment layout (image tag or local checkout); if unknown → `keystone_mcp_version_unknown`.
9. After fetch, the manifest must match the catalog entry on `name`, `kind`, `version`, and for resources `provides.kind` / `provides.version`; otherwise `manifest_invalid`. If manifest `compat.keystone_mcp` differs from catalog `keystone_mcp` (compared as `SpecifierSet`), the manifest wins and warning `compat_mismatch` is emitted.
10. Source `name` is local to the machine; operators may alias catalogs with `catalog add --name`.
11. Bundles: `bundle install [source/]bundle`; same ambiguity rules as step 4. Bundle members are resolved only in that source. Install order follows package `requires`, then bundle list order; cycles → `requires_cycle`.
12. `service install` when a listed `requires` dependency is missing → `requires_missing`; dependencies are not auto-installed.

## Compatibility with keystone-mcp

Ranges use PEP 440 via the `packaging` library (`SpecifierSet`, `Version`). Only operators `>=`, `<`, and `!=` are allowed. There must be exactly one `>=` and one `<`, and the lower bound must be strictly below the upper.

Compatibility test: `SpecifierSet(spec).contains(Version(installed), prereleases=True)` — so `0.5.1rc1` matches `>=0.5.0,<2.0`, while `2.0.0rc1` does not match `<2.0`.

Error texts (for invalid range string `<s>`):

- `keystone_mcp range '<s>' is not a valid version specifier`
- `keystone_mcp range '<s>': operator '~=' is not allowed; use >=, < and !=`
- `keystone_mcp range '<s>' must have exactly one lower bound (>=) and one upper bound (<)`
- `keystone_mcp range '<s>' is empty: the lower bound is not below the upper bound`

Manifest field `compat.keystone_mcp` is validated with the same rules (`compat.keystone_mcp: …` prefix in manifest errors).

The `keystonectl` product version is independent of keystone-mcp; only package/catalog compatibility fields express that relationship.

## Bundles

A bundle lists 1–50 unique **service** package names from the same catalog. Resource providers are not bundle members; they are installed through `needs` and resource binding.

## Refusal codes

| `error.id` | When | Exit |
|---|---|---|
| `package_not_found` | name absent from all considered sources | 3, not retryable |
| `package_ambiguous` | name in multiple sources without prefix | 2 (`missing` choice `package`) |
| `version_not_found` | `@version` not in catalog | 3, not retryable |
| `incompatible_keystone_mcp` | version or auto-select incompatible with keystone-mcp | 3, not retryable |
| `keystone_mcp_version_unknown` | cannot determine keystone-mcp version | 2 |
| `manifest_invalid` | fetched manifest disagrees with catalog | 3, not retryable |
| `requires_missing` | `requires` not satisfied | 3, not retryable |
| `source_unreachable` | network failure fetching catalog or package | 3, retryable |
| `catalog_invalid` | local catalog file fails validation | 3, not retryable |

Warnings include `compat_mismatch`, `local_source` (`file://` URLs), and `catalog_cached`.

## Not in this release

- Cryptographic signing of catalogs or packages.
- Pinning a package by SHA in the default catalog; tags are used, see [Tag or commit](#tag-or-commit).
- Revoking a release: remove the version from `catalog.yaml`.
- stdio transport packages in the catalog workflow.
