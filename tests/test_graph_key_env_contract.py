from modelkeyguard.graph_key_contract import scan_graph_key_env_contract


def test_graph_key_env_is_read_only_by_canonical_resolvers():
    assert scan_graph_key_env_contract() == []
