from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_oidc_protect_everything_script_has_valid_bash_syntax():
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "oidc_protect_everything_smoke.sh"

    result = subprocess.run(["bash", "-n", str(script)], cwd=repo_root, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_oidc_protect_everything_script_prints_required_env_contract():
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "oidc_protect_everything_smoke.sh"

    result = subprocess.run([str(script), "--print-required-env"], cwd=repo_root, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    text = result.stdout
    assert "MODELKEYGUARD_POSTGRES_DSN" in text
    assert "MODELKEYGUARD_GRAPH_KEY or MODELKEYGUARD_GRAPH_KEY_FILE" in text
    assert "KEYCLOAK_URL" in text
    assert "KEYCLOAK_INTROSPECTION_CLIENT_SECRET or KEYCLOAK_INTROSPECTION_CLIENT_SECRET_FILE" in text
    assert "MODELKEYGUARD_OIDC_USER_CLIENT_ID" in text
    assert "MODELKEYGUARD_OIDC_ADMIN_CLIENT_ID" in text


def test_oidc_protect_everything_script_pins_oidc_only_flags():
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "oidc_protect_everything_smoke.sh"
    text = script.read_text(encoding="utf-8")

    assert 'MODELKEYGUARD_STORE="${MODELKEYGUARD_STORE:-kogwistar_postgres}"' in text
    assert 'MODELKEYGUARD_AUTH_MODE="${MODELKEYGUARD_AUTH_MODE:-keycloak}"' in text
    assert 'MODELKEYGUARD_REQUIRE_KEYCLOAK="${MODELKEYGUARD_REQUIRE_KEYCLOAK:-1}"' in text
    assert 'MODELKEYGUARD_ADMIN_AUTH_MODE="${MODELKEYGUARD_ADMIN_AUTH_MODE:-keycloak}"' in text
    assert 'MODELKEYGUARD_ADMIN_REQUIRED_ROLE="${MODELKEYGUARD_ADMIN_REQUIRED_ROLE:-model.admin}"' in text
    assert 'MODELKEYGUARD_REQUIRE_MODEL_LIST_AUTH="${MODELKEYGUARD_REQUIRE_MODEL_LIST_AUTH:-1}"' in text
    assert "Refusing to run OIDC smoke because a gateway is already responding" in text
    assert "MODELKEYGUARD_PORT=8791 ./scripts/oidc_protect_everything_smoke.sh" in text
    assert 'admin_secret_status="$(curl' in text
    assert 'test "${admin_secret_status}" = "401"' in text
    assert 'admin_user_status="$(curl' in text
    assert 'test "${admin_user_status}" = "403"' in text


def test_get_agent_token_uses_repo_python_fallback():
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "get_agent_token.sh"
    text = script.read_text(encoding="utf-8")

    assert 'PYTHON_BIN="${PYTHON:-}"' in text
    assert '.venv/bin/python' in text
    assert 'python3' in text
    assert '| "$PYTHON_BIN" -c' in text


def test_bootstrap_secrets_local_creates_admin_secret_and_placeholders(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "bootstrap_secrets.sh"

    result = subprocess.run(["bash", str(script)], cwd=tmp_path, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    secrets_dir = tmp_path / "secrets"
    assert (secrets_dir / "modelkeyguard_graph_key").read_text(encoding="utf-8").strip()
    assert (secrets_dir / "modelkeyguard_admin_api_secret").read_text(encoding="utf-8").strip()
    assert (secrets_dir / "keycloak_client_secret").read_text(encoding="utf-8").strip() == "gateway-secret"
    assert (secrets_dir / "openai_provider_key").read_text(encoding="utf-8").strip() == "dry-run-placeholder-provider-key"
    assert "admin secret file" in result.stdout


def test_bootstrap_secrets_production_refuses_placeholder_provider_key(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "bootstrap_secrets.sh"

    result = subprocess.run(["bash", str(script), "--production"], cwd=tmp_path, capture_output=True, text=True, check=False)

    assert result.returncode == 1
    assert "Refusing to create secrets/openai_provider_key with a placeholder" in result.stderr
    assert not (tmp_path / "secrets" / "openai_provider_key").exists()


def test_bootstrap_secrets_production_accepts_real_provider_key(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "bootstrap_secrets.sh"
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = "sk-real-test-provider-key"

    result = subprocess.run(["bash", str(script), "--production"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    secrets_dir = tmp_path / "secrets"
    assert (secrets_dir / "openai_provider_key").read_text(encoding="utf-8").strip() == "sk-real-test-provider-key"
    assert (secrets_dir / "modelkeyguard_admin_api_secret").read_text(encoding="utf-8").strip() != "dev-modelkeyguard-admin-secret"
    assert (secrets_dir / "keycloak_client_secret").read_text(encoding="utf-8").strip() != "gateway-secret"
    assert "Production note:" in result.stdout
