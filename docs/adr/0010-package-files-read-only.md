# 0010. Package files mounted read-only

## Status

Accepted (keystonectl 0.3, 2026-10-19).

## Context

A stdio server is installed as a box on the stock `stdio-box` image
([0007](0007-stdio-servers-as-boxes.md)). The image already reads the provider directory from
`BOX_PROVIDER_DIR`, but nothing put that directory into the container. Authors baked it into a
derived image (`wrap-stdio --derived-image`), pushed the image to a registry, and copied the
digest into the manifest: one manual step and a mandatory registry for every provider.

The manifest has no host-level setting ([0004](0004-compose-generated-from-manifest.md)):
`volumes` are named volumes only, and a string volume or a `host`, `source` or `type` key is
rejected. [0007](0007-stdio-servers-as-boxes.md) says no new manifest fields are required for
boxes. The package cache under `.keystonectl/packages/` is replaced on every fetch and is mode
700, so it cannot be mounted.

## Decision

We let a package ask that one directory of its own tree appears inside a container, read-only,
through the optional field `containers.<role>.package_dir: {from, path}`. The deploy target
chooses how the files get there; a package still cannot name a host path.

- `from` is a path inside the package (`.` is the package root). `path` is the path in the
  container. One `package_dir` per container; `path` differs from the container's volume paths.
- The subtree holds only plain files and directories: no symbolic or hard links, devices, FIFOs
  or sockets. At most 900 KiB and 256 files, so the same files fit a Kubernetes ConfigMap.
- Compose target: `service apply` copies the subtree of the pinned version to
  `.keystonectl/package-dirs/<name>/<version>-<hash12>/` (directories 0755, files 0644), without
  following links, and the fragment mounts that copy with `:ro`. `hash12` is the start of the
  sha256 of every path and file in sorted order. A future Helm target uses a ConfigMap instead.
- The lock records `package_dirs`. Upgrade keeps the current and the previous copy, rollback
  returns to the previous one, remove deletes the copies unless the fragment is ejected.
- This record refines [0004](0004-compose-generated-from-manifest.md) and the sentence "no new
  manifest fields are required" in [0007](0007-stdio-servers-as-boxes.md). It does not replace
  either: `transport: stdio` stays rejected and host paths stay forbidden.

## Consequences

A box installs on the stock image with its provider directory as package files: no derived
image, no registry, no digest to copy. The installer, not the package, decides where files live,
so a package still cannot read or write the host. Costs: one more directory under
`.keystonectl/` that is world-readable on purpose (other uids read it in the container), a size
limit that keeps large assets in images, and file modes are not carried over (an executable bit
is lost). Docker creates a missing bind source as an empty directory; `check` reports a copy that
is missing or changed as `package_dir_missing`.

## Alternatives considered

- **Mount the package cache directly** — A fetch replaces the directory under a running
  container, and the cache is mode 700.
- **Host bind volumes in the manifest** — Gives a package the host; rejected by
  [0004](0004-compose-generated-from-manifest.md).
- **Keep derived images only** — Works, but every provider needs a registry and a release.
- **Named volume filled by an init container** — Needs an image that carries the files, which is
  the derived image again.

## Links

- [Service package](../spec/service-package.md) (Container, What is forbidden and why),
  [Packaging a service](../packaging-a-service.md#files-from-the-package),
  [What service apply writes](../generation.md),
  [Service lifecycle](../lifecycle.md),
  [0004](0004-compose-generated-from-manifest.md),
  [0007](0007-stdio-servers-as-boxes.md)
