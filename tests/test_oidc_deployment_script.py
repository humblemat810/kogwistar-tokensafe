from __future__ import annotations

import json
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
    assert 'if "${compose_cmd[@]}" exec -T gateway python3' in text
    assert 'label=com.docker.compose.service=gateway' in text
    assert '--filter "name=gateway"' in text
    assert 'keycloak_url = os.getenv("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/")' in text
    assert 'http://keycloak:8080/realms/modelguard/protocol/openid-connect/token' in text
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


def test_fresh_up_parity_smoke_has_valid_bash_syntax_and_contract():
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "fresh_up_parity_smoke.sh"

    result = subprocess.run(["bash", "-n", str(script)], cwd=repo_root, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    text = script.read_text(encoding="utf-8")
    assert "./scripts/deploy_remote_stack.sh fresh-up --ssh" in text
    assert "./scripts/deploy_remote_stack.sh up --ssh" in text
    assert "./scripts/deploy_remote_stack.sh down --ssh" in text
    assert "docker inspect token-safe-deploy-postgres-1" in text
    assert "assert_fresh_mount" in text
    assert "expected up to keep the active fresh mount path" in text
    assert "fresh-up reused the same Postgres bind mount path twice" in text
    assert "expected up after down to keep the most recent fresh mount path" in text
    assert "fresh-up vs up smoke passed" in text


def test_single_source_gateway_runner_has_valid_bash_syntax():
    repo_root = Path(__file__).resolve().parents[1]
    for name in ("render_deployment_env.sh", "gateway_from_deployment_targets.sh", "bootstrap_keycloak_admin_role.sh"):
        script = repo_root / "scripts" / name
        result = subprocess.run(["bash", "-n", str(script)], cwd=repo_root, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr


def test_production_compose_script_pins_build_context_and_secrets_contract():
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "production_compose.sh"
    remote_script = repo_root / "scripts" / "deploy_remote_stack.sh"
    text = script.read_text(encoding="utf-8")
    remote_text = remote_script.read_text(encoding="utf-8")

    assert "./scripts/bootstrap_secrets.sh --production" in text
    assert "stop|start" in text
    assert "compose_files=(-f docker-compose.yml -f docker-compose.container-secure.yml)" in text
    assert '"${compose_cmd[@]}" "${compose_files[@]}" down --remove-orphans -v' in text
    assert '"${compose_cmd[@]}" "${compose_files[@]}" down --remove-orphans' in text
    assert '"${compose_cmd[@]}" "${compose_files[@]}" up -d --build' in text
    assert '"${compose_cmd[@]}" "${compose_files[@]}" stop' in text
    assert '"${compose_cmd[@]}" "${compose_files[@]}" start' in text
    assert '"${compose_cmd[@]}" "${compose_files[@]}" logs -f' in text
    assert "fresh-up" in text
    assert "MODELKEYGUARD_FRESH_ROOT" in text
    assert "out/production_compose_fresh" in text
    assert "./scripts/reset_local_e2e_state.sh" in text
    assert "MODELKEYGUARD_POSTGRES_DATA_DIR" in text
    assert "MODELKEYGUARD_KEYCLOAK_DATA_DIR" in text
    assert "MODELKEYGUARD_KEYCLOAK_REALM_IMPORT_FILE" in text
    assert "gateway-secret" in text
    assert "bootstrap_keycloak_admin_role.sh" in text
    assert "bootstrap role" in text.lower() or "model.admin" in text
    assert 'require_dockerignore_entry "data"' in text
    assert 'require_dockerignore_entry "out"' in text
    assert 'require_dockerignore_entry "secrets"' in text
    assert 'require_dockerignore_entry "kogwistar_reference_only"' in text
    assert "secrets/modelkeyguard_admin_api_secret" in text
    assert "Provider keys" in text
    assert "./scripts/production_compose.sh up" in remote_text
    assert "./scripts/production_compose.sh fresh-up" in remote_text
    assert "./scripts/production_compose.sh start" in remote_text
    assert "./scripts/production_compose.sh stop" in remote_text
    assert "./scripts/production_compose.sh down" in remote_text
    assert "./scripts/production_compose.sh logs" in remote_text
    assert "./scripts/production_compose.sh config" in remote_text


def test_hardened_compose_pins_oidc_only_flags_without_provider_key_secret():
    repo_root = Path(__file__).resolve().parents[1]
    secure = (repo_root / "docker-compose.container-secure.yml").read_text(encoding="utf-8")
    compose = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")

    assert "MODELKEYGUARD_REQUIRE_KEYCLOAK" in secure
    assert "MODELKEYGUARD_ADMIN_AUTH_MODE: secret_or_keycloak" in secure
    assert "MODELKEYGUARD_REQUIRE_MODEL_LIST_AUTH" in secure
    assert "MODELKEYGUARD_PROVIDER_KEY_OPENAI_FILE" not in compose
    assert "openai_provider_key" not in compose
    assert "${MODELKEYGUARD_KEYCLOAK_REALM_IMPORT_FILE:-./keycloak/modelguard-realm.json}" in compose
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
    assert "MODELKEYGUARD_OIDC_BROWSER_CLIENT_ID" in keycloak_env
    assert "MODELKEYGUARD_OIDC_USAGE_CLIENT_ID" in keycloak_env
    assert "MODELKEYGUARD_ADMIN_REQUIRED_ROLE" in keycloak_env
    assert "MODELKEYGUARD_USAGE_REQUIRED_ROLE" in keycloak_env
    assert "docker-compose.gateway-only.yml" in deploy_readme
    assert "deployment-targets.env.example" in deploy_readme
    assert "out/deployment_targets_rendered/gateway.env" in deploy_readme
    assert "out/deployment_targets_rendered/gateway-compose.env" in deploy_readme
    assert "gateway.env.example" in gateway_compose
    assert "MODELKEYGUARD_GATEWAY_BIND" in gateway_compose
    assert "bootstrap_keycloak_admin_role.sh" in (repo_root / "scripts" / "README.md").read_text(encoding="utf-8")
    for text in (gateway_env, postgres_env, keycloak_env, target_env, deploy_readme):
        assert "keycloak-c.example" not in text
        assert "postgres-b.example" not in text
        assert "ollama-b.example" not in text


def test_keycloak_realm_default_is_empty_and_beginner_profile_is_opt_in():
    repo_root = Path(__file__).resolve().parents[1]
    default_realm = json.loads((repo_root / "keycloak" / "modelguard-realm.json").read_text(encoding="utf-8"))
    beginner_realm = json.loads((repo_root / "keycloak" / "modelguard-realm.beginner.json").read_text(encoding="utf-8"))

    assert default_realm.get("users", []) == []
    beginner_users = {user["username"] for user in beginner_realm.get("users", [])}
    assert {"alice", "admin"} <= beginner_users
    assert beginner_realm["clients"][0]["clientId"] == "modelguard-gateway"
    assert beginner_realm["roles"]["realm"]


def test_production_doc_pins_limited_model_key_and_user_quota_flow():
    repo_root = Path(__file__).resolve().parents[1]
    text = (repo_root / "docs_production.md").read_text(encoding="utf-8")

    assert "Register an end user:" in text
    assert '"lane":"user","subject_id":"user:alice"' in text
    assert "-F acl_mode='shared'" in text
    assert "-F shared_with_principals='agent:doc-ingestor'" in text
    assert "only `agent:doc-ingestor` can use the key" in text
    assert "which end-user quota is charged" in text
    assert "/admin/oidc/login?next=/admin/usage" in text
    assert "modelguard-admin-web" in text
    assert "modelguard-usage-agent" in text
    assert "model.usage.read" in text


def test_production_doc_pins_quota_patterns_and_model_specific_limits():
    repo_root = Path(__file__).resolve().parents[1]
    text = (repo_root / "docs_production.md").read_text(encoding="utf-8")

    assert "Quota patterns and limits" in text
    assert "infinite` is also supported for a lifetime quota that" in text.lower()
    assert 'There is still no custom rolling "every N days starting from a chosen day"\nsetting today.' in text
    assert "`day` resets at `00:00 UTC`" in text
    assert "`week` resets on Monday `00:00 UTC`" in text
    assert "`month` resets on the first day of the UTC month" in text
    assert "`infinite` never refreshes and accumulates forever" in text
    assert "| Goal | Lane | `subject_id` example |" in text
    assert "| One issued safe token | `token` | `token:abc123` |" in text
    assert "| Client credential with its own hard cap | `/admin/policy/tokens` + `lane=token` quota on `token:<jti>` |" in text
    assert "Per-user budget" in text
    assert "Per-principal budget" in text
    assert "Per-model budget" in text
    assert "register one key per model when you want a hard model-level cap" in text
    assert 'If you want this issued safe token to have its own hard cap, add a `token`\nquota on `token:<jti>` after issuance.' in text
    assert '"lane":"key","subject_id":"key:openai:prod-gpt4o","quota_name":"month","period":"month"' in text
    assert '"lane":"user","subject_id":"user:alice","quota_name":"lifetime","period":"infinite"' in text
    assert '"lane":"token","subject_id":"token:abc123","quota_name":"lifetime","period":"infinite"' in text


def test_production_doc_shows_explicit_key_examples_for_each_provider():
    repo_root = Path(__file__).resolve().parents[1]
    text = (repo_root / "docs_production.md").read_text(encoding="utf-8")

    for provider in ("openai", "azure_openai", "ollama", "gemini"):
        assert f"-F provider='{provider}'" in text


def test_usage_analysis_agent_tutorial_and_script_pin_reusable_library():
    repo_root = Path(__file__).resolve().parents[1]
    tutorial = (repo_root / "tutorial" / "usage_analysis_agent.md").read_text(encoding="utf-8")
    script = (repo_root / "scripts" / "usage_analysis_agent.py").read_text(encoding="utf-8")
    smoke = (repo_root / "scripts" / "usage_analysis_agent_smoke.sh").read_text(encoding="utf-8")

    assert "modelkeyguard.analytics" in tutorial
    assert "modelkeyguard.usage_agent" in tutorial
    assert "UsageAnalysisAgent.from_env" in tutorial
    assert "KeycloakServiceAccount" in tutorial
    assert "UsageAnalyticsClient" in tutorial
    assert "modelguard-usage-agent" in tutorial
    assert "model.usage.read" in tutorial
    assert "usage_analysis_agent.py" in tutorial
    assert "MODELKEYGUARD_ANALYTICS_SUBJECT_USER" in tutorial
    assert "fresh-up compose stack" in smoke
    assert "MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET" in smoke
    assert "usage_analysis_agent.py" in smoke
    assert "UsageAnalysisAgent" in script
    assert "--principal" in script
    assert "--key" in script


def test_remote_deployment_and_smoke_scripts_pin_required_workflow():
    repo_root = Path(__file__).resolve().parents[1]
    deploy = (repo_root / "scripts" / "deploy_remote_stack.sh").read_text(encoding="utf-8")
    smoke = (repo_root / "scripts" / "deployment_smoke.sh").read_text(encoding="utf-8")
    fresh_smoke = (repo_root / "scripts" / "fresh_up_parity_smoke.sh").read_text(encoding="utf-8")
    scripts_readme = (repo_root / "scripts" / "README.md").read_text(encoding="utf-8")
    adr_readme = (repo_root / "docs" / "adr" / "README.md").read_text(encoding="utf-8")
    docs = (repo_root / "docs_production.md").read_text(encoding="utf-8")
    readme = (repo_root / "README.md").read_text(encoding="utf-8")

    assert "another unprivileged user on the same machine" in deploy
    assert "Build the gateway image locally" in deploy
    assert "load it onto a remote SSH target" in deploy
    assert "Default: ~/token-safe-deploy" in deploy
    assert "Keycloak bootstrap admin for this deployment" in deploy
    assert "--shape MODE" in deploy
    assert "compose | gateway-only" in deploy
    assert "tmpfs-backed runtime directory" in deploy
    assert "rm -rf '$remote_root_expanded/secrets'" in deploy
    assert "rm -rf '$remote_root_expanded/secrets' '$runtime_state_file' '$dir'" in deploy
    assert 'cleanup_runtime_secrets "$ssh_target"' in deploy
    assert 'local remote="${1:-}"' in deploy
    assert "MODELKEYGUARD_KEYCLOAK_REALM_IMPORT_FILE" in deploy
    assert "browser OIDC redirect" in smoke
    assert "CLI/service-account token path" in smoke
    assert "usage-analysis agent" in smoke
    assert "fresh-up vs up smoke passed" in fresh_smoke
    assert "docker inspect token-safe-deploy-postgres-1" in fresh_smoke
    assert "deploy_remote_stack.sh" in scripts_readme
    assert "deployment_smoke.sh" in scripts_readme
    assert "fresh_up_parity_smoke.sh" in scripts_readme
    assert "Lifecycle parity notes" in scripts_readme
    assert "`fresh-up` creates a new timestamped rehearsal data root and records it" in scripts_readme
    assert "0007-fresh-up-and-up-bind-mount-parity.md" in adr_readme
    assert "Fresh-up and up bind-mount parity" in adr_readme
    assert "0008-graph-key-resolution-and-sealed-payload-contract.md" in adr_readme
    assert "Graph key resolution and sealed payload contract" in adr_readme
    assert "0009-explicit-model-key-selection-and-ambiguity-failure.md" in adr_readme
    assert "Explicit model key selection and ambiguity failure" in adr_readme
    assert "deploy_remote_stack.sh" in docs
    assert "deployment_smoke.sh" in docs
    assert "./scripts/production_compose.sh up" in deploy
    assert "./scripts/production_compose.sh fresh-up" in deploy
    assert "./scripts/production_compose.sh start" in deploy
    assert "./scripts/production_compose.sh stop" in deploy
    assert "./scripts/production_compose.sh down" in deploy
    assert "./scripts/production_compose.sh logs" in deploy
    assert "./scripts/production_compose.sh config" in deploy
    assert "For the remote compose path, the wrapper also generates a non-default Keycloak" in docs
    assert "keycloak_admin_first_setup.md" in docs
    assert "gateway-only deploy cannot bind" in deploy
    assert "deploy_remote_stack.sh" in readme
    assert "deployment_smoke.sh" in readme
    assert "keycloak_admin_first_setup.md" in readme


def test_keycloak_admin_first_setup_tutorial_pins_setup_flow():
    repo_root = Path(__file__).resolve().parents[1]
    tutorial = (repo_root / "tutorial" / "keycloak_admin_first_setup.md").read_text(encoding="utf-8")
    tutorial_index = (repo_root / "tutorial" / "README.md").read_text(encoding="utf-8")
    docs = (repo_root / "docs_production.md").read_text(encoding="utf-8")

    assert "create Keycloak alice" in tutorial
    assert "register user:alice" in tutorial
    assert "user:admin" in tutorial
    assert "take the keycloak username and prefix it with" in tutorial.lower()
    assert "register agent:doc-ingestor" in tutorial
    assert "Give the agent and reviewer OIDC machine credentials" in tutorial
    assert "modelguard-usage-agent" in tutorial
    assert "set up quotas for alice and agent:doc-ingestor" in tutorial.lower()
    assert "bootstrap-operator quota" in tutorial.lower()
    assert "OIDC admin role" in tutorial
    assert "For Alice to be a backend admin, all of these must be true:" in tutorial
    assert "modelkeyguard has the matching policy subject `user:alice`" in tutorial.lower()
    assert "Client authentication` to `On" in tutorial
    assert "Turn `Service accounts roles` `On" in tutorial
    assert "Do not use the browser client `modelguard-admin-web` for machines" in tutorial
    assert "creating a Keycloak client does not automatically create a" in tutorial
    assert "safe token issued in step 7" in tutorial
    assert "easiest local case: omit `--admin-base-url` entirely" in tutorial.lower()
    assert "local kogwistar postgres-backed store" in tutorial.lower()
    assert "--admin-base-url http://127.0.0.1:8789" in tutorial
    assert "--admin-secret \"$MODELKEYGUARD_ADMIN_API_SECRET\"" in tutorial
    assert "bootstrap_secrets.sh" in tutorial
    assert "modelkeyguard_admin_api_secret" in tutorial
    assert "MODELKEYGUARD_ADMIN_API_SECRET_FILE" in tutorial
    assert "log in as alice and inspect the quota pages" in tutorial.lower()
    assert "use `--admin-bearer-token \"$admin_token\"` only when the gateway is explicitly" in tutorial.lower()
    assert "modelkeyguard_admin_auth_mode=keycloak" in tutorial.lower()
    assert "secret_or_keycloak" in tutorial.lower()
    assert "for a true remote deployment, replace `http://127.0.0.1:8789` with the remote" in tutorial.lower()
    assert "MODELKEYGUARD_SAMPLE_PROVIDER" in tutorial
    assert "model.usage.read" in tutorial
    assert "usage_analysis_agent.py" in tutorial
    assert "keycloak_admin_first_setup.md" in tutorial_index
    assert "keycloak_admin_first_setup.md" in docs
    assert "easiest local path is to call `modelkeyguard" in docs.lower()
    assert "same kogwistar postgres-backed store" in docs.lower()
    assert "--admin-base-url http://127.0.0.1:8789" in docs
    assert "--admin-secret" in docs
    assert "bootstrap_secrets.sh" in docs
    assert "modelkeyguard_admin_api_secret" in docs
    assert "MODELKEYGUARD_ADMIN_API_SECRET_FILE" in docs
    assert "--admin-bearer-token" in docs
    assert "replace `http://127.0.0.1:8789` with the remote" in docs


def test_remote_deploy_bootstrap_admin_pair_is_printed_not_persisted():
    repo_root = Path(__file__).resolve().parents[1]
    deploy = (repo_root / "scripts" / "deploy_remote_stack.sh").read_text(encoding="utf-8")

    assert "Keycloak bootstrap admin for this deployment" in deploy
    for line in deploy.splitlines():
        if "keycloak_bootstrap_admin_" in line:
            assert "runtime_state_file" not in line
            assert "write_secret" not in line
            assert "secrets/" not in line
            assert "mktemp" not in line


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
    assert f"MODELKEYGUARD_GATEWAY_ENV_FILE={out_dir.resolve()}/gateway.env" in compose_env
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
