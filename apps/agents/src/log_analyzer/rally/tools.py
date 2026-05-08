"""構成4 監視エージェント用のツール群（モック実装）。

Phase 2 では実機 / 構成管理 DB を引く想定だが、PoC 期間中は
``samples/topology/*.json`` の固定データから返す。tool 呼び出しは
監視エージェント関数内で予測可能なタイミングで実行し、結果を LLM の
コンテキストとして渡す（LLM 主導のツール呼び出しは Phase 2 後半で検討）。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

# `samples/topology/*.json` を解決する。本ファイルから 5 階層上が repo ルート。
_TOPOLOGY_DIR = Path(__file__).resolve().parents[5] / "samples" / "topology"


@lru_cache(maxsize=1)
def _load_all_topologies() -> list[dict]:
    if not _TOPOLOGY_DIR.exists():
        return []
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(_TOPOLOGY_DIR.glob("*.json"))
    ]


def read_topology(target_ip: str) -> dict:
    """target_ip 周辺のネットワークトポロジ情報を返すモックツール。

    一致する host エントリが見つからなければ ``matched_topology: null`` で
    返す（LLM はそれを見て「トポロジ未登録」と判断できる）。
    """
    for topology in _load_all_topologies():
        for host in topology.get("hosts", []):
            if host.get("ip") == target_ip:
                return {
                    "matched_topology": topology.get("topology_id"),
                    "host": host,
                    "neighbors": [
                        n for n in topology.get("neighbors", [])
                        if target_ip in (n.get("src"), n.get("dst"))
                    ],
                    "policy": topology.get("policy", {}),
                }
    return {
        "matched_topology": None,
        "note": f"no topology entry found for {target_ip}",
    }
