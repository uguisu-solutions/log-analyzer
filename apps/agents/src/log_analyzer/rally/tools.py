"""構成4 監視エージェント用のツール群（モック実装）。

Phase 2 では実機 / 構成管理 DB を引く想定だが、PoC 期間中は
``samples/topology/*.json`` の固定データから返す。tool 呼び出しは
監視エージェント関数内で予測可能なタイミングで実行し、結果を LLM の
コンテキストとして渡す（LLM 主導のツール呼び出しは Phase 2 後半で検討）。

提供ツール:
- ``read_topology(target_ip)``: ネットワーク／FW 構成（hosts / policy / neighbors）
- ``get_config(service_id)``: サービス別の構成（DNS / Auth / App 等の設定値・既知の問題）
"""
from __future__ import annotations

import json
import re
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
        # service_configs.json は別ツールで読むのでここでは除外
        if "service_config" not in p.name
    ]


@lru_cache(maxsize=1)
def _load_service_configs() -> dict[str, dict]:
    """``service_configs.json`` を 1 つにマージ。複数あれば後者が前者を上書き。"""
    out: dict[str, dict] = {}
    if not _TOPOLOGY_DIR.exists():
        return out
    for p in sorted(_TOPOLOGY_DIR.glob("*service_config*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        services = data.get("services") or {}
        if isinstance(services, dict):
            out.update(services)
    return out


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


def get_config(service_id: str) -> dict:
    """``service_id`` のサービス構成を返すモックツール。

    `hostname` / `ip` のどちらでも一致を試す。見つからなければ ``matched: null``。
    """
    configs = _load_service_configs()
    if service_id in configs:
        return {"matched": True, "service_id": service_id, "config": configs[service_id]}
    # IP で逆引き
    for sid, cfg in configs.items():
        if cfg.get("ip") == service_id:
            return {"matched": True, "service_id": sid, "config": cfg}
    return {
        "matched": False,
        "service_id": service_id,
        "note": f"no service config found for {service_id}",
    }


# ログから「対象サービス」を雑に拾うためのヒューリスティック
_SERVICE_HOSTNAME_RE = re.compile(r"\b(dns\d+|auth-server|app-server-\d+|fw\d+)\b")


def extract_target_service(log: str, fallback: str = "app-server-1") -> str:
    """ログから注目すべきサービス ID を 1 つ抜き出す（最も頻出のものを採用）。

    監視が ``get_config(service_id)`` を 1 回叩く際の引数決定に使う。
    """
    counts: dict[str, int] = {}
    for m in _SERVICE_HOSTNAME_RE.findall(log):
        counts[m] = counts.get(m, 0) + 1
    if not counts:
        return fallback
    return max(counts.items(), key=lambda kv: kv[1])[0]
