from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from ..key_manager import html_escape

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_TEMPLATE_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"


def get_static_dir() -> Path:
    return _STATIC_DIR


def render_admin_usage_page() -> str:
    return _load_template("admin_usage.html")


def render_admin_history_page() -> str:
    return _load_template("admin_history.html")


def render_admin_keys_page(views: list[Any]) -> str:
    rows = []
    for view in views:
        rows.append(
            f"<tr><td><code>{html_escape(view.key_id)}</code></td><td>{html_escape(view.provider)}</td><td>{html_escape(', '.join(view.models))}</td><td>{html_escape(view.display_name)}</td><td>{html_escape(view.intended_use)}</td><td>{html_escape(view.status)}</td><td><code>{html_escape(view.active_secret_ref or '')}</code></td><td>{html_escape(view.expires_at_epoch or '')}</td><td><form method='post' action='/admin/keys/{html_escape(view.key_id)}/revoke'><input name='reason' placeholder='reason'><button>Revoke</button></form></td></tr>"
        )
    table_rows = "".join(rows) or "<tr><td colspan='9'>No managed keys</td></tr>"
    template = _load_template("admin_keys.html")
    return template.replace("{{rows}}", table_rows)


def render_admin_policy_page(
    *,
    users: list[dict[str, str]],
    applications: list[dict[str, str]],
    principals: list[dict[str, str]],
    quotas: list[dict[str, str]],
    message: str = "",
    error: str = "",
    issued_token: str | None = None,
    issued_token_meta: dict[str, str] | None = None,
) -> str:
    users_rows = "".join(
        f"<tr><td><code>{html_escape(row['id'])}</code></td><td>{html_escape(row.get('display_name', ''))}</td></tr>"
        for row in users
    ) or "<tr><td colspan='2'>No users</td></tr>"
    app_rows = "".join(
        f"<tr><td><code>{html_escape(row['id'])}</code></td><td>{html_escape(row.get('display_name', ''))}</td></tr>"
        for row in applications
    ) or "<tr><td colspan='2'>No applications</td></tr>"
    principal_rows = "".join(
        f"<tr><td><code>{html_escape(row['id'])}</code></td><td>{html_escape(row.get('kind', ''))}</td><td>{html_escape(row.get('namespace', ''))}</td><td>{html_escape(row.get('application_id', ''))}</td><td>{html_escape(row.get('groups', ''))}</td></tr>"
        for row in principals
    ) or "<tr><td colspan='5'>No principals</td></tr>"
    quota_rows = "".join(
        f"<tr><td><code>{html_escape(row['id'])}</code></td><td>{html_escape(row.get('lane', ''))}</td><td><code>{html_escape(row.get('subject_id', ''))}</code></td><td>{html_escape(row.get('quota_name', ''))}</td><td>{html_escape(row.get('period', ''))}</td><td>{html_escape(row.get('max_usd', ''))}</td><td>{html_escape(row.get('max_tokens', ''))}</td><td>{html_escape(row.get('max_requests', ''))}</td><td>{html_escape(row.get('revoked', ''))}</td></tr>"
        for row in quotas
    ) or "<tr><td colspan='9'>No quota revisions</td></tr>"

    flash = ""
    if error:
        flash = f"<div class='flash flash-error'>{html_escape(error)}</div>"
    elif message:
        flash = f"<div class='flash flash-ok'>{html_escape(message)}</div>"

    token_panel = ""
    if issued_token:
        safe_meta = issued_token_meta or {}
        token_panel = (
            "<div class='token-panel'>"
            "<h3>One-Time Safe Token</h3>"
            "<p>This token is shown only in this response. Store it now. It cannot be retrieved later.</p>"
            f"<textarea readonly rows='3'>{html_escape(issued_token)}</textarea>"
            f"<pre>{html_escape(json.dumps(safe_meta, indent=2, sort_keys=True))}</pre>"
            "</div>"
        )

    template = _load_template("admin_policy.html")
    return (
        template.replace("{{flash}}", flash)
        .replace("{{token_panel}}", token_panel)
        .replace("{{users_rows}}", users_rows)
        .replace("{{applications_rows}}", app_rows)
        .replace("{{principals_rows}}", principal_rows)
        .replace("{{quotas_rows}}", quota_rows)
    )


def _load_template(name: str) -> str:
    path = _TEMPLATE_DIR / name
    return path.read_text(encoding="utf-8")
