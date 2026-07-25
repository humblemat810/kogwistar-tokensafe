from __future__ import annotations

import pytest

from modelkeyguard.governance_runtime import try_load_policy
from modelkeyguard.policy_loader import load_policy_json


def test_missing_custom_policy_fails_closed(tmp_path):
    missing = tmp_path / "missing-policy.json"
    with pytest.raises(FileNotFoundError):
        load_policy_json(missing)
    with pytest.raises(FileNotFoundError):
        try_load_policy(str(missing))

