from __future__ import annotations

from pathlib import Path
from typing import Any

from ..key_manager import html_escape

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_TEMPLATE_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"


def get_static_dir() -> Path:
    return _STATIC_DIR


def render_admin_usage_page() -> str:
    return _load_template("admin_usage.html")


def render_admin_keys_page(views: list[Any]) -> str:
    rows = []
    for view in views:
        rows.append(
            f"<tr><td><code>{html_escape(view.key_id)}</code></td><td>{html_escape(view.provider)}</td><td>{html_escape(', '.join(view.models))}</td><td>{html_escape(view.status)}</td><td><code>{html_escape(view.active_secret_ref or '')}</code></td><td>{html_escape(view.expires_at_epoch or '')}</td><td><form method='post' action='/admin/keys/{html_escape(view.key_id)}/revoke'><input name='reason' placeholder='reason'><button>Revoke</button></form></td></tr>"
        )
    table_rows = "".join(rows) or "<tr><td colspan='7'>No managed keys</td></tr>"
    template = _load_template("admin_keys.html")
    return template.replace("{{rows}}", table_rows)


def _load_template(name: str) -> str:
    path = _TEMPLATE_DIR / name
    return path.read_text(encoding="utf-8")
