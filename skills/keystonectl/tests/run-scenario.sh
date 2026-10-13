#!/usr/bin/env bash
# Run the keystonectl lines of FAKE_SCENARIO. keystonectl must be the fake on PATH.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
name="${FAKE_SCENARIO:?}"
script="${WORK:?}/scenario.sh"

"$here/extract-scenario.sh" "$name" | python3 "$here/filter-scenario.py" >"$script"
bash "$script"
