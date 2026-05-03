# Kogwistar ModelKeyGuard (PyPI Install Scope)

This package provides:

- Python library modules under `modelkeyguard`
- CLI entrypoint: `modelkeyguard`
- PyPI-safe bash wrapper export:
  - `modelkeyguard export-scripts --dir ./modelkeyguard-scripts`

This package does **not** bundle the repository operational script tree
(`./scripts/*.sh`) as fully working deploy/compose artifacts.

## Why this scope exists

Many repository scripts depend on repository-local files such as:

- `docker-compose.yml`
- `deploy/*`
- `config/*`
- `keycloak/*`
- local relative-path conventions

Shipping those as standalone PyPI-installed scripts would create dual-truth and
drift risk.

## If you need full operational scripts

Use the GitHub repository directly (matching the same release/tag) and run:

```bash
bash ./scripts/<name>.sh
```

Repository docs:

- Main README: `README.md`
- Scripts index: `scripts/README.md`
