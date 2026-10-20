# 0011. Local packages live in the deployment, without git

## Status

Accepted (keystonectl 0.3, 2026-10-19).

## Context

Every package came from git. A catalog version row names a tag or a commit, and even a
`file://` source goes through `git clone` or `git fetch`; the lock pins the commit, and a tag
that moves is refused as `tag_moved`. An operator's own package — a wrapper for a stdio server,
a bridge to a remote server, a service whose image is already in a registry — needed a
repository, a commit, a tag, a push and a catalog entry before the first install.

The agent that sets up a deployment does not run git. It writes files into the deployment
directory, and nothing there could be installed.

## Decision

We let a package live in the deployment's own folder `<root>/local/<name>/`, without git, and pin
it by the hash of its content instead of a commit.

- A local package is part of the deployment's state, like `services.lock.yaml`. When `local/`
  exists, the source `local` is always there; it is not written in `sources.yaml`, and
  `catalog add` refuses the name `local`.
- `local/<name>/` holds `keystone-service.yaml`; the folder name is the package name. A package
  has one version: the one in its manifest. Package choice follows the catalog rules: `local/<name>`
  explicitly, `package_ambiguous` when another catalog has the same name.
- A fetch reads the folder under the `package_dir` rules of [0010](0010-package-files-read-only.md)
  (plain files and folders, no links, 900 KiB and 256 files), copies it into the package cache,
  and records `content_sha256`, the `subtree_digest` of the folder. The lock entry has
  `source: local`, `ref: local`, `commit: null`.
- The content of a version cannot change. The same version with another `content_sha256` is
  refused as `local_changed`; a higher version goes through `service upgrade`; a lower one is
  `version_not_found`.
- Rollback restores the previous version from keystonectl's own copies — the `.bak` files, the
  lock's `previous` and the `package_dir` copies — not from history. It never reads `local/`.
- keystonectl writes into `local/` in one case only: `package import-snapshot` puts the tool
  snapshot that a box took under a signed-in account into `local/<name>/` and rebuilds that
  package's manifest and policy example, after it has checked the snapshot's hash. It never
  installs or upgrades by itself. `init` creates the empty folder once; `remove` leaves
  `local/<name>/` in place.

## Consequences

The agent installs its own package by writing files and running `service apply local/<name>`:
no repository, no tag, no push. A silent edit is caught the same way a moved tag is. Costs: there
is no history, so only one previous version can be restored, and only while its copies remain; a
local package is limited to 900 KiB and 256 files, so large assets stay in images; and the
folder must be backed up with the deployment, because nothing else holds it. `check` warns
`local_package_missing` when an installed package's folder is gone.

## Alternatives considered

- **A git repository per deployment** — Brings back commits, tags and pushes that the agent does
  not run.
- **`file://` catalog without git** — Keeps a catalog file and version rows that the operator has
  to maintain by hand next to the manifest.
- **Install straight from `local/` without a copy** — A later edit would change a running
  service under the same version, and rollback would have nothing to return to.

## Links

- [Catalog and sources](../spec/catalog.md#the-local-source),
  [Deployment layout](../spec/deployment-layout.md),
  [Packaging a service](../packaging-a-service.md#a-package-of-your-own-without-a-repository),
  [0010](0010-package-files-read-only.md)
