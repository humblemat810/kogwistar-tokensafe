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


def test_bootstrap_secrets_production_does_not_require_provider_key(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "bootstrap_secrets.sh"

    result = subprocess.run(["bash", str(script), "--production"], cwd=tmp_path, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    secrets_dir = tmp_path / "secrets"
    assert (secrets_dir / "modelkeyguard_graph_key").exists()
    assert (secrets_dir / "modelkeyguard_admin_api_secret").exists()
    assert (secrets_dir / "keycloak_client_secret").exists()
    assert not (tmp_path / "secrets" / "openai_provider_key").exists()
    assert "Provider keys are normally registered after deploy through /admin/keys" in result.stdout


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


def test_production_compose_script_has_valid_bash_syntax():
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "production_compose.sh"

    result = subprocess.run(["bash", "-n", str(script)], cwd=repo_root, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_single_source_gateway_runner_has_valid_bash_syntax():
    repo_root = Path(__file__).resolve().parents[1]
    for name in ("render_deployment_env.sh", "gateway_from_deployment_targets.sh"):
        script = repo_root / "scripts" / name
        result = subprocess.run(["bash", "-n", str(script)], cwd=repo_root, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr


def test_production_compose_script_pins_build_context_and_secrets_contract():
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "production_compose.sh"
    text = script.read_text(encoding="utf-8")

    assert "./scripts/bootstrap_secrets.sh --production" in text
    assert "fresh-up" in text
    assert "MODELKEYGUARD_FRESH_ROOT" in text
    assert "out/production_compose_fresh" in text
    assert "./scripts/reset_local_e2e_state.sh" in text
    assert "gateway-secret" in text
    assert 'require_dockerignore_entry "data"' in text
    assert 'require_dockerignore_entry "out"' in text
    assert 'require_dockerignore_entry "secrets"' in text
    assert 'require_dockerignore_entry "kogwistar_reference_only"' in text
    assert "secrets/modelkeyguard_admin_api_secret" in text
    assert "Provider keys" in text


def test_hardened_compose_pins_oidc_only_flags_without_provider_key_secret():
    repo_root = Path(__file__).resolve().parents[1]
    secure = (repo_root / "docker-compose.container-secure.yml").read_text(encoding="utf-8")
    compose = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")

    assert "MODELKEYGUARD_REQUIRE_KEYCLOAK" in secure
    assert "MODELKEYGUARD_ADMIN_AUTH_MODE: keycloak" in secure
    assert "MODELKEYGUARD_REQUIRE_MODEL_LIST_AUTH" in secure
    assert "MODELKEYGUARD_PROVIDER_KEY_OPENAI_FILE" not in compose
    assert "openai_provider_key" not in compose
    assert "${MODELKEYGUARD_GATEWAY_BIND:-127.0.0.1:8789}:8789" in compose
    assert "${MODELKEYGUARD_POSTGRES_BIND:-127.0.0.1:5432}:5432" in compose
    assert "${MODELKEYGUARD_KEYCLOAK_BIND:-127.0.0.1:8080}:8080" in compose


def test_root_dockerignore_excludes_runtime_state_and_reference_clone():
    repo_root = Path(__file__).resolve().parents[1]
    text = (repo_root / ".dockerignore").read_text(encoding="utf-8")

    assert "\ndata\n" in f"\n{text}"
    assert "\nout\n" in f"\n{text}"
    assert "\nsecrets\n" in f"\n{text}"
    assert "\nkogwistar_reference_only\n" in f"\n{text}"


def test_split_target_deploy_templates_document_required_contract():
    repo_root = Path(__file__).resolve().parents[1]
    gateway_env = (repo_root / "deploy" / "gateway.env.example").read_text(encoding="utf-8")
    postgres_env = (repo_root / "deploy" / "postgres.env.example").read_text(encoding="utf-8")
    keycloak_env = (repo_root / "deploy" / "keycloak.env.example").read_text(encoding="utf-8")
    target_env = (repo_root / "deploy" / "deployment-targets.env.example").read_text(encoding="utf-8")
    gateway_compose = (repo_root / "deploy" / "docker-compose.gateway-only.yml").read_text(encoding="utf-8")
    deploy_readme = (repo_root / "deploy" / "README.md").read_text(encoding="utf-8")

    assert "MODELKEYGUARD_POSTGRES_HOST" in target_env
    assert "MODELKEYGUARD_KEYCLOAK_PUBLIC_URL" in target_env
    assert "MODELKEYGUARD_GATEWAY_PUBLIC_URL" in target_env
    assert "MODELKEYGUARD_POSTGRES_HOST" in gateway_env
    assert "MODELKEYGUARD_KEYCLOAK_PUBLIC_URL" in gateway_env
    assert "MODELKEYGUARD_POSTGRES_DSN" in gateway_env
    assert "MODELKEYGUARD_GRAPH_KEY_FILE" in gateway_env
    assert "KEYCLOAK_URL" in gateway_env
    assert "MODELKEYGUARD_ADMIN_AUTH_MODE=keycloak" in gateway_env
    assert "MODELKEYGUARD_REQUIRE_KEYCLOAK=1" in gateway_env
    assert "MODELKEYGUARD_POSTGRES_DSN" in postgres_env
    assert "pgvector" in postgres_env
    assert "KEYCLOAK_INTROSPECTION_CLIENT_SECRET_FILE" in keycloak_env
    assert "MODELKEYGUARD_ADMIN_REQUIRED_ROLE" in keycloak_env
    assert "docker-compose.gateway-only.yml" in deploy_readme
    assert "deployment-targets.env.example" in deploy_readme
    assert "gateway.env.example" in gateway_compose
    assert "MODELKEYGUARD_GATEWAY_BIND" in gateway_compose
    for text in (gateway_env, postgres_env, keycloak_env, target_env, deploy_readme):
        assert "keycloak-c.example" not in text
        assert "postgres-b.example" not in text
        assert "ollama-b.example" not in text


def test_render_deployment_env_generates_matching_component_files(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "render_deployment_env.sh"
    targets = tmp_path / "targets.env"
    out_dir = tmp_path / "rendered"
    targets.write_text(
        "\n".join(
            [
                "MODELKEYGUARD_GATEWAY_HOST=gateway-a.internal",
                "MODELKEYGUARD_GATEWAY_PORT=8789",
                "MODELKEYGUARD_GATEWAY_PUBLIC_URL=https://gateway.example",
                "MODELKEYGUARD_POSTGRES_HOST=postgres-b.internal",
                "MODELKEYGUARD_POSTGRES_PORT=5432",
                "MODELKEYGUARD_POSTGRES_DB=modelguard",
                "MODELKEYGUARD_POSTGRES_USER=modelguard",
                "MODELKEYGUARD_KEYCLOAK_HOST=keycloak-c.internal",
                "MODELKEYGUARD_KEYCLOAK_PORT=443",
                "MODELKEYGUARD_KEYCLOAK_PUBLIC_URL=https://keycloak.example",
                "MODELKEYGUARD_KEYCLOAK_REALM=modelguard",
                "MODELKEYGUARD_KEYCLOAK_INTROSPECTION_CLIENT_ID=modelguard-gateway",
                "MODELKEYGUARD_ADMIN_REQUIRED_ROLE=model.admin",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run([str(script), str(targets), str(out_dir)], cwd=repo_root, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    gateway_env = (out_dir / "gateway.env").read_text(encoding="utf-8")
    postgres_env = (out_dir / "postgres.env").read_text(encoding="utf-8")
    keycloak_env = (out_dir / "keycloak.env").read_text(encoding="utf-8")
    compose_env = (out_dir / "gateway-compose.env").read_text(encoding="utf-8")

    assert "MODELKEYGUARD_POSTGRES_DSN=postgresql://modelguard:<postgres-password>@postgres-b.internal:5432/modelguard" in gateway_env
    assert "KEYCLOAK_URL=https://keycloak.example" in gateway_env
    assert "MODELKEYGUARD_PORT=8789" in gateway_env
    assert "MODELKEYGUARD_POSTGRES_DSN=postgresql://modelguard:<postgres-password>@postgres-b.internal:5432/modelguard" in postgres_env
    assert "KEYCLOAK_URL=https://keycloak.example" in keycloak_env
    assert f"MODELKEYGUARD_GATEWAY_ENV_FILE={out_dir}/gateway.env" in compose_env
    assert "MODELKEYGUARD_GATEWAY_BIND=127.0.0.1:8789" in compose_env


def test_render_deployment_env_can_render_from_exported_environment(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "render_deployment_env.sh"
    out_dir = tmp_path / "rendered"
    env = os.environ.copy()
    env.update(
        {
            "MODELKEYGUARD_GATEWAY_HOST": "gateway-a.internal",
            "MODELKEYGUARD_GATEWAY_PORT": "8789",
            "MODELKEYGUARD_GATEWAY_PUBLIC_URL": "https://gateway.example",
            "MODELKEYGUARD_POSTGRES_HOST": "postgres-b.internal",
            "MODELKEYGUARD_POSTGRES_PORT": "5432",
            "MODELKEYGUARD_POSTGRES_DB": "modelguard",
            "MODELKEYGUARD_POSTGRES_USER": "modelguard",
            "MODELKEYGUARD_KEYCLOAK_HOST": "keycloak-c.internal",
            "MODELKEYGUARD_KEYCLOAK_PORT": "443",
            "MODELKEYGUARD_KEYCLOAK_PUBLIC_URL": "https://keycloak.example",
            "MODELKEYGUARD_KEYCLOAK_REALM": "modelguard",
            "MODELKEYGUARD_KEYCLOAK_INTROSPECTION_CLIENT_ID": "modelguard-gateway",
            "MODELKEYGUARD_ADMIN_REQUIRED_ROLE": "model.admin",
        }
    )

    result = subprocess.run([str(script), "--from-env", str(out_dir)], cwd=repo_root, env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    gateway_env = (out_dir / "gateway.env").read_text(encoding="utf-8")
    assert "MODELKEYGUARD_POSTGRES_DSN=postgresql://modelguard:<postgres-password>@postgres-b.internal:5432/modelguard" in gateway_env
    assert "KEYCLOAK_URL=https://keycloak.example" in gateway_env


def test_gateway_from_deployment_targets_runner_renders_from_one_env_file(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "gateway_from_deployment_targets.sh"
    targets = tmp_path / "targets.env"
    out_dir = tmp_path / "rendered"
    targets.write_text(
        "\n".join(
            [
                "MODELKEYGUARD_GATEWAY_HOST=gateway-a.internal",
                "MODELKEYGUARD_GATEWAY_PORT=8789",
                "MODELKEYGUARD_GATEWAY_PUBLIC_URL=https://gateway.example",
                "MODELKEYGUARD_POSTGRES_HOST=postgres-b.internal",
                "MODELKEYGUARD_POSTGRES_PORT=5432",
                "MODELKEYGUARD_POSTGRES_DB=modelguard",
                "MODELKEYGUARD_POSTGRES_USER=modelguard",
                "MODELKEYGUARD_KEYCLOAK_HOST=keycloak-c.internal",
                "MODELKEYGUARD_KEYCLOAK_PORT=443",
                "MODELKEYGUARD_KEYCLOAK_PUBLIC_URL=https://keycloak.example",
                "MODELKEYGUARD_KEYCLOAK_REALM=modelguard",
                "MODELKEYGUARD_KEYCLOAK_INTROSPECTION_CLIENT_ID=modelguard-gateway",
                "MODELKEYGUARD_ADMIN_REQUIRED_ROLE=model.admin",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(script), "--env-file", str(targets), "--out-dir", str(out_dir), "render"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (out_dir / "gateway.env").exists()
    assert "KEYCLOAK_URL=https://keycloak.example" in (out_dir / "gateway.env").read_text(encoding="utf-8")


def test_render_deployment_env_rejects_placeholder_targets(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "render_deployment_env.sh"
    out_dir = tmp_path / "rendered"
    targets = repo_root / "deploy" / "deployment-targets.env.example"

    result = subprocess.run([str(script), str(targets), str(out_dir)], cwd=repo_root, capture_output=True, text=True, check=False)

    assert result.returncode == 1
    assert "before rendering deployment env files" in result.stderr
