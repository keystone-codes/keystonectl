# Layout fixtures

Copies of upstream deployment directories used by `tests/test_layout.py`.
They are data. Do not edit them to match a newer upstream without recording
the new commit here.

## keystone-mcp-production

Source: keystone-mcp `examples/production/` on branch `release/0.5.0`.
Commit: `6b04a198101dac22815b17d5bcf55b0e1c6463d8`.

Files: `docker-compose.yml`, `upstreams.yaml`, `policy/rbac-policy.yaml`, `Caddyfile`.
Copied as published. The pinned image in this tree is
`ghcr.io/keystone-codes/keystone-mcp:0.5.0`.

## keystone-deploy-mcp

Source: keystone-deploy `mcp/` on branch `release/catalog`.
Commit: `aef1ab37b4657574ab24466e5b84b00c6c8f6e18`.

`docker-compose.yml` is the upstream file with comments removed.
`keystone-mcp.vars` lists every `KEYSTONE_MCP_*` variable from `mcp/.env.example`
with comments removed and values emptied, except `KEYSTONE_MCP_UPSTREAMS=[]`.
Tests copy that file to `.env`.
