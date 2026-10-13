#!/usr/bin/env bash
# Print the command block of one skill scenario.
set -eu

name="${1:?scenario name}"
here="$(cd "$(dirname "$0")" && pwd)"
skill="${2:-$here/../SKILL.md}"

awk -v name="$name" '
  $0 == "<!-- scenario: " name " -->" { capture = 1; next }
  $0 == "<!-- /scenario -->" { capture = 0; next }
  capture { print }
' "$skill"
