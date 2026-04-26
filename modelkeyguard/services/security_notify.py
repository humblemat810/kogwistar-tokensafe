from __future__ import annotations

from email.message import EmailMessage
import json
import os
from pathlib import Path
import smtplib
import ssl
import urllib.request
from typing import Any


def configured_admin_users() -> set[str]:
    raw = os.getenv("ADMIN_WATCH_USERS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def append_security_event_log(event: dict[str, Any]) -> None:
    path = Path(os.getenv("ADMIN_SECURITY_EVENT_LOG_PATH", "out/admin_security_events.jsonl"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")


def notify_security_event(event: dict[str, Any]) -> dict[str, Any]:
    status = {
        "webhook": _notify_webhook(event),
        "slack": _notify_slack(event),
        "email": _notify_email(event),
    }
    append_security_event_log({"event": event, "notify_status": status})
    return status


def _notify_webhook(event: dict[str, Any]) -> str:
    url = os.getenv("ADMIN_SECURITY_WEBHOOK_URL", "").strip()
    if not url:
        return "skipped"
    data = json.dumps(event).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5):
            return "ok"
    except Exception as e:
        return f"error:{e}"


def _notify_slack(event: dict[str, Any]) -> str:
    url = os.getenv("ADMIN_SECURITY_SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return "skipped"
    text = f"[ModelKeyGuard] admin security event {event.get('event_type')} user={event.get('username')} host={event.get('host')}"
    data = json.dumps({"text": text, "event": event}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5):
            return "ok"
    except Exception as e:
        return f"error:{e}"


def _notify_email(event: dict[str, Any]) -> str:
    host = os.getenv("ADMIN_SECURITY_SMTP_HOST", "").strip()
    if not host:
        return "skipped"
    port = int(os.getenv("ADMIN_SECURITY_SMTP_PORT", "587"))
    user = os.getenv("ADMIN_SECURITY_SMTP_USER", "").strip()
    password = os.getenv("ADMIN_SECURITY_SMTP_PASSWORD", "").strip()
    to_addr = os.getenv("ADMIN_SECURITY_EMAIL_TO", "").strip()
    from_addr = os.getenv("ADMIN_SECURITY_EMAIL_FROM", user or "modelkeyguard@localhost")
    if not to_addr:
        return "error:missing_to"

    msg = EmailMessage()
    msg["Subject"] = f"[ModelKeyGuard] admin security event {event.get('event_type')}"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(json.dumps(event, indent=2, sort_keys=True))
    try:
        with smtplib.SMTP(host, port, timeout=10) as s:
            if os.getenv("ADMIN_SECURITY_SMTP_STARTTLS", "1") != "0":
                s.starttls(context=ssl.create_default_context())
            if user:
                s.login(user, password)
            s.send_message(msg)
        return "ok"
    except Exception as e:
        return f"error:{e}"
