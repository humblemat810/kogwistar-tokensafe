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
            f"<tr><td><code>{html_escape(view.key_id)}</code></td><td>{html_escape(view.provider)}</td><td>{html_escape(', '.join(view.models))}</td><td>{html_escape(view.display_name)}</td><td><code>{html_escape(view.upstream_url or '')}</code></td><td>{html_escape(view.intended_use)}</td><td>{html_escape(view.status)}</td><td><code>{html_escape(view.active_secret_ref or '')}</code></td><td>{html_escape(view.expires_at_epoch or '')}</td><td><form method='post' action='/admin/keys/{html_escape(view.key_id)}/revoke'><input name='reason' placeholder='reason'><button>Revoke</button></form></td></tr>"
        )
    table_rows = "".join(rows) or "<tr><td colspan='10'>No managed keys</td></tr>"
    template = _load_template("admin_keys.html")
    return template.replace("{{rows}}", table_rows)


def render_admin_policy_page(
    *,
    users: list[dict[str, str]],
    applications: list[dict[str, str]],
    principals: list[dict[str, str]],
    quotas: list[dict[str, str]],
    paging: dict[str, dict[str, Any]] | None = None,
    filters: dict[str, str] | None = None,
    message: str = "",
    error: str = "",
    issued_token: str | None = None,
    issued_token_meta: dict[str, str] | None = None,
) -> str:
    paging = paging or {}
    filters = filters or {}

    def _render_pager(meta: dict[str, Any], label: str) -> str:
        total = int(meta.get("total", 0) or 0)
        page = int(meta.get("page", 1) or 1)
        total_pages = int(meta.get("total_pages", 1) or 1)
        shown_start = int(meta.get("shown_start", 0) or 0)
        shown_end = int(meta.get("shown_end", 0) or 0)
        prev_url = str(meta.get("prev_url", "") or "")
        next_url = str(meta.get("next_url", "") or "")
        prev_link = f"<a href='{html_escape(prev_url)}'>Previous</a>" if prev_url else "<span>Previous</span>"
        next_link = f"<a href='{html_escape(next_url)}'>Next</a>" if next_url else "<span>Next</span>"
        return (
            "<div class='pager'>"
            f"<span>{html_escape(label)}: {shown_start}-{shown_end} of {total} (page {page}/{total_pages})</span>"
            f"<span class='pager-links'>{prev_link} {next_link}</span>"
            "</div>"
        )

    users_rows = "".join(
        f"<tr><td><a href='{html_escape(row.get('history_url', ''))}'><code>{html_escape(row['id'])}</code></a></td><td>{html_escape(row.get('display_name', ''))}</td></tr>"
        for row in users
    ) or "<tr><td colspan='2'>No users</td></tr>"
    app_rows = "".join(
        f"<tr><td><code>{html_escape(row['id'])}</code></td><td>{html_escape(row.get('display_name', ''))}</td></tr>"
        for row in applications
    ) or "<tr><td colspan='2'>No applications</td></tr>"
    principal_rows = "".join(
        f"<tr><td><a href='{html_escape(row.get('history_url', ''))}'><code>{html_escape(row['id'])}</code></a></td><td>{html_escape(row.get('kind', ''))}</td><td>{html_escape(row.get('namespace', ''))}</td><td>{html_escape(row.get('application_id', ''))}</td><td>{html_escape(row.get('groups', ''))}</td></tr>"
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
        .replace("{{page_size}}", html_escape(filters.get("page_size", "20")))
        .replace("{{users_q}}", html_escape(filters.get("users_q", "")))
        .replace("{{apps_q}}", html_escape(filters.get("apps_q", "")))
        .replace("{{principals_q}}", html_escape(filters.get("principals_q", "")))
        .replace("{{quotas_lane}}", html_escape(filters.get("quotas_lane", "")))
        .replace("{{quotas_subject_id}}", html_escape(filters.get("quotas_subject_id", "")))
        .replace("{{quotas_name}}", html_escape(filters.get("quotas_name", "")))
        .replace("{{quotas_revoked_any_selected}}", "selected" if filters.get("quotas_revoked", "any") == "any" else "")
        .replace("{{quotas_revoked_true_selected}}", "selected" if filters.get("quotas_revoked") == "true" else "")
        .replace("{{quotas_revoked_false_selected}}", "selected" if filters.get("quotas_revoked") == "false" else "")
        .replace("{{users_pager}}", _render_pager(paging.get("users", {}), "Users"))
        .replace("{{applications_pager}}", _render_pager(paging.get("applications", {}), "Applications"))
        .replace("{{principals_pager}}", _render_pager(paging.get("principals", {}), "Principals"))
        .replace("{{quotas_pager}}", _render_pager(paging.get("quotas", {}), "Quota revisions"))
        .replace("{{users_rows}}", users_rows)
        .replace("{{applications_rows}}", app_rows)
        .replace("{{principals_rows}}", principal_rows)
        .replace("{{quotas_rows}}", quota_rows)
    )


def _load_template(name: str) -> str:
    path = _TEMPLATE_DIR / name
    return path.read_text(encoding="utf-8")
