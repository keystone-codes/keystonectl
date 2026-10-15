#!/usr/bin/env bats

# Run each skill scenario against fake keystonectl.

setup() {
  REPO="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd)"
  export REPO
  WORK="$BATS_TEST_TMPDIR/work"
  mkdir -p "$WORK/bin"
  export WORK
  ln -s "$REPO/skills/keystonectl/tests/fake-keystonectl" "$WORK/bin/keystonectl"
  export PATH="$WORK/bin:$PATH"
  export FAKE_JOURNAL="$WORK/journal"
  export FAKE_COUNTER="$WORK/counter"
  export FAKE_FIXTURES="$REPO/skills/keystonectl/tests/fixtures"
  printf '1\n' >"$FAKE_COUNTER"
  : >"$FAKE_JOURNAL"
  local name
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    unset "$name"
  done < <(env | awk -F= '/^KEYSTONE_SETUP_/ {print $1}')
}

run_scenario() {
  local name="$1"
  export FAKE_SCENARIO="$name"
  export SCENARIO_DIR="$FAKE_FIXTURES/$name"
  printf '1\n' >"$FAKE_COUNTER"
  : >"$FAKE_JOURNAL"
  if [[ -f "$SCENARIO_DIR/answers.env" ]]; then
    set -a
    # shellcheck disable=SC1090,SC1091
    source "$SCENARIO_DIR/answers.env"
    set +a
  fi
  if [[ -f "$SCENARIO_DIR/secrets/pg-url" ]]; then
    install -m 600 "$SCENARIO_DIR/secrets/pg-url" "$WORK/pg-url"
    export KEYSTONE_SETUP_POSTGRES_URL_FILE="$WORK/pg-url"
  fi
  if [[ -f "$SCENARIO_DIR/secrets/s3-access-key" ]]; then
    install -m 600 "$SCENARIO_DIR/secrets/s3-access-key" "$WORK/s3-access-key"
    export KEYSTONE_SETUP_S3_ACCESS_KEY_FILE="$WORK/s3-access-key"
  fi
  if [[ -f "$SCENARIO_DIR/secrets/s3-secret-key" ]]; then
    install -m 600 "$SCENARIO_DIR/secrets/s3-secret-key" "$WORK/s3-secret-key"
    export KEYSTONE_SETUP_S3_SECRET_KEY_FILE="$WORK/s3-secret-key"
  fi
  "$REPO/skills/keystonectl/tests/run-scenario.sh"
  python3 "$REPO/skills/keystonectl/tests/assert-scenario.py" "$name" "$FAKE_JOURNAL"
}

@test "file-mcp-minio" {
  run_scenario file-mcp-minio
}

@test "bundle-install" {
  run_scenario bundle-install
}

@test "docs-service-external-postgres" {
  run_scenario docs-service-external-postgres
}

@test "dns-retry" {
  run_scenario dns-retry
}

@test "eject" {
  run_scenario eject
}

@test "remove" {
  run_scenario remove
}

@test "atlassian-profiles" {
  run_scenario atlassian-profiles
}
