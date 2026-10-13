# Contributing

## Development

```bash
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

## Releasing

1. Bump the version in `pyproject.toml`, `src/keystonectl/__init__.py`, and add a dated
   `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md` in one pull request.
2. After merge, the release owner pushes tag `vX.Y.Z` to `main`.
3. The `Release` workflow (`release.yml`) publishes the wheel and sdist to PyPI via trusted
   publishing and opens a GitHub Release with notes from `CHANGELOG.md`.
4. Dry run without publishing: **Actions → Release → Run workflow** with `dry_run: true`
   (runs version gate, tests, build, and wheel smoke; skips PyPI and GitHub Release).
5. Forks without a PyPI trusted publisher can set repository variable `SKIP_PYPI_PUBLISH=true`.
