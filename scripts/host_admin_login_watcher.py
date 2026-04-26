#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

from modelkeyguard.services.security_watch import parse_host_security_line


def _post_security_event(base_url: str, secret: str, event: dict[str, object]) -> tuple[int, str]:
    payload = json.dumps(event).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/admin/security-events",
        data=payload,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-modelkeyguard-security-secret": secret,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body
    except Exception as e:
        return 599, str(e)


def _tail_auth_log(path: Path, *, from_start: bool, poll_seconds: float) -> Iterable[str]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        if not from_start:
            f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if line:
                yield line
                continue
            time.sleep(max(0.1, poll_seconds))


def _follow_journalctl(command: str) -> Iterable[str]:
    proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        yield line


def _split_users(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Watch host auth events and push admin events to ModelKeyGuard")
    p.add_argument("--gateway-url", default=os.getenv("MODELKEYGUARD_GATEWAY_URL", "http://127.0.0.1:8789"))
    p.add_argument("--secret", default=os.getenv("SECURITY_EVENT_SHARED_SECRET", ""))
    p.add_argument("--watch-users", default=os.getenv("ADMIN_WATCH_USERS", ""))
    p.add_argument("--source", choices=["authlog", "journalctl"], default=os.getenv("HOST_SECURITY_SOURCE", "authlog"))
    p.add_argument("--auth-log", default=os.getenv("HOST_AUTH_LOG_PATH", "/var/log/auth.log"))
    p.add_argument(
        "--journalctl-cmd",
        default=os.getenv(
            "HOST_SECURITY_JOURNALCTL_CMD",
            "journalctl -f -n 0 -o short --no-pager _COMM=sshd _COMM=sudo",
        ),
    )
    p.add_argument("--poll-seconds", type=float, default=float(os.getenv("HOST_SECURITY_POLL_SECONDS", "1.0")))
    p.add_argument("--from-start", action="store_true", help="read existing log lines instead of tail-from-end")
    args = p.parse_args(argv)

    if not args.secret.strip():
        print("SECURITY_EVENT_SHARED_SECRET is required")
        return 2

    host = socket.gethostname()
    users = _split_users(args.watch_users)
    seen_hashes: set[int] = set()

    line_source = (
        _tail_auth_log(Path(args.auth_log), from_start=args.from_start, poll_seconds=args.poll_seconds)
        if args.source == "authlog"
        else _follow_journalctl(args.journalctl_cmd)
    )
    print(f"watching source={args.source} host={host} users={'*' if not users else ','.join(sorted(users))}")

    for line in line_source:
        event = parse_host_security_line(line, host=host)
        if not event:
            continue
        if users and str(event.get("username") or "") not in users:
            continue
        digest = hash((event.get("event_type"), event.get("username"), event.get("raw")))
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        if len(seen_hashes) > 5000:
            seen_hashes = set(list(seen_hashes)[-2000:])

        status, body = _post_security_event(args.gateway_url, args.secret, event)
        print(f"forward status={status} user={event.get('username')} type={event.get('event_type')} body={body[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
