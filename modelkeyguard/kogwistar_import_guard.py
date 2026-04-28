from __future__ import annotations

import importlib.util
import os
from pathlib import Path


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def enforce_installed_kogwistar_only() -> None:
    """Fail fast if kogwistar resolves to a local repo clone path.

    This keeps runtime imports pinned to pip-installed packages (site-packages)
    and prevents accidental shadowing by local reference clones.
    """

    if not _truthy(os.getenv("MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY", "1")):
        return

    spec = importlib.util.find_spec("kogwistar")
    if spec is None or not spec.origin:
        raise RuntimeError("kogwistar package is not installed. Install it with pip for installed-only runtime mode.")

    origin = Path(spec.origin).resolve()
    repo_root = Path(__file__).resolve().parents[1]
    if str(origin).startswith(str(repo_root)):
        raise RuntimeError(
            "kogwistar import resolved to repository-local path. "
            "Use pip-installed kogwistar only; local clone is reference-only."
        )
