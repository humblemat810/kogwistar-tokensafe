from __future__ import annotations

import re
import time
from typing import Any

_SSH_ACCEPT_RE = re.compile(
    r"sshd\[\d+\]: Accepted (?P<method>\S+) for (?P<username>[^\s]+) from (?P<source_ip>[^\s]+) port (?P<port>\d+)"
)
_SUDO_COMMAND_RE = re.compile(r"sudo:\s+(?P<username>[^\s]+)\s*:\s+.*COMMAND=(?P<command>.+)$")
_SUDO_SESSION_RE = re.compile(
    r"sudo: pam_unix\(sudo:session\): session opened for user (?P<target_user>[^\s]+) by (?P<username>[^\s\(]+)"
)


def parse_host_security_line(line: str, *, host: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text:
        return None

    ssh = _SSH_ACCEPT_RE.search(text)
    if ssh:
        return {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event_type": "admin_ssh_login",
            "username": ssh.group("username"),
            "host": host,
            "source_ip": ssh.group("source_ip"),
            "auth_method": ssh.group("method"),
            "raw": text,
        }

    sudo_cmd = _SUDO_COMMAND_RE.search(text)
    if sudo_cmd:
        return {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event_type": "admin_sudo_command",
            "username": sudo_cmd.group("username"),
            "host": host,
            "source_ip": "",
            "auth_method": "sudo",
            "raw": text,
            "command": sudo_cmd.group("command").strip(),
        }

    sudo_session = _SUDO_SESSION_RE.search(text)
    if sudo_session:
        return {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event_type": "admin_sudo_session_open",
            "username": sudo_session.group("username"),
            "host": host,
            "source_ip": "",
            "auth_method": "sudo",
            "raw": text,
            "target_user": sudo_session.group("target_user"),
        }

    return None
