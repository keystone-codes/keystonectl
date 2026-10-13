#!/usr/bin/env python3
"""Turn one skill scenario into a bash script a test can run."""

from __future__ import annotations

import sys

_FILE_UPDATED = "Operator: The file is updated."
_REFRESH = 'install -m 600 "$SCENARIO_DIR/secrets/pg-url-fixed" "$KEYSTONE_SETUP_POSTGRES_URL_FILE"'


def main() -> None:
    lines_out: list[str] = []
    for line in sys.stdin.read().splitlines():
        stripped = line.strip()
        if stripped == _FILE_UPDATED:
            lines_out.append(_REFRESH)
            continue
        if stripped.startswith(("Agent:", "Operator:")):
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        lines_out.append(line)
    sys.stdout.write("\n".join(lines_out) + "\n")


if __name__ == "__main__":
    main()
