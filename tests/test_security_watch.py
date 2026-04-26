from __future__ import annotations

from modelkeyguard.services.security_watch import parse_host_security_line


def test_parse_host_security_line_ssh_success():
    line = "Apr 26 00:00:01 node sshd[12345]: Accepted publickey for azureuser from 10.0.0.5 port 52311 ssh2"
    ev = parse_host_security_line(line, host="node-1")
    assert ev is not None
    assert ev["event_type"] == "admin_ssh_login"
    assert ev["username"] == "azureuser"
    assert ev["source_ip"] == "10.0.0.5"
    assert ev["auth_method"] == "publickey"


def test_parse_host_security_line_sudo_command():
    line = "Apr 26 00:00:02 node sudo:   azureuser : TTY=pts/0 ; PWD=/home/azureuser ; USER=root ; COMMAND=/usr/bin/systemctl restart docker"
    ev = parse_host_security_line(line, host="node-1")
    assert ev is not None
    assert ev["event_type"] == "admin_sudo_command"
    assert ev["username"] == "azureuser"
    assert "systemctl restart docker" in str(ev["command"])


def test_parse_host_security_line_sudo_session():
    line = "Apr 26 00:00:03 node sudo: pam_unix(sudo:session): session opened for user root by azureuser(uid=1000)"
    ev = parse_host_security_line(line, host="node-1")
    assert ev is not None
    assert ev["event_type"] == "admin_sudo_session_open"
    assert ev["username"] == "azureuser"
    assert ev["target_user"] == "root"


def test_parse_host_security_line_ignores_non_admin_line():
    line = "Apr 26 00:00:04 node systemd[1]: Started Some Service."
    assert parse_host_security_line(line, host="node-1") is None
