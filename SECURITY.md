# Security Policy

## Reporting a vulnerability

Please report suspected security vulnerabilities privately, using
[GitHub Security Advisories](https://github.com/keystone-codes/keystonectl/security/advisories/new)
on this repository. Do not open a public issue for a suspected vulnerability.

Include as much detail as you can: affected version/commit, reproduction
steps, and the impact you believe it has. We will acknowledge your report and
follow up with next steps as we investigate.

## Supported versions

| Version | Supported |
|---|---|
| `0.1.x` | yes |

Only the latest released minor version receives security fixes.

## Security model

keystonectl runs `sudo -n` (passwordless, non-interactive) to write the nginx
vhost and to run `certbot` when a service requests a public TLS endpoint
(`TLS=manual` skips both). It never stores a sudo password and fails the
command rather than prompt for one.

A secret answer is only ever a path to a file the operator prepared with mode
600. keystonectl reads that file at the point of use, refuses files with
wider permissions, and never writes a secret value to the lock file, logs,
`--json` output, or a compose fragment: output is redacted to `***`.

## Scope

In scope: the `keystonectl` CLI and library code in this repository —
manifest and catalog parsing, the lock file, compose and env file
generation, the nginx/TLS lifecycle, and secret handling.

Out of scope: the content of a catalog, a service package, or a deployment's
own `sources.yaml`, `services.lock.yaml`, or secret files — those are the
responsibility of whoever authors or operates them.
