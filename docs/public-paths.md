# Public paths

A package with `public_paths` is reachable from the internet only through those prefixes and its health check. keystonectl publishes the container port on `127.0.0.1` and writes an nginx site that proxies just those paths. The port is not reachable from another machine. Nginx on the host is.

`public_base_url_env` receives `https://<domain>`. `public_container` selects the container (the default is `mcp.container`). There is no body-size field: every proxied location uses `client_max_body_size 100m`. A larger limit is an edit of the vhost.

## Questions

| Variable | When |
|---|---|
| `KEYSTONE_SETUP_SERVICE_<NAME>_DOMAIN` | Required. Lowercase hostname, at least two labels. No default. |
| `KEYSTONE_SETUP_SERVICE_<NAME>_DNS` | `check` (default) or `skip` when the name is behind a proxy or in CI. |
| `KEYSTONE_SETUP_SERVICE_<NAME>_TLS` | `keep` (only if a current certificate is already on disk), `certbot`, `custom`, or `manual`. |
| `KEYSTONE_SETUP_SERVICE_<NAME>_CERT_PATH` and `_KEY_PATH` | Paths on the host when `TLS=custom`. Not secrets. |
| `KEYSTONE_SETUP_TLS_EMAIL` | Shared Let's Encrypt address when `TLS=certbot`. |
| `KEYSTONE_SETUP_SERVICE_<NAME>_TAKEOVER` | `yes` or `no` when another enabled nginx site already serves the domain. |

A later `service apply` reads `public.*` from the lock, so the same answers are not asked again.

`--defer-public` installs the service without the public steps. `public.state` stays `pending` and `public_base_url_env` is `https://public-domain-pending.invalid`, which cannot resolve. The next apply with a domain finishes publication.

`--certbot-staging` adds certbot `--test-cert`.

## Loopback port

The port is the smallest of `47600`–`47699` that is not already in the lock and that `bind` on `127.0.0.1` accepts. It is stored in the lock and reused. Nginx needs a stable address because the container address changes every recreate. When the range is full, apply stops with `port_range_exhausted`.

## Taking over a site

If `sites-enabled` already has a symlink whose `server_name` is this domain, apply asks before it removes that symlink. The file in `sites-available` stays, and `service remove` puts the symlink back. A regular file, or anything under `conf.d`, is left alone and apply stops with `domain_in_use`.

## extra_hosts

Every domain in `public.state: ready` is written into generated fragments as `<domain>:host-gateway`, so another container opens the same URL an outside client uses. A fragment that was edited by hand, or ejected, is not rewritten. Apply warns `extra_hosts_manual` and prints the line to add.

## Certificates

Four methods:

- `keep` uses the certificate already on disk. It is offered only when that certificate is present and not expired.
- `certbot` runs `certbot --nginx` for the domain. A Let's Encrypt certificate with more than 30 days left is not re-issued (`--keep-until-expiring` on a real call, and a rerun skips the call). Renewal is certbot's timer or cron. `service status` warns at 20 days.
- `custom` copies a PEM certificate and key to `/etc/ssl/keystone/<domain>/` (`fullchain.pem` mode `644`, `privkey.pem` mode `600`, directory mode `700`). Renewal is the operator's job. `service status` warns at 30 days.
- `manual` does not call nginx or sudo. keystonectl still allocates the port, writes the public URL and `extra_hosts`, and saves a sample nginx file under `.keystonectl/nginx/`. The operator's proxy must answer `https://<domain><healthz>` with 200 before `public.state` becomes `ready`.

A DNS, certificate, or external-check failure leaves the service running. The lock records `public.state: pending` and `public.last_error`. Apply again after fixing it. With `--json` the exit code is 3 and `retryable` comes from the error id.

### Caddy, written by hand

keystonectl does not generate Caddy configuration. For `TLS=manual`, a Caddy site that proxies the same prefixes looks like this (the upstream port is the loopback port from the lock):

```caddy
files.example.com {
	handle /u/* {
		reverse_proxy 127.0.0.1:47600
	}
	handle /d/* {
		reverse_proxy 127.0.0.1:47600
	}
	handle /healthz {
		reverse_proxy 127.0.0.1:47600
	}
	handle {
		respond 404
	}
}
```
