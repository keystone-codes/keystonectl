#!/usr/bin/env bash
# Smoke-test keystonectl installed from a built wheel (no Docker, no network).
#
# Usage: scripts/smoke_wheel.sh KEYSTONECTL_BIN
set -euo pipefail

BIN="${1:?Usage: scripts/smoke_wheel.sh KEYSTONECTL_BIN}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

run_capture() {
  local label=$1
  shift
  local out err code
  out="$(mktemp)"
  err="$(mktemp)"
  set +e
  "$@" >"$out" 2>"$err"
  code=$?
  set -e
  if [ "$code" -ne 0 ]; then
    log "--- ${label} stdout ---"
    cat "$out" || true
    log "--- ${label} stderr ---"
    cat "$err" || true
    die "${label} failed with exit code ${code}"
  fi
  cat "$out"
  rm -f "$out" "$err"
}

expected_version="$(python3 -c "import tomllib; from pathlib import Path; print(tomllib.loads(Path('${ROOT}/pyproject.toml').read_text())['project']['version'])")"
expected_line="keystonectl ${expected_version}"

actual="$(run_capture "${BIN} --version" "${BIN}" --version)"
if [ "$actual" != "$expected_line" ]; then
  die "--version expected '${expected_line}', got '${actual}'"
fi

run_capture "lint fixture" "${BIN}" lint "${ROOT}/tests/fixtures/packages/file-mcp" --json >/dev/null

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

run_capture "init" "${BIN}" init "${tmp}/deploy" --json >/dev/null
[ -f "${tmp}/deploy/services.lock.yaml" ] || die "init did not create services.lock.yaml"

check_out="$(mktemp)"
check_err="$(mktemp)"
set +e
"${BIN}" --root "${tmp}/deploy" check --json >"$check_out" 2>"$check_err"
check_code=$?
set -e
if [ "$check_code" -ne 0 ] && [ "$check_code" -ne 3 ]; then
  log "--- check stdout ---"
  cat "$check_out" || true
  log "--- check stderr ---"
  cat "$check_err" || true
  die "check failed with exit code ${check_code} (expected 0 or 3)"
fi
grep -q '"command"[[:space:]]*:[[:space:]]*"check"' "$check_out" \
  || die "check JSON missing command check"
rm -f "$check_out" "$check_err"

log "Smoke OK: ${BIN}"
