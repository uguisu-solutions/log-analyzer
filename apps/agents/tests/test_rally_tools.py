from log_analyzer.rally.tools import read_topology


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
