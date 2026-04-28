from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_reset_local_e2e_state_script_is_idempotent(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "reset_local_e2e_state.sh"

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    call_log = tmp_path / "calls.log"

    _write_exe(
        fake_bin / "docker",
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "docker $@" >> "{call_log}"
if [[ "${{1:-}}" == "compose" && "${{2:-}}" == "version" ]]; then
  echo "Docker Compose version v2.fake"
  exit 0
fi
exit 0
""",
    )
    _write_exe(
        fake_bin / "pkill",
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "pkill $@" >> "{call_log}"
exit 0
""",
    )
    _write_exe(
        fake_bin / "ss",
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "ss $@" >> "{call_log}"
exit 0
""",
    )
    _write_exe(
        fake_bin / "rg",
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "rg $@" >> "{call_log}"
exit 1
""",
    )

    data_dir = tmp_path / "data" / "postgres"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "stale.db").write_text("stale", encoding="utf-8")

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    llm_cache_dir = out_dir / "llm_call_cache"
    llm_cache_dir.mkdir(parents=True, exist_ok=True)
    (llm_cache_dir / "cached.joblib").write_text("cached", encoding="utf-8")
    target_files = [
        "modelkeyguard_graph.jsonl",
        "modelkeyguard_audit.jsonl",
        "review_results.jsonl",
        "single_e2e_graph.jsonl",
        "single_e2e_audit.jsonl",
        "single_e2e_policy.json",
        "azure_real_billing_graph.jsonl",
        "azure_real_billing_audit.jsonl",
        "azure_real_billing_policy.json",
        "azure_real_history_detail.json",
        "case1_graph.jsonl",
        "case1_audit.jsonl",
        "case1_review.jsonl",
        "case1_review_checkpoint.json",
        "case2_graph.jsonl",
        "case2_audit.jsonl",
        "quickstart_review_results.jsonl",
        "review_checkpoint.json",
        "finaldev_graph.jsonl",
        "finaldev_audit.jsonl",
        "finaldev_review.jsonl",
        "quickstart_graph.jsonl",
        "quickstart_audit.jsonl",
    ]
    for name in target_files:
        (out_dir / name).write_text("x", encoding="utf-8")
    keep_file = out_dir / "keep_me.txt"
    keep_file.write_text("keep", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["MODELKEYGUARD_RESET_DATA_DIR"] = str(data_dir)
    env["MODELKEYGUARD_RESET_OUT_DIR"] = str(out_dir)
    env["MODELKEYGUARD_GRAPH_PATH"] = str(tmp_path / "custom_graph.jsonl")
    env["MODELKEYGUARD_AUDIT_PATH"] = str(tmp_path / "custom_audit.jsonl")
    env["MODELKEYGUARD_REVIEW_OUT"] = str(tmp_path / "custom_review.jsonl")
    env["MODELKEYGUARD_STORE"] = "postgres"
    env["MODELKEYGUARD_POSTGRES_DSN"] = "postgresql://modelguard:modelguard@localhost:55433/modelguard"

    Path(env["MODELKEYGUARD_GRAPH_PATH"]).write_text("g", encoding="utf-8")
    Path(env["MODELKEYGUARD_AUDIT_PATH"]).write_text("a", encoding="utf-8")
    Path(env["MODELKEYGUARD_REVIEW_OUT"]).write_text("r", encoding="utf-8")

    # Run twice to pin idempotency.
    for _ in range(2):
        result = subprocess.run(
            ["bash", str(script)],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    assert not data_dir.exists()
    assert not llm_cache_dir.exists()
    assert not Path(env["MODELKEYGUARD_GRAPH_PATH"]).exists()
    assert not Path(env["MODELKEYGUARD_AUDIT_PATH"]).exists()
    assert not Path(env["MODELKEYGUARD_REVIEW_OUT"]).exists()
    for name in target_files:
        assert not (out_dir / name).exists()
    assert keep_file.exists()

    log_text = call_log.read_text(encoding="utf-8")
    assert "docker compose version" in log_text
    assert "docker compose down -v" in log_text
    assert "docker rm -f modelkeyguard-postgres" in log_text
