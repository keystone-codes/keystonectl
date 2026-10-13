#!/usr/bin/env bash
# End-to-end check of keystonectl against two keystone-mcp layouts.
# Pins are updated with the release. Digests are indexes, not tags.
set -euo pipefail

# release/0.5.0 of keystone-mcp. examples/production is copied from this commit.
KEYSTONE_MCP_REF="${E2E_KEYSTONE_MCP_REF:-6b04a198101dac22815b17d5bcf55b0e1c6463d8}"
KEYSTONE_MCP_URL="${E2E_KEYSTONE_MCP_URL:-https://github.com/keystone-codes/keystone-mcp.git}"
# keystone-codes/deploy, branch release/catalog.
KEYSTONE_DEPLOY_REF="${E2E_KEYSTONE_DEPLOY_REF:-dd4538167af85bf5842d88e43bca53748063879f}"
KEYSTONE_DEPLOY_URL="${E2E_KEYSTONE_DEPLOY_URL:-https://github.com/keystone-codes/deploy.git}"
# keystone-catalog main. The product default ref is v1.0.0; that tag is not on the repo.
CATALOG_REF="${E2E_CATALOG_REF:-1bc59512acccbd30cfbd958f78b713b21a7276a2}"
CATALOG_URL="${E2E_CATALOG_URL:-https://github.com/keystone-codes/keystone-catalog.git}"
# file-mcp 1.0.0, git f2928bf. hello-upstream 1.0.0, git c9dd693.
FILE_MCP_IMAGE="ghcr.io/keystone-codes/file-mcp@sha256:5976b83fe6b256d66246044c99fb769fff53981104bb1a769e8799bb37fef4c0"
HELLO_IMAGE="ghcr.io/keystone-codes/hello-upstream@sha256:f73a378bd40c3a176df5c7cf560464827d2a9b2c38238da6674342a73bff5337"
# minio-resource: official RELEASE.2025-09-07 and mc RELEASE.2025-08-13, packaged for GHCR.
MINIO_IMAGE="ghcr.io/keystone-codes/minio-server@sha256:af8333b0a5e9209abbb677c546b595dec87b45a227f33a2757b60e7d0ec5717f"
MC_IMAGE="ghcr.io/keystone-codes/minio-client@sha256:63ba7ba9b7ba59af56cb52d9052eb385cce4e8531b40d6b43639c9e7e514c97f"
# postgres-resource 7953f7a: postgres:16-alpine 16.15-alpine3.24.
PG_IMAGE="postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
# redis:7-alpine index, docker buildx imagetools inspect on 2026-10-13.
REDIS_IMAGE="redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"

LAYOUT="production-example"
PROFILE="ci"
STORAGE="both"
POSTGRES="both"
TLS="custom"
DOMAIN=""
KEYSTONECTL_SPEC=""
KEEP="false"
REPORT="e2e-report.jsonl"
PUBLISH_PORT="8080"

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
ROOT=""
WORK=""
DIAG=""
KEYSTONE_SERVICE=""
CLEANED="false"
UPGRADE_ACCEPT="1.0.0"

usage() {
  cat <<'EOF'
usage: scripts/e2e.sh [--layout production-example|keystone-deploy]
                      [--profile ci|vm] [--storage minio|external|both]
                      [--postgres provider|external|both] [--tls custom|certbot]
                      [--domain D] [--keystonectl SPEC] [--publish-port PORT]
                      [--keep] [--report FILE]
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --layout) LAYOUT="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --postgres) POSTGRES="$2"; shift 2 ;;
    --tls) TLS="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --keystonectl) KEYSTONECTL_SPEC="$2"; shift 2 ;;
    --publish-port) PUBLISH_PORT="$2"; shift 2 ;;
    --keep) KEEP="true"; shift ;;
    --report) REPORT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

case "$LAYOUT" in
  production-example|keystone-deploy) ;;
  *) echo "unknown layout: $LAYOUT" >&2; exit 1 ;;
esac
case "$PROFILE" in
  ci|vm) ;;
  *) echo "unknown profile: $PROFILE" >&2; exit 1 ;;
esac
case "$STORAGE" in
  minio|external|both) ;;
  *) echo "unknown storage: $STORAGE" >&2; exit 1 ;;
esac
case "$POSTGRES" in
  provider|external|both) ;;
  *) echo "unknown postgres: $POSTGRES" >&2; exit 1 ;;
esac
case "$TLS" in
  custom|certbot) ;;
  *) echo "unknown tls: $TLS" >&2; exit 1 ;;
esac
if [[ "$PROFILE" == "ci" && "$TLS" == "certbot" ]]; then
  echo "ci profile only runs --tls custom" >&2
  exit 1
fi
if [[ -z "$DOMAIN" ]]; then
  DOMAIN="files.e2e.test"
fi
if [[ -z "$KEYSTONECTL_SPEC" ]]; then
  if [[ "$PROFILE" == "ci" ]]; then
    KEYSTONECTL_SPEC="$REPO"
  else
    KEYSTONECTL_SPEC="pypi:0.1.0"
  fi
fi

want_storage() { [[ "$STORAGE" == "$1" || "$STORAGE" == "both" ]]; }
want_postgres() { [[ "$POSTGRES" == "$1" || "$POSTGRES" == "both" ]]; }

report_line() {
  local scenario="$1" result="$2" seconds="$3" note="${4:-}"
  REPORT_SCENARIO="$scenario" REPORT_RESULT="$result" REPORT_SECONDS="$seconds" \
    REPORT_NOTE="$note" REPORT_LAYOUT="$LAYOUT" python3 -c '
import json, os
row = {
    "layout": os.environ["REPORT_LAYOUT"],
    "scenario": os.environ["REPORT_SCENARIO"],
    "result": os.environ["REPORT_RESULT"],
    "seconds": float(os.environ["REPORT_SECONDS"]),
}
note = os.environ.get("REPORT_NOTE") or ""
if note:
    row["note"] = note
print(json.dumps(row, ensure_ascii=False))
' >>"$REPORT"
}

note() { report_line "deviation" "note" "0" "$1"; }

pass() {
  local scenario="$1" started="$2"
  local now seconds
  now="$(date +%s)"
  seconds="$((now - started))"
  printf 'PASS %s/%s\n' "$LAYOUT" "$scenario"
  report_line "$scenario" "pass" "$seconds"
}

fail() {
  local scenario="$1" reason="$2"
  printf 'FAIL %s/%s: %s\n' "$LAYOUT" "$scenario" "$reason" >&2
  report_line "$scenario" "fail" "0" "$reason"
  diagnostics || true
  cleanup || true
  exit 1
}

skip() {
  printf 'SKIP %s/%s\n' "$LAYOUT" "$1"
  report_line "$1" "skip" "0" "${2:-}"
}

jq_() { python3 "$HERE/e2e/jsonq.py" "$@"; }
lockq() { uv run --project "$REPO" python "$HERE/e2e/lockq.py" "$ROOT/services.lock.yaml" "$@"; }

has_line() {
  # Read the whole stream. Returning early would SIGPIPE the producer under pipefail.
  local needle="$1" line found=1
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "$needle" ]]; then
      found=0
    fi
  done
  return "$found"
}

has_prefix() {
  local needle="$1" line found=1
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "$needle"* ]]; then
      found=0
    fi
  done
  return "$found"
}

kc() {
  KC_RC=0
  KC_OUT="$(keystonectl --root "$ROOT" "$@" --json --non-interactive 2>>"$WORK/log/kc.err")" || KC_RC=$?
  printf '%s\n' "$KC_OUT" >"$WORK/log/kc.out"
}

clear_confirms() {
  local name
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    unset "$name"
  done < <(python3 -c 'import os
for key in os.environ:
    if key.startswith("KEYSTONE_SETUP_") and "CONFIRM_" in key:
        print(key)')
}

run_answered() {
  local rounds=0 var kind missing
  clear_confirms
  while [[ "$rounds" -lt 8 ]]; do
    kc "$@"
    if [[ "$KC_RC" -eq 0 ]]; then
      return 0
    fi
    if [[ "$KC_RC" -ne 2 ]]; then
      return "$KC_RC"
    fi
    missing="$(jq_ missing-vars <"$WORK/log/kc.out" || true)"
    if [[ -z "$missing" ]]; then
      return 2
    fi
    local filled="false"
    while IFS= read -r var; do
      [[ -n "$var" ]] || continue
      kind="$(python3 -c '
import json, os, sys
doc = json.load(open(sys.argv[1]))
want = sys.argv[2]
for item in doc.get("missing") or []:
    if item.get("var") == want:
        print(item.get("kind") or "")
        break
' "$WORK/log/kc.out" "$var")"
      if [[ "$kind" == "confirm" ]]; then
        if [[ "$var" == *_CONFIRM_UPGRADE ]]; then
          export "${var}=${UPGRADE_ACCEPT}"
        else
          export "${var}=yes"
        fi
        filled="true"
      elif [[ -z "${!var:-}" ]]; then
        printf '%s\n' "$var" >"$WORK/log/unanswered"
        return 2
      fi
    done <<<"$missing"
    if [[ "$filled" == "false" ]]; then
      return 2
    fi
    rounds="$((rounds + 1))"
  done
  return 2
}

write_secret() {
  local path="$1" value="$2"
  umask 077
  printf '%s\n' "$value" >"$path"
  chmod 600 "$path"
  umask 022
}

rand_hex() { openssl rand -hex "$1"; }

sha_file() { openssl dgst -sha256 "$1" | awk '{print $NF}'; }

snapshot_tree() {
  local out="$1" rel path file
  : >"$out"
  for rel in packages keystonectl/upstreams.yaml services.lock.yaml docker-compose.packages.yml; do
    path="$ROOT/$rel"
    if [[ -d "$path" ]]; then
      find "$path" -type f | sort | while IFS= read -r file; do
        sha_file "$file" >>"$out"
      done
    elif [[ -f "$path" ]]; then
      sha_file "$path" >>"$out"
    else
      printf 'missing %s\n' "$rel" >>"$out"
    fi
  done
}

git_identity() {
  git -c user.email=e2e@example.com -c user.name=e2e "$@"
}

pin_clone() {
  local url="$1" sha="$2" dest="$3"
  git init -q "$dest"
  git -C "$dest" remote add origin "$url"
  local -a auth=()
  if [[ -n "${KEYSTONE_DEPLOY_TOKEN:-}" && "$url" == *"/deploy.git" ]]; then
    local askpass="$WORK/secrets/askpass"
    cat >"$askpass" <<'EOF'
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' "x-access-token" ;;
  *) printf '%s\n' "$KEYSTONE_DEPLOY_TOKEN" ;;
esac
EOF
    chmod 700 "$askpass"
    auth=(env GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0)
  fi
  "${auth[@]}" git -C "$dest" fetch --depth 1 origin "$sha"
  git -C "$dest" checkout --quiet --detach FETCH_HEAD
}

ensure_network() {
  local name="$1"
  if ! docker network inspect "$name" >/dev/null 2>&1; then
    docker network create "$name" >/dev/null
  fi
}

compose() {
  # -f is relative to the current directory, not --project-directory.
  docker compose --project-directory "$ROOT" \
    -f "$ROOT/docker-compose.yml" \
    -f "$ROOT/docker-compose.packages.yml" \
    "$@"
}

rewrite_publish_port() {
  local file="$1"
  if [[ "$PUBLISH_PORT" == "8080" ]]; then
    return 0
  fi
  python3 -c '
import pathlib, sys
path, port = sys.argv[1], sys.argv[2]
file = pathlib.Path(path)
file.write_text(file.read_text().replace("127.0.0.1:8080:8080", f"127.0.0.1:{port}:8080"))
' "$file" "$PUBLISH_PORT"
}

install_keystonectl() {
  export PATH="${HOME}/.local/bin:${PATH}"
  if [[ "$KEYSTONECTL_SPEC" == pypi:* ]]; then
    uv tool install --force "keystonectl==${KEYSTONECTL_SPEC#pypi:}"
  else
    uv tool install --force --from "$KEYSTONECTL_SPEC" keystonectl
  fi
  command -v keystonectl >/dev/null
}

prepare_host() {
  if ! command -v nginx >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y nginx
  fi
  local line="127.0.0.1 files.e2e.test docs.e2e.test"
  if ! grep -qxF "$line" /etc/hosts; then
    printf '%s\n' "$line" | sudo tee -a /etc/hosts >/dev/null
    printf '%s\n' "$line" >"$WORK/state/hosts-line"
  fi
  mkdir -p "$WORK/tls"
  openssl req -x509 -newkey rsa:2048 -nodes -days 2 \
    -subj /CN=files.e2e.test \
    -addext subjectAltName=DNS:files.e2e.test \
    -keyout "$WORK/tls/key.pem" -out "$WORK/tls/cert.pem" >/dev/null 2>&1
  chmod 600 "$WORK/tls/key.pem"
  chmod 644 "$WORK/tls/cert.pem"
}

write_sources() {
  cat >"$ROOT/sources.yaml" <<EOF
schema_version: 1
sources:
  - name: keystone-codes
    url: ${CATALOG_URL}
    ref: ${CATALOG_REF}
    priority: 100
EOF
}

chown_policy() {
  local dir="$1"
  # chmod while this user still owns the directory. After chown, chmod is refused.
  chmod 775 "$dir"
  if [[ "$(id -u)" -eq 0 ]]; then
    chown -R 10001:10001 "$dir"
  else
    sudo chown -R 10001:10001 "$dir"
  fi
}

prepare_production_example() {
  local src="$WORK/src/keystone-mcp" stage="$WORK/stage"
  pin_clone "$KEYSTONE_MCP_URL" "$KEYSTONE_MCP_REF" "$src"
  mkdir -p "$stage"
  git -C "$src" archive HEAD examples/production .env.example | tar -C "$stage" -x
  mkdir -p "$WORK/prod"
  cp -R "$stage/examples/production/." "$WORK/prod/"
  ROOT="$WORK/prod"
  local pg redis salt
  pg="$(rand_hex 24)"
  redis="$(rand_hex 24)"
  salt="$(rand_hex 32)"
  write_secret "$WORK/secrets/pg" "$pg"
  write_secret "$WORK/secrets/redis" "$redis"
  umask 077
  cat >"$ROOT/.env" <<EOF
KEYSTONE_MCP_DEPLOYMENT_PROFILE=production
KEYSTONE_MCP_BASE_URL=http://127.0.0.1:${PUBLISH_PORT}
KEYSTONE_MCP_HOST=0.0.0.0
KEYSTONE_MCP_PORT=8080
KEYSTONE_MCP_REDIS_URL=redis://:${redis}@redis:6379/0
KEYSTONE_MCP_USERS_DB_URL=postgresql+psycopg://keystone:${pg}@postgres:5432/keystone
KEYSTONE_MCP_RBAC_MODE=enforce
KEYSTONE_MCP_RBAC_POLICY_PATH=/etc/keystone-mcp/policy/rbac-policy.yaml
KEYSTONE_MCP_RBAC_AUDIT_SUBJECT_SALT=${salt}
KEYSTONE_MCP_SUPERADMIN_EMAIL=admin@example.com
KEYSTONE_MCP_ENABLED_LOGIN_METHODS=api_key
KEYSTONE_MCP_PRIMARY_LOGIN_METHOD=api_key
KEYSTONE_MCP_UPSTREAMS_PATH=/etc/keystone-mcp/upstreams.yaml
KEYSTONE_MCP_UPSTREAM_ALLOW_INSECURE_HTTP=true
KEYSTONE_MCP_UPSTREAM_EGRESS_ALLOW_PRIVATE=true
KEYSTONE_MCP_METRICS_ENABLED=true
KEYSTONE_PG_PASSWORD=${pg}
KEYSTONE_REDIS_PASSWORD=${redis}
EOF
  umask 022
  chmod 600 "$ROOT/.env"
  mkdir -p "$ROOT/policy"
  cp "$HERE/e2e/fixtures/rbac-policy.yaml" "$ROOT/policy/rbac-policy.yaml"
  chown_policy "$ROOT/policy"
  local patch="$HERE/e2e/fixtures/production-example.patch"
  if grep -q '^diff ' "$patch" || grep -q '^--- ' "$patch"; then
    patch -d "$ROOT" -p1 <"$patch"
  fi
  rewrite_publish_port "$ROOT/docker-compose.yml"
  docker compose --project-directory "$ROOT" -f "$ROOT/docker-compose.yml" up -d --wait
  kc init --adopt "$ROOT"
  [[ "$KC_RC" -eq 0 ]] || fail prepare "init --adopt exited ${KC_RC}"
  local command
  command="$(jq_ compose-command <"$WORK/log/kc.out")"
  [[ "$command" == *"-f docker-compose.yml -f docker-compose.packages.yml"* ]] \
    || fail prepare "compose command is missing the packages file"
  kc init --adopt "$ROOT"
  [[ "$KC_RC" -eq 0 ]] || fail prepare "second init exited ${KC_RC}"
  [[ "$(jq_ changes <"$WORK/log/kc.out")" == "0" ]] || fail prepare "second init changed files"
  compose up -d --wait
  kc check
  [[ "$KC_RC" -eq 0 ]] || fail prepare "check after adopt exited ${KC_RC}"
  KEYSTONE_SERVICE="$(lockq layout-keystone-service)"
  [[ -n "$KEYSTONE_SERVICE" ]] || fail prepare "layout has no keystone service"
  write_sources
}

prepare_deploy_ci() {
  local src="$WORK/src/keystone-deploy"
  pin_clone "$KEYSTONE_DEPLOY_URL" "$KEYSTONE_DEPLOY_REF" "$src"
  mkdir -p "$WORK/deploy"
  cp -R "$src/." "$WORK/deploy/"
  ROOT="$WORK/deploy/mcp"
  local pg salt base
  pg="$(rand_hex 24)"
  salt="$(rand_hex 32)"
  base="http://127.0.0.1:${PUBLISH_PORT}"
  umask 077
  sed \
    -e "s|__BASE_URL__|${base}|" \
    -e "s|__SALT__|${salt}|" \
    -e "s|__PG_PASSWORD__|${pg}|" \
    "$HERE/e2e/fixtures/mcp.env.ci" >"$ROOT/.env"
  umask 022
  chmod 600 "$ROOT/.env"
  write_secret "$WORK/secrets/pg" "$pg"
  mkdir -p "$ROOT/policy" "$ROOT/env"
  cp "$HERE/e2e/fixtures/rbac-policy.yaml" "$ROOT/policy/rbac-policy.yaml"
  chown_policy "$ROOT/policy"
  local example dest
  for example in "$ROOT"/env/*.env.example; do
    [[ -f "$example" ]] || continue
    dest="${example%.example}"
    if [[ ! -f "$dest" ]]; then
      cp "$example" "$dest"
      chmod 600 "$dest"
    fi
  done
  rewrite_publish_port "$ROOT/docker-compose.yml"
  ensure_network keystone_mcp_net
  ensure_network keystone-mcp-upstreams
  docker run -d --name ks-cat-e2e-redis --network keystone_mcp_net --network-alias redis \
    "$REDIS_IMAGE" >/dev/null
  kc init --adopt "$ROOT"
  [[ "$KC_RC" -eq 0 ]] || fail prepare "init --adopt exited ${KC_RC}"
  local command
  command="$(jq_ compose-command <"$WORK/log/kc.out")"
  [[ "$command" == *"-f docker-compose.yml -f docker-compose.packages.yml"* ]] \
    || fail prepare "compose command is missing the packages file"
  kc init --adopt "$ROOT"
  [[ "$(jq_ changes <"$WORK/log/kc.out")" == "0" ]] || fail prepare "second init changed files"
  compose --profile keystone-mcp up -d --wait keystone-mcp
  kc check
  [[ "$KC_RC" -eq 0 ]] || fail prepare "check after adopt exited ${KC_RC}"
  KEYSTONE_SERVICE="$(lockq layout-keystone-service)"
  write_sources
}

prepare_deploy_vm() {
  local src="$WORK/src/keystone-deploy"
  pin_clone "$KEYSTONE_DEPLOY_URL" "$KEYSTONE_DEPLOY_REF" "$src"
  cp -R "$src/." "$WORK/deploy/"
  ROOT="$WORK/deploy/mcp"
  export KEYSTONE_SETUP_BUNDLE=minimal
  export KEYSTONE_SETUP_RESOURCE_S3=catalog
  export KEYSTONE_SETUP_SERVICE_FILE_MCP_DOMAIN="$DOMAIN"
  export KEYSTONE_SETUP_SERVICE_FILE_MCP_DNS=check
  export KEYSTONE_SETUP_TLS_EMAIL="${E2E_TLS_EMAIL:-admin@example.com}"
  if [[ "$TLS" == "certbot" ]]; then
    export KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS=certbot
  else
    export KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS=custom
    export KEYSTONE_SETUP_SERVICE_FILE_MCP_CERT_PATH="${E2E_CERT_PATH:?set E2E_CERT_PATH}"
    export KEYSTONE_SETUP_SERVICE_FILE_MCP_KEY_PATH="${E2E_KEY_PATH:?set E2E_KEY_PATH}"
  fi
  (cd "$WORK/deploy" && ./setup.sh --non-interactive)
  KEYSTONE_SERVICE="$(lockq layout-keystone-service)"
}

prepare_layout() {
  case "$LAYOUT" in
    production-example) prepare_production_example ;;
    keystone-deploy)
      if [[ "$PROFILE" == "vm" ]]; then
        prepare_deploy_vm
      else
        prepare_deploy_ci
      fi
      ;;
  esac
}

issue_api_key() {
  local json
  compose exec -T "$KEYSTONE_SERVICE" \
    keystone-mcp service-accounts add --name e2e --role administrator >/dev/null
  json="$(compose exec -T "$KEYSTONE_SERVICE" \
    keystone-mcp keys issue --subject svc:e2e --label e2e --days 1)"
  printf '%s' "$json" | python3 -c 'import json,sys; data=json.load(sys.stdin); sys.stdout.write(data["key"])' \
    >"$WORK/secrets/api-key"
  chmod 600 "$WORK/secrets/api-key"
  python3 -c '
import pathlib, sys
key = pathlib.Path(sys.argv[1]).read_text().strip()
path = pathlib.Path(sys.argv[2])
path.write_text("E2E_API_KEY=" + key + "\n")
path.chmod(0o600)
' "$WORK/secrets/api-key" "$WORK/secrets/client.env"
}

mcp_check() {
  local mode="$1" namespace="${2:-}"
  local cid image
  cid="$(compose ps -q "$KEYSTONE_SERVICE")"
  image="$(docker inspect --format '{{.Image}}' "$cid")"
  local -a args=(--keystone "http://127.0.0.1:${PUBLISH_PORT}/" --mode "$mode")
  if [[ -n "$namespace" ]]; then
    args+=(--namespace "$namespace")
  fi
  if [[ "$mode" == "files" ]]; then
    args+=(--ca /tls/cert.pem)
  fi
  docker run --rm --network host --env-file "$WORK/secrets/client.env" \
    -v "$HERE/e2e:/e2e:ro" -v "$WORK/tls:/tls:ro" --entrypoint python "$image" \
    /e2e/mcp_check.py "${args[@]}" >"$WORK/log/mcp.json"
  python3 -c 'import json,sys; doc=json.load(open(sys.argv[1])); raise SystemExit(0 if doc.get("ok") else 1)' \
    "$WORK/log/mcp.json"
}

export_file_public() {
  export KEYSTONE_SETUP_SERVICE_FILE_MCP_DOMAIN="$DOMAIN"
  if [[ "$PROFILE" == "ci" ]]; then
    export KEYSTONE_SETUP_SERVICE_FILE_MCP_DNS=skip
    export KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS=custom
    export KEYSTONE_SETUP_SERVICE_FILE_MCP_CERT_PATH="$WORK/tls/cert.pem"
    export KEYSTONE_SETUP_SERVICE_FILE_MCP_KEY_PATH="$WORK/tls/key.pem"
  else
    export KEYSTONE_SETUP_SERVICE_FILE_MCP_DNS=check
    if [[ "$TLS" == "certbot" ]]; then
      export KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS=certbot
      export KEYSTONE_SETUP_TLS_EMAIL="${E2E_TLS_EMAIL:-admin@example.com}"
    else
      export KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS=custom
      export KEYSTONE_SETUP_SERVICE_FILE_MCP_CERT_PATH="${E2E_CERT_PATH:?set E2E_CERT_PATH}"
      export KEYSTONE_SETUP_SERVICE_FILE_MCP_KEY_PATH="${E2E_KEY_PATH:?set E2E_KEY_PATH}"
    fi
  fi
}

curl_public() {
  local code
  curl --cacert "$WORK/tls/cert.pem" -fsS "https://${DOMAIN}/healthz" >/dev/null
  code="$(curl --cacert "$WORK/tls/cert.pem" -sS -o /dev/null -w '%{http_code}' "https://${DOMAIN}/mcp")"
  [[ "$code" == "404" ]]
}

status_file_mcp() {
  local attempts=0
  while [[ "$attempts" -lt 6 ]]; do
    kc service status file-mcp
    [[ "$KC_RC" -eq 0 ]] || return 1
    if jq_ package-status file-mcp <"$WORK/log/kc.out" | has_line "keystone ready" \
      && jq_ package-status file-mcp <"$WORK/log/kc.out" | has_prefix "container mcp state running"; then
      if jq_ package-status file-mcp <"$WORK/log/kc.out" | has_prefix "problem "; then
        return 1
      fi
      return 0
    fi
    sleep 5
    attempts="$((attempts + 1))"
  done
  return 1
}

scenario_agent_path_missing() {
  local started before after
  if [[ "$LAYOUT" == "keystone-deploy" && "$PROFILE" == "vm" ]]; then
    skip agent_path_missing "setup.sh already installed bundle minimal"
    return 0
  fi
  started="$(date +%s)"
  snapshot_tree "$WORK/state/before-missing"
  kc bundle install minimal
  [[ "$KC_RC" -eq 2 ]] || fail agent_path_missing "expected exit 2, got ${KC_RC}"
  jq_ missing-vars <"$WORK/log/kc.out" | has_line KEYSTONE_SETUP_RESOURCE_S3 \
    || fail agent_path_missing "RESOURCE_S3 was not asked"
  if ! jq_ missing-vars <"$WORK/log/kc.out" | has_line KEYSTONE_SETUP_SERVICE_FILE_MCP_DOMAIN; then
    note "bundle install asks RESOURCE_S3 before the domain; both are not in one missing list"
  fi
  snapshot_tree "$WORK/state/after-missing"
  before="$(sha_file "$WORK/state/before-missing")"
  after="$(sha_file "$WORK/state/after-missing")"
  [[ "$before" == "$after" ]] || fail agent_path_missing "files changed"
  pass agent_path_missing "$started"
}

scenario_install_file_mcp_minio() {
  local started
  started="$(date +%s)"
  if ! want_storage minio; then
    skip install_file_mcp_minio "storage is ${STORAGE}"
    return 0
  fi
  export KEYSTONE_SETUP_RESOURCE_S3=catalog
  export_file_public
  run_answered bundle install minimal || fail install_file_mcp_minio "bundle install exited ${KC_RC}"
  cp "$WORK/log/kc.out" "$WORK/log/install.json"
  curl_public || fail install_file_mcp_minio "public HTTP check failed"
  mcp_check files || fail install_file_mcp_minio "files client failed"
  status_file_mcp || fail install_file_mcp_minio "status is not ready"
  local warnings
  warnings="$(jq_ package-status file-mcp <"$WORK/log/kc.out")"
  if printf '%s\n' "$warnings" | has_prefix "warning "; then
    printf '%s\n' "$warnings" | has_line "warning tls_untrusted" \
      || fail install_file_mcp_minio "unexpected status warning"
  fi
  lockq resource s3 origin | has_line catalog || fail install_file_mcp_minio "s3 origin is not catalog"
  lockq service minio role | has_line resource || fail install_file_mcp_minio "minio is not a resource"
  pass install_file_mcp_minio "$started"
}

scenario_policy_untouched() {
  local started now
  started="$(date +%s)"
  now="$(sha_file "$ROOT/policy/rbac-policy.yaml")"
  [[ "$now" == "$POLICY_SHA" ]] || fail policy_untouched "policy file changed"
  python3 -c '
import pathlib, sys
text = pathlib.Path(sys.argv[1]).read_text()
start = text.split("reader:", 1)[1]
block = start.split("\n\n", 1)[0]
if "hello_*" not in block:
    raise SystemExit(1)
if "files_" in block:
    raise SystemExit(1)
' "$ROOT/policy/rbac-policy.yaml" || fail policy_untouched "reader role changed"
  if [[ -f "$WORK/log/install.json" ]]; then
    jq_ has-recommended-policy <"$WORK/log/install.json" | has_line yes \
      || fail policy_untouched "apply output has no recommended policy"
  fi
  pass policy_untouched "$started"
}

scenario_rerun_no_questions() {
  local started before_ks before_file after_ks after_file
  started="$(date +%s)"
  if ! want_storage minio; then
    skip rerun_no_questions "file-mcp was not installed from the catalog"
    return 0
  fi
  kc service plan file-mcp
  [[ "$KC_RC" -eq 0 ]] || fail rerun_no_questions "plan exited ${KC_RC}"
  [[ "$(jq_ changes <"$WORK/log/kc.out")" == "0" ]] || fail rerun_no_questions "plan changes is not 0"
  if jq_ missing-vars <"$WORK/log/kc.out" | has_prefix ""; then
    fail rerun_no_questions "plan still asks questions"
  fi
  before_ks="$(container_started "$KEYSTONE_SERVICE")"
  before_file="$(container_started file-mcp-mcp)"
  run_answered bundle install minimal || fail rerun_no_questions "second bundle install exited ${KC_RC}"
  jq_ packages <"$WORK/log/kc.out" | has_line "file-mcp unchanged" \
    || fail rerun_no_questions "file-mcp was not unchanged"
  after_ks="$(container_started "$KEYSTONE_SERVICE")"
  after_file="$(container_started file-mcp-mcp)"
  [[ "$before_ks" == "$after_ks" && "$before_file" == "$after_file" ]] \
    || fail rerun_no_questions "a container was recreated"
  pass rerun_no_questions "$started"
}

container_id() {
  compose ps -q "$1"
}

container_started() {
  local id
  id="$(container_id "$1")"
  docker inspect --format '{{.State.StartedAt}}' "$id"
}

env_names() {
  local id="$1"
  docker inspect "$id" | python3 -c '
import json, sys
doc = json.load(sys.stdin)[0]
for item in doc["Config"].get("Env") or []:
    print(item.split("=", 1)[0])
'
}

network_names() {
  docker inspect --format '{{range $name, $net := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$1"
}

build_local_catalogs() {
  local docs="$WORK/src/docs-service" files="$WORK/src/file-mcp-e2e" catalog="$WORK/e2e-catalog"
  mkdir -p "$docs"
  cp "$REPO/tests/e2e/packages/docs-service/keystone-service.yaml" "$docs/"
  git init -q "$docs"
  git -C "$docs" add keystone-service.yaml
  git_identity -C "$docs" commit -q -m "docs-service stand-in"
  git -C "$docs" tag v1.0.0
  mkdir -p "$files"
  cp -R "$HERE/e2e/fixtures/file-mcp/." "$files/"
  python3 -c '
import pathlib, sys
path = pathlib.Path(sys.argv[1])
path.write_text(path.read_text().replace("version: \"1.0.0\"", "version: \"0.9.0\"", 1))
' "$files/keystone-service.yaml"
  git init -q "$files"
  git -C "$files" add .
  git_identity -C "$files" commit -q -m "file-mcp 0.9.0 vendored from the 1.0.0 tree"
  git -C "$files" tag v0.9.0
  python3 -c '
import pathlib, sys
path = pathlib.Path(sys.argv[1])
path.write_text(path.read_text().replace("version: \"0.9.0\"", "version: \"1.0.0\"", 1))
' "$files/keystone-service.yaml"
  git -C "$files" add keystone-service.yaml
  git_identity -C "$files" commit -q -m "file-mcp 1.0.0"
  git -C "$files" tag v1.0.0
  mkdir -p "$catalog"
  cat >"$catalog/catalog.yaml" <<EOF
schema_version: 1
name: e2e
description: Local packages for the e2e script.
packages:
  docs-service:
    kind: service
    summary: Docs API stand-in.
    source: file://${docs}
    versions:
      - {version: 1.0.0, ref: v1.0.0, keystone_mcp: ">=0.5.0,<2.0"}
  file-mcp:
    kind: service
    summary: file-mcp versions used to exercise upgrade.
    source: file://${files}
    versions:
      - {version: 0.9.0, ref: v0.9.0, keystone_mcp: ">=0.5.0,<2.0"}
      - {version: 1.0.0, ref: v1.0.0, keystone_mcp: ">=0.5.0,<2.0"}
EOF
  git init -q "$catalog"
  git -C "$catalog" add catalog.yaml
  git_identity -C "$catalog" commit -q -m "e2e catalog"
  git -C "$catalog" tag v1.0.0
}

scenario_docs_service_postgres_provider() {
  local started
  started="$(date +%s)"
  if ! want_postgres provider; then
    skip docs_service_postgres_provider "postgres is ${POSTGRES}"
    return 0
  fi
  build_local_catalogs
  kc catalog add "file://${WORK}/e2e-catalog"
  [[ "$KC_RC" -eq 0 ]] || fail docs_service_postgres_provider "catalog add exited ${KC_RC}"
  jq_ warning-ids <"$WORK/log/kc.out" | has_line local_source \
    || fail docs_service_postgres_provider "local_source was not warned"
  export KEYSTONE_SETUP_RESOURCE_POSTGRES=catalog
  unset KEYSTONE_SETUP_RESOURCE_S3 || true
  run_answered service apply e2e/docs-service \
    || fail docs_service_postgres_provider "apply exited ${KC_RC}"
  if jq_ missing-vars <"$WORK/log/kc.out" | has_line KEYSTONE_SETUP_RESOURCE_S3; then
    fail docs_service_postgres_provider "S3 was asked again"
  fi
  lockq resource postgres origin | has_line catalog \
    || fail docs_service_postgres_provider "postgres origin is not catalog"
  lockq service postgres role | has_line resource \
    || fail docs_service_postgres_provider "postgres provider missing"
  local migrate api mcp
  migrate="$(container_id docs-service-migrate)"
  api="$(container_id docs-service-api)"
  mcp="$(container_id docs-service-mcp)"
  [[ "$(docker inspect --format '{{.State.ExitCode}}' "$migrate")" == "0" ]] \
    || fail docs_service_postgres_provider "migrate did not exit 0"
  local restart
  restart="$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$migrate")"
  [[ -z "$restart" || "$restart" == "no" ]] || fail docs_service_postgres_provider "migrate restarts"
  [[ "$(docker inspect --format '{{.State.Health.Status}}' "$api")" == "healthy" ]] \
    || fail docs_service_postgres_provider "api is not healthy"
  network_names "$mcp" | has_line keystone-mcp-upstreams \
    || fail docs_service_postgres_provider "mcp is not on the upstream network"
  if network_names "$mcp" | has_line keystone-resources; then
    fail docs_service_postgres_provider "mcp is on the resource network"
  fi
  network_names "$api" | has_line ks-pkg-docs-service \
    || fail docs_service_postgres_provider "api is not on the package network"
  network_names "$api" | has_line keystone-resources \
    || fail docs_service_postgres_provider "api is not on the resource network"
  if network_names "$migrate" | has_line keystone-mcp-upstreams; then
    fail docs_service_postgres_provider "migrate is on the upstream network"
  fi
  env_names "$mcp" | has_line HELLO_BEARER_TOKEN || fail docs_service_postgres_provider "mcp has no bearer"
  if env_names "$api" | has_line HELLO_BEARER_TOKEN; then
    fail docs_service_postgres_provider "api has the bearer"
  fi
  env_names "$migrate" | has_line PGHOST || fail docs_service_postgres_provider "migrate has no PGHOST"
  if env_names "$mcp" | has_line PGHOST; then
    fail docs_service_postgres_provider "mcp has PGHOST"
  fi
  env_names "$api" | has_line DOCS_S3_BUCKET || fail docs_service_postgres_provider "api has no S3 bucket"
  if env_names "$migrate" | has_line DOCS_S3_BUCKET; then
    fail docs_service_postgres_provider "migrate has S3"
  fi
  mcp_check hello docs || fail docs_service_postgres_provider "docs client failed"
  python3 -c '
import pathlib, sys
root = pathlib.Path(sys.argv[1])

def bucket(names):
    for name in names:
        path = root / name
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            if line.startswith("DOCS_S3_BUCKET=") or line.startswith("FILE_MCP_S3_BUCKET="):
                return line.split("=", 1)[1]
    return ""

docs = bucket(["packages/docs-service.s3.env", "packages/docs-service.env"])
files = bucket(["packages/file-mcp.s3.env", "packages/file-mcp.env"])
if not docs or not files or docs == files:
    raise SystemExit(1)
' "$ROOT" || fail docs_service_postgres_provider "the two S3 buckets are not distinct"
  pass docs_service_postgres_provider "$started"
}

write_override() {
  cat >"$ROOT/packages/file-mcp.override.yml" <<'EOF'
services:
  file-mcp-mcp:
    mem_limit: 256m
    environment:
      FILE_MCP_LOG_LEVEL: DEBUG
EOF
}

scenario_override_survives_upgrade() {
  local started before after limit
  started="$(date +%s)"
  if ! want_storage minio; then
    skip override_survives_upgrade "file-mcp catalog install did not run"
    return 0
  fi
  if [[ ! -d "$WORK/e2e-catalog" ]]; then
    build_local_catalogs
    kc catalog add "file://${WORK}/e2e-catalog" || fail override_survives_upgrade "catalog add failed"
  fi
  export KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE=yes
  kc service remove file-mcp
  [[ "$KC_RC" -eq 0 ]] || fail override_survives_upgrade "remove exited ${KC_RC}"
  unset KEYSTONE_SETUP_SERVICE_FILE_MCP_CONFIRM_REMOVE
  export_file_public
  export KEYSTONE_SETUP_RESOURCE_S3=catalog
  run_answered service apply e2e/file-mcp@0.9.0 \
    || fail override_survives_upgrade "apply 0.9.0 exited ${KC_RC}"
  write_override
  compose up -d --force-recreate file-mcp-mcp
  kc check
  [[ "$KC_RC" -eq 0 ]] || fail override_survives_upgrade "check exited ${KC_RC}"
  local id
  id="$(container_id file-mcp-mcp)"
  limit="$(docker inspect --format '{{.HostConfig.Memory}}' "$id")"
  [[ "$limit" == "268435456" ]] || fail override_survives_upgrade "memory limit is not 256m"
  env_names "$id" | has_line FILE_MCP_LOG_LEVEL || fail override_survives_upgrade "log level missing"
  kc service plan e2e/file-mcp@1.0.0 || true
  [[ -f "$ROOT/.keystonectl/packages/e2e/file-mcp/1.0.0/keystone-service.yaml" ]] \
    || fail override_survives_upgrade "1.0.0 is not in the cache"
  before="$(sha_file "$ROOT/packages/file-mcp.override.yml")"
  local old_fragment
  old_fragment="$(lockq service file-mcp fragment_sha256)"
  UPGRADE_ACCEPT="1.0.0"
  run_answered service upgrade file-mcp@1.0.0 \
    || fail override_survives_upgrade "upgrade exited ${KC_RC}"
  after="$(sha_file "$ROOT/packages/file-mcp.override.yml")"
  [[ "$before" == "$after" ]] || fail override_survives_upgrade "override file changed"
  id="$(container_id file-mcp-mcp)"
  limit="$(docker inspect --format '{{.HostConfig.Memory}}' "$id")"
  [[ "$limit" == "268435456" ]] || fail override_survives_upgrade "limit did not survive upgrade"
  local new_fragment file_hash
  new_fragment="$(lockq service file-mcp fragment_sha256)"
  file_hash="$(sha_file "$ROOT/packages/file-mcp.yml")"
  [[ "$new_fragment" == "$file_hash" && "$new_fragment" != "$old_fragment" ]] \
    || fail override_survives_upgrade "fragment hash did not move"
  mcp_check files || fail override_survives_upgrade "files client failed"
  pass override_survives_upgrade "$started"
}

scenario_direct_edit_is_caught() {
  local started copy
  started="$(date +%s)"
  [[ -f "$ROOT/packages/file-mcp.yml" ]] || { skip direct_edit_is_caught "file-mcp is not installed"; return 0; }
  copy="$WORK/state/file-mcp.yml"
  cp "$ROOT/packages/file-mcp.yml" "$copy"
  printf '\n# e2e-marker\n' >>"$ROOT/packages/file-mcp.yml"
  kc check
  jq_ warning-ids <"$WORK/log/kc.out" | has_line fragment_modified \
    || fail direct_edit_is_caught "check did not report fragment_modified"
  kc service upgrade file-mcp@1.0.0 || true
  [[ "$(jq_ error-id <"$WORK/log/kc.out")" == "fragment_modified" ]] \
    || fail direct_edit_is_caught "upgrade did not refuse"
  run_answered service apply file-mcp || true
  [[ "$(jq_ error-id <"$WORK/log/kc.out")" == "fragment_modified" ]] \
    || fail direct_edit_is_caught "apply did not refuse"
  grep -q e2e-marker "$ROOT/packages/file-mcp.yml" \
    || fail direct_edit_is_caught "the fragment was rewritten"
  cp "$copy" "$ROOT/packages/file-mcp.yml"
  kc check
  if jq_ warning-ids <"$WORK/log/kc.out" | has_line fragment_modified; then
    fail direct_edit_is_caught "fragment_modified remains"
  fi
  pass direct_edit_is_caught "$started"
}

scenario_eject_and_adopt() {
  local started
  started="$(date +%s)"
  [[ -f "$ROOT/packages/file-mcp.yml" ]] || { skip eject_and_adopt "file-mcp is not installed"; return 0; }
  kc service eject file-mcp
  [[ "$KC_RC" -eq 0 ]] || fail eject_and_adopt "eject exited ${KC_RC}"
  lockq service file-mcp managed | has_line ejected || fail eject_and_adopt "lock is not ejected"
  printf '\n# com.example.e2e: ejected\n' >>"$ROOT/packages/file-mcp.yml"
  kc check
  if jq_ warning-ids <"$WORK/log/kc.out" | has_line fragment_modified; then
    fail eject_and_adopt "ejected fragment was treated as a direct edit"
  fi
  kc service status file-mcp
  [[ "$KC_RC" -eq 0 ]] || fail eject_and_adopt "status exited ${KC_RC}"
  run_answered service apply file-mcp || fail eject_and_adopt "apply exited ${KC_RC}"
  grep -q 'com.example.e2e: ejected' "$ROOT/packages/file-mcp.yml" \
    || fail eject_and_adopt "apply rewrote the ejected fragment"
  UPGRADE_ACCEPT="yes"
  run_answered service adopt file-mcp || fail eject_and_adopt "adopt exited ${KC_RC}"
  lockq service file-mcp managed | has_line generated || fail eject_and_adopt "lock is not generated"
  if grep -q 'com.example.e2e: ejected' "$ROOT/packages/file-mcp.yml"; then
    fail eject_and_adopt "adopt left the ejected marker"
  fi
  local hash file_hash
  hash="$(lockq service file-mcp fragment_sha256)"
  file_hash="$(sha_file "$ROOT/packages/file-mcp.yml")"
  [[ "$hash" == "$file_hash" ]] || fail eject_and_adopt "fragment hash does not match"
  mcp_check files || fail eject_and_adopt "files client failed"
  pass eject_and_adopt "$started"
}

scenario_remove_and_reinstall() {
  local started
  started="$(date +%s)"
  [[ -f "$ROOT/packages/file-mcp.yml" || -f "$ROOT/packages/file-mcp.override.yml" ]] \
    || { skip remove_and_reinstall "file-mcp is not installed"; return 0; }
  run_answered service remove file-mcp || fail remove_and_reinstall "remove exited ${KC_RC}"
  jq_ kept <"$WORK/log/kc.out" | has_prefix "bucket " || fail remove_and_reinstall "bucket was not kept"
  jq_ kept <"$WORK/log/kc.out" | has_line "packages/file-mcp.override.yml" \
    || fail remove_and_reinstall "override was not reported as kept"
  if ! jq_ kept <"$WORK/log/kc.out" | has_prefix "ks-pkg-"; then
    note "file-mcp 1.0.0 declares no volumes, so remove names the bucket and the override without a volume"
  fi
  if grep -q 'name: file-mcp' "$ROOT/keystonectl/upstreams.yaml"; then
    fail remove_and_reinstall "file-mcp is still in the registry"
  fi
  if grep -q 'files_' "$ROOT/keystonectl/catalog-snapshot.yaml"; then
    fail remove_and_reinstall "files tools are still in the snapshot"
  fi
  if [[ -e "/etc/nginx/sites-enabled/keystone-svc-file-mcp" ]]; then
    fail remove_and_reinstall "nginx site is still enabled"
  fi
  [[ -f "$ROOT/packages/file-mcp.override.yml" ]] || fail remove_and_reinstall "override file was deleted"
  mcp_check absent files || fail remove_and_reinstall "files tools are still present"
  export_file_public
  run_answered service apply file-mcp || fail remove_and_reinstall "reinstall exited ${KC_RC}"
  mcp_check files || fail remove_and_reinstall "files client failed after reinstall"
  pass remove_and_reinstall "$started"
}

start_external_s3() {
  local access secret root_user root_secret
  access="$(rand_hex 8)"
  secret="$(rand_hex 16)"
  root_user="$(rand_hex 8)"
  root_secret="$(rand_hex 16)"
  write_secret "$WORK/secrets/s3-access" "$access"
  write_secret "$WORK/secrets/s3-secret" "$secret"
  write_secret "$WORK/secrets/s3-secret-bad" "not-the-secret"
  umask 077
  cat >"$WORK/secrets/minio-root.env" <<EOF
MINIO_ROOT_USER=${root_user}
MINIO_ROOT_PASSWORD=${root_secret}
EOF
  cat >"$WORK/secrets/s3-user.env" <<EOF
S3_ACCESS=${access}
S3_SECRET=${secret}
EOF
  umask 022
  chmod 600 "$WORK/secrets/minio-root.env" "$WORK/secrets/s3-user.env"
  ensure_network keystone-resources
  docker run -d --name ks-cat-e2e-external-s3 \
    --network keystone-resources --network-alias e2e-external-s3 \
    --env-file "$WORK/secrets/minio-root.env" \
    "$MINIO_IMAGE" server /data >/dev/null
  local attempt=0
  while [[ "$attempt" -lt 30 ]]; do
    if docker run --rm --network keystone-resources --env-file "$WORK/secrets/minio-root.env" \
      --entrypoint sh "$MC_IMAGE" -c \
      'mc alias set local http://e2e-external-s3:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc ready local' \
      >/dev/null 2>&1; then
      break
    fi
    sleep 2
    attempt="$((attempt + 1))"
  done
  [[ "$attempt" -lt 30 ]] || return 1
  cat >"$WORK/secrets/s3-policy.json" <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:*"],"Resource":["arn:aws:s3:::e2e-files","arn:aws:s3:::e2e-files/*","arn:aws:s3:::e2e-docs","arn:aws:s3:::e2e-docs/*"]}]}
EOF
  chmod 600 "$WORK/secrets/s3-policy.json"
  docker run --rm --network keystone-resources \
    --env-file "$WORK/secrets/minio-root.env" --env-file "$WORK/secrets/s3-user.env" \
    -v "$WORK/secrets/s3-policy.json:/policy.json:ro" \
    --entrypoint sh "$MC_IMAGE" -c '
      mc alias set local http://e2e-external-s3:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
      mc mb --ignore-existing local/e2e-files >/dev/null
      mc mb --ignore-existing local/e2e-docs >/dev/null
      mc admin user add local "$S3_ACCESS" "$S3_SECRET" >/dev/null
      mc admin policy create local e2e-files /policy.json >/dev/null
      mc admin policy attach local e2e-files --user "$S3_ACCESS" >/dev/null
    '
}

scenario_external_s3() {
  local started
  started="$(date +%s)"
  if ! want_storage external; then
    skip external_s3 "storage is ${STORAGE}"
    return 0
  fi
  if lockq service file-mcp managed | has_line missing; then
    :
  else
    run_answered service remove file-mcp || fail external_s3 "remove file-mcp exited ${KC_RC}"
  fi
  if ! lockq service docs-service managed | has_line missing; then
    run_answered service remove docs-service || fail external_s3 "remove docs-service exited ${KC_RC}"
  fi
  if ! lockq service minio managed | has_line missing; then
    run_answered service remove minio || fail external_s3 "remove minio exited ${KC_RC}"
  fi
  start_external_s3 || fail external_s3 "external MinIO did not become ready"
  export KEYSTONE_SETUP_RESOURCE_S3=external
  export KEYSTONE_SETUP_S3_ENDPOINT="http://e2e-external-s3:9000"
  export KEYSTONE_SETUP_S3_BUCKET=e2e-files
  export KEYSTONE_SETUP_S3_REGION=us-east-1
  export KEYSTONE_SETUP_S3_PATH_STYLE=true
  export KEYSTONE_SETUP_S3_ACCESS_KEY_FILE="$WORK/secrets/s3-access"
  export KEYSTONE_SETUP_S3_SECRET_KEY_FILE="$WORK/secrets/s3-secret-bad"
  export_file_public
  run_answered service apply file-mcp || true
  [[ "$KC_RC" -eq 3 ]] || fail external_s3 "bad secret exited ${KC_RC}"
  [[ "$(jq_ retryable <"$WORK/log/kc.out")" == "false" ]] || fail external_s3 "bad secret is retryable"
  jq_ error-id <"$WORK/log/kc.out" | has_prefix s3 || fail external_s3 "error is not an S3 failure"
  export KEYSTONE_SETUP_S3_SECRET_KEY_FILE="$WORK/secrets/s3-secret"
  run_answered service apply file-mcp || fail external_s3 "good secret exited ${KC_RC}"
  jq_ warning-ids <"$WORK/log/kc.out" | has_line s3_plain_http \
    || fail external_s3 "http warning missing"
  mcp_check files || fail external_s3 "files client failed"
  lockq resource s3 origin | has_line external || fail external_s3 "origin is not external"
  if lockq resource s3 provider | has_prefix ""; then
    fail external_s3 "external s3 still names a provider"
  fi
  pass external_s3 "$started"
}

start_external_pg() {
  local admin_pw app_pw
  admin_pw="$(rand_hex 16)"
  app_pw="$(rand_hex 16)"
  write_secret "$WORK/secrets/pg-password" "$app_pw"
  umask 077
  cat >"$WORK/secrets/pg-admin.env" <<EOF
POSTGRES_PASSWORD=${admin_pw}
PGPASSWORD=${admin_pw}
EOF
  printf 'postgresql://docs:%s@e2e-external-pg:5432/docs?sslmode=disable\n' "$app_pw" \
    >"$WORK/secrets/pg-url"
  chmod 600 "$WORK/secrets/pg-admin.env" "$WORK/secrets/pg-url"
  umask 022
  ensure_network keystone-resources
  docker run -d --name ks-cat-e2e-external-pg \
    --network keystone-resources --network-alias e2e-external-pg \
    --env-file "$WORK/secrets/pg-admin.env" \
    -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres \
    "$PG_IMAGE" >/dev/null
  local attempt=0
  while [[ "$attempt" -lt 30 ]]; do
    if docker run --rm --network keystone-resources --entrypoint pg_isready "$PG_IMAGE" \
      -h e2e-external-pg -U postgres >/dev/null 2>&1; then
      break
    fi
    sleep 2
    attempt="$((attempt + 1))"
  done
  [[ "$attempt" -lt 30 ]] || return 1
  docker run --rm --network keystone-resources \
    --env-file "$WORK/secrets/pg-admin.env" \
    -v "$WORK/secrets/pg-password:/pw:ro" \
    --entrypoint sh "$PG_IMAGE" -c '
      pw="$(cat /pw)"
      psql -h e2e-external-pg -U postgres -d postgres -v ON_ERROR_STOP=1 \
        -c "CREATE USER docs WITH PASSWORD '"'"'$pw'"'"'" \
        -c "CREATE DATABASE docs OWNER docs"
    '
}

scenario_external_postgres() {
  local started
  started="$(date +%s)"
  if ! want_postgres external; then
    skip external_postgres "postgres is ${POSTGRES}"
    return 0
  fi
  if ! lockq service docs-service managed | has_line missing; then
    run_answered service remove docs-service || fail external_postgres "remove docs-service exited ${KC_RC}"
  fi
  if ! lockq service postgres managed | has_line missing; then
    run_answered service remove postgres || fail external_postgres "remove postgres exited ${KC_RC}"
  fi
  start_external_pg || fail external_postgres "external Postgres did not become ready"
  export KEYSTONE_SETUP_RESOURCE_POSTGRES=external
  export KEYSTONE_SETUP_POSTGRES_HOST=e2e-external-pg
  export KEYSTONE_SETUP_POSTGRES_PORT=5432
  export KEYSTONE_SETUP_POSTGRES_DATABASE=docs
  export KEYSTONE_SETUP_POSTGRES_USER=docs
  export KEYSTONE_SETUP_POSTGRES_SSLMODE=disable
  export KEYSTONE_SETUP_POSTGRES_PASSWORD_FILE="$WORK/secrets/pg-password"
  write_secret "$WORK/secrets/pg-password-bad" "not-the-password"
  export KEYSTONE_SETUP_POSTGRES_PASSWORD_FILE="$WORK/secrets/pg-password-bad"
  unset KEYSTONE_SETUP_POSTGRES_URL_FILE || true
  run_answered service apply e2e/docs-service || true
  [[ "$KC_RC" -eq 3 ]] || fail external_postgres "bad password exited ${KC_RC}"
  [[ "$(jq_ retryable <"$WORK/log/kc.out")" == "false" ]] || fail external_postgres "bad password is retryable"
  jq_ error-id <"$WORK/log/kc.out" | has_prefix postgres \
    || fail external_postgres "error is not a Postgres failure"
  export KEYSTONE_SETUP_POSTGRES_PASSWORD_FILE="$WORK/secrets/pg-password"
  run_answered service apply e2e/docs-service || fail external_postgres "good password exited ${KC_RC}"
  local migrate api
  migrate="$(container_id docs-service-migrate)"
  api="$(container_id docs-service-api)"
  [[ "$(docker inspect --format '{{.State.ExitCode}}' "$migrate")" == "0" ]] \
    || fail external_postgres "migrate did not exit 0"
  [[ "$(docker inspect --format '{{.State.Health.Status}}' "$api")" == "healthy" ]] \
    || fail external_postgres "api is not healthy"
  lockq resource postgres origin | has_line external || fail external_postgres "origin is not external"
  run_answered service remove docs-service || fail external_postgres "remove before URL form exited ${KC_RC}"
  unset KEYSTONE_SETUP_POSTGRES_HOST KEYSTONE_SETUP_POSTGRES_PORT \
    KEYSTONE_SETUP_POSTGRES_DATABASE KEYSTONE_SETUP_POSTGRES_USER \
    KEYSTONE_SETUP_POSTGRES_SSLMODE KEYSTONE_SETUP_POSTGRES_PASSWORD_FILE
  export KEYSTONE_SETUP_POSTGRES_URL_FILE="$WORK/secrets/pg-url"
  run_answered service apply e2e/docs-service || fail external_postgres "URL form exited ${KC_RC}"
  pass external_postgres "$started"
}

scenario_partner_catalog() {
  local started cache hello_source hello_ref file_source file_ref
  started="$(date +%s)"
  cache="$ROOT/.keystonectl/catalogs/keystone-codes/catalog.yaml"
  [[ -f "$cache" ]] || fail partner_catalog "keystone-codes catalog is not cached"
  hello_source="$(uv run --project "$REPO" python -c 'import yaml,sys; d=yaml.safe_load(open(sys.argv[1])); print(d["packages"]["hello-upstream"]["source"])' "$cache")"
  hello_ref="$(uv run --project "$REPO" python -c 'import yaml,sys; d=yaml.safe_load(open(sys.argv[1])); print(d["packages"]["hello-upstream"]["versions"][0]["ref"])' "$cache")"
  file_source="$(uv run --project "$REPO" python -c 'import yaml,sys; d=yaml.safe_load(open(sys.argv[1])); print(d["packages"]["file-mcp"]["source"])' "$cache")"
  file_ref="$(uv run --project "$REPO" python -c 'import yaml,sys; d=yaml.safe_load(open(sys.argv[1])); print(d["packages"]["file-mcp"]["versions"][0]["ref"])' "$cache")"
  mkdir -p "$WORK/partner-catalog"
  sed \
    -e "s|@HELLO_SOURCE@|${hello_source}|" \
    -e "s|@HELLO_REF@|${hello_ref}|" \
    -e "s|@FILE_SOURCE@|${file_source}|" \
    -e "s|@FILE_REF@|${file_ref}|" \
    "$HERE/e2e/fixtures/partner-catalog.yaml.tmpl" >"$WORK/partner-catalog/catalog.yaml"
  git init -q "$WORK/partner-catalog"
  git -C "$WORK/partner-catalog" add catalog.yaml
  git_identity -C "$WORK/partner-catalog" commit -q -m "partner catalog"
  git -C "$WORK/partner-catalog" tag v1.0.0
  kc catalog add "file://${WORK}/partner-catalog"
  [[ "$KC_RC" -eq 0 ]] || fail partner_catalog "catalog add exited ${KC_RC}"
  jq_ warning-ids <"$WORK/log/kc.out" | has_line local_source \
    || fail partner_catalog "local_source was not warned"
  kc service plan file-mcp || true
  [[ "$KC_RC" -eq 2 ]] || fail partner_catalog "plan exited ${KC_RC}"
  jq_ missing-kind <"$WORK/log/kc.out" | has_line argument \
    || fail partner_catalog "missing kind is not argument"
  jq_ missing-choices <"$WORK/log/kc.out" | has_line keystone-codes/file-mcp \
    || fail partner_catalog "keystone-codes choice missing"
  jq_ missing-choices <"$WORK/log/kc.out" | has_line e2e-partner/file-mcp \
    || fail partner_catalog "partner choice missing"
  run_answered service apply e2e-partner/hello-upstream \
    || fail partner_catalog "hello apply exited ${KC_RC}"
  mcp_check hello hello || fail partner_catalog "hello client failed"
  pass partner_catalog "$started"
}

file_mcp_public_port() {
  uv run --project "$REPO" python -c '
import yaml, sys
doc = yaml.safe_load(open(sys.argv[1]))
for item in doc.get("services") or []:
    if item.get("name") == "file-mcp":
        port = int((item.get("public") or {}).get("host_port") or 0)
        if 47600 <= port <= 47699:
            print(port)
            raise SystemExit(0)
        raise SystemExit(1)
raise SystemExit(1)
' "$ROOT/services.lock.yaml"
}

start_manual_caddy() {
  local port config pid attempt
  # The production example Caddyfile is the file Caddy loads. The site serves
  # only the public prefixes, with this run's domain and loopback port.
  [[ -f "$ROOT/Caddyfile" ]] || return 1
  if ! grep -q 'reverse_proxy' "$ROOT/Caddyfile"; then
    return 1
  fi
  command -v caddy >/dev/null 2>&1 || return 1
  port="$(file_mcp_public_port)" || return 1
  case "$port" in
    ''|*[!0-9]*) return 1 ;;
  esac
  case "$DOMAIN" in
    ''|*[!A-Za-z0-9.-]*) return 1 ;;
  esac
  config="$ROOT/Caddyfile"
  cat >"$config" <<EOF
{
    admin off
}

${DOMAIN} {
    handle /u/* {
        reverse_proxy 127.0.0.1:${port}
    }
    handle /d/* {
        reverse_proxy 127.0.0.1:${port}
    }
    handle /healthz {
        reverse_proxy 127.0.0.1:${port}
    }
    handle {
        respond 404
    }
}
EOF
  caddy run --config "$config" >"$WORK/log/caddy.log" 2>&1 &
  pid="$!"
  printf '%s\n' "$pid" >"$WORK/state/caddy.pid"
  attempt=0
  while [[ "$attempt" -lt 45 ]]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 1
    fi
    if curl -fsS --max-time 2 "https://${DOMAIN}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
    attempt="$((attempt + 1))"
  done
  return 1
}

install_manual_vhost() {
  local sample="$ROOT/.keystonectl/nginx/keystone-svc-file-mcp.conf"
  [[ -f "$sample" ]] || return 1
  local dest="/etc/nginx/sites-available/keystone-svc-file-mcp"
  sudo cp "$sample" "$dest"
  sudo python3 -c '
import pathlib, sys
path, cert, key = sys.argv[1:]
text = pathlib.Path(path).read_text()
text = text.replace("/etc/ssl/keystone/files.e2e.test/fullchain.pem", cert)
text = text.replace("/etc/ssl/keystone/files.e2e.test/privkey.pem", key)
pathlib.Path(path).write_text(text)
' "$dest" "$WORK/tls/cert.pem" "$WORK/tls/key.pem"
  sudo ln -sfn "$dest" /etc/nginx/sites-enabled/keystone-svc-file-mcp
  printf '%s\n' /etc/nginx/sites-enabled/keystone-svc-file-mcp >"$WORK/state/nginx-site"
  sudo nginx -t
  sudo nginx -s reload || sudo service nginx reload
}

scenario_tls_manual() {
  local started log
  started="$(date +%s)"
  if [[ "$LAYOUT" != "production-example" ]]; then
    skip tls_manual "only the production example"
    return 0
  fi
  if ! lockq service file-mcp managed | has_line missing; then
    run_answered service remove file-mcp || fail tls_manual "remove exited ${KC_RC}"
  fi
  export KEYSTONE_SETUP_SERVICE_FILE_MCP_DOMAIN="$DOMAIN"
  export KEYSTONE_SETUP_SERVICE_FILE_MCP_DNS=skip
  export KEYSTONE_SETUP_SERVICE_FILE_MCP_TLS=manual
  unset KEYSTONE_SETUP_SERVICE_FILE_MCP_CERT_PATH KEYSTONE_SETUP_SERVICE_FILE_MCP_KEY_PATH || true
  : >"$WORK/log/runner.debug"
  KC_RC=0
  KC_OUT="$(uv run --project "$REPO" python -c '
import logging, sys
logging.basicConfig(level=logging.DEBUG, format="%(message)s")
from keystonectl.cli import main
sys.argv = [
    "keystonectl", "--root", sys.argv[1],
    "service", "apply", "file-mcp", "--json", "--non-interactive",
]
raise SystemExit(main())
' "$ROOT" 2>>"$WORK/log/runner.debug")" || KC_RC=$?
  printf '%s\n' "$KC_OUT" >"$WORK/log/kc.out"
  log="$WORK/log/runner.debug"
  if grep -E "run \['(sudo|nginx|certbot)'" "$log" >/dev/null 2>&1; then
    fail tls_manual "the executor invoked sudo, nginx, or certbot"
  fi
  [[ "$KC_RC" -eq 3 ]] || fail tls_manual "first apply exited ${KC_RC}"
  [[ "$(jq_ error-id <"$WORK/log/kc.out")" == "public_unreachable" ]] \
    || fail tls_manual "error is not public_unreachable"
  [[ "$(jq_ retryable <"$WORK/log/kc.out")" == "true" ]] || fail tls_manual "not retryable"
  [[ "$(jq_ public-state <"$WORK/log/kc.out")" == "pending" ]] || fail tls_manual "public state is not pending"
  file_mcp_public_port >/dev/null \
    || fail tls_manual "loopback port is outside 47600-47699"
  status_file_mcp || fail tls_manual "service is not running while public is pending"
  if [[ "$PROFILE" == "ci" ]]; then
    install_manual_vhost || fail tls_manual "could not install the sample vhost"
    export SSL_CERT_FILE="$WORK/tls/cert.pem"
  else
    start_manual_caddy || fail tls_manual "could not start Caddy"
  fi
  run_answered service apply file-mcp || fail tls_manual "second apply exited ${KC_RC}"
  [[ "$(jq_ public-state <"$WORK/log/kc.out")" == "ready" ]] || fail tls_manual "public state is not ready"
  [[ "$(jq_ public-tls <"$WORK/log/kc.out")" == "manual" ]] || fail tls_manual "tls is not manual"
  mcp_check files || fail tls_manual "files client failed"
  pass tls_manual "$started"
}

scenario_tls_certbot() {
  if [[ "$PROFILE" != "vm" || "$TLS" != "certbot" ]]; then
    skip tls_certbot "certbot runs only on the vm profile"
    return 0
  fi
  local started issuer
  started="$(date +%s)"
  issuer="$(echo | openssl s_client -connect "${DOMAIN}:443" -servername "$DOMAIN" 2>/dev/null \
    | openssl x509 -noout -issuer)"
  [[ "$issuer" == *"Let's Encrypt"* ]] || fail tls_certbot "issuer is not Let's Encrypt"
  kc service status file-mcp
  [[ "$KC_RC" -eq 0 ]] || fail tls_certbot "status exited ${KC_RC}"
  local before after
  before="$(wc -c </var/log/letsencrypt/letsencrypt.log || echo 0)"
  run_answered service apply file-mcp || fail tls_certbot "repeat apply exited ${KC_RC}"
  after="$(wc -c </var/log/letsencrypt/letsencrypt.log || echo 0)"
  [[ "$before" == "$after" ]] || fail tls_certbot "certbot issued again"
  pass tls_certbot "$started"
}

scenario_check_clean() {
  local started id
  started="$(date +%s)"
  kc check
  [[ "$KC_RC" -eq 0 ]] || fail check_clean "check exited ${KC_RC}"
  if jq_ problems <"$WORK/log/kc.out" | has_prefix ""; then
    fail check_clean "check reported problems"
  fi
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    case "$id" in
      tls_untrusted|s3_plain_http|postgres_plain_connection) ;;
      *) fail check_clean "unexpected warning ${id}" ;;
    esac
  done < <(jq_ warning-ids <"$WORK/log/kc.out")
  pass check_clean "$started"
}

scenario_legacy_gone() {
  local started root
  started="$(date +%s)"
  if [[ "$LAYOUT" != "keystone-deploy" ]]; then
    skip legacy_gone "only keystone-deploy"
    return 0
  fi
  root="$(dirname "$ROOT")"
  if grep -RIn --exclude-dir=.git -e configure_external_apis -e write_external_compose \
    -e KEYSTONE_MCP_FILE_API_URL -e EXTERNAL_APIS "$root" \
    | grep -v -e setup.sh -e README -e '.gitignore' >/dev/null; then
    fail legacy_gone "a removed name is still in the tree"
  fi
  if grep -RIn --exclude-dir=.git docker-compose.external.yml "$root" \
    | grep -v -e '.gitignore' -e README -e setup.sh >/dev/null; then
    fail legacy_gone "docker-compose.external.yml is still referenced"
  fi
  if grep -n 'build:' "$ROOT/docker-compose.yml" | grep 'keystone-mcp' >/dev/null; then
    fail legacy_gone "keystone-mcp still has a build"
  fi
  python3 -c '
import pathlib, sys
text = pathlib.Path(sys.argv[1]).read_text()
start = text.split("keystone-mcp:", 1)[1]
block = start.split("\n\n", 1)[0]
if "build:" in block:
    raise SystemExit(1)
' "$ROOT/docker-compose.yml" || fail legacy_gone "keystone-mcp service has build"
  if [[ "$PROFILE" == "vm" ]]; then
    [[ -f "$ROOT/services.lock.yaml" ]] || fail legacy_gone "lock is missing"
    lockq layout-version | has_line 1 || fail legacy_gone "layout_version is not 1"
  fi
  pass legacy_gone "$started"
}

remove_all() {
  local name
  kc service list || return 0
  python3 -c '
import json, sys
doc = json.load(open(sys.argv[1]))
services = (doc.get("result") or {}).get("services") or doc.get("result") or []
if isinstance(services, dict):
    services = services.get("services") or []
for item in services:
    if isinstance(item, dict) and item.get("name"):
        print(item["name"])
' "$WORK/log/kc.out" | while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    export "KEYSTONE_SETUP_SERVICE_$(printf '%s' "$name" | tr '[:lower:]-' '[:upper:]_')_CONFIRM_REMOVE=yes"
    kc service remove "$name" || true
  done
}

diagnostics() {
  [[ -n "${DIAG:-}" ]] || return 0
  mkdir -p "$DIAG"
  chmod 700 "$DIAG"
  if [[ -d "${WORK:-}/log" ]]; then
    kc service status || true
    cp "$WORK/log/kc.out" "$DIAG/status.json" 2>/dev/null || true
    kc check || true
    cp "$WORK/log/kc.out" "$DIAG/check.json" 2>/dev/null || true
  fi
  if [[ -n "$ROOT" && -f "$ROOT/docker-compose.yml" ]]; then
    compose logs --tail 100 >"$DIAG/compose.log" 2>&1 || true
  fi
  if [[ -f "${WORK:-}/log/caddy.log" ]]; then
    cp "$WORK/log/caddy.log" "$DIAG/caddy.log" 2>/dev/null || true
  fi
  if command -v nginx >/dev/null 2>&1; then
    { sudo nginx -t || true; } >"$DIAG/nginx.txt" 2>&1
  fi
  chmod 600 "$DIAG"/* 2>/dev/null || true
}

cleanup() {
  if [[ "$CLEANED" == "true" ]]; then
    return 0
  fi
  CLEANED="true"
  if [[ "$PROFILE" == "vm" && "$KEEP" == "true" ]]; then
    return 0
  fi
  if [[ -n "$ROOT" && -f "$ROOT/services.lock.yaml" ]]; then
    remove_all || true
    compose down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  local name
  for name in ks-cat-e2e-redis ks-cat-e2e-external-s3 ks-cat-e2e-external-pg; do
    if docker inspect "$name" >/dev/null 2>&1; then
      docker rm -f "$name" >/dev/null 2>&1 || true
    fi
  done
  if [[ -f "${WORK:-}/state/caddy.pid" ]]; then
    local caddy_pid
    caddy_pid="$(cat "${WORK}/state/caddy.pid")"
    if [[ -n "$caddy_pid" ]]; then
      kill "$caddy_pid" >/dev/null 2>&1 || true
    fi
  fi
  if [[ -f "${WORK:-}/state/nginx-site" ]]; then
    sudo rm -f "$(cat "$WORK/state/nginx-site")" || true
    sudo nginx -s reload >/dev/null 2>&1 || true
  fi
  if [[ -f "${WORK:-}/state/hosts-line" ]]; then
    local line
    line="$(cat "$WORK/state/hosts-line")"
    sudo python3 -c '
import pathlib, sys
path = pathlib.Path("/etc/hosts")
line = sys.argv[1]
kept = [item for item in path.read_text().splitlines() if item != line]
path.write_text("\n".join(kept) + "\n")
' "$line" || true
  fi
  if [[ -n "${WORK:-}" && -d "$WORK" ]]; then
    # Provision files are owned by container users. Take them back, then remove the tree.
    if [[ "$(id -u)" -ne 0 ]]; then
      sudo chown -R "$(id -u):$(id -g)" "$WORK" || true
    fi
    rm -rf "$WORK"
  fi
}

main() {
  mkdir -p "$(dirname "$REPORT")"
  : >"$REPORT"
  if [[ "$PUBLISH_PORT" != "8080" ]]; then
    note "publish port is ${PUBLISH_PORT} because 8080 is not used on this machine"
  fi
  note "production-example.patch has no diff; layout A is not edited"
  note "file-mcp has no published 0.9.0 tag; the local catalog vendors 0.9.0 from the 1.0.0 tree and the same image digest"
  note "sources.yaml pins catalog ${CATALOG_REF}; the product default ref v1.0.0 is not a tag on keystone-catalog"
  note "containers created by this script are named ks-cat-e2e-*; network aliases stay redis, e2e-external-s3, and e2e-external-pg"
  WORK="$(mktemp -d /tmp/ks-cat-e2e.XXXXXX)"
  DIAG="${REPO}/e2e-diagnostics"
  mkdir -p "$WORK/secrets" "$WORK/log" "$WORK/state" "$WORK/src"
  chmod 700 "$WORK" "$WORK/secrets" "$WORK/log"
  verify_image_pins
  uv sync --frozen --project "$REPO"
  if [[ "$PROFILE" == "ci" ]]; then
    prepare_host
  fi
  install_keystonectl
  prepare_layout
  issue_api_key
  if [[ -f "$ROOT/policy/rbac-policy.yaml" ]]; then
    POLICY_SHA="$(sha_file "$ROOT/policy/rbac-policy.yaml")"
  else
    POLICY_SHA=""
  fi
  scenario_agent_path_missing
  scenario_install_file_mcp_minio
  scenario_policy_untouched
  scenario_rerun_no_questions
  scenario_docs_service_postgres_provider
  scenario_override_survives_upgrade
  scenario_direct_edit_is_caught
  scenario_eject_and_adopt
  scenario_remove_and_reinstall
  scenario_external_s3
  scenario_external_postgres
  scenario_partner_catalog
  scenario_tls_manual
  scenario_tls_certbot
  scenario_check_clean
  scenario_legacy_gone
  cleanup
}

verify_image_pins() {
  grep -F -q "$FILE_MCP_IMAGE" "$HERE/e2e/fixtures/file-mcp/keystone-service.yaml" \
    || fail prepare "file-mcp fixture does not match FILE_MCP_IMAGE"
  grep -F -q "$HELLO_IMAGE" "$REPO/tests/e2e/packages/docs-service/keystone-service.yaml" \
    || fail prepare "docs-service fixture does not match HELLO_IMAGE"
}

on_exit() {
  status=$?
  if [[ "$status" -ne 0 ]]; then
    diagnostics || true
    cleanup || true
  fi
}

trap on_exit EXIT
main "$@"
trap - EXIT
