from log_analyzer.rally.tools import extract_target_service, get_config, read_topology


def test_read_topology_known_ip_returns_host_entry():
    result = read_topology("10.0.20.5")
    assert result["matched_topology"] == "fw01_v414"
    assert result["host"]["hostname"] == "app-server-1"
    assert any(r["dport"] == 80 for r in result["host"]["expected_inbound"])


def test_read_topology_unknown_ip_returns_none():
    result = read_topology("203.0.113.99")
    assert result["matched_topology"] is None
    assert "no topology entry found" in result["note"]


def test_read_topology_neighbors_filtered_by_ip():
    result = read_topology("10.0.20.5")
    for neighbor in result["neighbors"]:
        assert "10.0.20.5" in (neighbor.get("src"), neighbor.get("dst"))


# ---------------- get_config / extract_target_service ----------------


def test_get_config_by_hostname():
    """hostname 一致で構成を返す。"""
    result = get_config("dns01")
    assert result["matched"] is True
    assert result["service_id"] == "dns01"
    assert result["config"]["type"] == "bind9"
    assert "known_issue" in result["config"]


def test_get_config_by_ip_reverse_lookup():
    """IP からも逆引きで service 設定を取れる。"""
    result = get_config("10.0.0.10")
    assert result["matched"] is True
    assert result["service_id"] == "auth-server"
    assert result["config"]["ssh_config"]["PermitRootLogin"] == "no"


def test_get_config_unknown_service_returns_unmatched():
    result = get_config("nonexistent-service")
    assert result["matched"] is False
    assert "no service config found" in result["note"]


def test_extract_target_service_picks_most_frequent():
    """ログ中で最も頻出するサービス名が選ばれる。"""
    log = (
        "2026-05-08 dns01 named[]: ...\n"
        "2026-05-08 dns01 named[]: SERVFAIL\n"
        "2026-05-08 app-server-1 nginx: 502\n"
    )
    assert extract_target_service(log) == "dns01"


def test_extract_target_service_fallback():
    """ヒットしなければ fallback を返す。"""
    assert extract_target_service("unrelated log content", fallback="app-server-1") == "app-server-1"
