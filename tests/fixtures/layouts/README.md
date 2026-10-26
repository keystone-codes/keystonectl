# Layout fixtures

Copies of upstream deployment directories used by `tests/test_layout.py`.
They are data. Do not edit them to match a newer upstream without recording
the new commit here.

## keystone-mcp-production

Source: keystone-mcp `examples/production/` on branch `release/0.5.0`.
Commit: `f73310df8634a19406bf21972e023af9a3b44ac0`.

Files: `docker-compose.yml`, `upstreams.yaml`, `policy/rbac-policy.yaml`, `Caddyfile`.
Copied as published. The pinned image in this tree is
`ghcr.io/keystone-codes/keystone-mcp:0.5.0`.

## keystone-deploy-mcp

Source: keystone-deploy `mcp/` on branch `release/catalog`.
Commit: `dab74b9a4eeb28caa82f3571d9de7b5ae63bc518`.

`docker-compose.yml` is the upstream file with comments removed.
`keystone-mcp.vars` lists every `KEYSTONE_MCP_*` variable from `mcp/.env.example`
with comments removed and values emptied, except `KEYSTONE_MCP_UPSTREAMS=[]`.
Tests copy that file to `.env`.
