# Contributing

## Development

```bash
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

## Changelog lines

Add entries under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md). Released sections are
not edited retroactively.

Use only these subsections, in this order: `Added`, `Changed`, `Deprecated`, `Removed`,
`Fixed`, `Security`, `Migration`. An empty `[Unreleased]` section right after a release is
fine.

Each entry is one list item: `- ` followed by one complete sentence ending with a period.
Start with a capital letter, or with an identifier in backticks. Lowercase area prefixes
`e2e:`, `docs:`, and `skill:` are allowed before the sentence body.

Keep the text after `- ` to at most 160 characters. If it does not fit, split into two entries
or shorten the line and put details in documentation.

Wrap these in backticks: commands and subcommands (for example `service apply`), flags (for
example `--as`), paths and file names, error and warning ids, environment variables, JSON
field names, and lock file names.

`tests/test_changelog_style.py` checks `[Unreleased]` against these rules.

## Releasing

1. Bump the version in `pyproject.toml`, `src/keystonectl/__init__.py`, and add a dated
   `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md` in one pull request.
2. After merge, the release owner pushes tag `vX.Y.Z` to `main`.
3. The `Release` workflow (`release.yml`) publishes the wheel and sdist to PyPI via trusted
   publishing and opens a GitHub Release with notes from `CHANGELOG.md`.
4. Dry run without publishing: **Actions → Release → Run workflow** with `dry_run: true`
   (runs version gate, tests, build, and wheel smoke; skips PyPI and GitHub Release).
5. Forks without a PyPI trusted publisher can set repository variable `SKIP_PYPI_PUBLISH=true`.
