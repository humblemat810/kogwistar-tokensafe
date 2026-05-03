from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any


_DEFAULT_POLICY_PATHS = {
    Path("config/gateway_policy.json"),
    Path("gateway_policy.json"),
}


def _read_json_object(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("policy_root_must_be_json_object")
    return data


def _checkout_default_policy_path() -> Path:
    # When running from a source checkout (including editable installs where
    # package-data discovery can vary), keep a deterministic fallback path.
    return Path(__file__).resolve().parents[1] / "config" / "gateway_policy.json"


def load_policy_json(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        if p in _DEFAULT_POLICY_PATHS:
            try:
                packaged = resources.files("modelkeyguard").joinpath("data/gateway_policy.json")
                text = packaged.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                checkout_default = _checkout_default_policy_path()
                if checkout_default.exists():
                    return _read_json_object(checkout_default)
                return {}
            if not text:
                checkout_default = _checkout_default_policy_path()
                if checkout_default.exists():
                    return _read_json_object(checkout_default)
                return {}
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("policy_root_must_be_json_object")
            return data
        return {}
    return _read_json_object(p)
