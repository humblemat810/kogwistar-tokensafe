from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_policy_json(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("policy_root_must_be_json_object")
    return data

