from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any


_DEFAULT_POLICY_PATHS = {
    Path("config/gateway_policy.json"),
    Path("gateway_policy.json"),
}


def load_policy_json(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        if p in _DEFAULT_POLICY_PATHS:
            try:
                packaged = resources.files("modelkeyguard").joinpath("data/gateway_policy.json")
                text = packaged.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                return {}
            if not text:
                return {}
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("policy_root_must_be_json_object")
            return data
        return {}
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("policy_root_must_be_json_object")
    return data
