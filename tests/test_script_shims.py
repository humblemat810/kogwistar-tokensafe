from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path


def test_export_scripts_generates_pypi_safe_shims(tmp_path: Path):
    out_dir = tmp_path / "shim-bin"
    result = subprocess.run(
        [sys.executable, "-m", "modelkeyguard", "export-scripts", "--dir", str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "exported" in result.stdout

    expected = {
        "init_graph.sh",
        "inspect_graph.sh",
        "review_once.sh",
        "review_status.sh",
        "registration_seed.sh",
        "gateway.sh",
        "README.txt",
    }
    produced = {p.name for p in out_dir.iterdir()}
    assert expected <= produced

    for name in sorted(expected - {"README.txt"}):
        path = out_dir / name
        text = path.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash")
        assert "modelkeyguard " in text
        mode = path.stat().st_mode
        assert bool(mode & stat.S_IXUSR), f"{name} is not executable"


def test_export_scripts_readme_explains_scope(tmp_path: Path):
    out_dir = tmp_path / "shim-bin"
    result = subprocess.run(
        [sys.executable, "-m", "modelkeyguard", "export-scripts", "--dir", str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    readme = (out_dir / "README.txt").read_text(encoding="utf-8")
    assert "PyPI-safe script shims" in readme
    assert "not full replacements" in readme
    assert "./scripts/" in readme
