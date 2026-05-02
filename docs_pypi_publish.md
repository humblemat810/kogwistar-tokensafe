# PyPI Publish Guide (Dual Packages)

This repo publishes two package names from one source tree:

- `kogwistar-modelkeyguard` (canonical)
- `monkeyguard` (alias)

Both are built from the same code and version. During publish build, package
metadata is patched in a temporary copy so:

- `[project].name` is set per target package
- `[project].readme` is set to `README_PYPI.md`

This keeps GitHub README and PyPI README intentionally different.

## 1. One-time prerequisites

1. Own both package names on PyPI.
2. In GitHub repo secrets, set:
   - `PYPI_API_TOKEN_KOGWISTAR_MODELKEYGUARD`
   - `PYPI_API_TOKEN_MONKEYGUARD`
3. Ensure `pyproject.toml` version is the release version you want.

## 2. Local dry-run build (recommended)

Build and validate artifacts locally before CI publish:

```bash
./scripts/build_dual_pypi_dists.sh
```

Output folders:

- `out/pypi-dist/kogwistar-modelkeyguard/`
- `out/pypi-dist/monkeyguard/`

This step runs `twine check` for both package outputs.

## 3. Publish from GitHub Actions

Workflow:

- `.github/workflows/publish-pypi-dual.yml`

### Safe preview build (no upload)

Use **Actions -> Publish PyPI (Dual Package Names) -> Run workflow** with:

- `upload_to_pypi=false`
- `package_version=<optional exact version gate>`

This builds both artifacts and validates metadata, but does not upload.

### Actual upload

Option A (tag-driven):

1. Create/push a tag like `v0.4.1`.
2. Workflow runs and uploads both package names.

Option B (manual dispatch):

Run workflow with:

- `upload_to_pypi=true`
- optional `package_version` set to the exact version for guardrail.

## 4. Verification checklist

After upload:

1. Confirm both packages show the same version and release files.
2. Confirm both package pages render `README_PYPI.md` content.
3. Test install:
   - `pip install kogwistar-modelkeyguard==<version>`
   - `pip install monkeyguard==<version>`
4. Confirm CLI works:
   - `modelkeyguard --help`
5. Confirm PyPI-safe shim export works:
   - `modelkeyguard export-scripts --dir ./modelkeyguard-scripts`

## 5. Roll-forward guidance

- Prefer rolling forward with a new version if one package upload succeeds and
  the other fails, rather than trying to mutate an existing release.
- Keep package versions aligned between both names.
