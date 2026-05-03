from __future__ import annotations

import os
from pathlib import Path


# PyPI-safe wrappers only: each shim delegates to the canonical Python CLI.
SCRIPT_SHIMS: dict[str, list[str]] = {
    "init_graph.sh": ["init-graph"],
    "inspect_graph.sh": ["inspect-graph"],
    "review_once.sh": ["review-once"],
    "review_status.sh": ["review-status"],
    "registration_seed.sh": ["registration", "seed"],
    "gateway.sh": ["gateway"],
}


def _script_body(cli_args: list[str]) -> str:
    quoted = " ".join(cli_args)
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"modelkeyguard {quoted} \"$@\"\n"
    )


def export_pypi_safe_scripts(target_dir: str) -> tuple[int, list[Path]]:
    root = Path(target_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    emitted: list[Path] = []
    for filename, cli_args in sorted(SCRIPT_SHIMS.items()):
        path = root / filename
        path.write_text(_script_body(cli_args), encoding="utf-8")
        os.chmod(path, 0o755)
        emitted.append(path)

    readme = root / "README.txt"
    readme.write_text(
        "\n".join(
            [
                "ModelKeyGuard PyPI-safe script shims",
                "",
                "These files are generated wrappers around the installed",
                "`modelkeyguard` CLI and are safe to use from a pip-only install.",
                "",
                "They are not full replacements for repository-level scripts that",
                "require docker-compose files, deploy templates, or local repo layout.",
                "",
                "For full operational scripts, clone the repository and use ./scripts/.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    emitted.append(readme)
    return len(SCRIPT_SHIMS), emitted
