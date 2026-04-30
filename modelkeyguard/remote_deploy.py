from __future__ import annotations

import argparse
import os
import shlex
import subprocess
from pathlib import Path


COMMANDS = ("up", "fresh-up", "start", "stop", "down", "logs", "smoke", "config")


def _run(cmd: list[str], *, input_data: bytes | None = None, capture_output: bool = False) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        cmd,
        input=input_data,
        check=True,
        capture_output=capture_output,
    )


def _require_bin(name: str) -> None:
    if subprocess.run(["bash", "-lc", f"command -v {shlex.quote(name)} >/dev/null 2>&1"], check=False).returncode != 0:
        raise SystemExit(f"{name} is required for remote deployment")


def _find_source_root(explicit: str | None = None) -> Path:
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if root.exists():
            return root
        raise SystemExit(f"source root does not exist: {root}")

    env_root = os.getenv("MODELKEYGUARD_SOURCE_ROOT", "").strip()
    if env_root:
        return _find_source_root(env_root)

    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "scripts" / "deploy_remote_stack.sh").exists() and (candidate / "pyproject.toml").exists():
            return candidate
    raise SystemExit(
        "could not locate the repository source root; run from a checkout or pass --source-root /path/to/token-safe"
    )


def _resolve_remote_root(remote: str, path: str) -> str:
    if path.startswith("~"):
        home = _run(["ssh", remote, "printf %s \"$HOME\""], capture_output=True).stdout.decode("utf-8").strip()
        if path == "~":
            return home
        if path.startswith("~/"):
            return home + path[1:]
    return path


def _remote_exec(remote: str, remote_root: str, command: str) -> None:
    _run(["ssh", remote, f"cd {shlex.quote(remote_root)} && {command}"])


def _sync_repo(remote: str, source_root: Path, remote_root: str) -> None:
    rsync_cmd = [
        "rsync",
        "-a",
        "--delete",
        "--exclude",
        ".git/",
        "--exclude",
        ".venv/",
        "--exclude",
        "data/",
        "--exclude",
        "out/",
        "--exclude",
        "secrets/",
        "--exclude",
        "__pycache__/",
        "--exclude",
        "*.pyc",
        "--exclude",
        "kogwistar_reference_only/",
        f"{source_root}/",
        f"{remote}:{remote_root}/",
    ]
    _run(rsync_cmd)


def _remote_env_export(source_file: Path, remote: str, remote_root: str, source_root: Path) -> None:
    if not source_file.is_file():
        return
    remote_rel = source_file.resolve().relative_to(source_root)
    remote_path = f"{remote_root}/{remote_rel.as_posix()}"
    _run(["ssh", remote, f"mkdir -p {shlex.quote(str(Path(remote_path).parent))}"])
    _run(["rsync", "-a", str(source_file), f"{remote}:{remote_path}"])


def _stage_runtime_secrets(remote: str, remote_root: str, source_root: Path) -> None:
    runtime_state_file = f"{remote_root}/out/.runtime-secret-dir"
    runtime_secret_dir = ""
    try:
        runtime_secret_dir = _run(["ssh", remote, f"if [[ -f {shlex.quote(runtime_state_file)} ]]; then cat {shlex.quote(runtime_state_file)}; fi"], capture_output=True).stdout.decode("utf-8").strip()
    except subprocess.CalledProcessError:
        runtime_secret_dir = ""

    local_secrets = source_root / "secrets"
    if runtime_secret_dir and _run(["ssh", remote, f"test -d {shlex.quote(runtime_secret_dir)}"], capture_output=True).returncode == 0:
        if local_secrets.is_dir():
            _run(["rsync", "-a", "--delete", f"{local_secrets}/", f"{remote}:{runtime_secret_dir}/"])
    else:
        runtime_secret_dir = _run(["ssh", remote, "mktemp -d /dev/shm/token-safe-secrets.XXXXXX"], capture_output=True).stdout.decode("utf-8").strip()
        if local_secrets.is_dir():
            _run(["ssh", remote, f"mkdir -p {shlex.quote(runtime_secret_dir)}"])
            _run(["rsync", "-a", "--delete", f"{local_secrets}/", f"{remote}:{runtime_secret_dir}/"])
        _run(["ssh", remote, f"printf '%s\\n' {shlex.quote(runtime_secret_dir)} > {shlex.quote(runtime_state_file)}"])
    _run(["ssh", remote, f"rm -rf {shlex.quote(remote_root + '/secrets')} && ln -s {shlex.quote(runtime_secret_dir)} {shlex.quote(remote_root + '/secrets')} && printf '%s\\n' {shlex.quote(runtime_secret_dir)} > {shlex.quote(runtime_state_file)}"])


def _cleanup_runtime_secrets(remote: str, remote_root: str) -> None:
    runtime_state_file = f"{remote_root}/out/.runtime-secret-dir"
    try:
        runtime_secret_dir = _run(["ssh", remote, f"if [[ -f {shlex.quote(runtime_state_file)} ]]; then cat {shlex.quote(runtime_state_file)}; fi"], capture_output=True).stdout.decode("utf-8").strip()
    except subprocess.CalledProcessError:
        runtime_secret_dir = ""
    if not runtime_secret_dir:
        return
    try:
        _run(["ssh", remote, f"rm -rf {shlex.quote(remote_root + '/secrets')} {shlex.quote(runtime_state_file)} {shlex.quote(runtime_secret_dir)}"])
    except subprocess.CalledProcessError:
        pass


def _build_local_image(source_root: Path, image: str) -> None:
    _run(["docker", "build", "-t", image, str(source_root)])


def _load_remote_image(remote: str, image: str) -> None:
    save = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE)
    assert save.stdout is not None
    try:
        subprocess.run(["ssh", remote, "docker", "load"], stdin=save.stdout, check=True)
    finally:
        save.stdout.close()
        save.wait()


def _compose_env_prefix(image: str, bootstrap_user: str, bootstrap_password: str, realm_import_file: str | None) -> str:
    prefix = (
        f"MODELKEYGUARD_IMAGE={shlex.quote(image)} "
        f"MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME={shlex.quote(bootstrap_user)} "
        f"MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD={shlex.quote(bootstrap_password)}"
    )
    if realm_import_file:
        prefix += f" MODELKEYGUARD_KEYCLOAK_REALM_IMPORT_FILE={shlex.quote(realm_import_file)}"
    return prefix


def _random_secret() -> str:
    import secrets

    return secrets.token_urlsafe(24)


def _random_username() -> str:
    import secrets
    import string

    alphabet = string.ascii_lowercase + string.digits
    return "remote-admin-" + "".join(secrets.choice(alphabet) for _ in range(10))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="modelkeyguard deploy-remote",
        description="SSH-based remote deployment wrapper for a same-machine user or another host.",
    )
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--ssh", dest="ssh_target", default=os.getenv("MODELKEYGUARD_DEPLOY_TARGET", "localhost"))
    parser.add_argument("--remote-root", default=os.getenv("MODELKEYGUARD_DEPLOY_ROOT", "~/token-safe"))
    parser.add_argument("--source-root", default=os.getenv("MODELKEYGUARD_SOURCE_ROOT", ""))
    parser.add_argument("--shape", default=os.getenv("MODELKEYGUARD_DEPLOY_SHAPE", "compose"), choices=("compose", "gateway-only"))
    parser.add_argument("--targets-file", default=os.getenv("MODELKEYGUARD_DEPLOYMENT_TARGETS_FILE", "deploy/deployment-targets.env"))
    parser.add_argument("--keep-remote", action="store_true")
    parser.add_argument("--image", default=os.getenv("MODELKEYGUARD_IMAGE", "token-safe-gateway:latest"))
    parser.add_argument("--keycloak-bootstrap-admin-username", default=os.getenv("MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME", ""))
    parser.add_argument("--keycloak-bootstrap-admin-password", default=os.getenv("MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD", ""))
    parser.add_argument("--keycloak-realm-import-file", default=os.getenv("MODELKEYGUARD_KEYCLOAK_REALM_IMPORT_FILE", ""))
    args = parser.parse_args(argv)

    if args.shape not in {"compose", "gateway-only"}:
        raise SystemExit("--shape must be either compose or gateway-only")

    _require_bin("ssh")
    _require_bin("rsync")
    _require_bin("docker")

    source_root = _find_source_root(args.source_root)
    remote_root = _resolve_remote_root(args.ssh_target, args.remote_root)

    bootstrap_user = args.keycloak_bootstrap_admin_username or _random_username()
    bootstrap_password = args.keycloak_bootstrap_admin_password or _random_secret()

    _build_local_image(source_root, args.image)
    _load_remote_image(args.ssh_target, args.image)
    _sync_repo(args.ssh_target, source_root, remote_root)
    _stage_runtime_secrets(args.ssh_target, remote_root, source_root)

    if args.shape == "gateway-only":
        _remote_env_export(source_root / args.targets_file, args.ssh_target, remote_root, source_root)

    if args.command == "up":
        if args.shape == "compose":
            prefix = _compose_env_prefix(args.image, bootstrap_user, bootstrap_password, args.keycloak_realm_import_file or None)
            _remote_exec(
                args.ssh_target,
                remote_root,
                f"export {prefix}; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml up -d --no-build",
            )
        else:
            _remote_exec(
                args.ssh_target,
                remote_root,
                f"export MODELKEYGUARD_IMAGE={shlex.quote(args.image)}; ./scripts/gateway_from_deployment_targets.sh up --env-file {shlex.quote(args.targets_file)}",
            )
    elif args.command == "fresh-up":
        if args.shape == "compose":
            prefix = _compose_env_prefix(args.image, bootstrap_user, bootstrap_password, args.keycloak_realm_import_file or None)
            _remote_exec(
                args.ssh_target,
                remote_root,
                f"rm -rf data/postgres data/keycloak; mkdir -p data/postgres data/keycloak; export {prefix}; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml down --remove-orphans -v >/dev/null 2>&1 || true; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml up -d --no-build",
            )
        else:
            _remote_exec(
                args.ssh_target,
                remote_root,
                f"rm -rf out/production_compose_fresh; export MODELKEYGUARD_IMAGE={shlex.quote(args.image)}; ./scripts/gateway_from_deployment_targets.sh up --env-file {shlex.quote(args.targets_file)}",
            )
    elif args.command == "start":
        if args.shape == "compose":
            prefix = _compose_env_prefix(args.image, bootstrap_user, bootstrap_password, args.keycloak_realm_import_file or None)
            _remote_exec(args.ssh_target, remote_root, f"export {prefix}; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml start")
        else:
            _remote_exec(
                args.ssh_target,
                remote_root,
                f"export MODELKEYGUARD_IMAGE={shlex.quote(args.image)}; ./scripts/gateway_from_deployment_targets.sh up --env-file {shlex.quote(args.targets_file)}",
            )
    elif args.command == "stop":
        if args.shape == "compose":
            prefix = _compose_env_prefix(args.image, bootstrap_user, bootstrap_password, args.keycloak_realm_import_file or None)
            _remote_exec(args.ssh_target, remote_root, f"export {prefix}; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml stop")
        else:
            _remote_exec(
                args.ssh_target,
                remote_root,
                "docker compose -f deploy/docker-compose.gateway-only.yml --env-file out/deployment_targets_rendered/gateway.env --env-file out/deployment_targets_rendered/gateway-compose.env stop",
            )
    elif args.command == "down":
        if args.shape == "compose":
            prefix = _compose_env_prefix(args.image, bootstrap_user, bootstrap_password, args.keycloak_realm_import_file or None)
            _remote_exec(args.ssh_target, remote_root, f"export {prefix}; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml down --remove-orphans")
        else:
            _remote_exec(
                args.ssh_target,
                remote_root,
                "docker compose -f deploy/docker-compose.gateway-only.yml --env-file out/deployment_targets_rendered/gateway.env --env-file out/deployment_targets_rendered/gateway-compose.env down --remove-orphans",
            )
        _cleanup_runtime_secrets(args.ssh_target, remote_root)
        if not args.keep_remote:
            _remote_exec(args.ssh_target, remote_root, "rm -rf out/deployment_targets_rendered")
    elif args.command == "logs":
        if args.shape == "compose":
            prefix = _compose_env_prefix(args.image, bootstrap_user, bootstrap_password, args.keycloak_realm_import_file or None)
            _remote_exec(args.ssh_target, remote_root, f"export {prefix}; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml logs -f")
        else:
            _remote_exec(
                args.ssh_target,
                remote_root,
                "docker compose -f deploy/docker-compose.gateway-only.yml --env-file out/deployment_targets_rendered/gateway.env --env-file out/deployment_targets_rendered/gateway-compose.env logs -f",
            )
    elif args.command == "config":
        if args.shape == "compose":
            prefix = _compose_env_prefix(args.image, bootstrap_user, bootstrap_password, args.keycloak_realm_import_file or None)
            _remote_exec(args.ssh_target, remote_root, f"export {prefix}; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml config")
        else:
            _remote_exec(
                args.ssh_target,
                remote_root,
                f"export MODELKEYGUARD_IMAGE={shlex.quote(args.image)}; ./scripts/gateway_from_deployment_targets.sh config --env-file {shlex.quote(args.targets_file)}",
            )
    elif args.command == "smoke":
        if args.shape == "compose":
            _remote_exec(args.ssh_target, remote_root, "./scripts/deployment_smoke.sh")
        else:
            _remote_exec(
                args.ssh_target,
                remote_root,
                f"export MODELKEYGUARD_IMAGE={shlex.quote(args.image)}; set -a; . {shlex.quote(args.targets_file)}; set +a; ./scripts/deployment_smoke.sh",
            )

    if args.shape == "compose" and args.command in {"up", "fresh-up"}:
        print("Keycloak bootstrap admin for this deployment:")
        print(f"  username: {bootstrap_user}")
        print(f"  password: {bootstrap_password}")
        print("Keep this pair on the devops side if you need the Keycloak admin console.")
    return 0
